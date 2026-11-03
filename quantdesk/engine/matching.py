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
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.models import Bar, Order, Position, Tick, Trade

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
    ) -> None:
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_sell = stamp_tax_sell
        self.transfer_fee = transfer_fee

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

    def fee(self, side: Side | str, amount: float) -> float:
        s = side.value if isinstance(side, Side) else str(side)
        return self.buy_fee(amount) if s == Side.BUY.value else self.sell_fee(amount)


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
    fee = config.fee.fee(order.side, trade.amount)
    return MatchResult(
        order_id=order.order_id,
        filled_volume=volume,
        fill_price=price,
        fee=fee,
        trades=[trade],
    )


# ---------- bar 撞价 ----------

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
        price = round(price, 2)
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
    return _result(order, round(fill_price, 2), vol, seq, dt_ms, cfg)


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
        return _result(order, round(price, 2), rem, seq, dt_ms, cfg)

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

    return _result(order, round(fill_price, 2), rem, seq, dt_ms, cfg)


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

def affordable_volume(cash: float, price: float, desired: int,
                      fee: FeeCalculator, lot: int = constants.LOT_SIZE) -> int:
    """现金可负担的最大成交量（含费用，向下取整到整手；<一手返回 0）。

    回测与模拟盘共用的资金簿记规则；正式 RiskGate 资金闸在 D16。
    """
    if desired <= 0 or price <= 0:
        return 0
    est = int(cash / (price * (1 + fee.commission_rate + fee.transfer_fee)))
    vol = min(desired, max(est, 0))
    while vol > 0 and cash < price * vol + fee.buy_fee(price * vol):
        vol -= 1
    vol = (vol // lot) * lot        # 始终整手向下取整（不足一手 = 不可买）
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
            # T+1：当日买入 available 不增
            cash_delta = -(trade.amount + fee)
        else:
            new_vol = int(e["volume"]) - trade.volume
            e["volume"] = max(new_vol, 0)
            e["available"] = min(int(e["available"]), max(new_vol, 0))
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
