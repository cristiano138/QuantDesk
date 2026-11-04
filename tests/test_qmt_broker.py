"""QMT/miniQMT 交易通道（xtquant.xttrader）离线单测——全程走 tests/mocks/xtquant 假 SDK。

覆盖面（对应宪法适配器合同）：
- 会话：session_id 每次不同、连接/订阅失败抛 ConnectionLostError 且带引导；
- 下单：order_remark=本地 order_id 的回报锚、限价/市价常量与**沪深市价分支**、价格原样透传、
  返回 -1/0/异常一律 BrokerError（禁止静默）；
- 回报：SDK 回调线程只入队，广播在工作线程；委托/成交/委托错误/撤单错误四类归一化；
- 查询：委托状态码映射（含部成/已撤/废单/未识别码）、available 取 can_use_volume（T+1）、
  资金字段映射、查询失败抛 BrokerError；
- 韧性：断线回调与探活失败都能被 health_check 探测并 fail-closed 拒单，重连重放账户订阅；
- 安全：userdata 路径与资金账号在日志与异常文本里一律脱敏（前 4 位 + ***）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import date, datetime

import pytest

from quantdesk.adapters import describe_adapters, list_adapters
from quantdesk.adapters.base.optional import dependency_available
from quantdesk.adapters.brokers.qmt_broker import (
    QmtBroker,
    _qmt_time_to_ms,
    build_status_map,
    map_order_status,
    mask,
)
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import BrokerError, ConnectionLostError
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Account, Order, Position, Trade
from tests.mocks import fake_sdk

_USERDATA = r"D:\国金QMT\userdata"
_ACCOUNT = "1190000088"


def _order(symbol: str = "600000.SH", side: Side = Side.BUY, volume: int = 1000,
           price: float = 10.50, order_type: OrderType = OrderType.LIMIT,
           order_id: str = "QD20260101-0001") -> Order:
    return Order(order_id=order_id, symbol=symbol, side=side, price=price, volume=volume,
                 order_type=order_type, strategy_id="sma_cross",
                 status=OrderStatus.PENDING_CONFIRM)


@contextmanager
def broker_ctx(connect: bool = True, **kwargs):
    """假 SDK + 已连接的 broker（退出必 disconnect，不给后续用例留线程/会话）。

    建会话后立刻停掉守护线程：本文件用 check_once()/recover_once() 单步驱动状态迁移，
    线程若在后台自行重连，会让「断线 fail-closed」的断言变成时序竞态。
    """
    with fake_sdk("xtquant"):
        import xtquant
        from xtquant import xttrader
        xtquant.reset()
        bus = EventBus()
        broker = QmtBroker(event_bus=bus, **kwargs)
        try:
            if connect:
                # 默认给一份可用资金快照：真实客户端登录后 query_stock_asset 必有返回，
                # 探活据此判活；需要「会话已死」的用例自己把它置 None。
                xtquant.DATA["asset"] = xttrader.XtAsset(
                    total_asset=1_000_000.0, cash=1_000_000.0, market_value=0.0,
                    frozen_cash=0.0)
                broker.connect(userdata_path=_USERDATA, account_id=_ACCOUNT)
                broker.watchdog.stop()
            yield broker, xtquant, xttrader, bus
        finally:
            broker.disconnect()


def _wait_for(events: list[Event], etype: EventType, timeout: float = 3.0) -> Event:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hit = [e for e in events if e.type == etype]
        if hit:
            return hit[0]
        threading.Event().wait(0.01)
    raise AssertionError(f"未收到 {etype} 事件（已收到 {[e.type for e in events]}）")


# ==================== 会话建立 ====================
def test_connect_opens_session_registers_callback_and_subscribes_account():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        ctor = xtquant.find("XtQuantTrader")
        methods = [c["method"] for c in xtquant.CALLS]
        sub = xtquant.find("subscribe")
        assert ctor["args"] == (_USERDATA, broker._session_id)
        assert methods.index("register_callback") < methods.index("start") < \
            methods.index("connect") < methods.index("subscribe")
        assert sub["args"] == (_ACCOUNT,)
        assert broker.health_check() is True


def test_session_id_is_unique_per_session_and_reuse_is_impossible():
    """QMT 复用同一 session 会与残留会话冲突——每次建会话（含重连）都必须换新 id。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        first = broker._session_id
        assert xtquant.find("XtQuantTrader")["args"][1] == first
        xtquant.CALLS.clear()
        broker._reconnect()
        sessions = [c["args"][1] for c in xtquant.calls_of("XtQuantTrader")]
        assert len(sessions) == 1 and sessions[0] != first
        assert QmtBroker._new_session_id() != first


