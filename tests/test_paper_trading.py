"""D09：模拟盘通道测试（DoD：下单→撮合→落库→查询闭环；重启恢复；开关 ON 路由 paper）。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.app import app
from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.models import Tick
from quantdesk.engine.riskgate import RiskGate
from quantdesk.services.trading import TradingService
from quantdesk.store.db import init_db
from quantdesk.store.repo import account_get, latest_positions

SYM = "600000.SH"


def mk_tick(last=10.0, bids=None, asks=None, dt=datetime(2026, 11, 4, 9, 31)) -> Tick:
    return Tick(symbol=SYM, dt=dt, last=last, open=last, high=last, low=last,
                volume=10_000, amount=last * 10_000,
                bids=bids or [(round(last - 0.02, 2), 500)],
                asks=asks or [(round(last + 0.02, 2), 500)])


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "paper.db"
    init_db(p)
    return p


def test_place_fill_query_closed_loop(db):
    """API 下单 → 撮合 → 落库 → 查询闭环（DoD 第 1 条）。"""
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    svc.paper.on_tick(mk_tick(10.0))
    o = svc.place_order(SYM, "buy", 500, price=10.05)     # 限价 ≥ ask → 立即成交
    assert o.status == OrderStatus.FILLED
    assert o.filled_volume == 500

    # 查询面
    assert len(svc.list_orders()) == 1
    assert len(svc.trades()) == 1
    pos = svc.positions()
    assert len(pos) == 1 and pos[0].symbol == SYM and pos[0].volume == 500
    assert pos[0].available == 0                           # T+1 当日不可卖
    acct = svc.account()
    assert acct.cash < 100_000 and acct.market_value > 0
    # 库内自证（先落库）：orders/trades/positions/account 四表都有记录
    assert len(svc.paper.get_orders()) == 1
    assert latest_positions(db_path=db) != []
    assert account_get(db_path=db).cash == pytest.approx(acct.cash)


def test_limit_order_rests_until_tick(db):
    """限价低于市价 → 挂单；tick 下探 → 自动成交。"""
    svc = TradingService(db_path=db)
    svc.paper.on_tick(mk_tick(10.0))
    o = svc.place_order(SYM, "buy", 100, price=9.80)      # ask 10.02 > 9.80 → 不成交
    assert o.status == OrderStatus.SUBMITTED
    assert len(svc.trades()) == 0

    svc.paper.on_tick(mk_tick(9.75))                      # last ≤ 9.80 → 成交
    o2 = svc.get_order(o.order_id)
    assert o2.status == OrderStatus.FILLED
    assert o2.filled_volume == 100 and o2.avg_filled_price == 9.75


def test_cancel_resting_order(db):
    svc = TradingService(db_path=db)
    svc.paper.on_tick(mk_tick(10.0))
    o = svc.place_order(SYM, "buy", 100, price=9.0)
    assert o.status == OrderStatus.SUBMITTED
    assert svc.cancel_order(o.order_id) is True
    assert svc.get_order(o.order_id).status == OrderStatus.CANCELLED
    assert svc.cancel_order(o.order_id) is False          # 已终态
    # 撤单后 tick 下探不再成交
    svc.paper.on_tick(mk_tick(8.0))
    assert svc.get_order(o.order_id).status == OrderStatus.CANCELLED


def test_restart_recovery(db):
    """重启后持仓/资金恢复（DoD 第 2 条）：新实例 connect() 从 SQLite 回放。"""
    svc = TradingService(initial_cash=200_000.0, db_path=db)
    svc.paper.on_tick(mk_tick(10.0))
    svc.place_order(SYM, "buy", 500, price=10.05)          # 当日买入
    cash_before = svc.account().cash

    svc.stop()                                             # —— 重启 ——
    svc2 = TradingService(initial_cash=200_000.0, db_path=db)
    svc2.start()
    acct2 = svc2.account()
    assert acct2.cash == pytest.approx(cash_before)        # 资金恢复
    pos = svc2.positions()
    assert len(pos) == 1 and pos[0].volume == 500
    assert pos[0].available == 0                           # T+1 语义跨重启保持（当日买）
    # 次日释放
    svc2.paper.on_new_trading_day()
    assert svc2.positions()[0].available == 500


def test_t1_sell_after_release(db):
    """T+1 次日释放后可卖；卖出成交回笼资金。"""
    svc = TradingService(db_path=db)
    svc.paper.on_tick(mk_tick(10.0))
    svc.place_order(SYM, "buy", 500, price=10.05)
    # 当日卖出 → 拒（broker 层防负持仓）
    o = svc.place_order(SYM, "sell", 500, price=9.9)
    assert o.status == OrderStatus.REJECTED and "T+1" in o.message
    # 次日 → 可卖
    svc.paper.on_new_trading_day()
    o2 = svc.place_order(SYM, "sell", 500, price=9.9)
    assert o2.status == OrderStatus.FILLED
    assert svc.account().market_value == 0 and svc.positions() == []


def test_market_order_with_and_without_tick(db):
    svc = TradingService(db_path=db)
    # 无行情 → 市价单 fail-closed 拒绝（不排队）
    o = svc.place_order(SYM, "buy", 100, price=None, order_type="market")
    assert o.status == OrderStatus.REJECTED and "无最新行情" in o.message
    # 有行情 → 对手价 + 滑点
    svc2 = TradingService(db_path=db, settings=_StubSettings(simulation_only=True))
    svc2.paper.on_tick(mk_tick(10.0, asks=[(10.02, 500)]))
    from quantdesk.engine.matching import MatchConfig
    svc2.paper.match_cfg = MatchConfig(slippage=0.001)
    o2 = svc2.place_order(SYM, "buy", 100, price=None, order_type="market")
    assert o2.status == OrderStatus.FILLED
    assert o2.avg_filled_price == round(10.02 * 1.001, 2)


def test_insufficient_cash_reduced(tmp_path):
    from quantdesk.store.db import init_db as _idb

    # 场景一：1000 元买不起一手（100×10.05=1005+费 > 1000）→ 拒绝
    db1 = tmp_path / "a.db"
    _idb(db1)
    svc = TradingService(initial_cash=1_000.0, db_path=db1)
    svc.paper.on_tick(mk_tick(10.0))
    o = svc.place_order(SYM, "buy", 10_000, price=10.05)
    assert o.status == OrderStatus.REJECTED

    # 场景二：2000 元 → 缩量到一手成交，现金不为负
    db2 = tmp_path / "b.db"
    _idb(db2)
    svc2 = TradingService(initial_cash=2_000.0, db_path=db2)
    svc2.paper.on_tick(mk_tick(10.0))
    o2 = svc2.place_order(SYM, "buy", 10_000, price=10.05)
    assert o2.status == OrderStatus.FILLED
    assert o2.filled_volume == 100                          # 缩量到一手
    assert svc2.account().cash >= 0


class _StubSettings:
    def __init__(self, simulation_only: bool):
        self.simulation_only = simulation_only


def test_gate_switch_on_routes_paper(db):
    """开关 ON 时即使实盘通道缺位，下单也不报错（路由 paper）——DoD 第 3 条。"""
    gate = RiskGate(paper_broker=PaperBroker(db_path=db), live_broker=None,
                    settings=_StubSettings(simulation_only=True), db_path=db)
    gate.paper.connect()
    gate.paper.on_tick(mk_tick(10.0))
    from quantdesk.core.models import Order as O
    intent = O(order_id=gate.new_order_id(), symbol=SYM, side=Side.BUY,
               price=10.05, volume=100)
    out = gate.submit(intent)
    assert out.status == OrderStatus.FILLED                 # 正常路由 paper 成交


def test_gate_switch_off_no_live_fail_closed(db):
    """模拟盘 OFF 且实盘缺位 → fail-closed 拒单（不报错不排队）。"""
    gate = RiskGate(paper_broker=PaperBroker(db_path=db), live_broker=None,
                    settings=_StubSettings(simulation_only=False), db_path=db)
    from quantdesk.core.models import Order as O
    intent = O(order_id=gate.new_order_id(), symbol=SYM, side=Side.BUY,
               price=10.05, volume=100)
    out = gate.submit(intent)
    assert out.status == OrderStatus.REJECTED
    assert "fail-closed" in out.message
    # 拦截落库可查
    from quantdesk.store.repo import get_order

    assert get_order(out.order_id, db_path=db).status == "rejected"


def test_events_published_after_db_write(db):
    """先落库后广播：事件流里能看到 ORDER/TRADE/POSITION/ACCOUNT。"""
    from quantdesk.core.events import EventBus

    bus = EventBus()
    seen: list = []
    bus.subscribe(None, lambda e: seen.append(e.type.value))
    svc = TradingService(db_path=db)
    svc.bus = bus
    svc.paper.bus = bus
    svc.paper.on_tick(mk_tick(10.0))
    svc.place_order(SYM, "buy", 100, price=10.05)
    assert "order" in seen and "trade" in seen and "position" in seen and "account" in seen


# ---------- API 层 ----------

def test_api_trading_roundtrip(db, tmp_path, monkeypatch):
    init_db(tmp_path / "api.db")
    svc = TradingService(initial_cash=100_000.0, db_path=tmp_path / "api.db")
    from quantdesk.services import trading as mod
    monkeypatch.setattr(mod, "_service", svc)

    with TestClient(app) as client:
        # 注入行情（测试直接喂 tick）
        svc.paper.on_tick(mk_tick(10.0))
        r = client.post("/api/trading/orders", json={
            "symbol": SYM, "side": "buy", "volume": 100, "price": 10.05})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "filled"

        r2 = client.get("/api/trading/orders")
        assert r2.status_code == 200 and len(r2.json()) == 1
        r3 = client.get("/api/trading/positions")
        assert r3.json()[0]["volume"] == 100
        r4 = client.get("/api/trading/account")
        assert r4.json()["cash"] < 100_000
        r5 = client.get("/api/trading/trades")
        assert len(r5.json()) == 1

        # 挂单 → 撤单
        r6 = client.post("/api/trading/orders", json={
            "symbol": SYM, "side": "buy", "volume": 100, "price": 5.0})
        oid = r6.json()["order_id"]
        r7 = client.post(f"/api/trading/orders/{oid}/cancel")
        assert r7.status_code == 200 and r7.json()["accepted"] is True
        # 非法参数
        assert client.post("/api/trading/orders", json={
            "symbol": SYM, "side": "buy", "volume": 0}).status_code == 422
