"""代码审阅中真实复现的交易与实时策略缺陷；只使用临时库与离线替身。"""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from quantdesk.adapters.brokers.paper_broker import PaperBroker
from quantdesk.core.enums import Frequency, OrderStatus, OrderType, Side
from quantdesk.core.models import Bar, Order, Tick, Trade
from quantdesk.engine.matching import MatchConfig, PositionBook, match_on_bar, match_on_tick
from quantdesk.engine.runner import IPCStrategyContext, bar_to_dict
from quantdesk.store import repo
from quantdesk.store.db import init_db
from strategies.examples.dual_ma import DualMAStrategy
from tests.test_realtime_loop import _pump


def _trade(tid, side, when, symbol="00700.HK", price=10.0, volume=100):
    return Trade(tid, tid, symbol, side, price, volume, price * volume, when)


def test_t1_sell_consumes_only_overnight_available_shares():
    book = PositionBook()
    book.apply_trade(_trade("old", Side.BUY, 0, symbol="600000.SH"))
    book.on_new_trading_day()
    book.apply_trade(_trade("today", Side.BUY, 1, symbol="600000.SH"))
    book.apply_trade(_trade("sell", Side.SELL, 2, symbol="600000.SH", volume=50))
    assert book.position("600000.SH").volume == 150
    assert book.available("600000.SH") == 50
    book.apply_trade(_trade("sell2", Side.SELL, 3, symbol="600000.SH", volume=50))
    assert book.available("600000.SH") == 0
    assert book.position("600000.SH").volume == 100


@pytest.mark.parametrize("same_millisecond", [False, True])
def test_restart_does_not_resurrect_closed_position(tmp_path, same_millisecond):
    db = init_db(tmp_path / "replay.db")
    stamp = int(datetime.now().timestamp() * 1000)
    # ID 的字典序刻意与执行顺序相反；同毫秒要保留入账顺序。
    repo.append_trade(_trade("Z-buy", Side.BUY, stamp - 2), db_path=db)
    repo.append_trade(_trade("A-sell", Side.SELL, stamp - (2 if same_millisecond else 1)),
                      db_path=db)
    broker = PaperBroker(db_path=db)
    broker.connect()
    assert broker.get_position() == []


def test_connect_and_reconnect_do_not_duplicate_positions_or_resting_orders(tmp_path):
    db = init_db(tmp_path / "reconnect.db")
    stamp = int(datetime.now().timestamp() * 1000)
    repo.append_trade(_trade("buy", Side.BUY, stamp), db_path=db)
    resting = Order("resting", "00700.HK", Side.BUY, 1.0, 100,
                    status=OrderStatus.SUBMITTED)
    repo.upsert_order(resting, db_path=db)
    broker = PaperBroker(db_path=db)
    broker.connect()
    broker.connect()
    assert broker.get_position()[0].volume == 100
    broker.disconnect()
    repo.upsert_order(replace(resting, status=OrderStatus.CANCELLED), db_path=db)
    broker.connect()
    assert broker.get_position()[0].volume == 100
    assert "resting" not in broker._resting


@pytest.mark.parametrize("symbol", ["900901.SH", "200001.SZ"])
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("order_type", [OrderType.LIMIT, OrderType.MARKET])
def test_b_share_tick_and_bar_fills_keep_third_decimal(symbol, side, order_type):
    order = Order("B1", symbol, side, 0.173, 100, order_type=order_type)
    tick = Tick(symbol, datetime(2026, 11, 6, 10), 0.173, 0.173, 0.173, 0.173,
                1000, 173.0)
    bar = Bar(symbol, Frequency.D1, tick.dt, 0.173, 0.173, 0.173, 0.173, 1000, 173.0)
    config = MatchConfig(slippage=0)
    for result in (match_on_tick(order, tick, config), match_on_bar(order, bar, config)):
        assert result.fill_price == 0.173
        assert result.trades[0].price == 0.173
        assert result.trades[0].amount == 17.30


class _Client:
    def __init__(self):
        self.sent = []

    def send(self, tag, data):
        self.sent.append((tag, data))


def test_bundled_dual_ma_can_log_order_id_on_realtime_golden_cross():
    client = _Client()
    ctx = IPCStrategyContext(client, "dual_ma")
    strategy = DualMAStrategy()
    strategy.bind(ctx)
    for i, close in enumerate(list(range(20, 0, -1)) + [100]):
        bar = Bar("600000.SH", Frequency.D1, datetime(2026, 9, 1) + timedelta(days=i),
                  close, close, close, close, 1000, 1000 * close)
        ctx.on_event("bar", bar_to_dict(bar))
    ctx._strategy = strategy
    strategy.on_bar(bar)
    orders = [data for tag, data in client.sent if tag == "order_intent"]
    logs = [data["message"] for tag, data in client.sent if tag == "log"]
    assert len(orders) == 1
    assert orders[0]["order_id"] in logs[0]
    ack = ctx.sell("600000.SH", 100, price=10)
    assert isinstance(ack, Order)
    assert ack.order_id == client.sent[-1][1]["order_id"]