def test_connect_failure_nonzero_ret_raises_connection_lost_with_guidance():
    with fake_sdk("xtquant"):
        import xtquant
        xtquant.reset()
        xtquant.SCRIPT["connect_ret"] = -1
        broker = QmtBroker(event_bus=EventBus())
        try:
            with pytest.raises(ConnectionLostError) as ei:
                broker.connect(userdata_path=_USERDATA, account_id=_ACCOUNT)
        finally:
            broker.disconnect()
    msg = str(ei.value)
    assert "miniQMT" in msg and "返回 -1" in msg
    assert _ACCOUNT not in msg                 # 异常文本也会进日志，凭据不得出现


def test_account_subscribe_failure_raises_connection_lost():
    with fake_sdk("xtquant"):
        import xtquant
        xtquant.reset()
        xtquant.SCRIPT["subscribe_ret"] = -1
        broker = QmtBroker()
        try:
            with pytest.raises(ConnectionLostError) as ei:
                broker.connect(userdata_path=_USERDATA, account_id=_ACCOUNT)
        finally:
            broker.disconnect()
    assert "订阅失败" in str(ei.value) and "1190***" in str(ei.value)


@pytest.mark.parametrize("creds", [{}, {"userdata_path": _USERDATA}, {"account_id": _ACCOUNT}])
def test_connect_without_credentials_is_structured_error(creds):
    with broker_ctx(connect=False) as (broker, xtquant, xttrader, bus):
        with pytest.raises(BrokerError) as ei:
            broker.connect(**creds)
    assert "凭据保险库" in str(ei.value)
    assert xtquant.CALLS == []                 # 凭据没齐时不该去碰 SDK


# ==================== 下单 ====================
def test_place_order_uses_remark_as_the_only_report_anchor():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        placed = broker.place_order(_order())
        call = xtquant.find("order_stock")
    (account_id, code, qmt_side, volume, price_type, price, strategy, remark) = call["args"]
    assert account_id == _ACCOUNT
    assert code == "600000.SH" and volume == 1000
    assert qmt_side == xtconstant.STOCK_BUY
    assert price_type == xtconstant.FIX_PRICE
    assert price == 10.50                      # 宪法第 5 条：原样透传，绝不做复权换算
    assert strategy == "sma_cross"
    assert remark == placed.order_id           # 回报关联的唯一锚
    assert placed.broker_order_id == "88001" and placed.status is OrderStatus.SUBMITTED


def test_place_order_sell_maps_sell_constant():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        broker.place_order(_order(side=Side.SELL))
        call = xtquant.find("order_stock")
    assert call["args"][2] == xtconstant.STOCK_SELL


