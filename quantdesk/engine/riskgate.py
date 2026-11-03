"""RiskGate——订单唯一咽喉（D16：第②道闸 A 股规则校验全量落地）。

三道闸（宪法第 2 条，顺序强制）：
① 全局模拟盘开关：simulation_only ON → 路由内置模拟撮合（实盘通道缺位不影响）；
② A 股规则校验：T+1 可用 / 整手 / 委托价 ∈ [跌停, 涨停]（含 ST ±5%）/
   单票市值上限 / 日委托数上限 / 资金充足；拦截 → RISK 事件 + message 落库；
③ 实盘二次确认（PENDING_CONFIRM → WS 推送 → 10s 超时作废，D17 完整版）。

fail-closed 口径：
- 实盘路径缺通道/断线 → 直接拒单（不排队不重试）；
- 实盘限价单缺昨收数据 → 拒（无法校验涨跌停区间）；模拟盘缺昨收 → 放行并 LOG 告警
  （模拟盘用于策略迭代，实盘绝不绕过校验）。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd

from quantdesk.core import constants
from quantdesk.core.enums import AdjustType, Frequency, OrderStatus, OrderType, Side
from quantdesk.core.errors import ConnectionLostError
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Order
from quantdesk.core.symbols import board_of, is_st
from quantdesk.core.timeutil import now_ms
from quantdesk.engine.matching import FeeCalculator
from quantdesk.store import repo
from quantdesk.store.parquet_store import ParquetStore

__all__ = ["RiskGate"]


def _side_str(v) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _otype_str(v) -> str:
    return v.value if isinstance(v, OrderType) else str(v)


class _SimSettings:
    simulation_only = True


class RiskGate:
    """submit() 是一切下单意图的唯一入口；confirm/reject 为第③道闸回调。"""

    def __init__(self, paper_broker=None, live_broker=None,
                 settings=None, db_path=None, event_bus: EventBus | None = None,
                 store: ParquetStore | None = None,
                 max_position_value: float = 1_000_000.0,
                 max_daily_orders: int = 200,
                 fee: FeeCalculator | None = None,
                 confirm_timeout_s: float = 10.0) -> None:
        self.paper = paper_broker
        self.live = live_broker
        self.settings = settings          # None → 运行时读 config（支持 D19 热切）
        self.db_path = db_path
        self.bus = event_bus
        self.store = store
        self.max_position_value = float(max_position_value)
        self.max_daily_orders = int(max_daily_orders)
        self.fee = fee or FeeCalculator()
        self.confirm_timeout_s = float(confirm_timeout_s)
        self._pending_confirm: dict[str, Order] = {}
        self._armed_at: dict[str, float] = {}   # 确认窗口计时（重启后重新起算）
        self._lock = threading.RLock()
        self._prev_close_cache: dict[str, tuple[str, float]] = {}
        self._expiry_stop = threading.Event()
        if confirm_timeout_s > 0:
            self._expiry_thread = threading.Thread(target=self._expiry_loop,
                                                   name="riskgate-expiry", daemon=True)
            self._expiry_thread.start()

    # ---------- 超时作废（闸③ 10s 自动作废） ----------
    def _expiry_loop(self) -> None:
        while not self._expiry_stop.wait(0.2):
            try:
                self._expire_overdue()
            except Exception:                      # noqa: BLE001 — 守护循环不中断
                continue

    def _expire_overdue(self) -> None:
        deadline = time.monotonic() - self.confirm_timeout_s
        with self._lock:
            overdue = [oid for oid, armed in self._armed_at.items() if armed < deadline]
        for oid in overdue:
            with self._lock:
                o = self._pending_confirm.pop(oid, None)
                self._armed_at.pop(oid, None)
            if o is None:
                continue
            o = _replace(o, status=OrderStatus.EXPIRED_CONFIRM,
                         message=f"二次确认超时（{self.confirm_timeout_s:.0f}s）自动作废")
            repo.upsert_order(o, db_path=self.db_path)
            self._publish(EventType.RISK, {"action": "expired", "order": _to_dict(o)})

    def restore_pending(self) -> int:
        """重启恢复：从库重载待确认单，确认窗口重新起算（DoD：重启后仍在且可处理）。"""
        rows = repo.list_orders(status=OrderStatus.PENDING_CONFIRM, db_path=self.db_path)
        with self._lock:
            for o in rows:
                self._pending_confirm[o.order_id] = o
                self._armed_at[o.order_id] = time.monotonic()
        return len(rows)

    # ---------- 咽喉 ----------
    def submit(self, intent: Order) -> Order:
        o = intent
        if not getattr(o, "order_id", ""):
            o = _replace(o, order_id=self.new_order_id())

        sim = self._sim_only()
        broker = self.paper if sim else self.live
        if broker is None:
            return self._reject(o, "模拟盘开关 OFF 且实盘通道未接入——fail-closed 拒单"
                                if not sim else "模拟通道未装配（paper_broker 缺失）")

        # 闸②：A 股规则校验（两条路径都必经）
        violations = self.validate(o, broker=broker, live=not sim)
        if violations:
            return self._reject(o, "；".join(violations))

        if sim:
            # 闸① 路由：模拟撮合（无确认流）
            return broker.place_order(_replace(o, status=OrderStatus.SUBMITTED))

        # 实盘路径：闸③ 二次确认（D17 完整版：先落库 → WS 推送 → 超时作废）
        if not broker.health_check():
            return self._reject(o, "实盘通道断线——fail-closed 拒单（不排队不重试）")
        o = _replace(o, status=OrderStatus.PENDING_CONFIRM)
        with self._lock:
            self._pending_confirm[o.order_id] = o
            self._armed_at[o.order_id] = time.monotonic()
        repo.upsert_order(o, db_path=self.db_path)     # 先落库
        self._publish(EventType.RISK, {"action": "confirm_request", "order": _to_dict(o)})
        return o

    # ---------- 闸②：规则校验 ----------
    def validate(self, order: Order, broker=None, live: bool | None = None) -> list[str]:
        """返回违规原因列表（空 = 通过）。逐项独立检查，一次报全。

        broker 缺省时按闸①路由自动选取（sim→paper / 实盘→live）；
        live 缺省时由开关推断（实盘路径 fail-closed 口径）。
        """
        if broker is None:
            sim = self._sim_only()
            broker = self.paper if sim else self.live
            if live is None:
                live = not sim
        elif live is None:
            live = broker is self.live
        v: list[str] = []
        side = _side_str(order.side)
        otype = _otype_str(order.order_type)

        # 板块与代码合法性
        try:
            board = board_of(order.symbol)
        except Exception as e:                         # noqa: BLE001
            return [f"非法证券代码: {order.symbol}（{e}）"]
        if order.volume <= 0:
            v.append(f"委托数量必须 > 0（当前 {order.volume}）")
            return v                                   # 后续检查无意义

        # 整手（仅买入；卖出允许零股——A 股卖出可非整手）
        if side == Side.BUY.value:
            from quantdesk.core.constants import is_valid_lot

            if not is_valid_lot(order.volume, board):
                if board.name == "STAR":
                    v.append(f"科创板买入须 ≥200 股（当前 {order.volume}）")
                else:
                    v.append(f"买入须为 100 股整数倍（当前 {order.volume}）")

        # T+1：卖出量 ≤ available（查最新持仓快照）
        if side == Side.SELL.value and broker is not None:
            pos = None
            for p in broker.get_position(order.symbol):
                pos = p
                break
            avail = pos.available if pos else 0
            if order.volume > avail:
                v.append(f"T+1 可用不足：委托卖出 {order.volume} > 可用 {avail}")

        # 委托价 ∈ [跌停, 涨停]（仅限价单；市价单在撮合端控制）
        if otype == OrderType.LIMIT.value and order.price > 0:
            prev = self._prev_close(order.symbol)
            if prev is None:
                if live:
                    v.append("无昨收数据，无法校验涨跌停区间——fail-closed 拒单（请先下载数据）")
                else:
                    self._publish(EventType.LOG, {
                        "message": f"{order.symbol} 无昨收数据，跳过涨跌停校验（模拟盘）"})
            else:
                st = is_st(order.symbol)
                lo = constants.limit_down(prev, board, st)
                hi = constants.limit_up(prev, board, st)
                if not (lo - 1e-9 <= order.price <= hi + 1e-9):
                    tag = "（ST）" if st else ""
                    v.append(f"委托价 {order.price:.2f} 超出涨跌停区间{tag} "
                             f"[{lo:.2f}, {hi:.2f}]——疑似复权价误用")

        # 资金充足（买入：金额 + 费用 ≤ 可用现金）
        if side == Side.BUY.value and broker is not None and order.price > 0:
            acct = broker.get_account()
            notional = order.price * order.volume
            need = notional + self.fee.buy_fee(notional)
            if need > acct.cash + 1e-6:
                v.append(f"资金不足：需 {need:.2f} > 可用 {acct.cash:.2f}")

        # 单票市值上限（买入：现有持仓市值 + 本次金额 ≤ 上限）
        if side == Side.BUY.value and broker is not None and order.price > 0:
            pos = next(iter(broker.get_position(order.symbol)), None)
            held_val = pos.market_value if pos else 0.0
            new_val = held_val + order.price * order.volume
            if new_val > self.max_position_value + 1e-6:
                v.append(f"单票市值超限：{new_val:.2f} > 上限 {self.max_position_value:.2f}")

        # 日委托数上限
        if broker is not None:
            today_ms = _today_start_ms()
            n = sum(1 for o in broker.get_orders() if o.created_at >= today_ms)
            if n >= self.max_daily_orders:
                v.append(f"日委托数超限：今日已 {n} ≥ 上限 {self.max_daily_orders}")

        return v

    def _prev_close(self, symbol: str) -> float | None:
        """昨收（原始价口径，不复权）；日线表取 < 今日的最后一根。带当日缓存。"""
        if self.store is None:
            return None
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        cached = self._prev_close_cache.get(symbol)
        if cached and cached[0] == today:
            return cached[1]
        try:
            df = self.store.read(symbol, Frequency.D1, adjust=AdjustType.NONE)
        except Exception:                              # noqa: BLE001
            return None
        if df.empty:
            return None
        df = df[df["dt"] < pd.Timestamp(today)]
        if df.empty:
            return None
        px = float(df.iloc[-1]["close"])
        self._prev_close_cache[symbol] = (today, px)
        return px

    # ---------- 闸③：确认回调 ----------
    def confirm(self, order_id: str) -> Order:
        """用户确认 → 投递实盘。通道断线 → FAILED（fail-closed，不静默）。"""
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
            self._armed_at.pop(order_id, None)
        if o is None:
            raise ValueError(f"订单 {order_id} 不在待确认队列")
        if not self.live.health_check():
            o = _replace(o, status=OrderStatus.FAILED, message="确认时通道断线——fail-closed")
            repo.upsert_order(o, db_path=self.db_path)
            self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
            return o
        try:
            placed = self.live.place_order(_replace(o, status=OrderStatus.SUBMITTED))
        except ConnectionLostError as e:
            o = _replace(o, status=OrderStatus.FAILED, message=f"通道断线：{e}")
            repo.upsert_order(o, db_path=self.db_path)
            self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
            return o
        repo.upsert_order(placed, db_path=self.db_path)
        self._publish(EventType.ORDER, placed)
        return placed

    def reject(self, order_id: str) -> Order:
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
            self._armed_at.pop(order_id, None)
        if o is None:
            raise ValueError(f"订单 {order_id} 不在待确认队列")
        o = _replace(o, status=OrderStatus.REJECTED, message="用户拒绝（二次确认）")
        repo.upsert_order(o, db_path=self.db_path)
        self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
        return o

    # ---------- 辅助 ----------
    def new_order_id(self) -> str:
        return f"OD-{uuid4().hex[:12].upper()}"

    def _sim_only(self) -> bool:
        s = self.settings
        if s is None:
            from quantdesk.config import get_settings

            s = get_settings()
        return bool(getattr(s, "simulation_only", True))

    def _reject(self, o: Order, message: str) -> Order:
        o = _replace(o, status=OrderStatus.REJECTED, message=message)
        repo.upsert_order(o, db_path=self.db_path)     # 拦截落库，可查原因
        self._publish(EventType.RISK, {"action": "rejected", "order": _to_dict(o)})
        return o

    def _publish(self, etype: EventType, data) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data))


def _today_start_ms() -> int:
    d = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _replace(o: Order, **kw) -> Order:
    return dataclasses.replace(o, **kw)


def _to_dict(o: Order) -> dict:
    return {
        "order_id": o.order_id, "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "price": o.price, "volume": o.volume,
        "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "strategy_id": o.strategy_id, "message": o.message,
    }
