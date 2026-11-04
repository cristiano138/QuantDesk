"""通道韧性公共设施：指数退避重连 + 探活守护 + 重连后重放钩子。

为什么需要它：主流股票软件通道（miniQMT / 通达信 / 掘金终端 / easytrader 客户端）
都会「悄悄掉线」——客户端被登出、终端重启、网络抖动，而 SDK 不一定回报异常。
宪法义务：断线必须可被 health_check 探测，重连成功后必须自行重放订阅 / 刷新对账。
本模块把这条义务做成可复用的守护线程，各通道只提交两个函数：probe 与 reconnect。

线程纪律：
- 探活与重连都在守护线程内完成，回调线程（SDK 的推送线程）只能调用 mark_lost()
  做状态标记，不得在回调里 sleep 或重连；
- 所有对外事件经 EventBus 发布（本模块只入队，不做阻塞 I/O）。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.timeutil import now_ms

__all__ = ["Backoff", "ChannelHealth", "ReconnectWatchdog"]


class Backoff:
    """指数退避：1s → 2s → 4s → … cap 60s（±jitter 比例抖动，防多通道同时重连）。"""

    def __init__(self, base: float = 1.0, cap: float = 60.0, factor: float = 2.0,
                 jitter: float = 0.1) -> None:
        self.base = float(base)
        self.cap = float(cap)
        self.factor = float(factor)
        self.jitter = float(jitter)
        self._cur = float(base)
        self.attempts = 0

    def reset(self) -> None:
        self._cur = self.base
        self.attempts = 0

    def next_delay(self) -> float:
        d = min(self._cur, self.cap)
        self._cur = max(self._cur * self.factor, 0.0)
        self.attempts += 1
        if self.jitter and d:
            d += d * self.jitter * random.uniform(-1.0, 1.0)      # noqa: S311 — 抖动非安全用途
        return max(d, 0.0)


@dataclass
class ChannelHealth:
    """单通道健康快照（供 API 状态灯与 UI 接入向导展示）。"""

    name: str
    connected: bool = False
    probes: int = 0
    probe_failures: int = 0            # 连续失败次数（成功即清零）
    reconnect_attempts: int = 0
    reconnects: int = 0
    last_ok_ms: int | None = None
    last_error: str = ""
    state: str = "init"                # init / ok / lost / recovering
    history: list[str] = field(default_factory=list)   # 最近状态迁移（排障用）

    def to_dict(self) -> dict:
        return {
            "name": self.name, "connected": self.connected, "state": self.state,
            "probes": self.probes, "probe_failures": self.probe_failures,
            "reconnect_attempts": self.reconnect_attempts, "reconnects": self.reconnects,
            "last_ok_ms": self.last_ok_ms, "last_error": self.last_error,
        }


class ReconnectWatchdog:
    """周期探活 → 判死广播 → 退避重连 → 重连后重放订阅/刷新对账。

    参数：
      probe()      轻量探活，返回 bool（不得抛——抛了按失败处理）
      reconnect()  重建连接，失败抛异常（按重连失败处理，继续退避）
      on_reconnected() 重连成功后的义务动作：重放订阅列表、全量刷新持仓/资金对账
      interval     健康态下的探活间隔（默认 60s，与 D14 口径一致）
    """

    def __init__(self, *, name: str, probe: Callable[[], bool],
                 reconnect: Callable[[], None],
                 on_reconnected: Callable[[], None] | None = None,
                 bus: EventBus | None = None, interval: float = 60.0,
                 backoff: Backoff | None = None,
                 probe_timeout_note: str = "") -> None:
        self.name = name
        self._probe = probe
        self._reconnect = reconnect
        self._on_reconnected = on_reconnected
        self._bus = bus
        self.interval = float(interval)
        self.backoff = backoff or Backoff()
        self.note = probe_timeout_note
        self.health = ChannelHealth(name=name)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._force_check = threading.Event()   # mark_lost() 唤醒守护线程立即处理

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"watchdog-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._force_check.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    # ---------- 外部上报 ----------
    def mark_lost(self, reason: str) -> None:
        """SDK 回调侧报告断线（如 QMT disconnected 回调）：只标记 + 唤醒，不阻塞。"""
        with self._lock:
            self.health.last_error = reason
            self._transition("lost", reason)
        self._force_check.set()

    # ---------- 单步驱动（测试可直接调用，无需等线程） ----------
    def check_once(self) -> bool:
        """探活一次；健康返回 True。失败即转 lost 并广播，随后进入恢复流程。"""
        try:
            ok = bool(self._probe())
        except Exception as e:  # noqa: BLE001 探活异常按断线处理
            ok = False
            with self._lock:
                self.health.last_error = f"{e.__class__.__name__}: {e}"
        with self._lock:
            self.health.probes += 1
            if ok:
                self.health.probe_failures = 0
                self.health.last_ok_ms = now_ms()
                if self.health.state != "ok":
                    self._transition("ok", "探活通过")
            else:
                self.health.probe_failures += 1
                self._transition("lost", self.health.last_error or "探活失败")
        return ok

    def recover_once(self) -> bool:
        """尝试重连一次；成功则履行重连义务（重放订阅/刷新对账）并广播恢复。"""
        try:
            self._reconnect()
        except Exception as e:  # noqa: BLE001 重连失败继续退避
            with self._lock:
                self.health.reconnect_attempts += 1
                self.health.last_error = f"重连失败 {e.__class__.__name__}: {e}"
            return False
        with self._lock:
            self.health.reconnects += 1
            self.health.reconnect_attempts = 0
            self.health.last_ok_ms = now_ms()
        if self._on_reconnected is not None:
            try:
                self._on_reconnected()
            except Exception as e:  # noqa: BLE001 重放义务失败要留痕但不吞掉「已重连」事实
                with self._lock:
                    self.health.last_error = f"重连后义务失败: {e}"
                self._publish({"action": "channel_replay_failed", "channel": self.name,
                               "detail": str(e)})
        with self._lock:
            self._transition("ok", "重连成功")
        self.backoff.reset()
        return True

    # ---------- 守护循环 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            self._force_check.wait(timeout=self.interval)
            self._force_check.clear()
            if self._stop.is_set():
                return
            if self.check_once():
                continue
            # 已判死：按退避节奏重连，直到成功或线程停止（fail-closed 期间下单会被闸拒）
            while not self._stop.is_set():
                delay = self.backoff.next_delay()
                with self._lock:
                    if self.health.state != "recovering":
                        self._transition("recovering", f"{delay:.1f}s 后重连")
                if self._stop.wait(timeout=delay):
                    return
                if self.recover_once():
                    break

    # ---------- 内部 ----------
    def _transition(self, state: str, detail: str) -> None:
        """状态迁移（须在 _lock 内调用）：只在真正变化时广播，避免刷屏。"""
        prev = self.health.state
        if prev == state:
            return
        self.health.state = state
        self.health.connected = state == "ok"
        self.health.history.append(f"{state}:{detail}")
        del self.health.history[:-20]
        if state == "lost":
            self._publish({"action": "channel_down", "channel": self.name,
                           "detail": detail, "note": self.note})
        elif state == "ok" and prev in ("lost", "recovering"):
            self._publish({"action": "channel_up", "channel": self.name, "detail": detail})

    def _publish(self, payload: dict) -> None:
        if self._bus is not None:
            self._bus.publish(Event(EventType.RISK, data=payload, source="watchdog"))
