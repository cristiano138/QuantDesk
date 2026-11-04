"""实时链路集成测试：订阅上行 → 行情下行 → 策略下单意图 → 过闸 → 回报回投。

这一条链路是「同一份策略代码跑通三种模式」在实时模式下的落点，此前是断的：
策略的 ctx.buy() 只被广播成事件、从没进过 RiskGate，ctx.subscribe() 也没人接。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest

from quantdesk.core.enums import FeedCapability, Frequency, OrderStatus
from quantdesk.core.events import Event, EventBus, EventType
from quantdesk.core.models import Order, Position, Tick, Trade
from quantdesk.engine.runner import IPCStrategyContext, bar_from_dict
from quantdesk.services.realtime import RealtimePump


def _tick(symbol="600000.SH", last=10.0, dt=None, vol=1000.0) -> Tick:
    return Tick(symbol=symbol, dt=dt or datetime(2026, 11, 4, 9, 30, 10), last=last,
                open=last, high=last, low=last, volume=vol, amount=last * vol,
                bids=[(last - 0.01, 100)], asks=[(last + 0.01, 100)])


class StubFeed:
    """最小实时源：记录订阅、由测试手动推 tick。"""

    name = "rt_stub"
    capabilities = frozenset({FeedCapability.REALTIME_TICK})

    def __init__(self) -> None:
        self.subs: dict[str, list] = {}
        self.callbacks: list = []
        self.dropped: list[str] = []

    def subscribe_realtime(self, symbols, callback, subscriber="default") -> None:
        self.subs.setdefault(subscriber, []).extend(symbols)
        self.callbacks.append(callback)

    def unsubscribe_realtime(self, symbols, subscriber="default") -> None:
        self.dropped.extend(symbols)

    def drop_subscriber(self, subscriber: str) -> None:
        self.subs.pop(subscriber, None)

    def push(self, tick: Tick) -> None:
        for cb in self.callbacks:
            cb(tick)


class StubHub:
    def __init__(self, feed) -> None:
        self.feed = feed
        self.channel = ""

    def federated(self):
        return self.feed

    def realtime_channel_name(self) -> str:
        return "rt_stub"


class StubSupervisor:
    def __init__(self, sid: str) -> None:
        self.strategy_id = sid
        self.delivered: list[tuple[str, dict]] = []
        self.subscriptions: set[str] = set()

    def deliver(self, tag: str, data: dict) -> None:
        self.delivered.append((tag, data))


class StubManager:
    def __init__(self) -> None:
        self.supervisors: dict[str, StubSupervisor] = {}

    def supervisor(self, sid: str):
        return self.supervisors.get(sid)


class StubMarket:
    def __init__(self) -> None:
        self.ticks: list[Tick] = []

    def watchlist(self):
        return [{"symbol": "600000.SH"}]

    def on_tick(self, tick: Tick) -> None:
        self.ticks.append(tick)


def _pump(tmp_path, monkeypatch):
    feed = StubFeed()
    market, manager = StubMarket(), StubManager()
    sup = StubSupervisor("dual_ma")
    manager.supervisors["dual_ma"] = sup
    from quantdesk.config import Settings

    monkeypatch.setattr("quantdesk.config.get_settings",
                        lambda: Settings(simulation_only=True, data_dir=tmp_path))
    pump = RealtimePump(hub=StubHub(feed), market=market, manager=manager,
                        bus=EventBus(), store=_Store(tmp_path))
    return pump, feed, market, manager, sup


class _Store:
    """ParquetStore 桩：给出 N 根日线用于历史种子。"""

    def __init__(self, root, n=30) -> None:
        self.n = n
        self.reads: list[tuple[str, str]] = []

    def read(self, symbol, freq=Frequency.D1, start=None, end=None, adjust=None) -> pd.DataFrame:
        self.reads.append((symbol, getattr(freq, "value", str(freq))))
        idx = pd.date_range("2026-09-01", periods=self.n, freq="D")
        return pd.DataFrame({"dt": idx, "open": 10.0, "high": 10.5, "low": 9.8,
                             "close": 10.2, "volume": 1000.0, "amount": 10200.0})


def test_register_strategy_subscribes_and_seeds_history(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"], freq="1d")
    assert feed.subs["strategy:dual_ma"] == ["600000.SH"]
    seeded = [d for tag, d in sup.delivered if tag == "bar"]
    assert len(seeded) == 30                                  # 历史种子已灌入（启动即可决策）
    assert seeded[0]["symbol"] == "600000.SH"


def test_tick_fans_out_to_market_and_strategy_bar(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"], freq="1d")
    sup.delivered.clear()
    feed.push(_tick(last=10.4))
    assert market.ticks and market.ticks[0].last == 10.4      # 面板快照
    bars = [bar_from_dict(d) for tag, d in sup.delivered if tag == "bar"]
    assert len(bars) == 1 and bars[0].close == 10.4
    assert Frequency(bars[0].freq) is Frequency.D1     # 线格式走 .value，不是 "Frequency.D1"


def test_same_bar_is_upserted_not_appended_in_child_buffer(tmp_path, monkeypatch):
    """子进程侧：实时推的是「形成中的当前 bar」，重复 dt 必须覆盖而不是刷进缓冲。"""
    class Client:
        def __init__(self):
            self.sent = []

        def send(self, tag, data):
            self.sent.append((tag, data))

    ctx = IPCStrategyContext(Client(), "dual_ma")
    dt = datetime(2026, 11, 4, 9, 30)
    for price in (10.0, 10.1, 10.2):
        ctx.on_event("bar", {"symbol": "600000.SH", "freq": "1d", "dt": dt.isoformat(),
                             "open": 10.0, "high": price, "low": 9.9, "close": price,
                             "volume": 100.0, "amount": price * 100})
    hist = ctx.history("600000.SH")
    assert len(hist) == 1                                     # 三推一根（dt 相同）
    assert float(hist.iloc[-1]["close"]) == 10.2
    assert float(hist.iloc[-1]["high"]) == 10.2


def test_minute_rollover_closes_previous_bar(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"], freq="1m")
    sup.delivered.clear()
    t0 = datetime(2026, 11, 4, 9, 30, 5)
    feed.push(_tick(last=10.0, dt=t0))
    feed.push(_tick(last=10.3, dt=t0 + timedelta(seconds=20)))   # 同一分钟 → 更新
    feed.push(_tick(last=10.9, dt=t0 + timedelta(seconds=70)))   # 跨分钟 → 收上一根
    bars = [bar_from_dict(d) for tag, d in sup.delivered if tag == "bar"]
    assert bars[0].dt.hour == 9 and bars[0].dt.minute == 30 and bars[0].dt.second == 0
    assert bars[-1].dt.minute == 31 and bars[-1].dt.second == 0
    assert len({b.dt for b in bars}) == 2                        # 两根，不是同一根刷到底


def test_unregister_drops_subscription_and_stops_fanout(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.register_strategy("dual_ma", ["600000.SH"], freq="1d")
    pump.unregister_strategy("dual_ma")
    assert "strategy:dual_ma" not in feed.subs                  # 引用计数交给 FederatedFeed
    sup.delivered.clear()
    feed.push(_tick())
    assert sup.delivered == []                                  # 不再投给已停的策略


def test_watchlist_symbols_are_watched_on_start(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.start()
    assert feed.subs.get("panel") == ["600000.SH"]
    pump.stop()


def test_order_and_trade_reports_are_delivered_to_owning_strategy(tmp_path, monkeypatch):
    pump, feed, market, manager, sup = _pump(tmp_path, monkeypatch)
    pump.start()
    o = Order(order_id="OD-1", symbol="600000.SH", side="buy", price=10.0, volume=100,
              status=OrderStatus.SUBMITTED, strategy_id="dual_ma")
    pump._on_order_event(Event(EventType.ORDER, data=o))
    assert pump._on_trade_event is not None
    t = Trade(trade_id="T-1", order_id="OD-1", symbol="600000.SH", side="buy",
              price=10.0, volume=100, amount=1000.0, traded_at=1)
    pump.trading = _TradingWithOrder(o)
    pump._on_trade_event(Event(EventType.TRADE, data=t))
    tags = [tag for tag, _ in sup.delivered]
    assert "order" in tags and "trade" in tags


class _TradingWithOrder:
    def __init__(self, order) -> None:
        self.order = order
        self.matched = []

    def get_order(self, oid):
        return self.order if oid == self.order.order_id else None

    class _P:
        def on_tick(self, tick): ...

    @property
    def paper(self):
        return self._P()


# ---------- 意图过闸（真 RiskGate + 真 PaperBroker） ----------
def test_strategy_order_intent_goes_through_riskgate(tmp_path, monkeypatch):
    from quantdesk.adapters.brokers.paper_broker import PaperBroker
    from quantdesk.config import Settings
    from quantdesk.engine.riskgate import RiskGate
    from quantdesk.services.strategy_mgr import StrategyManager
    from quantdesk.services.trading import TradingService
    from quantdesk.store.db import init_db, reset_engine

    reset_engine()
    db = init_db(tmp_path / "loop.db")
    bus = EventBus()
    monkeypatch.setattr("quantdesk.config.get_settings",
                        lambda: Settings(simulation_only=True, data_dir=tmp_path))

    class _Manager(StrategyManager):
        """绕开策略子进程，直接调意图路由（真实子进程由 supervisor 测试覆盖）。"""

    trading = TradingService(initial_cash=100_000, db_path=db, bus=bus,
                             hub=_NoHub())
    trading.start()
    mgr = _Manager(db_path=db, event_bus=bus, trading=trading)

    seen: list[Order] = []
    bus.subscribe(EventType.ORDER, lambda e: seen.append(e.data))
    mgr._route_intent("order_intent", {"symbol": "600000.SH", "side": "buy",
                                       "volume": 100, "price": 10.0,
                                       "order_type": "limit", "strategy_id": "dual_ma"})
    assert seen, "意图必须产生订单事件"
    o = seen[0]
    assert o.strategy_id == "dual_ma"
    assert o.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED, OrderStatus.PART_FILLED)
    assert trading.list_orders()                                  # 已落库（先落库后广播）
    reset_engine()


class _NoHub:
    def live_broker(self):
        return None

    def stop(self):
        pass


def test_illegal_intent_is_rejected_not_silently_dropped(tmp_path, monkeypatch):
    from quantdesk.config import Settings
    from quantdesk.services.strategy_mgr import StrategyManager
    from quantdesk.services.trading import TradingService
    from quantdesk.store.db import init_db, reset_engine

    reset_engine()
    db = init_db(tmp_path / "loop2.db")
    monkeypatch.setattr("quantdesk.config.get_settings",
                        lambda: Settings(simulation_only=True, data_dir=tmp_path))
    events = []
    bus = EventBus()
    bus.subscribe(EventType.LOG, lambda e: events.append(e.data["message"]))
    trading = TradingService(initial_cash=100_000, db_path=db, bus=bus, hub=_NoHub())
    trading.start()
    mgr = StrategyManager(db_path=db, event_bus=bus, trading=trading)
    mgr._route_intent("order_intent", {"symbol": "600000.SH", "side": "buy",
                                       "volume": 37, "price": 10.0,
                                       "order_type": "limit", "strategy_id": "x"})
    assert mgr.trading.list_orders()[0].status is OrderStatus.REJECTED   # 碎股被闸拦下
    assert "100 股整数倍" in mgr.trading.list_orders()[0].message
    reset_engine()