def test_market_order_price_type_branches_by_market():
    """沪市市价≠深市市价：两套申报类型语义不同，映射表必须按市场后缀分支。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        broker.place_order(_order(symbol="600000.SH", order_type=OrderType.MARKET))
        broker.place_order(_order(symbol="000001.SZ", order_type=OrderType.MARKET,
                                  order_id="QD20260101-0002"))
        broker.place_order(_order(symbol="600000.SH", order_type=OrderType.LIMIT,
                                  order_id="QD20260101-0003"))
        calls = xtquant.calls_of("order_stock")
    sh_market, sz_market, limit = (c["args"][4] for c in calls)
    assert sh_market == xtconstant.MARKET_BEST_5
    assert sz_market == xtconstant.MARKET_PEER_PRICE_FIRST
    assert sh_market != sz_market
    assert limit == xtconstant.FIX_PRICE


def test_missing_constant_falls_back_to_placeholder_with_warning(caplog):
    """真机没这个常量名时：用兜底值 + 留痕，而不是猜一个值当权威、也不是直接崩。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        del xtconstant.MARKET_BEST_5
        with caplog.at_level("WARNING"):
            broker.place_order(_order(order_type=OrderType.MARKET))
            call = xtquant.find("order_stock")
        found = any("MARKET_BEST_5" in r.getMessage() for r in caplog.records)
    assert call["args"][4] == 1                 # _MARKET_PRICE_TYPE 里的兜底整数
    assert found


@pytest.mark.parametrize("ret", [-1, 0])
def test_order_stock_rejected_raises_broker_error(ret):
    """提交失败绝不静默：-1/0 都按未受理抛 BrokerError（RiskGate 据此落 FAILED）。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["order_stock_ret"] = ret
        with pytest.raises(BrokerError) as ei:
            broker.place_order(_order())
        assert xtquant.find("order_stock") is not None   # 确实提交过，不是本地拦截
    assert "未受理" in str(ei.value)


def test_order_stock_exception_raises_broker_error():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["order_stock_raises"] = True
        with pytest.raises(BrokerError) as ei:
            broker.place_order(_order())
    assert "模拟故障" in str(ei.value)


def test_place_order_never_retries_to_avoid_duplicate_orders():
    """下单不重试：xtquant 异常时无法判断柜台是否已受理，重试可能造成重复委托。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["fail"] = {"order_stock": 5}
        with pytest.raises(BrokerError):
            broker.place_order(_order())
    assert len(xtquant.calls_of("order_stock")) == 1


def test_place_order_when_channel_down_fails_closed():
    with broker_ctx(connect=False) as (broker, xtquant, xttrader, bus):
        with pytest.raises(ConnectionLostError) as ei:
            broker.place_order(_order())
    assert "fail-closed" in str(ei.value)
    assert xtquant.find("order_stock") is None


# ==================== 撤单 ====================
def test_cancel_order_uses_broker_id_mapping_and_returns_acceptance():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        placed = broker.place_order(_order())
        assert broker.cancel_order(placed.order_id) is True
        call = xtquant.find("cancel_order_stock")
        assert call["args"] == (_ACCOUNT, 88001)

        xtquant.SCRIPT["cancel_ret"] = False
        assert broker.cancel_order(placed.order_id) is False


