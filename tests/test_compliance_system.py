"""D28：合规报备 + 系统运维 API 测试。"""

from fastapi.testclient import TestClient

from quantdesk.adapters import list_adapters, reset_registry_for_test
from quantdesk.app import app
from quantdesk.config import data_dir
from quantdesk.store.db import init_db


def test_compliance_template_and_toggle(tmp_path, monkeypatch):
    """首次访问预置模板 → 勾选状态可更新 → 导出文本含勾选标记。"""
    db = tmp_path / "c.db"
    init_db(db)
    import quantdesk.api.routes_compliance as mod

    monkeypatch.setattr(mod, "DB_PATH", db)
    with TestClient(app) as client:
        r = client.get("/api/compliance")
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 6
        assert all("item" in i and "checked" in i for i in items)
        # 勾选第一项
        first = items[0]
        r2 = client.put("/api/compliance", json={
            "item": first["item"], "category": first["category"],
            "checked": True, "note": "已核对"})
        assert r2.status_code == 200
        assert r2.json()[0]["checked"] is True
        # 导出
        r3 = client.get("/api/compliance/export")
        assert r3.status_code == 200
        text = r3.content.decode("utf-8")
        assert "程序化交易报备清单" in text
        assert "[x]" in text and first["item"] in text
        assert "QuantDesk v" in text


def test_compliance_validation(tmp_path, monkeypatch):
    db = tmp_path / "c2.db"
    init_db(db)
    import quantdesk.api.routes_compliance as mod

    monkeypatch.setattr(mod, "DB_PATH", db)
    with TestClient(app) as client:
        assert client.put("/api/compliance", json={"item": ""}).status_code == 400


def test_adapters_panel_shape():
    """健康面板结构：注册表中存在的适配器 status=ok，未接入的 unavailable。"""
    reset_registry_for_test()
    with TestClient(app) as client:
        r = client.get("/api/system/adapters")
        assert r.status_code == 200
        body = r.json()
        assert body["simulation_only"] is True
        names = {f["name"] for f in body["feeds"]} | {b["name"] for b in body["brokers"]}
        assert names >= {"akshare_feed", "tushare_feed", "paper_broker"}
        for f in body["feeds"]:
            assert f["status"] in ("ok", "unavailable")
        registered = set(list_adapters())
        for f in body["feeds"]:
            assert f["registered"] == (f["name"] in registered)


def test_logs_listing(tmp_path, monkeypatch):
    """日志清单：按 mtime 倒序列出 data/logs 下的 log/csv。"""
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    probe = d / "crash_test_probe.log"
    probe.write_text("test", encoding="utf-8")
    try:
        with TestClient(app) as client:
            r = client.get("/api/system/logs")
            assert r.status_code == 200
            items = r.json()
            assert any(i["file"] == "crash_test_probe.log" and i["kind"] == "crash"
                       for i in items)
            # 倒序
            mtimes = [i["mtime"] for i in items]
            assert mtimes == sorted(mtimes, reverse=True)
    finally:
        probe.unlink(missing_ok=True)
