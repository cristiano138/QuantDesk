"""运行日志落库（LOG/RISK 事件 → SQLite，可检索）。

为什么要落库：策略与系统的 LOG 事件此前只进内存（supervisor 留 10 条）和 WS，
页面一关、进程一重启，「昨天 14:20 那次拒单到底写了什么」就没人答得上。
交易与风控的可回溯性是商业交付的硬指标，不是可选项。

写入纪律：
- 事件回调线程只入队（宪法：回调线程禁止阻塞），专职线程批量成事务写入；
- 队列满则丢弃并计数（日志不能反过来把主链路拖死），丢弃量经 stats() 如实上报；
- 保留期由盘后任务 purge，避免库无限增长。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS
from quantdesk.core.timeutil import now_ms
from quantdesk.store import repo

log = logging.getLogger(__name__)

__all__ = ["LogSink", "get_log_sink"]

_FLUSH_INTERVAL_S = 1.0
_BATCH_SIZE = 100
_QUEUE_SIZE = 5000


def _message_of(event: Event) -> tuple[str, str, str]:
    """事件 → (message, level, strategy_id)。结构化字段优先，其余整体 JSON。"""
    data = event.data
    if hasattr(data, "order_id"):                       # Order 对象（RISK/ORDER 事件）
        return (f"{getattr(data, 'symbol', '')} {getattr(data, 'side', '')} "
                f"{getattr(data, 'volume', '')}@{getattr(data, 'price', '')} → "
                f"{getattr(data, 'status', '')} {getattr(data, 'message', '')}".strip(),
                "WARN" if event.type == EventType.RISK else "INFO",
                str(getattr(data, "strategy_id", "") or ""))
    if isinstance(data, dict):
        msg = str(data.get("message") or data.get("action") or "")
        if not msg:
            msg = json.dumps(data, ensure_ascii=False, default=str)[:500]
        return (msg, str(data.get("level", "WARN" if event.type == EventType.RISK
                                  else "INFO")),
                str(data.get("strategy_id", "") or ""))
    return (str(data)[:500], "INFO", "")


class LogSink:
    def __init__(self, bus: EventBus | None = None, db_path=None,
                 types: tuple[EventType, ...] = (EventType.LOG, EventType.RISK)) -> None:
        self.bus = bus or SYSTEM_BUS
        self.db_path = db_path
        self.types = types
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sub_ids: list[int] = []
        self.written = 0
        self.dropped = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        for et in self.types:
            self._sub_ids.append(self.bus.subscribe(et, self._on_event))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="log-sink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        for sub_id in self._sub_ids:
            self.bus.unsubscribe(sub_id)
        self._sub_ids.clear()
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._flush(self._drain())                   # 收尾把队列里的写干净

    # ---------- 入队（事件线程） ----------
    def _on_event(self, event: Event) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:                            # 日志积压不反压主链路
            self.dropped += 1

    # ---------- 批量写（专职线程） ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._drain(wait=_FLUSH_INTERVAL_S)
            if batch:
                self._flush(batch)

    def _drain(self, wait: float | None = None) -> list[dict]:
        batch: list[dict] = []
        if wait is not None:
            try:
                first = self._q.get(timeout=wait)
            except queue.Empty:
                return batch
            batch.append(self._row(first))
        while len(batch) < _BATCH_SIZE:
            try:
                batch.append(self._row(self._q.get_nowait()))
            except queue.Empty:
                break
        return batch

    @staticmethod
    def _row(event: Event) -> dict:
        message, level, strategy_id = _message_of(event)
        return {"ts": event.ts or now_ms(), "level": level,
                "source": event.source or "system", "strategy_id": strategy_id,
                "message": message}

    def _flush(self, rows: list[dict]) -> None:
        if not rows:
            return
        try:
            self.written += repo.logs_append(rows, db_path=self.db_path)
        except Exception as e:  # noqa: BLE001 落库失败降级为计数，不影响交易链路
            log.error("日志落库失败（丢弃 %d 条）: %s", len(rows), e)
            self.dropped += len(rows)

    def flush_now(self) -> int:
        rows = self._drain()
        self._flush(rows)
        return len(rows)

    def stats(self) -> dict:
        return {"written": self.written, "dropped": self.dropped,
                "queued": self._q.qsize(), "running": self._thread is not None
                and self._thread.is_alive()}

    def query(self, **kw) -> dict:
        return repo.logs_query(db_path=self.db_path, **kw)

    def sources(self) -> list[dict]:
        return repo.logs_sources(db_path=self.db_path)

    def purge_before(self, ts_ms: int) -> int:
        return repo.logs_purge(ts_ms, db_path=self.db_path)


_sink: LogSink | None = None
_sink_lock = threading.Lock()


def get_log_sink() -> LogSink:
    global _sink
    with _sink_lock:
        if _sink is None:
            _sink = LogSink()
        return _sink