def test_cancel_order_learns_mapping_from_query_after_restart():
    """重启后本地没有映射，靠查询回报里的 order_remark 回填——撤单仍可工作。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["orders"] = [xttrader.XtOrder(
            order_id=70007, order_sysid="70007", stock_code="600000.SH", order_type=24,
            order_volume=1000, traded_volume=0, price=10.5, price_type=11,
            order_status=50, order_remark="QD-RESTORED", strategy_name="sma_cross")]
        orders = broker.get_orders()
        assert orders[0].order_id == "QD-RESTORED"
        assert broker.cancel_order("QD-RESTORED") is True
    assert xtquant.find("cancel_order_stock")["args"][1] == 70007


def test_cancel_unknown_order_returns_false_not_exception():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        assert broker.cancel_order("QD-NEVER-PLACED") is False
    assert xtquant.find("cancel_order_stock") is None


# ==================== 回报归一化 ====================
def test_order_report_maps_local_id_from_remark_anchor():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        events: list[Event] = []
        bus.subscribe(None, events.append)
        broker.place_order(_order(order_id="QD-ANCHOR-1"))
        events.clear()                           # place_order 自己会广播一份 SUBMITTED 快照
        xttrader.fire("on_stock_order", xttrader.XtOrder(
            order_id=88001, stock_code="600000.SH", order_type=23, order_volume=1000,
            traded_volume=400, price=10.5, traded_price=10.51, price_type=11,
            order_status=55, status_msg="部分成交", strategy_name="sma_cross",
            order_remark="QD-ANCHOR-1"))
        ev = _wait_for(events, EventType.ORDER)
    o = ev.data
    assert isinstance(o, Order) and o.order_id == "QD-ANCHOR-1"
    assert o.status is OrderStatus.PART_FILLED and o.filled_volume == 400
    assert o.avg_filled_price == 10.51 and o.message == "部分成交"
    assert o.broker_order_id == "88001" and o.symbol == "600000.SH"


def test_trade_report_becomes_trade_event_on_worker_thread():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        events: list[Event] = []
        threads: list[str] = []

        def handler(ev: Event) -> None:
            threads.append(threading.current_thread().name)
            events.append(ev)

        bus.subscribe(EventType.TRADE, handler)
        broker.place_order(_order(order_id="QD-TR-1"))
        fired = xttrader.fire("on_stock_trade", xttrader.XtTrade(
            order_id=88001, traded_id="T001", stock_code="600000.SH", order_type=23,
            traded_price=10.52, traded_volume=1000, traded_amount=10520.0,
            traded_time=93001, order_remark="QD-TR-1"))
        assert fired == 1
        ev = _wait_for(events, EventType.TRADE)
    t = ev.data
    assert isinstance(t, Trade) and t.order_id == "QD-TR-1" and t.trade_id == "T001"
    assert t.side is Side.BUY and t.amount == 10520.0 and t.volume == 1000
    assert t.traded_at == _qmt_time_to_ms("093001", today=date.today())
    assert threads == ["qmt-report"]            # 广播不在 SDK 回调线程


def test_slow_bus_consumer_does_not_block_sdk_report_thread():
    """宪法线程纪律：回报回调只入队，消费者再慢也拖不住 SDK 线程。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        entered, release = threading.Event(), threading.Event()
        seen: list[str] = []

        def slow(ev: Event) -> None:
            entered.set()
            release.wait(timeout=5.0)
            seen.append(threading.current_thread().name)

        bus.subscribe(EventType.ORDER, slow)
        t0 = time.perf_counter()
        xttrader.fire("on_stock_order", xttrader.XtOrder(
            order_id=1, stock_code="600000.SH", order_type=23, order_volume=100,
            price=10.0, order_status=50, order_remark="QD-SLOW"))
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"回报回调被消费者拖住 {elapsed:.3f}s（应当只入队）"
        assert entered.wait(timeout=5.0)
        release.set()
        for _ in range(200):
            if seen:
                break
            threading.Event().wait(0.01)
    assert seen == ["qmt-report"]


def test_order_error_report_marks_rejected_and_links_local_order():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        events: list[Event] = []
        bus.subscribe(None, events.append)
        broker.place_order(_order(order_id="QD-ERR-1"))
        events.clear()                           # 只看回报驱动的终态事件
        xttrader.fire("on_order_error", xttrader.XtOrderError(
            order_id=88001, error_id=48, error_msg="资金不足", stock_code="600000.SH"))
        ev = _wait_for(events, EventType.ORDER)
    assert ev.data.status is OrderStatus.REJECTED
    assert ev.data.order_id == "QD-ERR-1" and "资金不足" in ev.data.message


def test_cancel_error_report_broadcasts_risk_event():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        events: list[Event] = []
        bus.subscribe(None, events.append)
        broker.place_order(_order(order_id="QD-CAN-1"))
        xttrader.fire("on_cancel_error", xttrader.XtCancelError(
            order_id=88001, error_id=51, error_msg="已成交不可撤"))
        ev = _wait_for(events, EventType.RISK)
    assert ev.data["action"] == "cancel_rejected"
    assert ev.data["order_id"] == "QD-CAN-1" and "已成交不可撤" in ev.data["detail"]


