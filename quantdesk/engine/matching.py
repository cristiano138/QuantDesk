"""撮合引擎（D06）——回测与模拟盘共用同一实现。

铁律：
- 撮合是**纯函数**：不落库、不发事件、不改全局状态（调用方负责）。
- 费率全部读 core/constants（config 可覆盖，经 FeeCalculator 注入）。
- T+1：当日买入 available=0，次日由交易日驱动释放（PositionBook.on_new_trading_day）。

简化口径（均为有意取舍，测试锁定行为）：
- bar 撞价为纯价格规则：limit buy `low ≤ price` 即成交（一字涨停板不模拟排队语义）。
- tick 撞价 limit 单以 last 判定、成交价取对委托更优者；市价单按对手价 ± 滑点。
- tick 模式不按盘口档位量限额（无排队模型），bar 模式按 bar 量限额支持部分成交。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from quantdesk.core import constants
from quantdesk.core.enums import Board, OrderStatus, OrderType, Side
from quantdesk.core.models import Bar, Order, Position, Tick, Trade
from quantdesk.core.symbols import board_of, currency_of, is_hk

__all__ = [
    "FeeCalculator",
    "MatchConfig",
    "MatchResult",
    "match_on_bar",
    "match_on_tick",
    "apply_fill",
    "affordable_volume",
    "PositionBook",
]


# ---------- 费用 ----------

class FeeCalculator:
    """费用计算：佣金 max(amount×rate, min5) / 印花税仅卖出 / 过户费双边。

    默认读 constants；config 覆盖时在构造处传参，不读全局。
    """

    def __init__(
        self,
        commission_rate: float = constants.COMMISSION_RATE,
        min_commission: float = constants.MIN_COMMISSION,
        stamp_tax_sell: float = constants.STAMP_TAX_SELL,
        transfer_fee: float = constants.TRANSFER_FEE,
        hk_stamp_rate: float = constants.HK_STAMP_RATE,
        hk_frc_rate: float = constants.HK_FRC_RATE,
        hk_afrc_rate: float = constants.HK_AFRC_RATE,
        hk_settle_rate: float = constants.HK_SETTLE_RATE,
        hk_settle_min: float = constants.HK_SETTLE_MIN,
        hk_settle_max: float = constants.HK_SETTLE_MAX,
    ) -> None:
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_sell = stamp_tax_sell
        self.transfer_fee = transfer_fee
        self.hk_stamp_rate = hk_stamp_rate
        self.hk_frc_rate = hk_frc_rate
        self.hk_afrc_rate = hk_afrc_rate
        self.hk_settle_rate = hk_settle_rate
        self.hk_settle_min = hk_settle_min
        self.hk_settle_max = hk_settle_max

    def commission(self, amount: float) -> float:
        return round(max(amount * self.commission_rate, self.min_commission), 2)

    def buy_fee(self, amount: float) -> float:
        """买入费用 = 佣金 + 过户费（无印花税）。"""
        return round(self.commission(amount) + amount * self.transfer_fee, 2)

    def sell_fee(self, amount: float) -> float:
        """卖出费用 = 佣金 + 印花税（单边） + 过户费。"""
        return round(
            self.commission(amount)
            + amount * self.stamp_tax_sell
            + amount * self.transfer_fee,
            2,
        )

    def fee(self, side: Side | str, amount: float, symbol: str | None = None) -> float:
        """费用。给 symbol 就按该市场的真实费率走（港股另算三项），不给按 A 股。

        费率算错一次，模拟盘的盈亏就是错的——所以这里宁可多问一次板块。
        """
        if symbol and is_hk(symbol):
            return self.hk_fee(amount)
        s = side.value if isinstance(side, Side) else str(side)
        return self.buy_fee(amount) if s == Side.BUY.value else self.sell_fee(amount)

    def hk_fee(self, amount: float) -> float:
        """港股四项（港币口径，买卖双边同率）：印花税 0.1%（不足 1 港币按 1 港币计，
        向上取整到元）+ 联交所交易费 0.00565% + 证监会征费 0.0027% +
        股份交收费 0.002%（2~100 港币）。"""
        import math

        if amount <= 0:
            return 0.0
        stamp = max(math.ceil(amount * self.hk_stamp_rate), 1.0)
        frc = round(amount * self.hk_frc_rate, 2)
        afrc = round(amount * self.hk_afrc_rate, 2)
        settle = min(max(round(amount * self.hk_settle_rate, 2), self.hk_settle_min),
                     self.hk_settle_max)
        return round(stamp + frc + afrc + settle, 2)


# ---------- 撮合结果 ----------

@dataclass
class MatchConfig:
    slippage: float = 0.0             # 市价单滑点比例（0.001 = 0.1%）
    volume_limit_ratio: float = 1.0   # bar 模式单根最大成交 = bar.volume × 此比例
    fee: FeeCalculator = field(default_factory=FeeCalculator)


@dataclass
class MatchResult:
    """一次撞价的成交结果（可为空成交）。"""

    order_id: str
    filled_volume: int = 0
    fill_price: float = 0.0
    fee: float = 0.0
    trades: list[Trade] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.filled_volume > 0


def _remaining(order: Order) -> int:
    return max(order.volume - order.filled_volume, 0)


def _make_trade(order: Order, price: float, volume: int, seq: int, dt_ms: int) -> Trade:
    return Trade(
        trade_id=f"{order.order_id}-{seq}",
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        price=price,
        volume=volume,
        amount=round(price * volume, 2),
        traded_at=dt_ms,
    )


def _result(order: Order, price: float, volume: int, seq: int, dt_ms: int,
            config: MatchConfig) -> MatchResult:
    if volume <= 0 or price <= 0:
        return MatchResult(order_id=order.order_id)
    trade = _make_trade(order, price, volume, seq, dt_ms)
    fee = config.fee.fee(order.side, trade.amount, order.symbol)
    return MatchResult(
        order_id=order.order_id,
        filled_volume=volume,
        fill_price=price,
        fee=fee,
        trades=[trade],
    )


# ---------- bar 撞价 ----------

def _round_price(price: float, symbol: str) -> float:
    # B 股与港股保留千分位，不能把 0.173 的卖出成交价舍成 0.17。
    digits = 3 if currency_of(symbol) != "CNY" else 2
    return round(price, digits)

def match_on_bar(order: Order, bar: Bar, config: MatchConfig | None = None) -> MatchResult:
    """K 线撞价（回测主路径）。

    - limit buy：`low ≤ price` 成交；开盘价优先——`open ≤ price` 时按 open 成交（更优）。
    - limit sell：`high ≥ price` 成交；`open ≥ price` 时按 open 成交。
    - 市价单：按开盘价 ± 滑点成交（bar 内无盘口概念）。
    - 量上限：min(剩余量, bar.volume × volume_limit_ratio)，支持部分成交；
      bar.volume == 0（停牌/无成交）不成交。
    """
    cfg = config or MatchConfig()
    rem = _remaining(order)
    if rem <= 0:
        return MatchResult(order_id=order.order_id)

    seq = order.filled_volume  # 已成交量作为 trade 序号基数，保证 id 稳定
    dt_ms = int(bar.dt.timestamp() * 1000)

    if bar.volume <= 0:
        return MatchResult(order_id=order.order_id)
    vol_cap = max(int(bar.volume * cfg.volume_limit_ratio), 0)

    side = order.side.value if isinstance(order.side, Side) else str(order.side)
    is_buy = side == Side.BUY.value
    is_market = (order.order_type.value if isinstance(order.order_type, OrderType)
                 else str(order.order_type)) == OrderType.MARKET.value

    if is_market:
        # 市价：开盘价 ± 滑点，仍受 bar 量约束
        price = bar.open * ((1 + cfg.slippage) if is_buy else (1 - cfg.slippage))
        price = _round_price(price, order.symbol)
        vol = min(rem, vol_cap)
        return _result(order, price, vol, seq, dt_ms, cfg)

    price = order.price
    if price <= 0:
        return MatchResult(order_id=order.order_id)

    if is_buy:
        if bar.low > price:
            return MatchResult(order_id=order.order_id)
        fill_price = bar.open if bar.open <= price else price  # 开盘价优先
    else:
        if bar.high < price:
            return MatchResult(order_id=order.order_id)
        fill_price = bar.open if bar.open >= price else price

    vol = min(rem, vol_cap)
    return _result(order, _round_price(fill_price, order.symbol), vol, seq, dt_ms, cfg)


# ---------- tick 撞价 ----------

def match_on_tick(order: Order, tick: Tick, config: MatchConfig | None = None) -> MatchResult:
    """tick 撞价（模拟盘主路径）。

    - limit buy：`last ≤ price` 成交，成交价 min(price, last)（取对委托更优者）。
    - limit sell：`last ≥ price` 成交，成交价 max(price, last)。
    - 市价单：按对手价 ± 滑点（买取 ask[0]、卖取 bid[0]；盘口缺失退 last）。
    - tick 模式不按档位量限额（无排队模型），一次成交全部剩余量。
    """
    cfg = config or MatchConfig()
    rem = _remaining(order)
    if rem <= 0:
        return MatchResult(order_id=order.order_id)

    seq = order.filled_volume
    dt_ms = int(tick.dt.timestamp() * 1000)

    side = order.side.value if isinstance(order.side, Side) else str(order.side)
    is_buy = side == Side.BUY.value
    otype = order.order_type.value if isinstance(order.order_type, OrderType) \
        else str(order.order_type)

    if otype == OrderType.MARKET.value:
        if is_buy:
            opp = tick.asks[0][0] if tick.asks else tick.last
            price = opp * (1 + cfg.slippage)
        else:
            opp = tick.bids[0][0] if tick.bids else tick.last
            price = opp * (1 - cfg.slippage)
        return _result(order, _round_price(price, order.symbol), rem, seq, dt_ms, cfg)

    price = order.price
    if price <= 0:
        return MatchResult(order_id=order.order_id)

    if is_buy:
        if tick.last > price:
            return MatchResult(order_id=order.order_id)
        fill_price = min(price, tick.last)
    else:
        if tick.last < price:
            return MatchResult(order_id=order.order_id)
        fill_price = max(price, tick.last)

    return _result(order, _round_price(fill_price, order.symbol), rem, seq, dt_ms, cfg)


# ---------- 订单状态推进 ----------

def apply_fill(order: Order, result: MatchResult) -> Order:
    """把 MatchResult 应用到订单，返回新快照（不改入参——纯函数约定）。

    状态机：SUBMITTED → PART_FILLED → FILLED。调用方负责落库与广播。
    """
    if not result.filled:
        return order
    total_vol = order.filled_volume + result.filled_volume
    total_amt = order.filled_volume * order.avg_filled_price \
        + result.filled_volume * result.fill_price
    if total_vol >= order.volume:
        status = OrderStatus.FILLED
    else:
        status = OrderStatus.PART_FILLED
    return dataclasses.replace(
        order,
        filled_volume=total_vol,
        avg_filled_price=round(total_amt / total_vol, 4) if total_vol else 0.0,
        status=status,
        # updated_at 以成交时间为准（回测虚拟时钟 / 实时回报时间，不取墙钟）
        updated_at=result.trades[-1].traded_at if result.trades else order.updated_at,
    )


# ---------- 资金可负担量 ----------

def _lot_step(symbol: str | None) -> int:
    """按标的取递增单位；认不出代码时退回 A 股主板口径（从严，不会多给量）。"""
    if not symbol:
        return constants.LOT_SIZE
    try:
        return constants.lot_step(board_of(symbol))
    except Exception:                                  # noqa: BLE001
        return constants.LOT_SIZE


def affordable_volume(cash: float, price: float, desired: int,
                      fee: FeeCalculator, lot: int | None = None,
                      symbol: str | None = None) -> int:
    """现金可负担的最大成交量（含费用，向下取整到该市场的最小递增单位）。

    递增单位不能一律按 100 股：科创板/北交所是「起步量之后 1 股递增」，港股每手因发行人
    而异（快照不给，模拟按 1 股递增）。按 100 取整会把「买 1 股腾讯」算成资金不足——
    实测就是这个原因让港股单子在撮合端被静默拒掉。
    """
    if desired <= 0 or price <= 0:
        return 0
    step = lot if lot is not None else _lot_step(symbol)
    est = int(cash / (price * (1 + fee.commission_rate + fee.transfer_fee)))
    vol = min(desired, max(est, 0))
    while vol > 0 and cash < price * vol + fee.fee(Side.BUY, price * vol, symbol):
        vol -= step
    if vol < step:
        return 0
    vol = (vol // step) * step        # 始终整倍数向下取整（不足一个递增单位 = 不可买）
    return max(vol, 0)


# ---------- 持仓簿（T+1） ----------

class PositionBook:
    """内存持仓簿：维护 volume/available/avg_cost，表达 T+1 语义。

    只被模拟盘/回测的组合层持有；BUY 成交 available 不增（当日买入不可卖），
    交易日切换时 on_new_trading_day() 释放（available = volume）。
    """

    def __init__(self, strategy_id: str = "manual") -> None:
        self.strategy_id = strategy_id
        self._book: dict[str, dict[str, float]] = {}
        # symbol -> {volume, available, avg_cost}

    def _entry(self, symbol: str) -> dict[str, float]:
        return self._book.setdefault(symbol, {"volume": 0, "available": 0, "avg_cost": 0.0})

    @staticmethod
    def _is_t_plus_1(symbol: str) -> bool:
        """T+1 只对 A 股（含 B 股）成立；认不出版的代码按 A 股从严处理。"""
        try:
            return constants.is_t_plus_1(board_of(symbol))
        except Exception:                              # noqa: BLE001
            return True

    def apply_trade(self, trade: Trade, fee: float = 0.0) -> float:
        """应用成交，返回对现金的净影响（买为负含费、卖为正扣费）。

        均价成本含买入费用（成本口径）；卖出不改均价。
        """
        e = self._entry(trade.symbol)
        side = trade.side.value if isinstance(trade.side, Side) else str(trade.side)
        cash_delta = 0.0
        if side == Side.BUY.value:
            old_vol = int(e["volume"])
            old_cost = e["avg_cost"] * old_vol
            new_vol = old_vol + trade.volume
            e["volume"] = new_vol
            e["avg_cost"] = round((old_cost + trade.amount + fee) / new_vol, 4) if new_vol else 0.0
            # T+1：当日买入 available 不增。港股是 T+0，当日买入当日即可卖出，
            # 一律按 A 股冻结会让「今天买的港股今天卖不掉」——那是规则错，不是风控。
            if not self._is_t_plus_1(trade.symbol):
                e["available"] = int(e["available"]) + trade.volume
            cash_delta = -(trade.amount + fee)
        else:
            new_vol = int(e["volume"]) - trade.volume
            e["volume"] = max(new_vol, 0)
            e["available"] = max(0, min(int(e["available"]) - trade.volume, new_vol))
            cash_delta = trade.amount - fee
        return cash_delta

    def on_new_trading_day(self) -> None:
        """T+1 释放：前一交易日的买入全部转为可用（由交易日驱动调用）。"""
        for e in self._book.values():
            e["available"] = e["volume"]

    def position(self, symbol: str) -> Position | None:
        e = self._book.get(symbol)
        if not e or int(e["volume"]) <= 0:
            return None
        return Position(
            symbol=symbol,
            volume=int(e["volume"]),
            available=int(e["available"]),
            avg_cost=e["avg_cost"],
            market_value=round(e["avg_cost"] * e["volume"], 2),
            strategy_id=self.strategy_id,
        )

    def positions(self) -> list[Position]:
        return [p for s in sorted(self._book) if (p := self.position(s))]

    def available(self, symbol: str) -> int:
        e = self._book.get(symbol)
        return int(e["available"]) if e else 0

    def market_value(self, closes: dict[str, float]) -> float:
        """按最新价盯市；无最新价的标的按成本价（回测逐 bar 调用）。"""
        total = 0.0
        for symbol, e in self._book.items():
            vol = int(e["volume"])
            if vol <= 0:
                continue
            px = closes.get(symbol, e["avg_cost"])
            total += px * vol
        return round(total, 2)
