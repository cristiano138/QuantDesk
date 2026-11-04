"""easytrader 通道单测（全离线：假 SDK 见 tests/mocks/easytrader.py）。

覆盖宪法第 9 条的落点（默认禁用 → 注册表源头不可见）、GUI 串行执行器、
中文键/状态口径归一、空委托号不静默、poll diff 只在变化时广播、
控件异常截图取证、日志脱敏、health_check 可探测语义。
"""

from __future__ import annotations

import ctypes
import threading
from datetime import datetime

import pytest

from quantdesk import config as cfg
from quantdesk.adapters import describe_adapters, get_adapter_class, list_adapters
from quantdesk.adapters.brokers import easytrader_broker as eb
from quantdesk.adapters.brokers.easytrader_broker import (
    BALANCE_KEY_MAP,
    ENTRUST_STATUS_MAP,
    EasytraderBroker,
    _capture_fullscreen_bmp,
)
from quantdesk.core.enums import OrderStatus, OrderType, Side
from quantdesk.core.errors import AdapterError, BrokerError, ConnectionLostError
from quantdesk.core.events import EventBus, EventType
from quantdesk.core.models import Order
from quantdesk.core.secrets import mask
from tests.mocks import fake_sdk

THS_PATH = r"C:\同花顺软件\xiadan.exe"


# ---------- 夹具 ----------
@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch, tmp_path):
    """整个模块的截图/日志都落在 tmp_path，不污染仓库 data/。"""
    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)


@pytest.fixture()
def fake():
    with fake_sdk("easytrader"):
        import easytrader as et

        et.reset()
        yield et


@pytest.fixture()
def make_broker():
    """统一登记实例，用例结束逐个 disconnect（GUI worker 线程不留残留）。"""
    created: list[EasytraderBroker] = []

    def _make(**kw) -> EasytraderBroker:
        b = EasytraderBroker(**kw)
        created.append(b)
        return b

    yield _make
    for b in created:
        b.disconnect()


@pytest.fixture()
def connected(fake, make_broker):
    """返回「连接好」的工厂：bus/额外构造参数在调用时传（默认已 connect）。"""
    def _connect(bus=None, **kw) -> EasytraderBroker:
        b = make_broker(event_bus=bus, **kw)
        b.connect(ths_path=THS_PATH)
        return b

    return _connect


def _order(order_id: str = "OD-A", symbol: str = "600000.SH", side=Side.BUY,
           volume: int = 100, price: float = 10.0, **kw) -> Order:
    return Order(order_id=order_id, symbol=symbol, side=side, price=price, volume=volume, **kw)


def _entrust(no: str = "1", code: str = "600000", status: str = "已报",
             biz: str = "证券买入", price: float = 10.0, volume: int = 100,
             filled: int = 0) -> dict:
    return {"合同编号": no, "证券代码": code, "委托状态": status, "成交价格": price,
            "委托数量": volume, "成交数量": filled, "业务名称": biz,
            "日期": "2026-11-06", "时间": "09:30:00"}


def _trade(no: str = "1", code: str = "600000", price: float = 10.0, volume: int = 100,
           biz: str = "证券买入") -> dict:
    return {"成交编号": f"T{no}", "合同编号": no, "证券代码": code, "成交价格": price,
            "成交数量": volume, "业务名称": biz, "日期": "2026-11-06", "时间": "09:31:00"}