# ==================== 状态映射 ====================
@pytest.mark.parametrize("code,expected", [
    (48, OrderStatus.SUBMITTED), (49, OrderStatus.SUBMITTED), (50, OrderStatus.SUBMITTED),
    (51, OrderStatus.SUBMITTED), (52, OrderStatus.PART_FILLED), (53, OrderStatus.CANCELLED),
    (54, OrderStatus.CANCELLED), (55, OrderStatus.PART_FILLED), (56, OrderStatus.FILLED),
    (57, OrderStatus.REJECTED),
])
def test_xt_order_status_codes_map_to_core_statuses(code, expected):
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        status_map = build_status_map(xtconstant)
        assert map_order_status(code, status_map) is expected


def test_unknown_status_code_is_conservative_and_logged(caplog):
    """未识别的码不给终态：给 SUBMITTED，等后续回报或对账收敛（宁可晚判不可误判成交）。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        from xtquant import xtconstant
        with caplog.at_level("WARNING"):
            got = map_order_status(201, build_status_map(xtconstant))
            bad = map_order_status(None, build_status_map(xtconstant))
    assert got is OrderStatus.SUBMITTED and bad is OrderStatus.SUBMITTED
    assert any("201" in r.getMessage() for r in caplog.records)


def test_get_orders_normalizes_and_filters_by_status():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["orders"] = [
            xttrader.XtOrder(order_id=1, stock_code="600000.SH", order_type=23,
                             order_volume=1000, traded_volume=1000, price=10.5,
                             price_type=11, order_status=56, order_remark="A1"),
            xttrader.XtOrder(order_id=2, stock_code="000001.SZ", order_type=24,
                             order_volume=500, traded_volume=0, price=9.9,
                             price_type=11, order_status=54, order_remark="A2"),
        ]
        all_orders = broker.get_orders()
        filled = broker.get_orders(status=OrderStatus.FILLED)
    assert [o.order_id for o in all_orders] == ["A1", "A2"]
    assert all_orders[0].status is OrderStatus.FILLED
    assert all_orders[1].status is OrderStatus.CANCELLED
    assert [o.order_id for o in filled] == ["A1"]


def test_poll_is_noop_for_callback_channel():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        assert broker.poll() is None


# ==================== 查询归一化 ====================
def test_position_available_is_can_use_volume_t_plus_1():
    """available 必须取 can_use_volume：当日买入不可卖（T+1），RiskGate 第②道闸靠它。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["positions"] = [
            xttrader.XtPosition(stock_code="600000.SH", volume=1000, can_use_volume=0,
                                avg_price=10.2, market_value=10500.0),
            xttrader.XtPosition(stock_code="000001.SZ", volume=2000, can_use_volume=2000,
                                avg_price=9.0, market_value=19000.0),
        ]
        positions = broker.get_position()
        one = broker.get_position("600000.SH")
    assert [(p.volume, p.available) for p in positions] == [(1000, 0), (2000, 2000)]
    assert positions[0].avg_cost == 10.2 and positions[0].market_value == 10500.0
    assert len(one) == 1 and one[0].symbol == "600000.SH"


def test_account_fields_mapped_from_xt_asset():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["asset"] = xttrader.XtAsset(total_asset=1_052_000.0, cash=23_000.0,
                                                 market_value=1_029_000.0, frozen_cash=1_000.0)
        acc = broker.get_account()
    assert isinstance(acc, Account)
    assert (acc.total_asset, acc.cash, acc.market_value, acc.frozen) == \
        (1_052_000.0, 23_000.0, 1_029_000.0, 1_000.0)


def test_account_query_empty_raises_broker_error():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["asset"] = None
        with pytest.raises(BrokerError):
            broker.get_account()


