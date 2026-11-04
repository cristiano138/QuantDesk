"""ChannelHub 单测：主流股票软件通道的运行时装配与 fail-closed 语义。

不打真实 SDK：向注册表注入桩通道类，验证的是「装配中枢」的决策——
谁进链、谁被跳过、连不上时返回什么、实盘通道缺位时是否 fail-closed。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pandas as pd
import pytest

import quantdesk.adapters as reg
from quantdesk.adapters.base.broker import BrokerAdapter
from quantdesk.adapters.base.datafeed import DataFeedAdapter
from quantdesk.core.enums import FeedCapability, OrderStatus
from quantdesk.core.errors import ConnectionLostError
from quantdesk.core.events import EventBus
from quantdesk.core.models import Account, Order, Position, Tick, Trade
from quantdesk.services.channels import ChannelHub, _instantiate


class _Clock:
    """假配置对象：字段可变，模拟设置页热改。"""

    def __init__(self, **kw) -> None:
        self.simulation_only = True
        self.broker_channel = "paper"
        self.feed_priority_history = "hist_a,hist_b"
        self.feed_priority_realtime = "rt_a,rt_b"
        self.feed_priority_financial = "fin_a"
        self.__dict__.update(kw)


class HistFeed(DataFeedAdapter):
    name = "hist_a"
    label = "历史源A"
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL})

    def __init__(self, **kw) -> None:
        self.connected = False
        self.kw = kw

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_history(self, symbol, freq, start, end, adjust=None) -> pd.DataFrame:
        return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume", "amount"])

    def subscribe_realtime(self, symbols, callback) -> None:
        raise NotImplementedError

    def get_financial_data(self, symbol, report_type="income") -> pd.DataFrame:
        return pd.DataFrame()

    def health_check(self) -> bool:
        return self.connected


class BadFeed(HistFeed):
    name = "hist_b"
    label = "连不上的源"

    def connect(self) -> None:
        raise ConnectionLostError("miniQMT 未登录，请先登录客户端")


class RtFeed(DataFeedAdapter):
    name = "rt_a"
    label = "实时源A"
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.REALTIME_TICK})
    registry: list["RtFeed"] = []

    def __init__(self, **kw) -> None:
        self.connected = False
        self.watchdog = SimpleNamespace(health=SimpleNamespace(to_dict=lambda: {"state": "ok"}))
        RtFeed.registry.append(self)

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def get_history(self, symbol, freq, start, end, adjust=None) -> pd.DataFrame:
        return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume", "amount"])

    def subscribe_realtime(self, symbols, callback) -> None:
        self.symbols = list(symbols)

    def unsubscribe_realtime(self, symbols) -> None:
        self.symbols = [s for s in self.symbols if s not in symbols]

    def get_financial_data(self, symbol, report_type="income") -> pd.DataFrame:
        raise NotImplementedError

    def health_check(self) -> bool:
        return self.connected


class NoRtFeed(RtFeed):
    """声明了实时能力但名字叫 rt_b——用于验证链上顺序与能力过滤。"""
    name = "rt_b"
    capabilities = frozenset({FeedCapability.HISTORY_BAR})


class LiveBroker(BrokerAdapter):
    name = "qmt"
    is_paper = False
    instances: list["LiveBroker"] = []

    def __init__(self, event_bus=None, db_path=None) -> None:
        self.bus = event_bus
        self.db_path = db_path
        self.creds: dict = {}
        self.connected = False
        LiveBroker.instances.append(self)

    def connect(self, **credentials) -> None:
        if "userdata_path" not in credentials:
            raise ConnectionLostError("缺少 userdata_path")
        self.creds = credentials
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def place_order(self, order: Order) -> Order:
        return order

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_position(self, symbol=None) -> list[Position]:
        return []

    def get_account(self) -> Account:
        return Account(total_asset=1, cash=1, market_value=0)

    def get_orders(self, status=None) -> list[Order]:
        return []

    def get_trades(self, start=None) -> list[Trade]:
        return []

    def health_check(self) -> bool:
        return self.connected


@pytest.fixture
def stub_registry(monkeypatch):
    """把桩通道塞进注册表（monkeypatch 用例结束自动还原）。"""
    reg._scan()
    for cls, kind in ((HistFeed, "data"), (BadFeed, "data"), (RtFeed, "data"),
                      (NoRtFeed, "data"), (LiveBroker, "broker")):
        from quantdesk.adapters.base.optional import meta_of

        monkeypatch.setitem(reg._ALL, cls.name, cls)
        monkeypatch.setitem(reg._META, cls.name, meta_of(cls, kind))
    RtFeed.registry.clear()
    LiveBroker.instances.clear()
    return


def _hub(settings, monkeypatch, secrets_map=None):
    monkeypatch.setattr("quantdesk.services.channels.get_secret",
                        lambda key, default="": (secrets_map or {}).get(key, default))
    return ChannelHub(settings=settings, bus=EventBus())


# ---------- 行情链 ----------
def test_history_chain_skips_unconnectable_and_records_reason(stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    assert hub.history_chain() == ["hist_a"]
    assert "hist_b" in hub.feed_errors()
    assert "miniQMT 未登录" in hub.feed_errors()["hist_b"]     # 原因必须能给用户看
    hub.stop()


def test_feed_instances_are_reused_not_rebuilt(stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    a1, a2 = hub.feed("hist_a"), hub.feed("hist_a")
    assert a1 is a2
    hub.stop()


def test_unreachable_feed_is_cooldown_not_reconnected_every_request(stub_registry, monkeypatch):
    """连不上的源每次都重连 = 日志刷满 + 撞对端限流；冷却期只留原因，显式探活才重试。"""
    attempts: list[int] = []

    class CountingBad(BadFeed):
        def connect(self) -> None:
            attempts.append(1)
            raise ConnectionLostError("miniQMT 未登录")

    monkeypatch.setitem(reg._ALL, "hist_b", CountingBad)
    hub = _hub(_Clock(), monkeypatch)
    assert hub.feed("hist_b") is None
    assert len(attempts) == 1
    for _ in range(5):                                   # 页面轮询/多次外呼
        assert hub.feed("hist_b") is None
    assert len(attempts) == 1                            # 冷却期内不再重连
    assert "miniQMT" in hub.feed_errors()["hist_b"]      # 原因仍然如实可查
    assert hub.feed("hist_b", force=True) is None        # 用户点「测试连接」→ 真试一次
    assert len(attempts) == 2
    hub.stop()


def test_history_chain_uses_cooldown_and_keeps_ready_source(stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    assert hub.history_chain() == ["hist_a"]
    assert hub.history_chain() == ["hist_a"]             # 第二次不因坏源重连而变慢
    hub.stop()


def test_realtime_feed_picks_first_source_with_realtime_capability(
        stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    rt = hub.realtime_feed()
    assert isinstance(rt, RtFeed) and hub.realtime_channel_name() == "rt_a"
    hub.stop()


def test_realtime_feed_returns_none_when_no_realtime_source_ready(
        stub_registry, monkeypatch):
    hub = _hub(_Clock(feed_priority_realtime="rt_b"), monkeypatch)   # rt_b 无实时能力
    assert hub.realtime_feed() is None
    hub.stop()


def test_federated_is_assembled_from_resolved_chain(stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    fed = hub.federated()
    assert [f.name for f in fed.history_feeds] == ["hist_a", "hist_b"] or \
        [f.name for f in fed.history_feeds] == ["hist_a"]
    assert "federated" == fed.name
    hub.stop()


# ---------- 实盘通道 ----------
def test_paper_channel_yields_no_live_broker(stub_registry, monkeypatch):
    hub = _hub(_Clock(broker_channel="paper"), monkeypatch)
    assert hub.live_broker() is None
    assert hub.status()["live_connected"] is False
    hub.stop()


def test_live_channel_without_credentials_fails_closed(stub_registry, monkeypatch):
    hub = _hub(_Clock(broker_channel="qmt", simulation_only=False), monkeypatch, {})
    assert hub.live_broker() is None
    assert "缺凭据" in hub.status()["live_error"]
    assert "qmt.userdata_path" in hub.status()["live_error"]      # 指名缺哪一项
    assert not LiveBroker.instances                                # 压根没尝试连接
    hub.stop()


def test_live_channel_connects_with_vault_credentials(stub_registry, monkeypatch):
    secrets = {"qmt.userdata_path": r"F:\feixuqmt\userdata8", "qmt.account_id": "888000123"}
    hub = _hub(_Clock(broker_channel="qmt", simulation_only=False), monkeypatch, secrets)
    broker = hub.live_broker()
    assert isinstance(broker, LiveBroker)
    assert broker.creds == {"userdata_path": r"F:\feixuqmt\userdata8",
                            "account_id": "888000123"}
    assert hub.live_broker() is broker                             # 复用同一连接
    assert len(LiveBroker.instances) == 1                          # 全局只建一次
    hub.stop()


def test_unregistered_live_channel_is_fail_closed_not_exception(stub_registry, monkeypatch):
    hub = _hub(_Clock(broker_channel="no_such", simulation_only=False), monkeypatch)
    assert hub.live_broker() is None
    assert "不可用" in hub.status()["live_error"]
    hub.stop()


def test_hot_switch_channel_closes_previous_connection(stub_registry, monkeypatch):
    cfg = _Clock(broker_channel="qmt", simulation_only=False)
    secrets = {"qmt.userdata_path": "p", "qmt.account_id": "a"}
    hub = _hub(cfg, monkeypatch, secrets)
    first = hub.live_broker()
    assert first.connected
    cfg.broker_channel = "paper"                                   # 设置页切回模拟盘
    assert hub.live_broker() is None
    assert first.connected is False                                # 旧连接必须真关掉
    hub.stop()


def test_connection_failure_is_throttled_not_retried_every_submit(stub_registry, monkeypatch):
    cfg = _Clock(broker_channel="qmt", simulation_only=False)

    class Broken(LiveBroker):
        def connect(self, **credentials) -> None:
            raise ConnectionLostError("终端未登录")

    monkeypatch.setitem(reg._ALL, "qmt", Broken)
    from quantdesk.adapters.base.optional import meta_of

    monkeypatch.setitem(reg._META, "qmt", meta_of(Broken, "broker"))
    hub = _hub(cfg, monkeypatch, {"qmt.userdata_path": "p", "qmt.account_id": "a"})
    assert hub.live_broker() is None
    tries = len(Broken.instances) if hasattr(Broken, "instances") else len(LiveBroker.instances)
    assert hub.live_broker() is None                               # 第二次落在冷却期
    assert len(LiveBroker.instances) == tries                      # 没有每次都重连
    assert "连接失败" in hub.status()["live_error"]
    hub.stop()


# ---------- 观测 ----------
def test_status_shape_drives_channel_panel(stub_registry, monkeypatch):
    hub = _hub(_Clock(broker_channel="qmt", simulation_only=False), monkeypatch,
               {"qmt.userdata_path": "p", "qmt.account_id": "a"})
    st = hub.status()
    assert st["history_chain"] == ["hist_a"]
    assert st["realtime_active"] == "" or st["realtime_active"] == "rt_a"
    names = {c["name"] for c in st["channels"]}
    assert {"hist_a", "hist_b", "rt_a", "qmt"} <= names
    hub.live_broker()
    hub.realtime_feed()
    st = hub.status()
    assert st["live_connected"] is True
    row = next(c for c in st["channels"] if c["name"] == "qmt")
    assert row["connected"] is True and row["kind"] == "broker"
    rt_row = next(c for c in st["channels"] if c["name"] == "rt_a")
    assert rt_row["channel_health"]["state"] == "ok"               # 通道自检状态透出
    hub.stop()


def test_stop_disconnects_every_feed(stub_registry, monkeypatch):
    hub = _hub(_Clock(), monkeypatch)
    f = hub.feed("hist_a")
    assert f.connected
    hub.stop()
    assert f.connected is False
    assert hub._feeds == {}


# ---------- 构造参数适配 ----------
def test_instantiate_filters_unknown_kwargs():
    class OnlyBus:
        def __init__(self, event_bus=None):
            self.event_bus = event_bus

    obj = _instantiate(OnlyBus, event_bus="B", db_path="D", store="S")
    assert obj.event_bus == "B"

    class Wildcard:
        def __init__(self, **kw):
            self.kw = kw

    assert _instantiate(Wildcard, a=1, b=2).kw == {"a": 1, "b": 2}
