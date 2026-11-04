"""掘金（gm）交易通道适配器离线单测。

覆盖宪法与接入卡要求的行为：请求投递进终端线程执行（线程 id 断言）、token 脱敏、
仿真/实盘 endpoint 由参数切换（不写死）、限价/市价参数映射、下单失败抛 BrokerError、
回报状态映射（部成/已撤/废单）、cancel 受理语义、available 取可用量（T+1）、
health_check 断线可探测、重连后刷新对账、SDK 版本不支持抛结构化错误、超时不永久阻塞。
全程走 tests/mocks/gm 假 SDK，无需掘金终端或网络。
"""

from __future__ import annotations

import logging
import threading

import pytest

from quantdesk.adapters.brokers.gm_broker import GmBroker, _mask
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import AdapterError, BrokerError, ConnectionLostError
from quantdesk.core.events import Event, EventBus, EventType
from quantdesk.core.models import Order
from tests.mocks import fake_sdk


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def on(self, e: Event) -> None:
        self.events.append(e)

    def of(self, etype: EventType) -> list[Event]:
        return [e for e in self.events if e.type == etype]


def _connect(broker: GmBroker, endpoint: str = "ws://sim.myquant.cn:9000") -> None:
    broker.connect(token="abcdef123456", account_id="ACC-SIM-1",
                   endpoint=endpoint, mode="sim")


def _limit_order(oid: str = "L-1", price: float = 10.5,
                 otype: OrderType = OrderType.LIMIT) -> Order:
    return Order(order_id=oid, symbol="600000.SH", side=Side.BUY, price=price,
                 volume=100, order_type=otype, status=OrderStatus.SUBMITTED)


# ---------- 脱敏工具 ----------
def test_mask_keeps_only_first_four():
    assert _mask("abcdef123456") == "abcd***"
    assert _mask("abc") == "****"
    assert _mask("") == "<empty>"


# ---------- 终端线程模型：请求确实投递进终端线程执行 ----------
def test_place_order_runs_on_terminal_thread():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        bus = EventBus()
        b = GmBroker(event_bus=bus)
        _connect(b)
        main = threading.get_ident()
        term = b._term_thread.ident
        assert term is not None and term != main
        b.place_order(_limit_order())
        call = next(c for c in gm.CALLS if c["method"] == "order_stock")
        assert call["thread"] == term          # 掘金接口只在终端线程被调用
        assert call["thread"] != main
        b.disconnect()


def test_queries_run_on_terminal_thread():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        term = b._term_thread.ident
        b.get_account()
        b.get_position()
        b.get_orders()
        b.get_trades()
        for m in ("get_account_snapshot", "get_position", "get_orders",
                  "get_execution_reports"):
            c = next(x for x in gm.CALLS if x["method"] == m)
            assert c["thread"] == term
        b.disconnect()


# ---------- 超时：终端线程卡住时调用方按超时抛错，不永久阻塞 ----------
def test_request_timeout_raises_not_block_forever():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker(request_timeout=0.3)
        _connect(b)
        ev = threading.Event()
        gm.SCRIPT["block_on"] = ev             # 后续 SDK 调用挂起
        with pytest.raises(ConnectionLostError):
            b.place_order(_limit_order())
        assert b._lost is True                 # 超时按断线处理，可被 health 探测
        ev.set()
        b.disconnect()


# ---------- token 脱敏（日志只前 4 位 + ***） ----------
def test_token_never_logged_in_full(caplog):
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        with caplog.at_level(logging.INFO, logger="quantdesk.adapters.brokers.gm_broker"):
            _connect(b)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "abcdef123456" not in joined     # 全文绝不出现
        assert "abcd***" in joined
        b.disconnect()


# ---------- 仿真/实盘 endpoint 由参数切换，绝不写死 ----------
def test_endpoint_and_account_passed_through():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b, endpoint="ws://live.myquant.cn:9000")
        init = next(c for c in gm.CALLS if c["method"] == "__init__")
        assert init["kwargs"]["endpoint"] == "ws://live.myquant.cn:9000"
        assert init["kwargs"]["account_id"] == "ACC-SIM-1"
        b.disconnect()


def test_connect_requires_endpoint_and_token():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        with pytest.raises(BrokerError):
            b.connect(token="", account_id="x", endpoint="ws://y")
        with pytest.raises(BrokerError):
            b.connect(token="t", account_id="x", endpoint="")


# ---------- 限价/市价参数映射 ----------
def test_limit_vs_market_param_mapping():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        b.place_order(_limit_order(otype=OrderType.LIMIT, price=10.5))
        lim = next(c for c in gm.CALLS if c["method"] == "order_stock")["kwargs"]
        assert lim["order_type"] == "limit"
        assert lim["price_type"] == "limit"
        assert lim["price"] == 10.5
        assert lim["side"] == gm.ORDER_BUY
        assert lim["position"] == gm.POSITION_LONG
        b.place_order(_limit_order(otype=OrderType.MARKET))
        mk = [c for c in gm.CALLS if c["method"] == "order_stock"][-1]["kwargs"]
        assert mk["order_type"] == "market"
        assert mk["price_type"] == "latest"
        assert mk["price"] == 0.0              # 市价单不带限价
        b.disconnect()


