"""D08：回测指标 / 策略装载 / 回测 API 测试。"""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.core.enums import Side
from quantdesk.core.models import Trade
from quantdesk.services.backtest_svc import (
    BacktestService,
    available_strategies,
    compute_metrics,
    equity_to_json,
    load_strategy,
)
from tests.test_backtest import synth_df, write_store, SYMBOL


# ---------- 指标 ----------

def _equity(values) -> pd.DataFrame:
    return pd.DataFrame({
        "dt": pd.bdate_range("2020-01-01", periods=len(values)),
        "equity": values, "cash": values, "market_value": 0.0,
    })


def test_metrics_flat_equity():
    eq = _equity([100.0] * 10)
    m = compute_metrics(eq, [])
    assert m["total_return"] == 0.0
    assert m["annual_return"] == 0.0
    assert m["max_drawdown"] == 0.0
    assert m["sharpe"] == 0.0        # std=0
    assert m["trade_count"] == 0


def test_metrics_known_values():
    # 100 → 110，单调上涨 11 个点：总收益 10%，无回撤
    eq = _equity([100 + i for i in range(11)])
    m = compute_metrics(eq, [])
    assert m["total_return"] == pytest.approx(0.10, abs=1e-6)
    assert m["max_drawdown"] == 0.0
    assert m["annual_return"] > 0
    # 年化 = 1.1^(252/10) - 1
    assert m["annual_return"] == pytest.approx(1.1 ** 25.2 - 1, rel=1e-4)


def test_metrics_drawdown():
    eq = _equity([100, 120, 90, 95])          # 峰值 120 → 谷 90：回撤 25%
    m = compute_metrics(eq, [])
    assert m["max_drawdown"] == pytest.approx(0.25, abs=1e-6)


def test_metrics_round_trips_win_loss():
    fee_day = 0
    trades = [
        # 买入 1000@10（amount 10000），卖出 1000@11 → 盈利（含费后仍为正）
        Trade(trade_id="t1", order_id="o1", symbol=SYMBOL, side=Side.BUY,
              price=10.0, volume=1000, amount=10000.0, traded_at=1),
        Trade(trade_id="t2", order_id="o2", symbol=SYMBOL, side=Side.SELL,
              price=11.0, volume=1000, amount=11000.0, traded_at=2),
        # 买入 1000@10，卖出 1000@9.5 → 亏损
        Trade(trade_id="t3", order_id="o3", symbol=SYMBOL, side=Side.BUY,
              price=10.0, volume=1000, amount=10000.0, traded_at=3),
        Trade(trade_id="t4", order_id="o4", symbol=SYMBOL, side=Side.SELL,
              price=9.5, volume=1000, amount=9500.0, traded_at=4),
    ]
    m = compute_metrics(_equity([100.0, 101.0]), trades)
    assert m["trade_count"] == 4
    assert m["win_rate"] == pytest.approx(0.5)
    # 盈亏比 = 平均盈利 / 平均亏损绝对值：盈利≈900+，亏损≈-550+（费用同量级）
    assert m["profit_loss_ratio"] is not None and m["profit_loss_ratio"] > 1.0


def test_metrics_turnover():
    trades = [Trade(trade_id="t1", order_id="o1", symbol=SYMBOL, side=Side.BUY,
                    price=10.0, volume=1000, amount=10000.0, traded_at=1)]
    eq = _equity([100.0] * 252)               # 一年，平均净值 100
    m = compute_metrics(eq, trades)
    assert m["turnover"] == pytest.approx(10000.0 / 100.0 / 1.0, rel=1e-3)


def test_equity_json_serializable():
    eq = _equity([100.0, 101.5, 99.0])
    j = equity_to_json(eq)
    assert len(j) == 3
    assert set(j[0]) == {"dt", "equity", "cash", "market_value"}
    assert j[0]["dt"].startswith("2020-01")


# ---------- 策略装载 ----------