# ---------- 宪法第 9 条：默认禁用 → 交易 API 不可见（不是前端藏按钮） ----------
def _patch_enabled(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(cfg, "get_settings",
                        lambda: cfg.Settings(simulation_only=True,
                                             enable_easytrader=enabled))


def test_easytrader_disabled_by_default_is_invisible_to_trading_api(monkeypatch):
    _patch_enabled(monkeypatch, False)
    assert "easytrader" not in list_adapters()
    with pytest.raises(KeyError):
        get_adapter_class("easytrader")
    assert "easytrader" not in [r["name"] for r in describe_adapters(include_disabled=False)]
    row = next(r for r in describe_adapters() if r["name"] == "easytrader")
    assert row["status"] == "disabled"          # 面板可见（可引导启用），API 不可见


def test_easytrader_visible_after_enable_flag(monkeypatch):
    _patch_enabled(monkeypatch, True)
    assert "easytrader" in list_adapters()
    assert get_adapter_class("easytrader") is EasytraderBroker
    assert "easytrader" in [r["name"] for r in describe_adapters(include_disabled=False)]


def test_channel_metadata_declares_non_official_channel():
    assert EasytraderBroker.name == "easytrader"
    assert EasytraderBroker.official is False
    assert EasytraderBroker.default_enabled is False
    assert EasytraderBroker.enable_flag == "enable_easytrader"
    assert EasytraderBroker.is_paper is False
    assert EasytraderBroker.requires == ("easytrader",)
    p = EasytraderBroker.prereq
    for kw in ("独立下单程序", "锁", "设置页", "非官方"):
        assert kw in p
    assert "GUI 自动化" in EasytraderBroker.label


def test_mapping_tables_are_declared_before_use():
    assert ENTRUST_STATUS_MAP["已报"] is OrderStatus.SUBMITTED
    assert ENTRUST_STATUS_MAP["部成"] is OrderStatus.PART_FILLED
    assert ENTRUST_STATUS_MAP["已成"] is OrderStatus.FILLED
    assert ENTRUST_STATUS_MAP["已撤"] is OrderStatus.CANCELLED
    assert ENTRUST_STATUS_MAP["废单"] is OrderStatus.REJECTED
    assert BALANCE_KEY_MAP["cash"][0] == "可用资金"


# ---------- 生命周期 ----------
def test_connect_uses_ths_and_passes_xiadan_path(fake, connected):
    b = connected()
    assert fake.state.client == "ths"
    assert fake.state.connect_arg == THS_PATH
    assert b.health_check() is True
    assert b._connected is True


def test_connect_failure_raises_connection_lost_with_guidance(fake, make_broker, caplog):
    fake.state.connect_error = fake.UserError(f"无法定位窗口 {THS_PATH}")
    caplog.set_level("INFO")
    b = make_broker()
    with pytest.raises(ConnectionLostError) as ei:
        b.connect(ths_path=THS_PATH)
    msg = str(ei.value)
    for kw in ("登录", "路径", "版本"):
        assert kw in msg
    assert b.health_check() is False


def test_connect_without_path_raises_connection_lost(fake, make_broker):
    b = make_broker(ths_path="")
    with pytest.raises(ConnectionLostError) as ei:
        b.connect()
    assert "xiadan.exe" in str(ei.value)


def test_missing_sdk_raises_structured_error_with_install_hint(make_broker):
    # 不进 fake_sdk：本机没装 easytrader，必须给「怎么办」而不是 ImportError
    b = make_broker()
    with pytest.raises(AdapterError) as ei:
        b.connect(ths_path=THS_PATH)
    assert "easytrader" in str(ei.value) and "pip install" in str(ei.value)
    assert not isinstance(ei.value, ImportError)


def test_reconnect_after_disconnect_rebuilds_baseline(fake, make_broker):
    b = make_broker()
    b.connect(ths_path=THS_PATH)
    b.disconnect()
    assert b.health_check() is False
    b.connect(ths_path=THS_PATH)             # 守护重连：同一实例再次 connect
    assert b.health_check() is True
    assert b._baselined is True


# ---------- GUI 串行执行器 ----------
def test_concurrent_calls_serialize_on_one_worker_thread(fake, connected):
    b = connected()
    mark = len(fake.state.calls)
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def _run(fn, tag):
        barrier.wait()
        try:
            fn()
            results.append(tag)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_run, args=(act, f"t{i}"))
               for i, act in enumerate([b.get_account] * 3 + [b.get_position] * 2
                                       + [b.get_orders, b.get_trades, b.poll])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert len(results) == 8
    idents = {tid for _, tid in fake.state.calls[mark:]}
    assert len(idents) == 1                  # 并发调用只穿透到一个线程
    assert idents == {b._worker.thread_ident}
    assert threading.get_ident() not in idents   # 碰 SDK 的绝不是调用方线程


