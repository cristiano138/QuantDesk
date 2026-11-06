"""D17：二次确认流测试（第③道闸，服务端强制）。

DoD：确认请求后 kill 主进程 → 重启后待确认单仍在且可处理；超时作废；断线拒单。
"""

from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.errors import ConnectionLostError
from quantdesk.core.events import EventBus
from quantdesk.core.models import Account, Order, Order as O, Position, Trade
from quantdesk.engine.riskgate import RiskGate
from quantdesk.services.trading import TradingService
from quantdesk.store.db import init_db
from quantdesk.store.repo import get_order


class MockLiveBroker(BrokerAdapter):
    """mock 实盘通道：记录投递，可切换断线。"""

    name = "mock_live"
    is_paper = False

    def __init__(self):
        self.connected = False
        self.placed: list[Order] = []
        self.fail_with_disconnect = False

    def connect(self, **credentials):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def health_check(self):
        return self.connected

    def place_order(self, order: Order) -> Order:
        if self.fail_with_disconnect or not self.connected:
            raise ConnectionLostError("mock 通道断线")
        self.placed.append(order)
        return replace(order, status=OrderStatus.FILLED,
                       filled_volume=order.volume,
                       avg_filled_price=order.price or 10.0,
                       broker_order_id="BRK-1")

    def cancel_order(self, order_id):
        return False

    def get_position(self, symbol=None):
        return []

    def get_account(self):
        return Account(total_asset=100_000.0, cash=100_000.0, market_value=0.0)

    def get_orders(self, status=None):
        return []

    def get_trades(self, start=None):
        return []


class StubSettings:
    def __init__(self, simulation_only=False):
        self.simulation_only = simulation_only


def mk_order(oid="C1", price=10.0, volume=100):
    return Order(order_id=oid, symbol="600000.SH", side=Side.BUY, price=price,
                 volume=volume, order_type="limit", status="pending_confirm")


def _mk_prev_close_store(tmp_path):
    """昨收 10.00 → 主板涨跌停 [9.00, 11.00]（限价 10.0 可过闸②）。"""
    import pandas as pd

    from quantdesk.core.enums import AdjustType, Frequency
    from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore

    store = ParquetStore(root=tmp_path / "parquet")
    row = [{"dt": pd.Timestamp("2026-11-03"), "open": 10.0, "high": 10.0,
            "low": 10.0, "close": 10.0, "volume": 10000.0, "amount": 100000.0}]
    store.write("600000.SH", Frequency.D1, pd.DataFrame(row)[HISTORY_COLUMNS],
                AdjustType.NONE)
    return store


@pytest.fixture()
def env(tmp_path):
    init_db(tmp_path / "c.db")
    events: list = []
    bus = EventBus()
    bus.subscribe("risk", lambda e: events.append(e))
    live = MockLiveBroker()
    live.connect()
    gate = RiskGate(live_broker=live, settings=StubSettings(False),
                    db_path=tmp_path / "c.db", event_bus=bus,
                    store=_mk_prev_close_store(tmp_path),
                    confirm_timeout_s=0.0)        # 默认关超时线程（专项测试单独开）
    return {"tmp": tmp_path, "live": live, "gate": gate, "events": events, "bus": bus}


def test_submit_pends_and_persists(env):
    """sim OFF + live 在线 → PENDING_CONFIRM + 落库 + confirm_request 事件。"""
    gate = env["gate"]
    out = gate.submit(mk_order("C1"))
    assert out.status == OrderStatus.PENDING_CONFIRM
    row = get_order("C1", db_path=env["tmp"] / "c.db")
    assert row.status == "pending_confirm"
    assert any(e.data.get("action") == "confirm_request" for e in env["events"])


def test_confirm_routes_to_live(env):
    gate = env["gate"]
    gate.submit(mk_order("C2"))
    out = gate.confirm("C2")
    assert out.status == OrderStatus.FILLED
    assert out.broker_order_id == "BRK-1"
    assert len(env["live"].placed) == 1
    assert get_order("C2", db_path=env["tmp"] / "c.db").status == "filled"


def test_reject_marks_rejected(env):
    gate = env["gate"]
    gate.submit(mk_order("C3"))
    out = gate.reject("C3")
    assert out.status == OrderStatus.REJECTED
    assert "用户拒绝" in out.message
    assert env["live"].placed == []               # 未投递
    with pytest.raises(ValueError):
        gate.confirm("C3")                        # 已出队


def test_timeout_auto_expires(tmp_path):
    """超时 → EXPIRED_CONFIRM 自动作废 + 落库。"""
    init_db(tmp_path / "t.db")
    events: list = []
    bus = EventBus()
    bus.subscribe("risk", lambda e: events.append(e))
    live = MockLiveBroker()
    live.connect()
    gate = RiskGate(live_broker=live, settings=StubSettings(False),
                    db_path=tmp_path / "t.db", event_bus=bus,
                    store=_mk_prev_close_store(tmp_path), confirm_timeout_s=0.5)
    gate.submit(mk_order("T1"))
    assert get_order("T1", db_path=tmp_path / "t.db").status == "pending_confirm"
    import time

    time.sleep(1.2)                                # > 0.5s 超时窗口
    assert get_order("T1", db_path=tmp_path / "t.db").status == "expired_confirm"
    assert any(e.data.get("action") == "expired" for e in events)
    with pytest.raises(ValueError):
        gate.confirm("T1")
    assert live.placed == []


