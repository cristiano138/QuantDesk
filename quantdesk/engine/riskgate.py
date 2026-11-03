"""RiskGate——订单唯一咽喉（D09 最小版：第①道闸；②③道闸分别在 D16/D17 补齐）。

三道闸（宪法第 2 条）：
① 全局模拟盘开关：simulation_only ON → 一律路由内置模拟撮合
   （ON 时即使实盘通道缺位，下单也不报错）；
② A 股规则校验（T+1/整手/涨跌停价/仓位/日委托数/资金充足）——D16；
③ 实盘二次确认（PENDING_CONFIRM → WS 推送 → 10s 超时作废）——D17。

fail-closed：模拟盘 OFF 且实盘通道缺位/断线 → 直接拒单（不排队不重试）。
"""

from __future__ import annotations

import threading
from uuid import uuid4

from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Order
from quantdesk.store import repo

__all__ = ["RiskGate"]


class _SimSettings:
    """默认设置视图（真实配置经 get_settings 注入；可测试桩替换）。"""

    simulation_only = True


class RiskGate:
    """submit() 是一切下单意图的唯一入口；confirm/reject 为第③道闸回调。"""

    def __init__(self, paper_broker=None, live_broker=None,
                 settings=None, db_path=None, event_bus: EventBus | None = None) -> None:
        self.paper = paper_broker
        self.live = live_broker
        self.settings = settings          # None → 运行时读 config（支持 D19 热切）
        self.db_path = db_path
        self.bus = event_bus
        self._pending_confirm: dict[str, Order] = {}
        self._lock = threading.RLock()

    # ---------- 咽喉 ----------
    def submit(self, intent: Order) -> Order:
        o = intent
        if not getattr(o, "order_id", ""):
            o = _replace(o, order_id=self.new_order_id())
        if self._sim_only():
            # 闸①：全局模拟盘开关 ON → 一律 paper（实盘通道缺位不影响）
            if self.paper is None:
                return self._reject(o, "模拟通道未装配（paper_broker 缺失）")
            placed = self.paper.place_order(_replace(o, status=OrderStatus.SUBMITTED))
            return placed
        # 模拟盘 OFF → 实盘路径
        if self.live is None:
            return self._reject(o, "模拟盘开关 OFF 且实盘通道未接入——fail-closed 拒单")
        if not self.live.health_check():
            return self._reject(o, "实盘通道断线——fail-closed 拒单（不排队不重试）")
        # 闸③：二次确认（D17 完整版：先落库 → WS 推送 → 10s 超时）
        o = _replace(o, status=OrderStatus.PENDING_CONFIRM)
        with self._lock:
            self._pending_confirm[o.order_id] = o
        repo.upsert_order(o, db_path=self.db_path)     # 先落库
        self._publish(EventType.RISK, {"action": "confirm_request", "order": _to_dict(o)})
        return o

    def confirm(self, order_id: str) -> Order:
        """二次确认回调（D17 完整版；当前最小实现：确认后投递实盘）。"""
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
        if o is None:
            raise ValueError(f"订单 {order_id} 不在待确认队列")
        placed = self.live.place_order(_replace(o, status=OrderStatus.SUBMITTED))
        return placed

    def reject(self, order_id: str) -> Order:
        with self._lock:
            o = self._pending_confirm.pop(order_id, None)
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


def _replace(o: Order, **kw) -> Order:
    import dataclasses

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