def test_gui_call_timeout_raises_connection_lost(fake, make_broker):
    hold = threading.Event()

    class _SlowUser:
        client = None

        def stock_balance(self):
            hold.wait(5)
            return {"可用资金": 1.0}

    b = make_broker(call_timeout_s=0.05)
    b._user = _SlowUser()
    b._connected = True
    with pytest.raises(ConnectionLostError) as ei:
        b.get_account()
    assert "超时" in str(ei.value)
    hold.set()
    b.disconnect()


def test_submit_timeout_is_tracked_as_unconfirmed(fake, make_broker):
    """下单超时=结果未知：不能谎报失败，必须留给对账（unconfirmed_orders）。"""
    hold = threading.Event()

    class _SlowUser:
        client = None

        def stock_balance(self):
            return {"可用资金": 1.0}

        def order_buy(self, symbol="", price=0.0, amount=0, **kw):
            hold.wait(5)
            return "9"

    b = make_broker(call_timeout_s=0.05)
    b._user = _SlowUser()
    b._connected = True
    with pytest.raises(ConnectionLostError):
        b.place_order(_order())
    assert b.unconfirmed_orders() == ["OD-A"]
    hold.set()
    b.disconnect()


# ---------- 中文键口径 ----------
def test_get_account_maps_chinese_balance_keys(fake, connected):
    fake.state.balance = {"可用资金": 12345.6, "参考市值": 7000.0, "股票市值": 7000.0,
                         "冻结市值": 100.0, "总资产": 19445.6}
    acct = connected().get_account()
    assert (acct.total_asset, acct.cash, acct.market_value, acct.frozen) == (
        19445.6, 12345.6, 7000.0, 100.0)
    assert isinstance(acct.updated_at, int)


def test_get_account_rejects_empty_balance(fake, connected):
    b = connected()
    fake.state.balance = {}
    with pytest.raises(BrokerError):
        b.get_account()


def test_connect_without_balance_data_fails_closed(fake, make_broker):
    fake.state.balance = {}                        # 连上窗口但取不到资金 = 没真连上
    b = make_broker()
    with pytest.raises(ConnectionLostError) as ei:
        b.connect(ths_path=THS_PATH)
    assert "stock_balance" in str(ei.value)


def test_account_with_unrecognized_chinese_keys_warns(fake, connected, caplog):
    b = connected()
    caplog.set_level("WARNING")
    fake.state.balance = {"财富总览": 1.0, "活期": 2.0}
    acct = b.get_account()
    assert (acct.total_asset, acct.cash, acct.market_value) == (0.0, 0.0, 0.0)
    assert "中文键" in caplog.text                 # 措辞变了要说出来，不能静默报 0


def test_calls_after_disconnect_fail_closed(fake, connected):
    b = connected()
    b.disconnect()
    with pytest.raises(ConnectionLostError):
        b.get_account()
    with pytest.raises(ConnectionLostError):
        b.poll()


def test_position_available_comes_from_keyong_gufen(fake, connected):
    fake.state.positions = [{"证券代码": "600000", "证券名称": "浦发银行", "当前拥股": 1000,
                            "可用股份": 600, "成本价": 8.5, "参考市值": 8500.0, "交易所": "上海"}]
    p = connected().get_position("600000.SH")[0]
    assert (p.symbol, p.volume, p.available) == ("600000.SH", 1000, 600)  # T+1 可用落点
    assert p.avg_cost == 8.5 and p.market_value == 8500.0


def test_position_without_available_column_is_zero(fake, connected, caplog):
    fake.state.positions = [{"证券代码": "000001", "当前拥股": 500, "成本价": 10.0}]
    caplog.set_level("WARNING")
    p = connected().get_position()[0]
    assert (p.symbol, p.volume, p.available) == ("000001.SZ", 500, 0)   # 不虚增可卖
    assert "可用" in caplog.text


def test_orders_map_chinese_status_and_warn_on_unknown(fake, connected, caplog):
    fake.state.entrusts = [_entrust(no="1", status="部成", filled=40),
                          _entrust(no="2", code="000001", status="外星状态", biz="申购")]
    caplog.set_level("WARNING")
    b = connected()
    orders = {o.broker_order_id: o for o in b.get_orders()}
    assert orders["1"].status is OrderStatus.PART_FILLED
    assert orders["1"].filled_volume == 40 and orders["1"].avg_filled_price == 10.0
    assert orders["2"].status is OrderStatus.SUBMITTED        # 未识别 → 不误判终态
    assert "外星状态" in caplog.text
    assert orders["2"].side == "申购"                         # 认不出方向就原样上报
    assert "业务名称" in caplog.text
    assert len(b.get_orders(status=OrderStatus.PART_FILLED)) == 1


