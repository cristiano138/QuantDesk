"""实时路由单测：主源单一 + 订阅引用计数 + 重连重放 + 回调隔离。

对应 D14 的顺延 DoD：两个策略订阅同一标的外部只订一份、退订计数归零才真退订、
策略停止/崩溃都要注销引用、重连后订阅自动重放。
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantdesk.adapters.base.datafeed import DataFeedAdapter
from quantdesk.adapters.data.federated import FederatedFeed
from quantdesk.core.enums import FeedCapability
from quantdesk.core.errors import FeedCapabilityError
from quantdesk.core.models import Tick


def _tick(symbol: str, last: float = 10.0) -> Tick:
    from datetime import datetime

    return Tick(symbol=symbol, dt=datetime(2026, 11, 4, 9, 30), last=last, open=last,
                high=last, low=last, volume=100, amount=1000)


class RealtimeStub(DataFeedAdapter):
    """记录外部订阅行为的假主源。"""

    name = "rt_stub"
    capabilities = frozenset({FeedCapability.REALTIME_TICK})

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.callback = None
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def health_check(self) -> bool:
        return self.connected

    def get_history(self, symbol, freq, start, end, adjust=None) -> pd.DataFrame:
        return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume", "amount"])

    def subscribe_realtime(self, symbols, callback) -> None:
        self.subscribed.extend(symbols)
        self.callback = callback

    def unsubscribe_realtime(self, symbols) -> None:
        self.unsubscribed.extend(symbols)

    def get_financial_data(self, symbol, report_type="income") -> pd.DataFrame:
        raise FeedCapabilityError("stub 无财务")


@pytest.fixture
def fed():
    rt = RealtimeStub()
    f = FederatedFeed(history_feeds=[], financial_feeds=[], realtime_feed=rt)
    f.connect()
    return f, rt


def test_no_realtime_source_gives_actionable_error():
    f = FederatedFeed(history_feeds=[])
    with pytest.raises(FeedCapabilityError) as ei:
        f.subscribe_realtime(["600000.SH"], lambda t: None)
    assert "miniQMT" in str(ei.value)                    # 告诉用户去哪接，而不是「未实现」


def test_two_subscribers_same_symbol_causes_one_external_subscription(fed):
    f, rt = fed
    got_a, got_b = [], []
    f.subscribe_realtime(["600000.SH"], got_a.append, subscriber="s1")
    f.subscribe_realtime(["600000.SH"], got_b.append, subscriber="s2")
    assert rt.subscribed == ["600000.SH"]               # 对外只一份
    assert f.subscriber_count("600000.SH") == 2
    rt.callback(_tick("600000.SH", 10.5))               # 主源推一条
    assert got_a == got_b and len(got_a) == 1           # 分发给两个订阅方


def test_unsubscribe_only_reaches_source_when_refcount_hits_zero(fed):
    f, rt = fed
    f.subscribe_realtime(["600000.SH", "000001.SZ"], lambda t: None, subscriber="s1")
    f.subscribe_realtime(["600000.SH"], lambda t: None, subscriber="s2")
    f.unsubscribe_realtime(["600000.SH"], subscriber="s1")
    assert rt.unsubscribed == []                        # s2 还在，不能真退订
    f.unsubscribe_realtime(["600000.SH"], subscriber="s2")
    assert rt.unsubscribed == ["600000.SH"]
    assert f.subscriber_count("600000.SH") == 0
    assert "000001.SZ" in f.active_symbols()


def test_drop_subscriber_cleans_every_symbol_of_that_strategy(fed):
    f, rt = fed
    f.subscribe_realtime(["600000.SH", "000001.SZ", "600519.SH"],
                         lambda t: None, subscriber="crash_me")
    f.subscribe_realtime(["600000.SH"], lambda t: None, subscriber="alive")
    f.drop_subscriber("crash_me")                       # 崩溃/停止路径
    assert rt.unsubscribed == ["000001.SZ", "600519.SH"]  # 只有 600000 还被 alive 引用
    assert f.active_symbols() == ["600000.SH"]


def test_resync_replays_full_active_symbol_set(fed):
    f, rt = fed
    f.subscribe_realtime(["600000.SH"], lambda t: None, subscriber="s1")
    f.subscribe_realtime(["000001.SZ"], lambda t: None, subscriber="s2")
    rt.subscribed.clear()
    f.resync_realtime()                                 # 重连后重放
    assert sorted(rt.subscribed) == ["000001.SZ", "600000.SH"]


def test_one_subscriber_raising_does_not_break_others(fed):
    f, rt = fed
    good: list[Tick] = []

    def bad(_t):
        raise RuntimeError("策略回调炸了")

    f.subscribe_realtime(["600000.SH"], bad, subscriber="s1")
    f.subscribe_realtime(["600000.SH"], good.append, subscriber="s2")
    rt.callback(_tick("600000.SH"))                     # 不应向外抛
    assert len(good) == 1


def test_capabilities_include_realtime_and_history_union(fed):
    f, rt = fed
    assert FeedCapability.REALTIME_TICK in f.capabilities

    class Hist(DataFeedAdapter):
        name = "h"
        capabilities = frozenset({FeedCapability.HISTORY_BAR})

        def connect(self) -> None: ...
        def disconnect(self) -> None: ...
        def get_history(self, *a, **k) -> pd.DataFrame:
            return pd.DataFrame()
        def subscribe_realtime(self, symbols, callback) -> None: ...
        def get_financial_data(self, symbol, report_type="income") -> pd.DataFrame:
            return pd.DataFrame()

    f2 = FederatedFeed(history_feeds=[Hist()], realtime_feed=None)
    assert FeedCapability.REALTIME_TICK not in f2.capabilities
    assert FeedCapability.HISTORY_BAR in f2.capabilities


def test_disconnect_clears_subscription_state(fed):
    f, rt = fed
    f.subscribe_realtime(["600000.SH"], lambda t: None, subscriber="s1")
    f.disconnect()
    assert f.active_symbols() == []
    assert rt.connected is False