def test_query_failure_retries_once_then_broker_error():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["positions"] = [xttrader.XtPosition(stock_code="600000.SH", volume=100,
                                                          can_use_volume=100, avg_price=1.0,
                                                          market_value=100.0)]
        xtquant.SCRIPT["fail"] = {"query_stock_positions": 1}
        assert len(broker.get_position()) == 1
        assert len(xtquant.calls_of("query_stock_positions")) == 2   # 1 原始 + 1 重试

        xtquant.SCRIPT["fail"] = {"query_stock_positions": 3}
        with pytest.raises(BrokerError):
            broker.get_position()


def test_get_trades_filters_by_start_time():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["trades"] = [
            xttrader.XtTrade(order_id=1, traded_id="T1", stock_code="600000.SH",
                             order_type=23, traded_price=10.0, traded_volume=100,
                             traded_amount=1000.0, traded_time=93000, order_remark="A1"),
            xttrader.XtTrade(order_id=2, traded_id="T2", stock_code="600000.SH",
                             order_type=23, traded_price=11.0, traded_volume=100,
                             traded_amount=1100.0, traded_time=143000, order_remark="A1"),
        ]
        today = date.today()
        all_trades = broker.get_trades()
        afternoon = broker.get_trades(start=datetime.combine(
            today, datetime.strptime("12:00:00", "%H:%M:%S").time()))
    assert [t.trade_id for t in all_trades] == ["T1", "T2"]
    assert [t.trade_id for t in afternoon] == ["T2"]
    assert all_trades[0].order_id == "A1"


def test_qmt_time_formats_normalized_to_millis():
    """回报里 HHMMSS / YYYYMMDDHHMMSS / epoch 秒/毫秒都可能出现——统一毫秒（UTC 存储口径）。"""
    today = date(2026, 1, 5)
    assert _qmt_time_to_ms(93001, today=today) == _qmt_time_to_ms("093001", today=today)
    assert _qmt_time_to_ms("20260105093001", today=today) == _qmt_time_to_ms(
        "093001", today=today)
    assert _qmt_time_to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert _qmt_time_to_ms(1_700_000_000) == 1_700_000_000_000
    assert isinstance(_qmt_time_to_ms(None), int)


# ==================== 断线可探测 + fail-closed ====================
def test_disconnected_callback_fails_closed_and_is_detectable():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["connect_ret"] = -1        # 重连也连不上：状态必须稳定停在断线
        fired = xttrader.fire("on_disconnected")
        assert fired == 1
        assert broker.health_check() is False
        with pytest.raises(ConnectionLostError):
            broker.place_order(_order())
        assert broker.watchdog.health.state in ("lost", "recovering")


def test_health_check_detects_dead_query_and_blocks_orders():
    """客户端被悄悄登出时 SDK 不一定抛异常——探活必须能把它判死（宪法合同）。"""
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["fail"] = {"query_stock_asset": 5}
        assert broker.health_check() is False
        assert broker.health_check() is False     # 判死后不再外呼，直接报不可用
        with pytest.raises(ConnectionLostError):
            broker.place_order(_order())


def test_reconnect_replays_account_subscription_and_refreshes_reconcile_surface():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["asset"] = xttrader.XtAsset(total_asset=100.0, cash=50.0,
                                                 market_value=50.0, frozen_cash=0.0)
        xtquant.DATA["positions"] = [xttrader.XtPosition(stock_code="600000.SH", volume=100,
                                                          can_use_volume=100, avg_price=1.0,
                                                          market_value=100.0)]
        events: list[Event] = []
        bus.subscribe(None, events.append)
        xtquant.CALLS.clear()
        assert broker.watchdog.recover_once() is True
        subs = xtquant.calls_of("subscribe")
        acc_ev = _wait_for(events, EventType.ACCOUNT)
        pos_ev = _wait_for(events, EventType.POSITION)
    assert len(subs) == 1 and subs[0]["args"] == (_ACCOUNT,)   # 账户订阅重放
    assert acc_ev.data.total_asset == 100.0
    assert len(pos_ev.data) == 1