def test_symbol_conversion_stays_inside_adapter(fake, connected):
    fake.state.entrusts = [_entrust(no="7", code="688001", status="已报")]
    o = connected().get_orders()[0]
    assert o.symbol == "688001.SH"
    assert o.created_at == int(datetime.strptime("2026-11-06 09:30:00",
                                                 "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


# ---------- 下单 / 撤单 ----------
def test_place_order_fills_broker_order_id(fake, connected):
    b = connected()
    out = b.place_order(_order(order_id="OD-B", side=Side.SELL, volume=300, price=9.9))
    assert out.broker_order_id == "2" and out.status is OrderStatus.SUBMITTED
    assert out.order_id == "OD-B"
    assert fake.state.kwargs["order_sell"] == {"symbol": "600000", "price": 9.9, "amount": 300}
    assert b._no_to_local["2"] == ("OD-B", "manual")


def test_order_buy_returns_empty_entrust_no_raises_broker_error(fake, connected):
    fake.state.order_buy_result = ""
    b = connected()
    with pytest.raises(BrokerError) as ei:
        b.place_order(_order())
    assert "未" in str(ei.value) and "受理" in str(ei.value)
    assert b._bindings == {}                       # 未受理不留脏映射


def test_market_order_is_refused_until_verified(fake, connected):
    b = connected()
    o = _order(order_type=OrderType.MARKET)
    with pytest.raises(BrokerError) as ei:
        b.place_order(o)
    assert "市价" in str(ei.value)


def test_place_order_before_connect_fails_closed(make_broker):
    b = make_broker()
    with pytest.raises(ConnectionLostError):
        b.place_order(_order())


def test_cancel_order_payload_carries_entrust_no(fake, connected):
    b = connected()
    b.place_order(_order(order_id="OD-C"))          # → 合同编号 "1"
    fake.state.entrusts = [_entrust(no="1")]
    b.poll()                                        # 让原始委托行进缓存
    assert b.cancel_order("OD-C") is True
    payload = fake.state.kwargs["cancel_order"]["order"]
    assert payload["entrust_no"] == "1" and payload["合同编号"] == "1"
    assert payload["证券代码"] == "600000"          # 整行回传（部分版本要求）


def test_cancel_order_without_mapping_raises(fake, connected):
    b = connected()
    with pytest.raises(BrokerError) as ei:
        b.cancel_order("OD-NOPE")
    assert "映射" in str(ei.value)


def test_cancel_failure_is_not_silent(fake, connected):
    b = connected()
    b.place_order(_order(order_id="OD-D"))
    fake.state.errors["cancel_order"] = fake.UserError("该合同已成交，无法撤单")
    with pytest.raises(BrokerError):
        b.cancel_order("OD-D")


# ---------- poll：只在变化时发布 ----------
def test_poll_publishes_only_changes(fake, connected):
    bus = EventBus()
    seen: list[tuple[str, object]] = []
    bus.subscribe(None, lambda e: seen.append((e.type.value, e.data)))
    fake.state.entrusts = [_entrust(no="1")]
    fake.state.positions = [{"证券代码": "600000", "当前拥股": 100, "可用股份": 0,
                            "成本价": 10.0, "参考市值": 1000.0}]
    b = connected(bus=bus)
    assert seen == []                              # 连接即建基线，不刷历史
    b.poll()
    assert seen == []                              # 无变化不发布
    fake.state.entrusts = [_entrust(no="1", status="已成", filled=100)]
    fake.state.trades = [_trade(no="1")]
    b.poll()
    kinds = [k for k, _ in seen]
    assert EventType.ORDER.value in kinds and EventType.TRADE.value in kinds
    order_evt = next(d for k, d in seen if k == EventType.ORDER.value)
    assert order_evt.order_id == "ths-1"           # 无本地映射 → 按合同编号归一
    assert order_evt.status is OrderStatus.FILLED
    trade_evt = next(d for k, d in seen if k == EventType.TRADE.value)
    assert trade_evt.symbol == "600000.SH" and trade_evt.amount == 1000.0
    seen.clear()
    b.poll()
    assert seen == []                              # 同一快照反复轮询不重复发
    fake.state.balance = {"可用资金": 1.0, "股票市值": 2.0, "冻结市值": 0.0, "总资产": 3.0}
    b.poll()
    assert EventType.ACCOUNT.value in [k for k, _ in seen]
    seen.clear()
    fake.state.positions = [{"证券代码": "600000", "当前拥股": 200, "可用股份": 100,
                            "成本价": 10.0, "参考市值": 2000.0}]
    b.poll()
    assert EventType.POSITION.value in [k for k, _ in seen]


def test_poll_local_binding_keeps_engine_order_id(fake, connected):
    bus = EventBus()
    seen: list = []
    bus.subscribe(None, lambda e: seen.append(e))
    b = connected(bus=bus)
    b.place_order(_order(order_id="OD-E"))         # 合同编号 "1"
    fake.state.entrusts = [_entrust(no="1", status="已成", filled=100)]
    b.poll()
    orders = [e.data for e in seen if e.type is EventType.ORDER]
    assert any(o.order_id == "OD-E" and o.status is OrderStatus.FILLED for o in orders)


def test_poll_before_connect_fails_closed(make_broker):
    b = make_broker()
    with pytest.raises(ConnectionLostError):
        b.poll()


def test_snapshot_for_reconcile_returns_all_four_in_one_gui_task(fake, connected):
    fake.state.entrusts = [_entrust(no="1")]
    fake.state.trades = [_trade(no="1")]
    fake.state.positions = [{"证券代码": "600000", "当前拥股": 100, "可用股份": 100,
                            "成本价": 10.0, "参考市值": 1000.0}]
    seen: list[str] = []
    bus = EventBus()
    bus.subscribe(None, lambda e: seen.append(e.type.value))
    b = connected(bus=bus)
    mark = len(fake.state.calls)
    orders, trades, positions, account = b.snapshot_for_reconcile()
    assert (len(orders), len(trades), len(positions)) == (1, 1, 1)
    assert account.total_asset == 150000.0
    # 四样数据只花一个 GUI 任务（一次界面抢占）
    assert [n for n, _ in fake.state.calls[mark:]] == [
        "stock_balance", "stock_positions", "today_entrusts", "today_trades"]
    b.poll()                       # 对账快照不得搅乱 poll 的 diff 基线
    assert seen == []


def test_get_trades_filters_by_start(fake, connected):
    fake.state.trades = [_trade(no="1")]
    b = connected()
    assert len(b.get_trades()) == 1
    assert b.get_trades(start=datetime(2027, 1, 1)) == []


def test_restart_loses_local_mapping_and_warns(fake, connected, make_broker, caplog):
    b1 = connected()
    b1.place_order(_order(order_id="OD-F"))
    fake.state.entrusts = [_entrust(no="1")]
    caplog.set_level("WARNING")
    b2 = make_broker()
    b2.connect(ths_path=THS_PATH)                  # 模拟进程重启：映射表空
    assert "对账" in caplog.text
    o = b2.get_orders()[0]
    assert o.order_id == "ths-1" and o.broker_order_id == "1"
    b2.disconnect()


# ---------- 控件异常 → 截图取证 ----------
@pytest.fixture()
def shot_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    return tmp_path / "logs" / "easytrader"


def test_control_error_raises_adapter_error_and_saves_screenshot(fake, connected, shot_dir):
    b = connected()
    fake.state.errors["today_entrusts"] = fake.ElementNotFoundError("控件 委托查询 找不到")
    assert not shot_dir.exists() or not list(shot_dir.glob("*"))   # 正常路径不留图
    with pytest.raises(AdapterError) as ei:
        b.get_orders()
    msg = str(ei.value)
    assert "版本可能已升级" in msg and "锁版本" in msg
    assert "ElementNotFoundError" in msg
    files = list(shot_dir.glob("*"))
    assert files, "控件异常必须在 data/logs/easytrader/ 留下取证截图"
    assert files[0].read_bytes().startswith(b"\x89PNG")
    assert files[0].parent.name == "easytrader"


def test_version_mismatch_text_is_treated_as_control_error(fake, connected, shot_dir):
    b = connected()
    fake.state.errors["stock_positions"] = NotImplementedError("同花顺版本不匹配")
    with pytest.raises(AdapterError) as ei:
        b.get_position()
    assert "同花顺版本不匹配" in str(ei.value)
    assert list(shot_dir.glob("*.png"))


def test_screenshot_failure_does_not_mask_original_error(fake, connected, shot_dir, monkeypatch):
    monkeypatch.setattr(eb, "_capture_fullscreen_bmp", lambda path: False)
    b = connected()
    fake.state.errors["stock_balance"] = fake.ElementNotFoundError("找不到下单窗口")
    fake.state.window_capture_error = RuntimeError("屏幕保护中，抓不了")
    with pytest.raises(AdapterError) as ei:
        b.get_account()
    assert "找不到下单窗口" in str(ei.value)        # 原异常文本仍在
    assert "截图失败" in str(ei.value)
    assert not list(shot_dir.glob("*"))


def test_window_capture_failure_falls_back_to_gdi_capture(fake, connected, shot_dir):
    """窗口句柄抓图失败 → 纯 stdlib GDI 全屏兜底，取证不能断。"""
    if not (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "gdi32")):
        pytest.skip("非 Windows：GDI 抓屏兜底不适用")
    b = connected()
    fake.state.errors["stock_balance"] = fake.ElementNotFoundError("找不到下单窗口")
    fake.state.window_capture_error = RuntimeError("窗口对象已失效")
    with pytest.raises(AdapterError) as ei:
        b.get_account()
    assert "找不到下单窗口" in str(ei.value)
    shots = list(shot_dir.glob("*.bmp"))
    assert shots and shots[0].read_bytes().startswith(b"BM")
    assert shots[0].name in str(ei.value)


def test_non_control_error_stays_broker_error_without_screenshot(fake, connected, shot_dir):
    b = connected()
    fake.state.errors["stock_balance"] = fake.UserError("资金账号被其他程序占用")
    with pytest.raises(BrokerError) as ei:
        b.get_account()
    assert isinstance(ei.value, BrokerError)
    assert not list(shot_dir.glob("*"))


@pytest.mark.skipif(not (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "gdi32")),
                    reason="非 Windows：GDI 抓屏兜底不适用")
