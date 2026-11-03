"""统一事件流 WebSocket（宪法 API 层：/ws/events 统一事件流）。

- 订阅进程级 SYSTEM_BUS，把服务层事件（ORDER/TRADE/POSITION/ACCOUNT/LOG/RISK…）
  以 JSON 下行到前端；15s 无事件发心跳保活；
- 回调线程只入队（asyncio.Queue），由事件循环转发——符合宪法回调约定。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from quantdesk.core.events import Event, EventType, SYSTEM_BUS

router = APIRouter()

_QUEUE_MAX = 1000
_HEARTBEAT_S = 15.0


def _serialize(e: Event) -> str:
    t = e.type.value if isinstance(e.type, EventType) else str(e.type)
    payload: dict[str, Any] = {
        "type": t,
        "ts": e.ts,
        "source": e.source,
        "data": _jsonable(e.data),
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _jsonable(data: Any) -> Any:
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        return dataclasses.asdict(data)
    if isinstance(data, list):
        return [_jsonable(x) for x in data]
    if isinstance(data, dict):
        return {str(k): _jsonable(v) for k, v in data.items()}
    if isinstance(data, (str, int, float, bool)) or data is None:
        return data
    return str(data)          # 兜底：未知类型转字符串（不炸下行通道）


@router.websocket("/ws/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAX)
    dropped = 0

    def _on_event(e: Event) -> None:
        nonlocal dropped
        try:
            q.put_nowait(e)
        except asyncio.QueueFull:
            dropped += 1       # 前端消费过慢：丢弃最旧无法 O(1)，直接丢弃新事件并计数

    sub_id = SYSTEM_BUS.subscribe(None, _on_event)
    try:
        while True:
            try:
                e = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_S)
                await ws.send_text(_serialize(e))
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "heartbeat", "ts": 0}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass                    # 客户端断开/发送失败：清理后退出
    finally:
        SYSTEM_BUS.unsubscribe(sub_id)
