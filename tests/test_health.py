"""D01 冒烟：health 端点契约（宪法 DoD：{status, version, simulation_only: true}）。"""

from fastapi.testclient import TestClient

from quantdesk.app import app


def test_health_ok() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["simulation_only"] is True  # 默认全局模拟盘（宪法第 2 条）
    assert isinstance(body["version"], str) and body["version"]
