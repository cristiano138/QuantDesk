"""D03：SQLite 持久化测试（DoD：init_db 幂等；子进程重读完整；WAL 并发读）。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.models import Order, Position, Trade
from quantdesk.store import repo
from quantdesk.store.db import get_engine, init_db, reset_engine

CHILD_CODE = """
import json
from quantdesk.store import repo
o = repo.get_order("D-TEST-1")
trades = repo.list_trades()
pos = repo.latest_positions()
print(json.dumps({
    "order": None if o is None else o.__dict__,
    "trades": [t.__dict__ for t in trades],
    "positions": [p.__dict__ for p in pos],
}))
"""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """独立数据目录 + 干净引擎缓存。"""
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    from quantdesk.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    db_path = init_db(tmp_path / "quantdesk.db")
    yield db_path
    reset_engine()
    get_settings.cache_clear()


def _sample_order() -> Order:
    return Order(
        order_id="D-TEST-1",
        symbol="600000.SH",
        side=Side.BUY,
        price=12.34,
        volume=100,
        order_type=OrderType.LIMIT,
        strategy_id="manual",
        status=OrderStatus.PENDING_CONFIRM,
        created_at=1730000000000,
        updated_at=1730000000000,
    )


def _sample_trade() -> Trade:
    return Trade(
        trade_id="T-1",
        order_id="D-TEST-1",
        symbol="600000.SH",
        side=Side.BUY,
        price=12.34,
        volume=100,
        amount=1234.0,
        traded_at=1730000001000,
    )


def test_init_db_idempotent(db):
    p1 = init_db(db)  # 重复执行不报错
    p2 = init_db(db)
    assert p1 == p2
    with get_engine(db).connect() as conn:
        from sqlalchemy import text

        version = conn.execute(text("SELECT version FROM schema_version WHERE id=1")).scalar()
    assert version == 1


def test_wal_mode_enabled(db):
    with get_engine(db).connect() as conn:
        from sqlalchemy import text

        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


def test_order_roundtrip(db):
    repo.upsert_order(_sample_order(), db_path=db)
    o = repo.get_order("D-TEST-1", db_path=db)
    assert o is not None
    assert o.symbol == "600000.SH"
    assert o.side is Side.BUY
    assert o.status is OrderStatus.PENDING_CONFIRM  # D17 二次确认状态可表达
    assert o.volume == 100

    o2 = _sample_order()
    o2.status = OrderStatus.FILLED
    o2.filled_volume = 100
    repo.upsert_order(o2, db_path=db)  # upsert 覆盖
    assert repo.get_order("D-TEST-1", db_path=db).status is OrderStatus.FILLED


def test_trades_and_positions(db):
    repo.append_trade(_sample_trade(), db_path=db)
    repo.append_trade(_sample_trade(), db_path=db)  # 幂等：同 trade_id 不重复
    assert len(repo.list_trades(db_path=db)) == 1

    repo.save_positions_snapshot([
        Position(symbol="600000.SH", volume=100, available=0,
                 avg_cost=12.34, market_value=1234.0, strategy_id="manual"),
    ], snapshot_at=1730000002000, db_path=db)
    repo.save_positions_snapshot([
        Position(symbol="600000.SH", volume=200, available=0,
                 avg_cost=12.10, market_value=2420.0, strategy_id="manual"),
        Position(symbol="000001.SZ", volume=300, available=300,
                 avg_cost=11.00, market_value=3300.0, strategy_id="manual"),
    ], snapshot_at=1730000003000, db_path=db)
    pos = repo.latest_positions(db_path=db)
    assert {p.symbol for p in pos} == {"600000.SH", "000001.SZ"}
    assert all(p.volume == 200 or p.volume == 300 for p in pos)
    assert next(p for p in pos if p.symbol == "600000.SH").available == 0  # T+1 语义


def test_subprocess_restart_read(tmp_path, monkeypatch, db):
    """DoD：插入样例 → 新起子进程重读 → 数据完整（重启不丢）。"""
    repo.upsert_order(_sample_order(), db_path=db)
    repo.append_trade(_sample_trade(), db_path=db)
    repo.save_positions_snapshot([
        Position(symbol="600000.SH", volume=100, available=0,
                 avg_cost=12.34, market_value=1234.0, strategy_id="manual"),
    ], snapshot_at=1730000002000, db_path=db)

    env = {**dict(__import__("os").environ), "QUANTDESK_DATA_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_CODE],
        capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)

    assert data["order"] is not None
    assert data["order"]["symbol"] == "600000.SH"
    assert data["order"]["status"] == "pending_confirm"
    assert len(data["trades"]) == 1
    assert data["trades"][0]["trade_id"] == "T-1"
    assert data["positions"][0]["symbol"] == "600000.SH"
    assert data["positions"][0]["available"] == 0


def test_wal_concurrent_read(db):
    """WAL 冒烟：写连接提交后，另一连接立即可读。"""
    import time

    repo.upsert_order(_sample_order(), db_path=db)
    time.sleep(0.05)
    assert repo.get_order("D-TEST-1", db_path=db) is not None
    assert len(repo.list_orders(status=OrderStatus.PENDING_CONFIRM, db_path=db)) == 1


def test_watchlist_and_compliance(db):
    repo.watchlist_add("600000.SH", name="浦发银行", db_path=db)
    repo.watchlist_add("600000.SH", name="浦发银行", db_path=db)  # 去重
    assert len(repo.watchlist_list(db_path=db)) == 1
    assert repo.watchlist_remove("600000.SH", db_path=db) is True
    assert repo.watchlist_remove("600000.SH", db_path=db) is False

    repo.compliance_upsert("已向券商报备程序化交易", category="报备", checked=False, db_path=db)
    repo.compliance_upsert("已向券商报备程序化交易", category="报备", checked=True, db_path=db)
    rows = repo.compliance_list(db_path=db)
    assert len(rows) == 1 and rows[0].checked is True


def test_backtest_run_lifecycle(db):
    run_id = repo.backtest_create("dual_ma", "600000.SH", "1d", "2020-01-01", "2026-11-01",
                                  params_json='{"fast":5,"slow":20}', db_path=db)
    assert run_id > 0
    row = repo.backtest_get(run_id, db_path=db)
    assert row.status == "running"
    repo.backtest_finish(run_id, metrics_json='{"total_return":0.12}', db_path=db)
    row = repo.backtest_get(run_id, db_path=db)
    assert row.status == "completed" and row.finished_at is not None


def test_default_data_dir_never_points_at_the_workspace():
    """看门狗：默认数据目录必须落在临时区，不在工作区里。

    这条不测任何业务，只钉住一轮真实事故——测试夹具漏隔离时，合成 LOG 事件、
    paper 账户与假日志会写进开发库的 `data/quantdesk.db`（本机实测多出 181 行测试
    痕迹才被发现）。conftest 的全局隔离被删掉或有人新增一条绕过它的路径时，
    这条先红，而不是等用户打开日志页看见一堆假数据。
    """
    from quantdesk.store.db import default_db_path

    workspace = Path(__file__).resolve().parents[1]
    p = default_db_path().resolve()
    assert workspace not in p.parents, f"默认库写进了工作区：{p}（应被 tests/conftest.py 隔离）"
    parquet_root = (workspace / "data" / "parquet").resolve()
    assert p != (workspace / "data" / "quantdesk.db").resolve()
    assert not str(p).startswith(str(parquet_root.parent)), "Parquet/库根目录都必须在临时区"
