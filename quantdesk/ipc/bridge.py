"""ZMQ IPC 桥（D11）：主进程 ↔ 策略子进程。

信道（宪法 IPC 层）：
- 事件下行：主进程 XPUB 广播 → 策略进程 SUB 订阅（bar/tick/订单回报/控制指令）；
- 订单意图上行：策略进程 PUSH → 主进程 PULL（order_intent / log / heartbeat）；
- 序列化：msgpack + 类型标签（payload 内 "t" 字段）；
- 心跳：策略子进程后台线程定期 PUSH heartbeat（D12 supervisor 据此判死）。

Windows 注意：transport 用 tcp://127.0.0.1（本机回环，仅本机监听，符合本地化红线）；
端口用 tcp://127.0.0.1:0 由系统分配（LAST_ENDPOINT 回读）。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import msgpack
import zmq

__all__ = ["BridgeMain", "BridgeClient", "pack", "unpack"]


def pack(data: dict) -> bytes:
    return msgpack.packb(data, use_bin_type=True)


def unpack(raw: bytes) -> dict:
    return msgpack.unpackb(raw, raw=False)


class BridgeMain:
    """主进程侧：XPUB（事件广播）+ PULL（订单意图/心跳/日志上行）。"""

    def __init__(self) -> None:
        ctx = zmq.Context.instance()
        self._xpub = ctx.socket(zmq.XPUB)
        self._xpub.bind("tcp://127.0.0.1:0")
        self.event_url = self._xpub.getsockopt(zmq.LAST_ENDPOINT).decode()
        self._pull = ctx.socket(zmq.PULL)
        self._pull.bind("tcp://127.0.0.1:0")
        self.order_url = self._pull.getsockopt(zmq.LAST_ENDPOINT).decode()

    # ---------- 下行 ----------
    def publish(self, tag: str, data: dict) -> None:
        """广播事件（tag ∈ bar/tick/order/position/account/control…）。"""
        self._xpub.send_multipart([tag.encode("utf-8"), pack(data)])

    # ---------- 上行 ----------
    def recv_intent(self, timeout_ms: int = 1000) -> tuple[str, dict] | None:
        """收一条上行消息，返回 (tag, 内层数据)（信封已拆）。POLLIN 超时返回 None。"""
        if not self._pull.poll(timeout_ms):
            return None
        parts = self._pull.recv_multipart()
        payload = unpack(parts[-1])
        return str(payload.get("t", "")), dict(payload.get("d") or {})

    def drain_intents(self, max_items: int = 1000) -> list[tuple[str, dict]]:
        out = []
        while len(out) < max_items:
            item = self.recv_intent(timeout_ms=0)
            if item is None:
                break
            out.append(item)
        return out

    def close(self) -> None:
        self._xpub.close(0)
        self._pull.close(0)


class BridgeClient:
    """策略子进程侧：SUB（订阅事件）+ PUSH（上行意图）+ 心跳线程。"""

    def __init__(self, strategy_id: str, heartbeat_interval: float = 0.0) -> None:
        self.strategy_id = strategy_id
        self.heartbeat_interval = heartbeat_interval    # >0 启用自动心跳（D12 默认 5s）
        ctx = zmq.Context.instance()
        self._sub = ctx.socket(zmq.SUB)
        self._push = ctx.socket(zmq.PUSH)
        self._push.setsockopt(zmq.LINGER, 0)
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self.session_id = uuid.uuid4().hex[:12]

    # ---------- 连接 ----------
    def connect(self, event_url: str, order_url: str) -> None:
        self._sub.connect(event_url)
        self._sub.setsockopt(zmq.SUBSCRIBE, b"")        # 订阅全部类型
        self._push.connect(order_url)
        if self.heartbeat_interval > 0:
            self.start_heartbeat(self.heartbeat_interval)

    def start_heartbeat(self, interval: float) -> None:
        self.heartbeat_interval = interval
        self._hb_stop.clear()
        if self._hb_thread is not None:
            return

        def _loop() -> None:
            while not self._hb_stop.wait(interval):
                try:
                    self.send("heartbeat", {"strategy_id": self.strategy_id,
                                            "session": self.session_id,
                                            "ts": time.time()})
                except Exception:                        # noqa: BLE001 心跳失败不致命
                    continue

        self._hb_thread = threading.Thread(target=_loop, name="ipc-heartbeat", daemon=True)
        self._hb_thread.start()

    # ---------- 上行 ----------
    def send(self, tag: str, data: dict) -> None:
        payload = {"t": tag, "sid": self.strategy_id, "d": data}
        self._push.send(pack(payload))

    # ---------- 下行 ----------
    def recv_event(self, timeout_ms: int = 1000) -> tuple[str, dict] | None:
        if not self._sub.poll(timeout_ms):
            return None
        parts = self._sub.recv_multipart()
        tag = parts[0].decode("utf-8")
        return tag, unpack(parts[-1])

    def run_loop(self, handler: Callable[[str, dict], bool | None],
                 stop: threading.Event | None = None) -> None:
        """阻塞收事件循环：handler(tag, data) 返回 False 退出；stop 置位退出。"""
        while stop is None or not stop.is_set():
            item = self.recv_event(timeout_ms=100)
            if item is None:
                continue
            if handler(*item) is False:
                return

    def close(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2)
        self._sub.close(0)
        self._push.close(0)
