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


def mk_tick(last=10.0, bids=None, asks=None, dt=None) -> Tick:
    # 成交时间戳取自 tick：写死日期会在跨零点后让「当日买入 available=0」这条断言变假红，
    # 所以默认用"今天 09:31"——测试要测的是 T+1 语义，不是某一天
    return Tick(symbol=SYM, dt=dt or datetime.now().replace(hour=9, minute=31, second=0,
                                                            microsecond=0),
                last=last, open=last, high=last, low=last,
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


def test_insufficient_cash_rejected_by_gate(tmp_path):
    """D16 闸②：资金不足前置拒单（券商真实行为——委托即验资），现金不为负。"""
    from quantdesk.store.db import init_db as _idb

    # 场景一：1000 元买不起一手（100×10.05=1005+费 > 1000）→ 拒绝
    db1 = tmp_path / "a.db"
    _idb(db1)
    svc = TradingService(initial_cash=1_000.0, db_path=db1)
    svc.paper.on_tick(mk_tick(10.0))
    o = svc.place_order(SYM, "buy", 10_000, price=10.05)
    assert o.status == OrderStatus.REJECTED

    # 场景二：2000 元同样不足以买 10000 股 → 闸②前置拒绝（不再走缩量路径）
    db2 = tmp_path / "b.db"
    _idb(db2)
    svc2 = TradingService(initial_cash=2_000.0, db_path=db2)
    svc2.paper.on_tick(mk_tick(10.0))
    o2 = svc2.place_order(SYM, "buy", 10_000, price=10.05)
    assert o2.status == OrderStatus.REJECTED
    assert "资金不足" in o2.message
    assert svc2.account().cash == 2_000.0                   # 分文未动


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


def test_part_filled_resting_order_survives_restart(tmp_path):
    """回归：部分成交挂单的余量在重启后仍存活（此前只恢复 SUBMITTED）。"""
    import dataclasses

    from quantdesk.store.db import init_db as _idb
    from quantdesk.store.repo import upsert_order

    db = tmp_path / "pf.db"
    _idb(db)
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    svc.start()
    # 直接种一条 part_filled 挂单到库（模拟重启前的部分成交状态）
    o = dataclasses.replace(
        svc.place_order(SYM, "buy", 500, price=9.0),
        status=OrderStatus.PART_FILLED, filled_volume=200, avg_filled_price=9.0)
    upsert_order(o, db_path=db)

    svc.stop()
    svc2 = TradingService(initial_cash=100_000.0, db_path=db)
    svc2.start()                                   # _restore 应把它放回挂单簿
    assert o.order_id in svc2.paper._resting
    # tick 下探 → 余量（300）继续成交
    svc2.paper.on_tick(mk_tick(8.8))
    o2 = svc2.get_order(o.order_id)
    assert o2 is not None and o2.status == OrderStatus.FILLED
    assert o2.filled_volume == 500                 # 200 已成交 + 300 余量补齐


def test_concurrent_tick_and_order_threads(tmp_path):
    """并发冒烟：实时回调线程（on_tick）与下单线程并发操控同一 broker，
    无异常、现金不为负、账目自洽（D14 接实时源前的并发防护回归）。"""
    import threading

    from quantdesk.store.db import init_db as _idb

    db = tmp_path / "conc.db"
    _idb(db)
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    errors: list[str] = []

    def feeder():
        for i in range(200):
            try:
                price = 10.0 + 0.01 * (i % 20)
                svc.paper.on_tick(mk_tick(round(price, 2),
                                          dt=datetime(2026, 11, 4, 9, 30)))
            except Exception as e:                 # noqa: BLE001
                errors.append(f"feeder: {e}")

    def trader():
        for i in range(50):
            try:
                side = "buy" if i % 2 == 0 else "sell"
                svc.place_order(SYM, side, 100,
                                price=round(9.0 + 0.01 * (i % 30), 2))
            except Exception as e:                 # noqa: BLE001
                errors.append(f"trader: {e}")

    threads = [threading.Thread(target=feeder), threading.Thread(target=trader),
               threading.Thread(target=trader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors[:5]
    # 不变量：现金非负；总资产 = 现金 + 市值
    acct = svc.account()
    assert acct.cash >= -1e-6
    assert acct.total_asset == pytest.approx(acct.cash + acct.market_value, abs=0.02)


# ---------- 按需行情：没被实时泵订阅过的标的也要能立刻成交 ----------

def fake_row(symbol, last=10.0, prev=9.9, bids=None, asks=None, ts_ms=None):
    import time

    return {"symbol": symbol, "name": "浦发银行", "last": last, "open": prev,
            "high": round(last * 1.02, 2), "low": round(last * 0.98, 2),
            "volume": 120_000, "amount": last * 120_000, "prev_close": prev,
            "limit_up": round(prev * 1.1, 2), "limit_down": round(prev * 0.9, 2),
            "bids": bids or [[round(last - 0.01, 2), 500]],
            "asks": asks or [[round(last + 0.01, 2), 500]],
            "ts_ms": ts_ms or int(time.time() * 1000)}


def test_order_without_tick_fetches_quote_and_fills(db, monkeypatch):
    """实时泵只推它订阅过的标的；用户点的这只不在里面 → 下单当场补一次行情，立刻撮合。

    补不上行情时不假装成交：限价单照样挂着（等下一次行情），市价单 fail-closed 拒单。
    """
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    calls = []

    def quote(self, symbol):
        calls.append(symbol)
        return fake_row(symbol, last=10.0, prev=9.9)

    monkeypatch.setattr(TradingService, "_quote_row", quote)
    o = svc.place_order(SYM, "buy", 100, price=10.0)
    assert o.status == OrderStatus.FILLED, o.message
    assert calls == [SYM]                     # 运行期要取的字段必须在调用点取（宪法第 11 条⑤）
    assert svc.get_order(o.order_id).status == OrderStatus.FILLED


def test_market_order_no_longer_dies_for_missing_tick(db, monkeypatch):
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    monkeypatch.setattr(TradingService, "_quote_row", lambda self, s: fake_row(s, last=10.0))
    o = svc.place_order(SYM, "buy", 100, price=None, order_type="market")
    assert o.status == OrderStatus.FILLED, o.message


def test_no_quote_available_keeps_old_behaviour(db, monkeypatch):
    """取不到行情就如实回到老口径：市价单拒单，限价单挂起，绝不凭空造一个成交价。"""
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    monkeypatch.setattr(TradingService, "_quote_row", lambda self, s: {})
    m = svc.place_order(SYM, "buy", 100, price=None, order_type="market")
    assert m.status == OrderStatus.REJECTED and "无最新行情" in m.message
    lim = svc.place_order(SYM, "buy", 100, price=9.0)
    assert lim.status == OrderStatus.SUBMITTED          # 挂着，没有被静默丢掉


def test_stale_tick_is_refreshed_before_matching(db, monkeypatch):
    """手上那份行情已经是昨天的了，得先换成今天的，否则今天的委托按昨天价格撮合。"""
    from datetime import timedelta

    svc = TradingService(initial_cash=100_000.0, db_path=db)
    svc.paper.on_tick(mk_tick(10.0, dt=datetime.now() - timedelta(days=1)))
    seen = []

    def quote(self, symbol):
        seen.append(symbol)
        return fake_row(symbol, last=12.0, prev=11.8)

    monkeypatch.setattr(TradingService, "_quote_row", quote)
    o = svc.place_order(SYM, "buy", 100, price=12.0)
    assert seen == [SYM]
    assert o.status == OrderStatus.FILLED and o.avg_filled_price == 12.0


def test_fresh_tick_is_not_refetched(db, monkeypatch):
    """已有新鲜 tick 就不换撮合依据（闸问一次官方涨跌停是另一回事，那边有 TTL 缓存）。"""
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    svc.paper.on_tick(mk_tick(10.0, dt=datetime.now()))
    before = svc.paper.latest_tick(SYM)
    monkeypatch.setattr(TradingService, "_quote_row",
                        lambda self, s: fake_row(s, last=99.0))
    o = svc.place_order(SYM, "buy", 100, price=9.0)
    assert svc.paper.latest_tick(SYM) is before          # 没被 99 元那份行情顶掉
    assert o.status == OrderStatus.SUBMITTED             # 按 10 元行情，9 元买单挂起


def test_symbol_is_normalised_before_it_reaches_the_gate(db, monkeypatch):
    """手输 sh600000 / 600000 都要能下单，落库统一内部代码。"""
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    monkeypatch.setattr(TradingService, "_quote_row", lambda self, s: fake_row(s, last=10.0))
    o = svc.place_order("sh600000", "buy", 100, price=10.0)
    assert o.symbol == SYM
    assert o.status == OrderStatus.FILLED, o.message


def test_hk_single_share_market_order_fills(db, monkeypatch):
    """实测缺陷：可负担量按 100 股取整，「买 1 股腾讯」被算成资金不足静默拒单。

    港股每手股数由发行人自定、快照不提供 → 递增单位必须是 1 股（界面另注明实际每手）。
    """
    svc = TradingService(initial_cash=100_000.0, db_path=db)
    monkeypatch.setattr(TradingService, "_quote_row",
                        lambda self, s: fake_row("00700.HK", last=419.4, prev=401.6))
    o = svc.place_order("00700.HK", "buy", 1, price=None, order_type="market")
    assert o.status == OrderStatus.FILLED, o.message
    assert svc.paper.get_position("00700.HK")[0].available == 1      # T+0：立刻能卖


def test_market_order_on_yesterday_quote_is_rejected(db):
    """缓存里那份隔夜行情是**昨收**，按它撮合等于用昨天的价成交今天的单——必须拒，不是照成交。"""
    from datetime import timedelta

    svc = TradingService(initial_cash=100_000.0, db_path=db)
    svc.paper.on_tick(mk_tick(10.0, dt=datetime.now() - timedelta(days=1)))
    o = svc.place_order(SYM, "buy", 100, price=None, order_type="market")
    assert o.status == OrderStatus.REJECTED
    assert "收盘价" in o.message and "今日休市" in o.message
    # 限价单不受影响：价格条件由委托自己给出，挂着等行情即可
    lim = svc.place_order(SYM, "buy", 100, price=9.0)
    assert lim.status == OrderStatus.SUBMITTED