def test_snapshot_for_reconcile_returns_full_tuple():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.DATA["asset"] = xttrader.XtAsset(total_asset=100.0, cash=100.0,
                                                 market_value=0.0, frozen_cash=0.0)
        xtquant.DATA["positions"] = [xttrader.XtPosition(stock_code="600000.SH", volume=100,
                                                          can_use_volume=0, avg_price=1.0,
                                                          market_value=100.0)]
        xtquant.DATA["orders"] = [xttrader.XtOrder(order_id=5, stock_code="600000.SH",
                                                   order_type=23, order_volume=100,
                                                   traded_volume=0, price=1.0, price_type=11,
                                                   order_status=50, order_remark="A5")]
        xtquant.DATA["trades"] = []
        orders, trades, positions, account = broker.snapshot_for_reconcile()
    assert [len(orders), len(trades), len(positions)] == [1, 0, 1]
    assert orders[0].order_id == "A5" and positions[0].available == 0
    assert isinstance(account, Account)


def test_snapshot_for_reconcile_fails_closed_when_channel_down():
    with broker_ctx() as (broker, xtquant, xttrader, bus):
        xtquant.SCRIPT["connect_ret"] = -1
        xttrader.fire("on_disconnected")
        with pytest.raises(ConnectionLostError):
            broker.snapshot_for_reconcile()


# ==================== 日志脱敏 ====================
def test_credentials_never_appear_in_logs(caplog):
    with caplog.at_level("DEBUG"):
        with broker_ctx() as (broker, xtquant, xttrader, bus):
            xtquant.SCRIPT["order_stock_ret"] = -1
            with pytest.raises(BrokerError):
                broker.place_order(_order())
            xtquant.SCRIPT["connect_ret"] = -1
            xttrader.fire("on_disconnected")
            broker.health_check()
            with pytest.raises(ConnectionLostError):
                broker.place_order(_order())
            xtquant.SCRIPT["fail"] = {"query_stock_asset": 5}
            with pytest.raises(ConnectionLostError):
                broker.get_account()
    text = caplog.text
    assert _ACCOUNT not in text
    assert _USERDATA not in text
    assert mask(_ACCOUNT) in text               # 前 4 位 + *** 的形态必须在
    assert mask(_USERDATA) in text


def test_error_messages_carry_masked_credentials_only():
    """异常文本会被 ChannelHub 原样写进日志——抛错时同样只能带掩码。"""
    with broker_ctx(connect=False) as (broker, xtquant, xttrader, bus):
        with pytest.raises(BrokerError) as ei:
            broker.connect(userdata_path=_USERDATA, account_id="")
    assert _ACCOUNT not in str(ei.value) and _USERDATA not in str(ei.value)


# ==================== 元数据 / 注册表 ====================
def test_registry_meta_for_qmt_broker():
    with fake_sdk("xtquant"):
        rows = {r["name"]: r for r in describe_adapters()}
        assert "qmt" in rows and "qmt" in list_adapters()
        row = rows["qmt"]
        assert row["kind"] == "broker" and row["is_paper"] is False
        assert row["official"] is True and row["requires"] == ["xtquant"]
        assert row["default_enabled"] is True and row["enable_flag"] == ""
        assert "miniQMT" in row["label"] and row["prereq"]


def test_adapter_files_do_not_import_xtquant_at_module_level():
    """顶层 import SDK 会让注册表扫描直接崩（宪法第 1 条的可观测性前提）。"""
    assert not dependency_available("xtquant")


def test_new_session_id_format():
    sid = QmtBroker._new_session_id()
    assert isinstance(sid, int) and len(str(sid)) == 12      # YYYYMMDD + 4 位随机
