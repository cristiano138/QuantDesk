"""回归：无行情快照返回 null（空对象曾导致前端整个 React 应用崩溃卸载）。"""

from datetime import datetime

from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.models import Tick
from quantdesk.services.market_data import MarketDataService
from quantdesk.store.parquet_store import ParquetStore


def test_snapshot_returns_null_when_no_tick(tmp_path, monkeypatch):
    svc = MarketDataService(store=ParquetStore(root=tmp_path / "p"), bus=None)
    from quantdesk.services import market_data as mod
    monkeypatch.setattr(mod, "_service", svc)
    with TestClient(app) as client:
        r = client.get("/api/market/snapshot", params={"symbol": "600000.SH"})
        assert r.status_code == 200
        assert r.json() is None                    # 不是 {} —— 前端可判空
        # 有行情 → 完整快照
        svc.on_tick(Tick(symbol="600000.SH", dt=datetime(2026, 11, 4, 9, 31),
                         last=10.0, open=10.0, high=10.0, low=10.0,
                         volume=1, amount=1, bids=[(9.9, 1)], asks=[(10.1, 1)]))
        r2 = client.get("/api/market/snapshot", params={"symbol": "600000.SH"})
        assert r2.json()["last"] == 10.0
