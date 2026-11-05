"""对账服务单测：券商快照 vs 本地库的差异检出（差异即事故）。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Tick, Trade
from quantdesk.services.reconcile import ReconcileService
from quantdesk.store import repo
from quantdesk.store.db import init_db, reset_engine


@pytest.fixture
def db(tmp_path):
    reset_engine()
    path = init_db(tmp_path / "reconcile.db")
    yield path
    reset_engine()


def _order(oid="OD-1", status=OrderStatus.SUBMITTED, filled=0, broker_id="B1") -> Order:
    return Order(order_id=oid, broker_order_id=broker_id, symbol="600000.SH",
                 side=Side.BUY, price=10.0, volume=100, status=status,
                 filled_volume=filled, strategy_id="manual")


def _trade(tid="T-1", price=10.0, volume=100, order_id="OD-1") -> Trade:
    return Trade(trade_id=tid, order_id=order_id, symbol="600000.SH", side=Side.BUY,
                 price=price, volume=volume, amount=price * volume,
                 traded_at=int(datetime.now().timestamp() * 1000), broker_trade_id="BT-1")


class StubLive:
    """带 snapshot_for_reconcile 的实盘通道桩。"""

    name = "qmt"
    is_paper = False

    def __init__(self, orders=(), trades=(), positions=(), account=None) -> None:
        self.orders = list(orders)
        self.trades = list(trades)
        self.positions = list(positions)
        self.account = account or Account(total_asset=100_000, cash=90_000, market_value=10_000)

    def snapshot_for_reconcile(self):
        return self.orders, self.trades, self.positions, self.account

    def get_account(self) -> Account:
        return self.account


class PollOnlyBroker:
    """只实现 BrokerAdapter ABC 的通道（没有专用快照接口，走逐项查询 fallback）。"""

    name = "easytrader"
    is_paper = False

    def __init__(self, orders=(), trades=(), positions=(), account=None) -> None:
        self.orders = list(orders)
        self.trades = list(trades)
        self.positions = list(positions)
        self.account = account or Account(total_asset=100_000, cash=90_000,
                                          market_value=10_000)
        self.queries = 0

    def get_orders(self, status=None):
        self.queries += 1
        return self.orders

    def get_trades(self, start=None):
        return self.trades

    def get_position(self, symbol=None):
        return self.positions

    def get_account(self) -> Account:
        return self.account


def _svc(broker, db, bus=None, sim=False):
    return ReconcileService(live_broker=broker, db_path=db, bus=bus or EventBus(),
                            settings=SimpleNamespace(simulation_only=sim,
                                                     reconcile_interval_s=30.0))


def test_clean_run_records_a_no_diff_round(db):
    broker = StubLive(orders=[_order()])
    repo.upsert_order(_order(), db_path=db)
    svc = _svc(broker, db)
    res = svc.run_once()
    assert res["diffs"] == 0
    assert svc.recent_diffs()[0]["kind"] == "clean"       # 无差异也留审计痕迹
    assert repo.reconcile_last_run(db_path=db)["diffs"] == 0


def test_order_present_at_broker_but_missing_locally(db):
    svc = _svc(StubLive(orders=[_order()]), db)
    res = svc.run_once()
    assert res["by_kind"]["order_missing_local"] == 1
    d = svc.recent_diffs()[0]
    assert d["kind"] == "order_missing_local" and d["ref"] == "OD-1"


def test_state_drift_local_open_but_broker_filled_is_reported(db):
    repo.upsert_order(_order(status=OrderStatus.SUBMITTED), db_path=db)
    broker = StubLive(orders=[_order(status=OrderStatus.FILLED, filled=100)])
    svc = _svc(broker, db)
    res = svc.run_once()
    assert res["by_kind"]["order_state_mismatch"] == 1
    assert svc.recent_diffs()[0]["detail"]["broker"] == "filled"


def test_trade_at_broker_not_in_local_db_is_reported(db):
    svc = _svc(StubLive(trades=[_trade()]), db)
    res = svc.run_once()
    assert res["by_kind"]["trade_missing_local"] == 1


def test_local_trade_matched_by_broker_trade_id_is_not_reported(db):
    repo.append_trade(_trade(tid="LOCAL-9", price=10.0), db_path=db)
    svc = _svc(StubLive(trades=[_trade(tid="BROKER-9")]), db)
    assert svc.run_once()["diffs"] == 0


def test_position_volume_and_available_mismatch(db):
    repo.save_positions_snapshot([Position(symbol="600000.SH", volume=100, available=0,
                                           avg_cost=10.0, market_value=1000.0)], db_path=db)
    broker = StubLive(positions=[Position(symbol="600000.SH", volume=100, available=100,
                                          avg_cost=10.0, market_value=1000.0)],
                      account=Account(total_asset=100_000, cash=99_000, market_value=1_000))
    svc = _svc(broker, db)
    res = svc.run_once()
    assert res["by_kind"]["position_mismatch"] == 1        # T+1 可用量不一致必须报
    d = next(x for x in svc.recent_diffs() if x["kind"] == "position_mismatch")
    assert d["detail"]["broker_available"] == 100 and d["detail"]["local_available"] == 0


def test_account_internal_inconsistency_detected(db):
    broken = Account(total_asset=100_000, cash=90_000, market_value=5_000)  # 少 5000
    res = _svc(StubLive(account=broken), db).run_once()
    assert res["by_kind"]["account_mismatch"] == 1


def test_diff_publishes_risk_event_but_clean_does_not(db):
    bus = EventBus()
    got: list[Event] = []
    bus.subscribe(EventType.RISK, got.append)
    _svc(StubLive(orders=[_order()]), db, bus=bus).run_once()
    assert got and got[0].data["action"] == "reconcile_diff"

    bus2 = EventBus()
    got2: list[Event] = []
    bus2.subscribe(EventType.RISK, got2.append)
    broker = StubLive(orders=[_order()])
    repo.upsert_order(_order(), db_path=db)
    _svc(broker, db, bus=bus2).run_once()
    assert got2 == []                                      # 无差异不刷屏


def test_falls_back_to_abc_queries_when_no_snapshot_api(db):
    broker = PollOnlyBroker(orders=[_order()])
    res = _svc(broker, db).run_once()
    assert broker.queries == 1
    assert res["by_kind"]["order_missing_local"] == 1


def test_no_channel_available_skips_without_raising(db):
    svc = ReconcileService(live_broker=None, paper_broker=None, db_path=db,
                           settings=SimpleNamespace(simulation_only=False,
                                                    reconcile_interval_s=30.0))
    assert svc.run_once()["skipped"] == "无可用通道"


def test_simulation_reconciles_paper_book_against_db(db):
    """模拟盘也核对：paper 内存簿与库不一致就是本地缺陷，实盘前必须先抓出来。"""
    from quantdesk.adapters.brokers.paper_broker import PaperBroker

    paper = PaperBroker(initial_cash=100_000, db_path=db)
    paper.connect()
    paper.on_tick(Tick(symbol="600000.SH", dt=datetime.now(), last=10.0, open=10.0,
                       high=10.0, low=10.0, volume=100_000, amount=1_000_000))
    paper.place_order(Order(order_id="OD-P", symbol="600000.SH", side=Side.BUY,
                            price=10.0, volume=100, status=OrderStatus.SUBMITTED))
    svc = ReconcileService(paper_broker=paper, db_path=db,
                           settings=SimpleNamespace(simulation_only=True,
                                                    reconcile_interval_s=30.0))
    assert svc.run_once()["diffs"] == 0
    # 篡改内存簿 → 立刻检出（证明这条自检真的在跑）
    paper.book.apply_trade(_trade(tid="GHOST", order_id="OD-X"), fee=0.0)
    res = svc.run_once()
    assert res["diffs"] >= 1


def test_loop_runs_periodically_and_stops(db, monkeypatch):
    broker = StubLive(orders=[_order()])
    svc = ReconcileService(live_broker=broker, db_path=db, bus=EventBus(), interval=0.05,
                           settings=SimpleNamespace(simulation_only=False,
                                                    reconcile_interval_s=0.05))
    svc.start()
    import time

    time.sleep(0.3)
    svc.stop()
    assert svc.status()["running"] is False
    assert svc.last_result["diffs"] == 1


# ---------------- 处置闭环与升级补列 ----------------

def test_diff_can_be_resolved_and_reopened(db):
    """差异要能认领并留下结论：只列不处置，红条会变成噪音，真差异反而没人看。"""
    repo.reconcile_record([{"kind": "order_missing", "ref": "OD-9",
                            "detail": {"local": "FILLED", "broker": None}}],
                          channel="paper", ran_at=1730000000000, db_path=db)
    rows = repo.reconcile_recent(db_path=db)
    did = rows[0]["id"]
    assert rows[0]["resolved"] is False and rows[0]["resolve_note"] == ""

    done = repo.reconcile_resolve(did, note="券商回报晚到，本地已补齐", db_path=db)
    assert done["resolved"] is True and "回报晚到" in done["resolve_note"]
    assert repo.reconcile_recent(db_path=db, unresolved_only=True) == []
    assert len(repo.reconcile_recent(db_path=db)) == 1        # 已处置仍在档案里，不消失

    back = repo.reconcile_resolve(did, resolve=False, db_path=db)
    assert back["resolved"] is False and back["resolved_at"] == 0


def test_service_status_counts_only_open_diffs(db):
    repo.reconcile_record([{"kind": "order_missing", "ref": "A", "detail": {}},
                           {"kind": "position_mismatch", "ref": "B", "detail": {}}],
                          channel="paper", db_path=db)
    svc = ReconcileService(bus=EventBus(), db_path=db)
    assert svc.status()["open"] == 2
    open_rows = svc.open_diffs()
    svc.resolve(open_rows[0]["id"], note="已核对")
    assert svc.status()["open"] == 1
    assert [d["kind"] for d in svc.open_diffs()] == [d["kind"] for d in open_rows[1:]]


def test_resolve_unknown_id_is_404(db, monkeypatch):
    from fastapi.testclient import TestClient

    from quantdesk.app import app

    svc = ReconcileService(bus=EventBus(), db_path=db)
    monkeypatch.setattr("quantdesk.services.reconcile.get_reconcile_service", lambda: svc)
    client = TestClient(app)
    r = client.post("/api/channels/reconcile/999999/resolve", json={"note": "x"})
    assert r.status_code == 404 and "不存在" in r.json()["detail"]


def test_old_database_gets_new_columns_without_losing_rows(tmp_path):
    """升级纪律：SQLModel 的 create_all 只建表不加列。

    老库缺列的话，新代码第一次 SELECT 就报 no such column——用户数据没坏，
    但软件直接打不开。ensure_columns 负责补齐，且不许动已有行。
    """
    import sqlite3

    from quantdesk.store.db import ensure_columns, get_engine

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE reconcile_diff (id INTEGER PRIMARY KEY, ran_at INTEGER, channel TEXT,"
        " kind TEXT, ref TEXT, detail_json TEXT, created_at INTEGER);"
        "INSERT INTO reconcile_diff (ran_at, channel, kind, ref, detail_json, created_at)"
        " VALUES (1730000000000,'paper','order_missing','OD-1','{\"local\":\"FILLED\"}',1);"
    )
    con.commit()
    con.close()

    engine = get_engine(path)
    added = ensure_columns(engine)
    assert "reconcile_diff.resolved" in added
    assert ensure_columns(engine) == []               # 幂等：第二次什么都不做

    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(reconcile_diff)")}
    rows = list(con.execute("SELECT id, ref, resolved, resolve_note FROM reconcile_diff"))
    con.close()

    assert {"resolved", "resolve_note", "resolved_at"} <= cols
    assert rows == [(1, "OD-1", 0, "")]               # 老行还在，新列取默认值
    # 补齐后 ORM 读得动，且这条差异真的算"未处置"
    assert [d["ref"] for d in repo.reconcile_recent(db_path=path, unresolved_only=True)] == ["OD-1"]


def test_ensure_columns_is_no_op_on_fresh_database(tmp_path):
    from quantdesk.store.db import ensure_columns, get_engine, init_db

    path = init_db(tmp_path / "fresh.db")
    assert ensure_columns(get_engine(path)) == []


def test_ensure_columns_is_no_op_on_fresh_database(tmp_path):
    from quantdesk.store.db import ensure_columns, get_engine, init_db

    path = init_db(tmp_path / "fresh.db")
    assert ensure_columns(get_engine(path)) == []


# ==================================================== 保留期与"心跳不是差异"
#
# 界面实测翻出来的：对账每 30s 落一条 kind="clean"（无差异也是审计证据，这是刻意的），
# 但读取侧把它一起算进了「待处置 N 项」——装机半小时后对账页 137 行全是心跳、
# 红条写着"待处置 137"，而那时一条真事故都没有。噪音会把真事故淹掉。

def test_clean_heartbeats_are_never_counted_as_open_diffs(db):
    for _ in range(3):
        assert _svc(StubLive(), db).run_once()["diffs"] == 0
    _svc(StubLive(orders=[_order()]), db).run_once()          # 一轮真差异

    svc = _svc(StubLive(), db)
    open_rows = svc.open_diffs(limit=100)
    assert len(open_rows) == 1, f"待处置只该有那 1 条真差异，实际 {open_rows}"
    assert open_rows[0]["kind"] == "order_missing_local"
    assert svc.status()["open"] == 1, "红条计数按心跳增长＝红条失去意义"

    only_diffs = svc.recent_diffs(limit=50, diffs_only=True)
    assert all(r["kind"] != "clean" for r in only_diffs)
    audit = svc.recent_diffs(limit=50, diffs_only=False)
    assert any(r["kind"] == "clean" for r in audit), "审计视角仍要能看到每轮无差异的记录"


def test_retention_purge_deletes_only_old_clean_heartbeats(db):
    """保留期前的旧心跳删掉；旧真差异一行都不许动（事故档案不许被定时任务删）。"""
    import time

    from quantdesk.store.db import get_session
    from quantdesk.store.models_db import ReconcileDiffRow
    from sqlmodel import select

    old = int((time.time() - 400 * 86400) * 1000)
    recent = int(time.time() * 1000)
    with get_session(db) as s:
        s.add(ReconcileDiffRow(ran_at=old, channel="qmt", kind="clean", detail_json="{}"))
        s.add(ReconcileDiffRow(ran_at=old, channel="qmt", kind="clean", detail_json="{}"))
        s.add(ReconcileDiffRow(ran_at=old, channel="qmt", kind="position_mismatch",
                               ref="600000.SH", detail_json="{}"))
        s.add(ReconcileDiffRow(ran_at=recent, channel="qmt", kind="clean", detail_json="{}"))
        s.commit()

    deleted = repo.reconcile_purge(int((time.time() - 90 * 86400) * 1000), db_path=db)
    assert deleted == 2, f"应只删掉两条过期心跳，实际 {deleted}"
    with get_session(db) as s:
        kinds = [(r.kind, r.ran_at == old) for r in s.exec(select(ReconcileDiffRow)).all()]
    assert ("position_mismatch", True) in kinds, "过期真差异被删了——事故档案不许自动清理"
    assert ("clean", False) in kinds, "保留期内的心跳不该被清掉"


def test_retention_job_reports_counts(tmp_path, monkeypatch):
    from quantdesk.config import get_settings
    from quantdesk.services.scheduler import PostCloseScheduler

    reset_engine()
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    init_db()
    events = []
    sched = PostCloseScheduler(bus=SimpleNamespace(publish=events.append))
    out = sched.retention_job()
    assert out["days"] >= 1 and isinstance(out["logs"], int) and isinstance(out["clean_diffs"], int)
    assert "error" not in out, out
    assert any("保留期清理" in str(e.data.get("message", "")) for e in events), \
        "清理动作要广播出去，不然库变小这件事没人知道"
    reset_engine()
    get_settings.cache_clear()


def test_diffs_only_honoured_on_both_branches(db, monkeypatch):
    """`diffs_only` 不许在「只看未处置」分支被静默忽略——界面上那个勾点了要有反应。"""
    import quantdesk.api.routes_channels as api_ch
    import quantdesk.services.reconcile as rc

    _svc(StubLive(), db).run_once()                       # 一轮无差异 → 落 clean
    _svc(StubLive(orders=[_order()]), db).run_once()      # 一轮真差异
    svc = _svc(StubLive(), db)
    monkeypatch.setattr(rc, "_service", svc)

    default = api_ch.reconcile_state(limit=50, unresolved_only=True, diffs_only=True)
    assert all(x["kind"] != "clean" for x in default["diffs"]), default["diffs"]
    assert default["status"]["open"] == 1, "红条计数按差异算，不按心跳算"

    audit = api_ch.reconcile_state(limit=50, unresolved_only=True, diffs_only=False)
    assert any(x["kind"] == "clean" for x in audit["diffs"]), \
        "同一份数据、同一个参数，两条分支口径必须一致（一处生效一处忽略＝接口在骗调用方）"
