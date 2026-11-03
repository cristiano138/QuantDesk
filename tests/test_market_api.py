"""D18：行情服务与 API 测试（Parquet 直读不外呼 / 快照节流 / 自选股）。"""

import time
from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.events import EventBus
from quantdesk.core.models import Tick
from quantdesk.services.market_data import MarketDataService
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore

SYM = "600000.SH"


@pytest.fixture()
def store(tmp_path):
    s = ParquetStore(root=tmp_path / "p")
    dts = pd.bdate_range("2020-01-01", periods=60)
    df = pd.DataFrame({
        "dt": dts,
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2,
        "volume": 1000.0, "amount": 10200.0,
    })[HISTORY_COLUMNS]
    s.write(SYM, Frequency.D1, df, AdjustType.FORWARD)
    s.write(SYM, Frequency.D1, df, AdjustType.NONE)
    return s


def test_history_reads_parquet_only(store):
    """历史读取走本地 Parquet（切周期不外呼——无任何网络适配器参与）。"""
    svc = MarketDataService(store=store, bus=None)   # 不接任何 feed
    df = svc.history(SYM, "1d", start="2020-01-01")
    assert len(df) == 60
    # 周期可用性
    assert "1d" in svc.freqs_available(SYM)
    assert "1m" not in svc.freqs_available(SYM)


def test_on_tick_throttled_broadcast(store):
    """tick 广播节流 ≈4/s（250ms 窗口合并取最新）。"""
    published: list = []
    bus = EventBus()
    bus.subscribe("tick", lambda e: published.append(e.data))
    svc = MarketDataService(store=store, bus=bus)
    for i in range(20):
        svc.on_tick(Tick(symbol=SYM, dt=datetime(2026, 11, 4, 9, 30, i), last=10 + i,
                         open=10, high=10, low=10, volume=1, amount=1,
                         bids=[(9.9, 100)], asks=[(10.1, 100)]))
        time.sleep(0.03)
    # 20 次 on_tick（总时长 ~600ms）→ 节流后发布数 2~4（≈4/s 上限）
    assert 2 <= len(published) <= 4
    # 窗口外的 tick 必然发布：静默期后补一条
    time.sleep(0.3)
    svc.on_tick(Tick(symbol=SYM, dt=datetime(2026, 11, 4, 9, 31), last=99,
                     open=10, high=10, low=10, volume=1, amount=1,
                     bids=[(9.9, 100)], asks=[(10.1, 100)]))
    assert published[-1]["last"] == 99                # 合并保留最新
    # 快照始终是最新值
    snap = svc.snapshot(SYM)
    assert snap["last"] == 99


def test_watchlist_crud(store, tmp_path):
    from quantdesk.store.db import init_db

    init_db(tmp_path / "w.db")
    svc = MarketDataService(store=store, bus=None, db_path=tmp_path / "w.db")
    svc.watchlist_add("000001.SZ", name="平安银行")
    svc.watchlist_add("600000.SH")
    items = svc.watchlist()
    assert {w["symbol"] for w in items} >= {"000001.SZ", "600000.SH"}
    assert svc.watchlist_remove("000001.SZ") is True
    assert svc.watchlist_remove("000001.SZ") is False


def test_api_market_endpoints(store, tmp_path, monkeypatch):
    from quantdesk.services import market_data as mod
    from quantdesk.store.db import init_db

    init_db(tmp_path / "w.db")
    monkeypatch.setattr(mod, "_service",
                        MarketDataService(store=store, bus=None, db_path=tmp_path / "w.db"))
    with TestClient(app) as client:
        r = client.get("/api/market/history", params={"symbol": SYM, "freq": "1d"})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 60 and rows[0]["dt"].startswith("2020-01")
        assert client.get("/api/market/freqs", params={"symbol": SYM}).json() == ["1d"]
        assert client.get("/api/market/snapshot", params={"symbol": SYM}).json() in ({}, None)
        # 自选股
        client.post("/api/market/watchlist", json={"symbol": "000001.SZ", "name": "平安银行"})
        assert client.get("/api/market/watchlist").json()[0]["symbol"] == "000001.SZ"
        assert client.delete("/api/market/watchlist/000001.SZ").status_code == 200
        assert client.delete("/api/market/watchlist/000001.SZ").status_code == 404
        assert client.get("/api/market/history",
                          params={"symbol": "999999.SH"}).json() == []