def test_symbol_converted_to_gm_format():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        b.place_order(Order(order_id="S-1", symbol="000001.SZ", side=Side.SELL,
                            price=11.0, volume=200, order_type=OrderType.LIMIT))
        kw = next(c for c in gm.CALLS if c["method"] == "order_stock")["kwargs"]
        assert kw["symbol"] == "SZSE.000001"   # 内部码 → gm 源格式
        assert kw["side"] == gm.ORDER_SELL
        b.disconnect()


# ---------- 下单失败抛 BrokerError（含返回码与 errmsg） ----------
def test_place_order_error_code_raises_broker_error():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["place_code"] = 1001
        gm.SCRIPT["place_msg"] = "资金不足"
        with pytest.raises(BrokerError) as ei:
            b.place_order(_limit_order())
        assert "1001" in str(ei.value) and "资金不足" in str(ei.value)
        b.disconnect()


def test_place_order_fills_broker_id_and_publishes():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        bus = EventBus()
        col = Collector()
        bus.subscribe(EventType.ORDER, col.on)
        b = GmBroker(event_bus=bus)
        _connect(b)
        gm.SCRIPT["broker_order_id"] = "GM-XYZ-9"
        placed = b.place_order(_limit_order("L-9"))
        assert placed.broker_order_id == "GM-XYZ-9"
        assert placed.status == OrderStatus.SUBMITTED
        assert len(col.of(EventType.ORDER)) == 1
        assert col.of(EventType.ORDER)[0].data.broker_order_id == "GM-XYZ-9"
        b.disconnect()


# ---------- 回报状态映射（部成/已撤/废单/成交）经工作线程广播 ----------
def _wait_reports(b: GmBroker, bus_col: Collector, etype: EventType, n: int = 1,
                 timeout: float = 3.0) -> None:
    import time
    t0 = time.monotonic()
    while len(bus_col.of(etype)) < n:
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"等待 {etype} 回报超时（{n} 条）")
        time.sleep(0.02)


@pytest.mark.parametrize("raw,expected", [
    (6, OrderStatus.PART_FILLED),
    (7, OrderStatus.FILLED),
    (3, OrderStatus.CANCELLED),
    (5, OrderStatus.REJECTED),
    (11, OrderStatus.FAILED),
    (2, OrderStatus.SUBMITTED),
])
def test_order_status_mapping(raw, expected):
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        bus = EventBus()
        col = Collector()
        bus.subscribe(EventType.ORDER, col.on)
        b = GmBroker(event_bus=bus)
        _connect(b)
        gm.SCRIPT["broker_order_id"] = "GM-BR-1"
        b.place_order(_limit_order("L-map"))
        # 注入回报：掘金推送线程调用回调，适配器只入队，工作线程归一化后广播
        b._session.simulate_order_status(
            {"order_id": "GM-BR-1", "symbol": "SHSE.600000", "side": 2,
             "status": raw, "volume": 100, "price": 10.5, "filled_volume": 50})
        _wait_reports(b, col, EventType.ORDER, n=2)
        last = col.of(EventType.ORDER)[-1].data
        assert last.status == expected
        assert last.order_id == "L-map"        # 经 broker_order_id 映射回本地 order_id
        b.disconnect()


def test_execution_report_becomes_trade():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        bus = EventBus()
        col = Collector()
        bus.subscribe(EventType.TRADE, col.on)
        b = GmBroker(event_bus=bus)
        _connect(b)
        gm.SCRIPT["broker_order_id"] = "GM-BR-2"
        b.place_order(_limit_order("L-tr"))
        b._session.simulate_execution_report(
            {"order_id": "GM-BR-2", "exec_id": "EX-1", "symbol": "SHSE.600000",
             "side": 2, "price": 10.5, "volume": 100, "amount": 1050.0,
             "timestamp": 1_700_000_000})
        _wait_reports(b, col, EventType.TRADE)
        tr = col.of(EventType.TRADE)[0].data
        assert tr.order_id == "L-tr"
        assert tr.symbol == "600000.SH"        # gm 源 → 内部码
        assert tr.price == 10.5 and tr.volume == 100
        assert tr.trade_id == "EX-1"
        b.disconnect()


def test_unrecognized_status_conservatively_submitted(caplog):
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        with caplog.at_level(logging.WARNING,
                             logger="quantdesk.adapters.brokers.gm_broker"):
            st = b._map_status(9999)
        assert st == OrderStatus.SUBMITTED
        assert "未识别" in " ".join(r.getMessage() for r in caplog.records)
        b.disconnect()