def test_ipc_history_keeps_frequencies_separate_and_datetime_typed():
    ctx = IPCStrategyContext(_Client(), "s")
    for freq, close in [(Frequency.D1, 10), (Frequency.M5, 20), (Frequency.D1, 11)]:
        bar = Bar("600000.SH", freq, datetime(2026, 11, 6, 9, 30),
                  close, close, close, close, 1000, close * 1000)
        ctx.on_event("bar", bar_to_dict(bar))
    daily = ctx.history("600000.SH", "1d")
    minute = ctx.history("600000.SH", "5m")
    assert len(daily) == len(minute) == 1
    assert daily.close.iloc[0] == 11 and minute.close.iloc[0] == 20
    assert str(daily.dt.dtype) == "datetime64[ns]"
    assert ctx.history("600000.SH", "15m").empty
    assert ctx.history("600000.SH", bars=0).empty


def test_timezone_aware_live_bar_updates_naive_history_seed():
    ctx = IPCStrategyContext(_Client(), "s")
    dt = datetime(2026, 11, 6)
    bar = Bar("600000.SH", Frequency.D1, dt, 10, 10, 10, 10, 100, 1000)
    ctx.on_event("bar", {**bar_to_dict(bar), "warmup": True})
    ctx.on_event("bar", bar_to_dict(replace(bar, dt=dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
                                          close=11)))
    h = ctx.history("600000.SH")
    assert len(h) == 1 and h.close.iloc[0] == 11
    assert str(h.dt.dtype) == "datetime64[ns]"


@pytest.mark.parametrize("freq,minutes", [("5m", 5), ("15m", 15), ("30m", 30), ("60m", 60)])
def test_realtime_strategy_rolls_each_requested_minute_period(tmp_path, monkeypatch, freq, minutes):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"], freq=freq)
    assert pump.store.reads[-1][1] == freq
    assert pump.status()["strategies"]["dual_ma"]["freq"] == freq
    sup.delivered.clear()
    start = datetime(2026, 11, 6, 10, 0)
    for dt, last in [(start, 10.0), (start + timedelta(minutes=minutes), 11.0)]:
        feed.push(Tick("600000.SH", dt, last, last, last, last, 1000, last * 1000))
    bars = [d for tag, d in sup.delivered if tag == "bar"]
    assert len({d["dt"] for d in bars}) == 2
    assert any(tag == "tick" for tag, _ in sup.delivered)


def test_history_seed_does_not_execute_strategy_callbacks(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"])
    ctx = IPCStrategyContext(_Client(), "dual_ma")
    class Strategy:
        calls = 0

        def on_bar(self, bar):
            self.calls += 1

    strategy = Strategy()
    ctx._strategy = strategy
    for tag, data in sup.delivered:
        ctx.on_event(tag, data)
    assert strategy.calls == 0
    assert len(ctx.history("600000.SH")) == 30
    live = Bar("600000.SH", Frequency.D1, datetime(2026, 11, 6), 10, 10, 10, 10, 100, 1000)
    ctx.on_event("bar", bar_to_dict(live))
    assert strategy.calls == 1


def test_ipc_order_id_survives_riskgate_and_duplicate_intent_is_not_resubmitted(tmp_path):
    from quantdesk.config import Settings
    from quantdesk.core.events import EventBus
    from quantdesk.services.strategy_mgr import StrategyManager
    from quantdesk.services.trading import TradingService
    from tests.test_realtime_loop import _NoHub

    db = init_db(tmp_path / "intent.db")
    bus = EventBus()
    trading = TradingService(db_path=db, bus=bus, hub=_NoHub(),
                             settings=Settings(simulation_only=True, data_dir=tmp_path))
    trading.start()
    manager = StrategyManager(db_path=db, event_bus=bus, trading=trading)
    client = _Client()
    context = IPCStrategyContext(client, "s")
    ack = context.buy("600000.SH", 100, price=10)
    try:
        tag, data = client.sent[-1]
        manager._route_intent(tag, data)
        assert repo.get_order(ack.order_id, db_path=db).strategy_id == "s"
        seen = []
        submit = trading.gate.submit
        trading.gate.submit = lambda order: (seen.append(order), submit(order))[1]
        manager._route_intent(tag, data)
        assert seen == []
        assert len(repo.list_orders(db_path=db)) == 1
    finally:
        trading.stop()
