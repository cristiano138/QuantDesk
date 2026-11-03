"""D19：设置服务测试（开关热切 / 持久化 / WS 广播 / 动态读配置路由）。"""

import pytest
from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.config import get_settings, reset_settings_overrides, update_settings
from quantdesk.core.events import EventBus
from quantdesk.engine.riskgate import RiskGate
from quantdesk.services.settings_svc import apply_settings


@pytest.fixture(autouse=True)
def _clean_overrides():
    reset_settings_overrides()
    yield
    reset_settings_overrides()
    get_settings.cache_clear()


def test_read_defaults():
    from quantdesk.services.settings_svc import get_current_settings

    cur = get_current_settings()
    assert cur["simulation_only"] is True           # 宪法默认
    assert cur["broker_channel"] == "paper"


def test_update_persists_and_broadcasts():
    events = []
    bus = EventBus()
    bus.subscribe("settings", lambda e: events.append(e.data))
    out = apply_settings({"simulation_only": False}, bus=bus)
    assert out["simulation_only"] is False
    # 持久化：新读（缓存已失效）仍为 False
    assert get_settings().simulation_only is False
    assert events and events[0]["simulation_only"] is False
    # 切回
    out2 = apply_settings({"simulation_only": True}, bus=bus)
    assert out2["simulation_only"] is True
    assert get_settings().simulation_only is True


def test_invalid_values_rejected():
    with pytest.raises(ValueError):
        apply_settings({"simulation_only": "yes"})
    with pytest.raises(ValueError):
        apply_settings({"commission_rate": -0.1})
    with pytest.raises(ValueError):
        apply_settings({"broker_channel": "magic"})
    with pytest.raises(ValueError):
        apply_settings({"host": "0.0.0.0"})          # 红线字段不可经此修改


def test_riskgate_reads_live_config_each_submit(monkeypatch, tmp_path):
    """闸①动态读配置：切开关即时影响下一单路由（无需重启/重建闸）。"""
    from quantdesk.store.db import init_db

    init_db(tmp_path / "g.db")
    import quantdesk.config as cfg_mod

    gate = RiskGate(paper_broker=None, live_broker=None, settings=None,
                    db_path=tmp_path / "g.db")     # settings=None → 每次读 config
    from quantdesk.core.models import Order

    o = Order(order_id="X1", symbol="600000.SH", side="buy", price=10.0, volume=100)

    class S:
        simulation_only = True

    monkeypatch.setattr(cfg_mod, "get_settings", lambda: S())
    # 模拟开 → paper 缺位 → 「模拟通道未装配」拒单（不是 fail-closed 文案）
    out1 = gate.submit(o)
    assert "模拟通道未装配" in out1.message

    class S2:
        simulation_only = False

    monkeypatch.setattr(cfg_mod, "get_settings", lambda: S2())
    out2 = gate.submit(o)
    assert "fail-closed" in out2.message            # 实盘缺位 → fail-closed


def test_api_settings_roundtrip(monkeypatch):
    reset_settings_overrides()
    with TestClient(app) as client:
        r = client.get("/api/settings")
        assert r.status_code == 200 and r.json()["simulation_only"] is True
        r2 = client.put("/api/settings", json={"simulation_only": False})
        assert r2.status_code == 200 and r2.json()["simulation_only"] is False
        assert client.get("/api/settings").json()["simulation_only"] is False
        assert client.put("/api/settings", json={"commission_rate": -1}).status_code == 400
        # 还原（不留全局开关被改的状态）
        client.put("/api/settings", json={"simulation_only": True})