def test_fullscreen_capture_fallback_writes_readable_bmp(tmp_path, fake, connected):
    """没有窗口句柄时的兜底取证：纯 stdlib GDI 抓屏。"""
    b = connected()
    b._user.client = None                          # 连窗口对象都拿不到（窗口都没找到的场景）
    out = tmp_path / "shot.bmp"
    assert _capture_fullscreen_bmp(out) is True
    data = out.read_bytes()
    assert data.startswith(b"BM") and len(data) > 100
    b.disconnect()


# ---------- health_check 可探测语义 ----------
def test_health_check_true_when_balance_readable(fake, connected):
    assert connected().health_check() is True


def test_health_check_false_on_probe_exception(fake, connected):
    b = connected()
    fake.state.errors["stock_balance"] = fake.UserError("会话掉了")
    assert b.health_check() is False               # 不外抛，只报False（守护据此判死）


def test_health_check_false_when_disconnected(make_broker):
    assert make_broker().health_check() is False


# ---------- 日志脱敏 ----------
def test_logs_and_messages_never_leak_fund_path(fake, connected, make_broker, caplog):
    caplog.set_level("INFO")
    fake.state.connect_error = fake.UserError(f"窗口查找失败：{THS_PATH}")
    b = make_broker()
    with pytest.raises(ConnectionLostError) as ei:
        b.connect(ths_path=THS_PATH)
    assert THS_PATH not in str(ei.value)
    assert THS_PATH not in caplog.text
    assert mask(THS_PATH) in str(ei.value)         # 只留前 4 位 + ***
    fake.state.connect_error = None
    b2 = connected()
    b2.get_account()
    assert THS_PATH not in caplog.text
    assert mask(THS_PATH) in caplog.text
