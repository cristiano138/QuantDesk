"""策略子进程守护（D12）：心跳判死 + 自动重启（带上限）+ 崩溃日志落盘。

规则（宪法引擎层）：
- 心跳 5s（子进程 heartbeat 信道）；15s 无心跳判死；
- 自动重启上限 3 次/小时，超限标记 failed 待人工；
- 崩溃前最后 10 条策略日志落盘 data/logs/crash_<strategy>_<ts>.log；
- 重启后重发 POSITION 快照（子进程持仓视图从 SQLite 恢复）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from quantdesk.config import data_dir
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.engine.runner import spawn_strategy_process
from quantdesk.ipc.bridge import BridgeMain
from quantdesk.store import repo

__all__ = ["StrategySupervisor"]


class StrategySupervisor:
    """一个策略一个守护器：持有 BridgeMain + 子进程句柄 + 监控线程。"""

    def __init__(self, strategy_path: str, params: dict, strategy_id: str,
                 db_path=None, event_bus: EventBus | None = None,
                 heartbeat_interval: float = 5.0, dead_after: float = 15.0,
                 max_restarts_per_hour: int = 3, on_intent=None,
                 monitor_tick: float = 0.2, spawn_grace: float = 30.0) -> None:
        self.strategy_path = strategy_path
        self.params = dict(params)
        self.strategy_id = strategy_id
        self.db_path = db_path
        self.bus = event_bus
        self.heartbeat_interval = heartbeat_interval
        self.dead_after = dead_after
        self.max_restarts_per_hour = max_restarts_per_hour
        self._on_intent = on_intent            # 上行意图回调（订单路由等）
        self.monitor_tick = monitor_tick
        self.spawn_grace = spawn_grace         # 首次心跳前的宽限（覆盖慢启动导入期）

        self.bridge = BridgeMain()
        self._proc = None
        self.status = "stopped"                # stopped/running/restarting/failed
        self.restarts: deque[float] = deque(maxlen=100)
        self.last_heartbeat: float = 0.0
        self._ever_beat = False                # 重启宽限：首次心跳到达前不做心跳判死
        self._spawn_monotonic: float = 0.0
        self._republish_until: float = 0.0     # 持仓快照重发窗口（slow-joiner 对策）
        self._last_republish: float = 0.0
        self.ready_seen = threading.Event()    # 子进程 ready（由监控线程独占消费 PULL）
        self.last_logs: deque[str] = deque(maxlen=10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.is_alive():
                return
            self._respawn()
            self.status = "running"
            self.last_heartbeat = time.monotonic()
            if self._thread is None:
                self._stop.clear()
                self._thread = threading.Thread(target=self._monitor_loop,
                                                name=f"supervisor-{self.strategy_id}",
                                                daemon=True)
                self._thread.start()
            self._publish(EventType.LOG, {"message": f"策略 {self.strategy_id} 启动"})

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止：控制指令 → 等退出 → 兜底 terminate → 监控线程收尾。"""
        self._stop.set()
        with self._lock:
            if self._proc is not None and self._proc.is_alive():
                try:
                    self.bridge.publish("control", {"action": "stop"})
                    self._proc.join(timeout=timeout)
                except Exception:                     # noqa: BLE001
                    pass
                if self._proc.is_alive():
                    self._proc.terminate()
            self.status = "stopped"
        if self._thread is not None:
            self._thread.join(timeout=3)

    def shutdown(self) -> None:
        self.stop()
        self.bridge.close()

    # ---------- 内部 ----------
    def _respawn(self) -> None:
        self._proc = spawn_strategy_process(
            self.strategy_path, self.params, self.strategy_id,
            self.bridge.event_url, self.bridge.order_url,
            heartbeat_interval=self.heartbeat_interval)
        self.last_heartbeat = time.monotonic()
        self._spawn_monotonic = time.monotonic()
        self._ever_beat = False
        # PUB/SUB slow-joiner：spawn 后立即广播会丢（子进程 SUB 未订阅）。
        # 策略：spawn 后 3s 内每 0.5s 重发一次持仓快照（见 _monitor_loop），
        # 外加收到 "ready" 时立即补发——双保险保证子进程拿到恢复上下文。
        self._republish_until = time.monotonic() + 3.0
        self._last_republish = 0.0

    def _publish_position_snapshot(self) -> None:
        """把 SQLite 最新持仓快照广播给子进程（重启上下文恢复）。"""
        positions = [
            {"symbol": p.symbol, "volume": p.volume, "available": p.available,
             "avg_cost": p.avg_cost, "market_value": p.market_value,
             "strategy_id": p.strategy_id}
            for p in repo.latest_positions(db_path=self.db_path)
        ]
        self.bridge.publish("position", {"positions": positions})

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_intents()
                self._republish_snapshot_if_due()
                self._check_health()
            except Exception:                      # noqa: BLE001 — 停机竞态不炸守护线程
                if self._stop.is_set():
                    return
            time.sleep(self.monitor_tick)

    def _republish_snapshot_if_due(self) -> None:
        """spawn 后 3s 窗口内每 0.5s 重发持仓快照（覆盖 SUB 订阅传播的不确定时点）。"""
        now = time.monotonic()
        if now < self._republish_until and now - self._last_republish >= 0.5:
            self._last_republish = now
            try:
                self._publish_position_snapshot()
            except Exception:                      # noqa: BLE001
                pass

    def _drain_intents(self) -> None:
        for tag, data in self.bridge.drain_intents(max_items=500):
            if tag == "heartbeat":
                self.last_heartbeat = time.monotonic()
                self._ever_beat = True
            elif tag == "ready":
                # 子进程 SUB 已连接（ready 在 connect 之后发出）→ 此刻补发持仓快照不丢
                self.ready_seen.set()
                try:
                    self._publish_position_snapshot()
                except Exception:                  # noqa: BLE001
                    pass
            elif tag == "log":
                self.last_logs.append(str(data.get("message", "")))
                self._publish(EventType.LOG, data | {"strategy_id": self.strategy_id})
            elif self._on_intent is not None:
                try:
                    self._on_intent(tag, data)
                except Exception:                 # noqa: BLE001 回调异常不拖垮守护
                    self._publish(EventType.LOG, {"message": "on_intent 回调异常"})

    def _check_health(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            # 判死条件一：进程退出（崩溃/被杀）
            if not proc.is_alive():
                exitcode = proc.exitcode
                self._dump_crash_log(f"进程退出 exitcode={exitcode}")
                self._publish(EventType.LOG, {"message":
                    f"策略 {self.strategy_id} 崩溃（exitcode={exitcode}），准备重启"})
                self._try_restart()
                return
            # 判死条件二：心跳超时。
            # 宽限规则：收到过首次心跳 → 按 dead_after 判；
            # 从未收到心跳（启动即挂死/卡死在导入期）→ 按 spawn_grace 判。
            if self.status == "running":
                if self._ever_beat:
                    overdue = time.monotonic() - self.last_heartbeat > self.dead_after
                else:
                    overdue = time.monotonic() - self._spawn_monotonic > self.spawn_grace
            else:
                overdue = False
            if overdue:
                self._dump_crash_log(f"心跳超时 > {self.dead_after}s，判死")
                proc.terminate()
                proc.join(timeout=3)
                if proc.is_alive():
                    proc.kill()
                self._try_restart()

    def _try_restart(self) -> None:
        now = time.monotonic()
        hour_ago = now - 3600
        recent = [t for t in self.restarts if t > hour_ago]
        if len(recent) >= self.max_restarts_per_hour:
            self.status = "failed"
            self._publish(EventType.LOG, {"message":
                f"策略 {self.strategy_id} 重启超限（{len(recent)}/小时），标记 failed 待人工"})
            return
        self.restarts.append(now)
        self._respawn()
        self.status = "running"
        self._publish(EventType.LOG, {"message":
            f"策略 {self.strategy_id} 已自动重启（{len(recent)}/{self.max_restarts_per_hour}）"})

    def _dump_crash_log(self, reason: str) -> None:
        """崩溃前最后 10 条日志落盘。"""
        try:
            d = data_dir() / "logs"
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            f = d / f"crash_{self.strategy_id}_{ts}.log"
            f.write_text("\n".join([f"[{ts}] {reason}"] + list(self.last_logs)),
                         encoding="utf-8")
        except Exception:                          # noqa: BLE001 落盘失败不阻断重启
            pass

    def _publish(self, etype: EventType, data: dict) -> None:
        if self.bus is not None:
            self.bus.publish(Event(etype, data=data | {"strategy_id": self.strategy_id},
                                   source="supervisor"))
