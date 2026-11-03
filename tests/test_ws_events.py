"""D10：/ws/events 统一事件流（订阅 SYSTEM_BUS → JSON 下行 + 心跳）。"""

import json

import pytest
from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.core.events import Event, EventType, SYSTEM_BUS


def test_ws_receives_published_events():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as ws:
            SYSTEM_BUS.publish(Event(EventType.LOG, data={"message": "hello-ws"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "log"
            assert msg["data"]["message"] == "hello-ws"
            # dataclass 事件（Order 等）可序列化
            from quantdesk.core.models import Order

            o = Order(order_id="X1", symbol="600000.SH", side="buy", price=10.0, volume=100)
            SYSTEM_BUS.publish(Event(EventType.ORDER, data=o))
            msg2 = json.loads(ws.receive_text())
            assert msg2["type"] == "order"
            assert msg2["data"]["order_id"] == "X1"


def test_ws_dataclass_list_and_dict_data():
    from quantdesk.core.models import Position

    with TestClient(app) as client:
        with client.websocket_connect("/ws/events") as ws:
            pos = Position(symbol="600000.SH", volume=100, available=0,
                           avg_cost=10.0, market_value=1000.0)
            SYSTEM_BUS.publish(Event(EventType.POSITION, data=[pos]))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "position"
            assert msg["data"][0]["symbol"] == "600000.SH"