def test_available_strategies_contains_examples():
    ss = available_strategies()
    names = {s["name"] for s in ss}
    assert {"dual_ma", "grid"} <= names
    for s in ss:
        assert isinstance(s["params"], dict)


def test_load_strategy_and_reject_bad_names():
    cls = load_strategy("dual_ma")
    assert cls.name == "dual_ma"
    strat = cls(params={"fast": 3})
    assert strat.params["fast"] == 3 and strat.params["slow"] == 20  # 默认合并
    with pytest.raises(ValueError):
        load_strategy("../evil")
    with pytest.raises(ValueError):
        load_strategy("os")
    with pytest.raises(ValueError):
        load_strategy("not_exist")


# ---------- 服务与 API ----------

def test_service_run_dual_ma_synthetic(tmp_path):
    """dual_ma 在合成数据上完整跑通：指标完整、equity 非空、落库可查。"""
    from quantdesk.store.db import init_db
    init_db(tmp_path / "test.db")
    store = write_store(tmp_path, synth_df(300))
    svc = BacktestService(store=store, db_path=tmp_path / "test.db")
    out = svc.run(strategy="dual_ma", symbols=[SYMBOL], start="2020-01-01",
                  params={"volume": 800})
    assert out["bars"] == 300
    assert set(out["metrics"]) >= {"total_return", "annual_return", "max_drawdown",
                                   "sharpe", "win_rate", "turnover", "trade_count"}
    assert len(out["equity"]) == 300
    # 落库可查
    got = svc.get(out["run_id"])
    assert got is not None and got["status"] == "completed"
    assert got["metrics"]["trade_count"] == out["metrics"]["trade_count"]


def test_service_run_no_data_raises(tmp_path):
    from quantdesk.store.parquet_store import ParquetStore
    svc = BacktestService(store=ParquetStore(root=tmp_path / "p"),
                          db_path=tmp_path / "t.db")
    with pytest.raises(ValueError, match="本地无数据"):
        svc.run(strategy="dual_ma", symbols=["000000.SH"])


def test_api_backtest_roundtrip(tmp_path, monkeypatch):
    """API 下发回测：同步模式直接返回结果；异步模式返回 run_id 并轮询到完成。"""
    from quantdesk.store.db import init_db
    init_db(tmp_path / "api.db")
    store = write_store(tmp_path, synth_df(120))
    svc = BacktestService(store=store, db_path=tmp_path / "api.db")
    from quantdesk.services import backtest_svc as mod
    monkeypatch.setattr(mod, "_service", svc)
    with TestClient(app) as client:
        r = client.get("/api/backtest/strategies")
        assert r.status_code == 200
        assert {s["name"] for s in r.json()} >= {"dual_ma", "grid"}

        body = {"strategy": "dual_ma", "symbols": [SYMBOL], "params": {"volume": 500}}
        r2 = client.post("/api/backtest", json=body | {"async_run": False})
        assert r2.status_code == 200, r2.text
        out = r2.json()
        assert out["bars"] == 120
        run_id = out["run_id"]

        r3 = client.get(f"/api/backtest/{run_id}")
        assert r3.status_code == 200
        assert r3.json()["metrics"] == out["metrics"]

        # 异步：立即返回 run_id，轮询到 completed（进度可见是这条改造的目的）
        r5 = client.post("/api/backtest", json=body)
        assert r5.status_code == 200 and r5.json()["async"] is True
        async_id = r5.json()["run_id"]
        for _ in range(200):
            state = client.get(f"/api/backtest/{async_id}").json()
            if state["status"] in ("completed", "failed"):
                break
        assert state["status"] == "completed", state
        assert state["metrics"] == out["metrics"]
        assert client.get(f"/api/backtest/{async_id}/progress").json()["percent"] == 100.0
        assert client.get("/api/backtest").json()          # 运行清单

        assert client.get("/api/backtest/99999").status_code == 404
        r4 = client.post("/api/backtest",
                         json={"strategy": "nope", "symbols": [SYMBOL]})
        assert r4.status_code == 400