def test_restart_pending_survives_but_requires_new_channel_confirmation(tmp_path):
    """待确认记录重启不丢；缺少原通道身份时不能把旧确认投递到新连接。"""
    init_db(tmp_path / "r.db")
    live1 = MockLiveBroker()
    live1.connect()
    gate1 = RiskGate(live_broker=live1, settings=StubSettings(False),
                     db_path=tmp_path / "r.db",
                     store=_mk_prev_close_store(tmp_path), confirm_timeout_s=0.0)
    gate1.submit(mk_order("R1"))
    gate1.submit(mk_order("R2"))
    # —— 进程死亡（gate1/live1 随之消亡，只剩 SQLite）——
    # —— 重启：新 gate + 新 live，从库恢复 ——
    live2 = MockLiveBroker()
    live2.connect()
    gate2 = RiskGate(live_broker=live2, settings=StubSettings(False),
                     db_path=tmp_path / "r.db",
                     store=_mk_prev_close_store(tmp_path), confirm_timeout_s=0.0)
    n = gate2.restore_pending()
    assert n == 2                                  # 待确认单仍在
    out = gate2.confirm("R1")                      # 且可处理
    assert out.status == OrderStatus.FAILED
    assert live2.placed == []
    assert "重新提交" in out.message
    assert get_order("R2", db_path=tmp_path / "r.db").status == "pending_confirm"


def test_confirm_after_switching_to_paper_never_calls_live(env):
    gate = env["gate"]
    gate.submit(mk_order("SWITCH"))
    gate.settings.simulation_only = True
    assert gate.confirm("SWITCH").status == OrderStatus.REJECTED
    assert env["live"].placed == []


def test_confirm_rechecks_available_cash(env, monkeypatch):
    gate = env["gate"]
    gate.submit(mk_order("CASH"))
    monkeypatch.setattr(env["live"], "get_account", lambda: Account(0, 0, 0))
    out = gate.confirm("CASH")
    assert out.status == OrderStatus.REJECTED and "资金不足" in out.message
    assert env["live"].placed == []


def test_confirm_never_switches_to_another_broker(env):
    gate = env["gate"]
    gate.submit(mk_order("CHANNEL"))
    other = MockLiveBroker()
    other.connect()
    gate.live = other
    out = gate.confirm("CHANNEL")
    assert out.status == OrderStatus.FAILED
    assert env["live"].placed == other.placed == []


def test_confirm_checks_expiry_even_if_background_worker_is_stopped(env):
    import time

    gate = env["gate"]
    gate.confirm_timeout_s = 10
    gate.submit(mk_order("LATE"))
    gate._armed_at["LATE"] = time.monotonic() - 11
    out = gate.confirm("LATE")
    assert out.status == OrderStatus.EXPIRED_CONFIRM
    assert env["live"].placed == []


def test_restore_pending_does_not_extend_confirmation_window(env):
    gate = env["gate"]
    gate.submit(mk_order("RESTORE"))
    armed = gate._armed_at["RESTORE"]
    gate.restore_pending()
    assert gate._armed_at["RESTORE"] == armed


def test_disconnected_fail_closed_reject(env):
    """断线状态提交 → 直接拒单（不排队不重试）。"""
    gate = env["gate"]
    env["live"].disconnect()
    out = gate.submit(mk_order("D1"))
    assert out.status == OrderStatus.REJECTED
    assert "fail-closed" in out.message
    assert env["live"].placed == []
    env["live"].connect()                          # 恢复后可正常走确认流
    out2 = gate.submit(mk_order("D2"))
    assert out2.status == OrderStatus.PENDING_CONFIRM


def test_confirm_while_disconnected_fails_closed(env):
    gate = env["gate"]
    gate.submit(mk_order("F1"))
    env["live"].disconnect()                       # 确认前断线
    out = gate.confirm("F1")
    assert out.status == OrderStatus.FAILED
    assert "fail-closed" in out.message
    assert env["live"].placed == []
    # 提交中断线（place_order 抛 ConnectionLostError）→ FAILED
    env["live"].fail_with_disconnect = True
    env["live"].connect()                          # health 探活恢复，但 place_order 抛断线
    gate.submit(mk_order("F2"))
    out2 = gate.confirm("F2")
    assert out2.status == OrderStatus.FAILED
    assert "断线" in out2.message


# ---------- API 层 ----------

def test_api_confirm_reject_endpoints(tmp_path, monkeypatch):
    init_db(tmp_path / "api.db")
    live = MockLiveBroker()
    live.connect()
    svc = TradingService(initial_cash=1_000_000.0, db_path=tmp_path / "api.db",
                         settings=StubSettings(False))
    svc.gate.live = live                            # 换上 mock 实盘
    svc.gate.store = _mk_prev_close_store(tmp_path)  # 闸②涨跌停校验数据
    from quantdesk.services import trading as mod

    monkeypatch.setattr(mod, "_service", svc)
    with TestClient(app) as client:
        r = client.post("/api/trading/orders", json={
            "symbol": "600000.SH", "side": "buy", "volume": 100, "price": 10.0})
        body = r.json()
        assert body["status"] == "pending_confirm"  # 实盘路径进入确认流
        oid = body["order_id"]
        rc = client.post(f"/api/trading/orders/{oid}/confirm")
        assert rc.status_code == 200 and rc.json()["status"] == "filled"
        # 拒绝路径
        r2 = client.post("/api/trading/orders", json={
            "symbol": "600000.SH", "side": "buy", "volume": 100, "price": 10.0})
        oid2 = r2.json()["order_id"]
        rr = client.post(f"/api/trading/orders/{oid2}/reject")
        assert rr.status_code == 200 and rr.json()["status"] == "rejected"
        # 重复确认 → 409
        assert client.post(f"/api/trading/orders/{oid}/confirm").status_code == 409
        assert client.post("/api/trading/orders/NOPE/confirm").status_code == 409


from quantdesk.app import app  # noqa: E402  — 置底避免与 fixture 环境相互影响
