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
    """通道面板结构：按注册表适配器名上报，含就绪状态/前置条件/能力声明。

    旧口径按「文件名」列表再与注册名比对（akshare_feed vs akshare），永远判红——
    现统一以注册表元数据为唯一事实源。
    """
    reset_registry_for_test()
    with TestClient(app) as client:
        r = client.get("/api/system/adapters")
        assert r.status_code == 200
        body = r.json()
        assert body["simulation_only"] is True
        rows = {c["name"]: c for c in body["channels"]}
        assert {"akshare", "tushare", "paper", "federated"} <= set(rows)
        for name, row in rows.items():
            assert row["status"] in ("ready", "unready", "disabled")
            assert row["kind"] in ("data", "broker")
            assert row["label"] and "capabilities" in row and "prereq" in row
        assert rows["paper"]["is_paper"] is True
        assert rows["akshare"]["status"] == "ready"      # akshare 在本机为已安装依赖
        assert body["priority"]["realtime"]              # 通道路由优先级可见


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
