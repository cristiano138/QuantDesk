"""内置模拟撮合通道（D09）——与未来实盘共用同一套库表与数据面。

宪法适配器合同：
- 只执行不风控：收到的 Order 一定已过 RiskGate（第①道闸路由到这里）；
- 失败抛 BrokerError，禁止静默失败；
- 先落库后广播：订单/成交先写 SQLite（WAL）再发 EventBus；
- 重启恢复：connect() 从 SQLite 回放成交与账户，恢复持仓/资金/T+1 可用。
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import replace
from datetime import datetime, timezone

from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import BrokerError
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Tick, Trade
from quantdesk.core.timeutil import now_ms
from quantdesk.engine.matching import (
    FeeCalculator,
    MatchConfig,
    PositionBook,
    affordable_volume,
    apply_fill,
    match_on_tick,
)
from quantdesk.store import repo

__all__ = ["PaperBroker"]


def _side_str(v) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _status_str(v) -> str:
    return v.value if isinstance(v, OrderStatus) else str(v)


def _ms_to_local_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _is_today_quote(dt: datetime) -> bool:
    """这份行情是不是今天取的。

    模拟撮合只肯认当天的价：收盘后（或隔夜重启）缓存里那份 tick 是**昨收/昨日收盘**，
    拿它当现价撮合，界面就会出现「昨天 10.00 元买入、今天开盘 9.20」这种凭空多出来
    或凭空消失的盈亏。naive 时间戳按本机墙钟理解，与 paper 回放口径一致。
    """
    try:
        today = datetime.now().astimezone().date()
        d = dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return d.astimezone().date() >= today
    except Exception:                                  # noqa: BLE001 判不出日期就当不可用
        return False


class PaperBroker(BrokerAdapter):
    """模拟撮合：tick 撞价复用 engine/matching；状态持久化与实盘同表。"""

    name = "paper"
    is_paper = True
    label = "内置模拟盘"
    prereq = "无前置条件（默认通道，模拟盘开关开启时所有委托都由这里执行）"
    official = True

    def __init__(self, initial_cash: float = 1_000_000.0,
                 db_path=None, event_bus: EventBus | None = None,
                 match_config: MatchConfig | None = None) -> None:
        self.initial_cash = float(initial_cash)
        self.db_path = db_path
        self.bus = event_bus
        self.match_cfg = match_config or MatchConfig()
        self.book = PositionBook()
        self.cash: float = self.initial_cash
        self._latest: dict[str, Tick] = {}
        self._resting: dict[str, Order] = {}
        self._connected = False
        # 并发防护：D14 起实时回调线程（on_tick）与 API 线程（place_order）并发
        # 进入撮合/记账，共享 book/cash/resting——全程持锁（RLock 允许内部重入）
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------
    def connect(self, **credentials) -> None:
        with self._lock:
            self._restore()
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            if self._connected:
                self._snapshot_positions()
            self._connected = False

    def health_check(self) -> bool:
        return self._connected

    # ---------- 行情注入（运行时/测试喂 tick；D14 接实时源后由运行时调用） ----------
    def on_tick(self, tick: Tick) -> None:
        with self._lock:
            self._latest[tick.symbol] = tick
            self._match_resting(tick)

    def latest_tick(self, symbol: str) -> Tick | None:
        """当前用来撮合的最新快照（没有就返回 None，不编一个）。

        下单编排要先知道「手上这只票的行情过没过期」——挂单只有行情到达才会重撞，
        没行情时委托会一直不成交，用户看到的就不是「拒绝」而是「点了没反应」。
        """
        with self._lock:
            return self._latest.get(symbol)

    # ---------- 交易 ----------
    def place_order(self, order: Order) -> Order:
        with self._lock:
            return self._place_order_locked(order)

    def _place_order_locked(self, order: Order) -> Order:
        if not self._connected:
            raise BrokerError("paper 通道未连接")
        o = replace(order, status=OrderStatus.SUBMITTED)
        # 卖出量不得超过可用（T+1；正式闸在 D16，这里防组合簿负数）
        if _side_str(o.side) == Side.SELL.value and o.volume > self.book.available(o.symbol):
            o = replace(o, status=OrderStatus.REJECTED,
                        message=f"T+1 可用不足（available={self.book.available(o.symbol)}）")
            repo.upsert_order(o, db_path=self.db_path)
            self._publish(EventType.RISK, o)
            return o

        repo.upsert_order(o, db_path=self.db_path)      # 先落库
        tick = self._latest.get(o.symbol)
        if o.order_type == OrderType.MARKET:
            if tick is None:
                o = replace(o, status=OrderStatus.REJECTED,
                            message="无最新行情，市价单无法撮合（fail-closed）")
                repo.upsert_order(o, db_path=self.db_path)
                self._publish(EventType.RISK, o)
                return o
            if not _is_today_quote(tick.dt):
                # 休市/隔夜那份行情给的是**收盘价**，按它算成交等于用昨天的价成交今天的单
                o = replace(o, status=OrderStatus.REJECTED,
                            message=f"最新行情是 {_ms_to_local_date(int(tick.dt.timestamp()*1000))}"
                                    "的收盘价，今日休市——市价单不做撮合（限价单照旧挂起等行情）")
                repo.upsert_order(o, db_path=self.db_path)
                self._publish(EventType.RISK, o)
                return o

        if tick is not None:
            o = self._try_fill(o, tick)
            if _status_str(o.status) in (OrderStatus.FILLED.value,
                                         OrderStatus.REJECTED.value):
                return o
        # 未成交/部分成交 → 挂单簿
        self._resting[o.order_id] = o
        self._publish(EventType.ORDER, o)
        return o

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            o = self._resting.pop(order_id, None)
        if o is None:
            return False
        o = replace(o, status=OrderStatus.CANCELLED, message="用户撤单",
                    updated_at=now_ms())
        repo.upsert_order(o, db_path=self.db_path)
        self._publish(EventType.ORDER, o)
        return True

    # ---------- 查询（与实盘同表） ----------
    def get_position(self, symbol: str | None = None) -> list[Position]:
        ps = self.book.positions()
        if symbol is None:
            return ps
        return [p for p in ps if p.symbol == symbol]

    def get_account(self) -> Account:
        closes = {s: t.last for s, t in self._latest.items()}
        mv = self.book.market_value(closes)
        return Account(total_asset=round(self.cash + mv, 2), cash=round(self.cash, 2),
                       market_value=mv, frozen=0.0)

    def get_orders(self, status: OrderStatus | None = None) -> list[Order]:
        return repo.list_orders(status=status, db_path=self.db_path)

    def get_trades(self, start: datetime | None = None) -> list[Trade]:
        start_ms = int(start.timestamp() * 1000) if start else None
        return repo.list_trades(start_ms=start_ms, limit=10_000, db_path=self.db_path)

    # ---------- T+1 / 对账 ----------
    def on_new_trading_day(self, today: str | None = None) -> None:
        """交易日切换（calendar 判定由运行时驱动，D14 接实时源后挂定时）。"""
        self.book.on_new_trading_day()
        self._snapshot_positions()
        self._publish(EventType.ACCOUNT, self.get_account())

    # ---------- 内部 ----------
    def _try_fill(self, o: Order, tick: Tick) -> Order:
        """尝试按当前 tick 撮合；资金不足缩量；返回最新订单快照。"""
        r = match_on_tick(o, tick, self.match_cfg)
        if not r.filled:
            return o
        fill_vol = r.filled_volume
        if _side_str(o.side) == Side.BUY.value:
            fill_vol = affordable_volume(self.cash, r.fill_price, fill_vol,
                                         self.match_cfg.fee, symbol=o.symbol)
            if fill_vol <= 0:
                o = replace(o, status=OrderStatus.REJECTED, message="资金不足拒绝")
                repo.upsert_order(o, db_path=self.db_path)
                self._publish(EventType.RISK, o)
                return o
            if fill_vol < r.filled_volume:
                # 资金不足：订单有效量对齐到可负担量（消息留痕），剩余意图作废
                o = replace(o, volume=o.filled_volume + fill_vol,
                            message=(o.message or "")
                            + f"资金不足缩量至 {o.filled_volume + fill_vol} 股；")
                r = match_on_tick(o, tick, self.match_cfg)
                if not r.filled:
                    return o
        # 先落库
        for t in r.trades:
            repo.append_trade(t, db_path=self.db_path)
        self.cash = round(self.cash + self.book.apply_trade(r.trades[0], fee=r.fee), 2)
        repo.account_set(self.cash, self.initial_cash, db_path=self.db_path)
        o = apply_fill(o, r)
        repo.upsert_order(o, db_path=self.db_path)
        self._snapshot_positions()
        # 后广播
        for t in r.trades:
            self._publish(EventType.TRADE, t)
        self._publish(EventType.ORDER, o)
        self._publish(EventType.POSITION, self.book.positions())
        self._publish(EventType.ACCOUNT, self.get_account())
        return o

    def _match_resting(self, tick: Tick) -> None:
        """行情到达后重撞挂单簿。"""
        for oid in [k for k, v in self._resting.items() if v.symbol == tick.symbol]:
            o = self._try_fill(self._resting[oid], tick)
            if _status_str(o.status) in (OrderStatus.FILLED.value,
                                         OrderStatus.REJECTED.value):
                self._resting.pop(oid, None)
            else:
                self._resting[oid] = o

    def _restore(self) -> None:
        """从 SQLite 回放：账户 + 全部成交 → 持仓/T+1/资金；当日挂单恢复。"""
        row = repo.account_get(db_path=self.db_path)
        if row is not None:
            self.cash = row.cash
            self.initial_cash = row.initial_cash or self.initial_cash
        else:
            repo.account_set(self.cash, self.initial_cash, db_path=self.db_path)

        trades = repo.list_trades(limit=10 ** 9, db_path=self.db_path)
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        fee: FeeCalculator = self.match_cfg.fee
        pre, cur = [], []
        for t in trades:
            (pre if _ms_to_local_date(t.traded_at) < today else cur).append(t)
        for t in pre:
            self.book.apply_trade(t, fee=fee.fee(t.side, t.amount, t.symbol))
        self.book.on_new_trading_day()               # 历史买入全部转为可用
        for t in cur:                                # 今日买入 available 不增
            self.book.apply_trade(t, fee=fee.fee(t.side, t.amount, t.symbol))
        # 当日未成交挂单恢复（重启不丢挂单；含部分成交的余量）
        for st in (OrderStatus.SUBMITTED, OrderStatus.PART_FILLED):
            for o in repo.list_orders(status=st, db_path=self.db_path):
                self._resting[o.order_id] = o

    def _snapshot_positions(self) -> None:
        repo.save_positions_snapshot(self.book.positions(), db_path=self.db_path)

    def _publish(self, etype: EventType, data) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data))


ADAPTER = PaperBroker
