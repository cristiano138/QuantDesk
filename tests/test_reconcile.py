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
