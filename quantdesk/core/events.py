"""事件总线（EventBus）。

- 同步分发：publish 在调用线程内依次调用 handler；
- 线程安全：subscribe/unsubscribe 加锁；publish 持快照派发，不持锁回调；
- 单 handler 异常只记日志不扩散（D02 DoD：单 handler 抛异常不影响其他订阅者）；
- 事件类型（宪法核心契约）：TICK·BAR·ORDER·TRADE·POSITION·ACCOUNT·LOG·TIMER·RISK。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)


class EventType(str, Enum):
    TICK = "tick"
    BAR = "bar"
    ORDER = "order"
    TRADE = "trade"
    POSITION = "position"
    ACCOUNT = "account"
    LOG = "log"
    TIMER = "timer"
    RISK = "risk"
    SETTINGS = "settings"   # D19 扩展（宪法枚举未删减，新增事件类型）


@dataclass
class Event:
    type: EventType | str
    data: Any = None
    source: str = ""
    ts: int = field(default_factory=now_ms)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[int, tuple[EventType | None, Handler]] = {}
        self._next_id = 0

    # ---------- 管理 ----------
    def subscribe(self, etype: EventType | str | None = None, handler: Handler | None = None) -> int:
        """订阅。etype=None 收全部类型（通配）。返回 sub_id。"""
        if handler is None:
            raise ValueError("handler 不能为空")
        if isinstance(etype, str):
            etype = EventType(etype)
        with self._lock:
            self._next_id += 1
            sub_id = self._next_id
            self._subs[sub_id] = (etype, handler)
            return sub_id

    def unsubscribe(self, sub_id: int) -> bool:
        with self._lock:
            return self._subs.pop(sub_id, None) is not None

    def subscriber_count(self, etype: EventType | str | None = None) -> int:
        with self._lock:
            if etype is None:
                return len(self._subs)
            if isinstance(etype, str):
                etype = EventType(etype)
            return sum(1 for t, _ in self._subs.values() if t is None or t is etype)

    # ---------- 派发 ----------
    def publish(self, event: Event) -> int:
        """同步派发。返回实际调用的 handler 数（含异常 handler）。"""
        etype = EventType(event.type) if isinstance(event.type, str) else event.type
        with self._lock:
            snapshot = [(t, h) for t, h in self._subs.values() if t is None or t is etype]
        called = 0
        for _, handler in snapshot:
            called += 1
            try:
                handler(event)
            except Exception:  # noqa: BLE001 单 handler 异常隔离，只记日志
                log.exception("EventBus handler 异常（已隔离）: type=%s", etype)
        return called


# 进程内统一事件枢纽（D10）：服务层发布 → /ws/events 下行到前端。
# 各服务默认发布到这里；测试可用独立 EventBus 注入隔离。
SYSTEM_BUS = EventBus()