# ---------- cancel 受理语义 ----------
def test_cancel_order_accepted_true():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["broker_order_id"] = "GM-C-1"
        b.place_order(_limit_order("L-c"))
        assert b.cancel_order("L-c") is True
        c = next(x for x in gm.CALLS if x["method"] == "cancel_order")
        assert c["kwargs"]["order_id"] == "GM-C-1"     # 撤的是掘金侧 id
        b.disconnect()


def test_cancel_order_rejected_returns_false():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["cancel_code"] = 500
        gm.SCRIPT["broker_order_id"] = "GM-C-2"
        b.place_order(_limit_order("L-c2"))
        assert b.cancel_order("L-c2") is False         # 未受理，不抛（最终状态以回报为准）
        b.disconnect()


# ---------- available 取可用量（T+1） ----------
def test_position_available_from_can_use_volume():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["positions"] = [{"symbol": "SHSE.600519", "volume": 300,
                                  "can_use_volume": 100, "vwap": 1700.0,
                                  "market_value": 510000.0}]
        ps = b.get_position()
        assert ps[0].symbol == "600519.SH"
        assert ps[0].volume == 300
        assert ps[0].available == 100           # T+1：可用 = can_use_volume
        assert ps[0].avg_cost == 1700.0
        b.disconnect()


def test_account_fields_normalized():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["account"] = [{"balance": 200000.0, "available": 150000.0,
                                "market_value": 50000.0, "frozen": 5000.0}]
        acc = b.get_account()
        assert (acc.total_asset, acc.cash, acc.market_value, acc.frozen) == \
            (200000.0, 150000.0, 50000.0, 5000.0)
        b.disconnect()


# ---------- health_check 断线可探测 ----------
def test_health_check_ok_when_connected():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        assert b.health_check() is True
        b.disconnect()


def test_health_check_detects_query_failure():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["query_fail"] = "终端掉线"
        assert b.health_check() is False        # 轻量查询失败 → 探活为 False
        b.disconnect()


def test_disconnected_callback_marks_lost_and_blocks_order():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        b._session.simulate_disconnected("掘金终端被关闭")
        assert b._lost is True
        assert b.health_check() is False
        with pytest.raises(ConnectionLostError):
            b.place_order(_limit_order())       # fail-closed，不排队不重试
        b.disconnect()


# ---------- 重连成功后刷新对账 ----------
def test_reconnect_refreshes_reconcile():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        gm.SCRIPT["query_fail"] = "掉线"
        b._lost = True
        # 重连：恢复查询，reconnect 内部重建会话并刷新账户/持仓/委托
        gm.SCRIPT["query_fail"] = None
        before = len([c for c in gm.CALLS if c["method"] == "create_context"])
        b.reconnect()
        assert b._lost is False
        # 刷新对账：get_account/get_position/get_orders 各被调用一次（终端线程）
        for m in ("get_account_snapshot", "get_position", "get_orders"):
            assert any(c["method"] == m for c in gm.CALLS)
        assert before == 0                      # 新版接口走 Terminal，不建 context
        snap = b.snapshot_for_reconcile()
        assert len(snap) == 4
        b.disconnect()


# ---------- SDK 版本不支持：结构化错误，绝不乱调接口 ----------
def test_unsupported_sdk_version_raises_structured_error():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        # 抹掉两套接口，模拟不受支持的 SDK 版本
        saved = (gm.Terminal, gm.create_context)
        del gm.Terminal
        del gm.create_context
        try:
            with pytest.raises(AdapterError) as ei:
                _connect(b)
            assert "不受支持" in str(ei.value)
            assert "docs/integration/gm.md" in str(ei.value)
        finally:
            gm.Terminal, gm.create_context = saved


def test_context_surface_used_when_terminal_absent():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        saved = gm.Terminal
        del gm.Terminal                          # 旧版 SDK：无 Terminal，只有 create_context
        try:
            b = GmBroker()
            _connect(b)
            assert b._surface == "context"
            assert any(c["method"] == "create_context" for c in gm.CALLS)
            # 旧接口命名空间在 context.rest.api，下单仍走该命名空间且在终端线程
            term = b._term_thread.ident
            b.place_order(_limit_order())
            oc = next(c for c in gm.CALLS if c["method"] == "order_stock")
            assert oc["thread"] == term
            b.disconnect()
        finally:
            gm.Terminal = saved


# ---------- poll() 空实现（回调型通道） ----------
def test_poll_is_noop():
    with fake_sdk("gm"):
        import gm.api as gm
        gm.reset()
        b = GmBroker()
        _connect(b)
        n = len(gm.CALLS)
        b.poll()
        assert len(gm.CALLS) == n
        b.disconnect()
