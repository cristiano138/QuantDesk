"""通达信通道离线单测（假 pytdx / 假 TdxAiData，全用例不联网）。

覆盖：代码互转与市场位、freq→category 映射、列归一化（含 pytdx 时间串解析与升序）、
请求队列串行性（多线程并发只碰一个工作线程）、无数据返回空表、复权请求明确拒绝（不伪造）、
health_check 异常返回 False、订阅/退订与引用计数归零停轮询、重连后订阅集合重放、
成交量单位（手）不改写、传输层选择（官方优先 / 缺失降级 / 两者都缺给引导）。
"""

from __future__ import annotations

import concurrent.futures as cf
import threading

import pandas as pd
import pytest

from quantdesk.adapters import describe_adapters
from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS
from quantdesk.adapters.data import tdx_aidata as tdx_mod
from quantdesk.adapters.data.tdx_aidata import (
    BAR_PAGE_SIZE,
    PYTDX_BAR_CATEGORY,
    TDX_HQ_SERVERS,
    TdxAiDataFeed,
    TdxOfficialTransport,
    TdxPytdxTransport,
    market_and_code,
    normalize_bars,
    quote_to_tick,
)
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import (
    AdapterError,
    ConnectionLostError,
    FeedCapabilityError,
    SymbolError,
)
from quantdesk.core.symbols import from_internal
from tests.mocks import fake_sdk

START = pd.Timestamp("2024-01-02")
END = pd.Timestamp("2024-12-31")


def _feed(**kw) -> TdxAiDataFeed:
    """离线夹具：限速归零、两台假服务器、不起守护线程、走 pytdx 传输层。

    传输层选路本身有专门用例（official 优先 / 缺失降级 / 强制指定），机制用例固定用
    pytdx，免得两个假 SDK 同时在 sys.path 上时被选成官方传输层（假 SDK 的调用名不同）。
    """
    kw.setdefault("servers", [("10.0.0.1", 7709)])
    kw.setdefault("min_interval", 0.0)
    kw.setdefault("poll_interval", 0.05)
    kw.setdefault("start_watchdog", False)
    kw.setdefault("transport", "pytdx")
    feed = TdxAiDataFeed(**kw)
    feed.connect()
    return feed


def _hq():
    """取假 pytdx 模块（必须在 with fake_sdk("pytdx") 内调用）。"""
    import pytdx.hq as hq_mod
    return hq_mod


def _hide_sdks(monkeypatch, *names: str) -> None:
    """假装某些 SDK 没装。

    fake_sdk 是把整个 tests/mocks 目录挂到 sys.path，所以两个假 SDK 会同时可见；
    「官方 SDK 缺失」这类分支必须显式屏蔽依赖探测才能测到。
    """
    real = tdx_mod.dependency_available
    hidden = set(names)
    monkeypatch.setattr(tdx_mod, "dependency_available",
                        lambda m: False if m in hidden else real(m))


# --------------------------------------------------------------------------
# 元数据与注册表
# --------------------------------------------------------------------------
def test_class_metadata_matches_channel_card():
    assert TdxAiDataFeed.name == "tdx_aidata"
    assert TdxAiDataFeed.label == "通达信行情（官方数据组件 / 免费开源版）"
    assert TdxAiDataFeed.requires == ("TdxAiData", "pytdx")
    assert set(TdxAiDataFeed.capabilities) == {FeedCapability.HISTORY_BAR,
                                               FeedCapability.REALTIME_TICK,
                                               FeedCapability.TRANSACTION}


def test_registry_describes_channel_without_importing_sdk():
    row = next(r for r in describe_adapters() if r["name"] == "tdx_aidata")
    assert row["kind"] == "data" and row["label"] == "通达信行情（官方数据组件 / 免费开源版）"
    assert "history_bar" in row["capabilities"] and "transaction" in row["capabilities"]
    assert set(row["requires"]) == {"TdxAiData", "pytdx"}
    # requires 的「全部存在」语义 vs 本通道的「二者择一」实况：面板必须能看到缺哪个
    assert row["status"] in ("ready", "unready")
    assert set(row["missing"]) <= {"TdxAiData", "pytdx"}


def test_server_list_is_a_candidate_pool_not_a_single_ip():
    assert len(TDX_HQ_SERVERS) >= 3
    assert all(isinstance(h, str) and isinstance(p, int) and 1024 < p < 65535
               for h, p in TDX_HQ_SERVERS)


def test_deps_ready_accepts_either_transport(monkeypatch):
    with fake_sdk("pytdx"):
        _hide_sdks(monkeypatch, "TdxAiData")
        assert TdxAiDataFeed.deps_ready() is True          # 只有 pytdx 也算通道可用
    # 本机可能真的装了 pytdx（早前实测公开服务器时装的），所以"两者都缺"要显式屏蔽，
    # 否则这条断言测的是这台机器的 pip list，不是通道语义
    _hide_sdks(monkeypatch, "TdxAiData", "pytdx")
    assert TdxAiDataFeed.deps_ready() is False             # 两者都缺


# --------------------------------------------------------------------------
# 纯函数：代码互转 / 列归一化
# --------------------------------------------------------------------------
def test_market_bit_of_symbols():
    assert market_and_code("600000.SH") == (1, "600000")
    assert market_and_code("000001.SZ") == (0, "000001")
    assert market_and_code("300750.SZ") == (0, "300750")
    with pytest.raises(SymbolError):
        market_and_code("830001.BJ")                       # tdx 源不支持北交所（core 口径）


def test_symbol_roundtrip_via_core():
    assert from_internal("000001.SZ", "tdx") == "0.000001"
    assert from_internal("600000.SH", "tdx") == "1.600000"


def test_freq_to_pytdx_category_table():
    assert PYTDX_BAR_CATEGORY[Frequency.M1] == 7
    assert PYTDX_BAR_CATEGORY[Frequency.M5] == 0
    assert PYTDX_BAR_CATEGORY[Frequency.M15] == 1
    assert PYTDX_BAR_CATEGORY[Frequency.M30] == 2
    assert PYTDX_BAR_CATEGORY[Frequency.H1] == 3
    assert PYTDX_BAR_CATEGORY[Frequency.D1] == 4
    assert PYTDX_BAR_CATEGORY[Frequency.W1] == 5
    assert PYTDX_BAR_CATEGORY[Frequency.MO1] == 6


def test_normalize_bars_columns_ascending_and_daily_dt_truncated():
    with fake_sdk("pytdx"):
        hq = _hq()
        df = normalize_bars(hq.make_bars(periods=5, start="2024-01-02"), Frequency.D1)
    assert list(df.columns) == HISTORY_COLUMNS
    assert df["dt"].is_monotonic_increasing
    # pytdx 日线时间串带 15:00 收盘时刻，归一化后截到 00:00 才能与 AKShare/Tushare 逐行对数
    assert df["dt"].dt.time.unique().tolist() == [pd.Timestamp("2024-01-02 00:00").time()]
    assert df["dt"].iloc[0] == pd.Timestamp("2024-01-02")
    assert df["open"].gt(0).all() and df["amount"].gt(0).all()


def test_normalize_bars_intraday_keeps_time_of_day():
    with fake_sdk("pytdx"):
        hq = _hq()
        df = normalize_bars(hq.make_bars(periods=4, intraday=True, start="2024-01-02 09:35"),
                            Frequency.M5)
    assert df["dt"].iloc[0] == pd.Timestamp("2024-01-02 09:35")
    assert df["dt"].iloc[-1] == pd.Timestamp("2024-01-02 09:50")


def test_normalize_bars_falls_back_to_segmented_fields():
    with fake_sdk("pytdx"):
        hq = _hq()
        df = normalize_bars(hq.make_bars(periods=3, segmented=True), Frequency.D1)
    assert len(df) == 3
    assert df["dt"].iloc[1] == pd.Timestamp("2024-01-03")


def test_normalize_bars_keeps_volume_in_lots_untouched():
    """vol 是「手」（1 手 = 100 股）：归一化不改写数值，换算归使用层，单位必须写进文档。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        recs = hq.make_bars(periods=2)
        df = normalize_bars(recs, Frequency.D1)
    assert df["volume"].tolist() == [r["vol"] for r in recs]
    assert "手" in normalize_bars.__doc__


def test_normalize_bars_empty_and_unparsable():
    empty = normalize_bars([], Frequency.D1)
    assert empty.empty and list(empty.columns) == HISTORY_COLUMNS
    assert normalize_bars(None, Frequency.D1).empty is True
    bogus = normalize_bars([{"open": 1.0, "close": 2.0}], Frequency.D1)   # 无时间字段
    assert bogus.empty and list(bogus.columns) == HISTORY_COLUMNS


# --------------------------------------------------------------------------
# 传输层选择
# --------------------------------------------------------------------------
def test_pytdx_used_when_official_sdk_missing(monkeypatch):
    with fake_sdk("pytdx"):
        _hide_sdks(monkeypatch, "TdxAiData")
        feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0,
                             start_watchdog=False)
        feed.connect()                             # 自动选路：官方缺失 → pytdx
        try:
            d = feed.describe()
            assert d["transport"] == "pytdx"
            assert d["transport_official"] is False
            assert "降级" in d["transport_reason"]
        finally:
            feed.disconnect()


def test_official_sdk_preferred_when_present():
    with fake_sdk("pytdx", "TdxAiData"):
        feed = _feed(transport=None)                 # 自动选路：官方 SDK 优先
        try:
            d = feed.describe()
            assert d["transport"] == "TdxAiData"
            assert d["transport_official"] is True
            assert d["resolved_methods"]["bars"] == "get_kline"
        finally:
            feed.disconnect()


def test_transport_can_be_forced_to_pytdx(monkeypatch):
    with fake_sdk("pytdx", "TdxAiData"):
        feed = _feed(transport="pytdx")
        try:
            assert feed.describe()["transport"] == "pytdx"
            assert "强制" in feed.describe()["transport_reason"]
        finally:
            feed.disconnect()


def test_official_falls_back_to_pytdx_when_handshake_fails():
    with fake_sdk("pytdx", "TdxAiData"):
        import TdxAiData as official
        official.CONNECT_ERROR = RuntimeError("官方 SDK 未登录/权限未开通")
        feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0,
                             start_watchdog=False)
        try:
            feed.connect()
            d = feed.describe()
            assert d["transport"] == "pytdx"                  # 官方握手失败 → 换传输层而不是失败
            assert d["transport_reason"].startswith("官方 SDK 连接失败已降级 pytdx")
        finally:
            official.CONNECT_ERROR = None
            feed.disconnect()


def test_both_sdks_missing_gives_install_guidance(monkeypatch):
    _hide_sdks(monkeypatch, "TdxAiData", "pytdx")
    feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0)
    with pytest.raises(AdapterError) as ei:
        feed.connect()
    msg = str(ei.value)
    assert "pytdx" in msg and "TdxAiData" in msg          # 给「怎么办」而不是堆栈
    assert feed.describe()["connected"] is False


def test_forced_official_without_sdk_gives_guidance(monkeypatch):
    with fake_sdk("pytdx"):
        _hide_sdks(monkeypatch, "TdxAiData")
        feed = TdxAiDataFeed(transport="official", servers=[("10.0.0.1", 7709)])
        with pytest.raises(AdapterError):
            feed.connect()


# --------------------------------------------------------------------------
# 历史 K 线
# --------------------------------------------------------------------------
def test_get_history_normalizes_and_passes_market_and_category():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(market=0, code="000001", periods=10, start="2024-01-02")
        feed = _feed()
        try:
            df = feed.get_history("000001.SZ", Frequency.D1, START.to_pydatetime(),
                                  END.to_pydatetime(), AdjustType.NONE)
            assert list(df.columns) == HISTORY_COLUMNS
            assert len(df) == 10 and df["dt"].is_monotonic_increasing
            assert hq.SCRIPT.args_of("get_security_bars")[0] == (4, 0, "000001", 0,
                                                                 BAR_PAGE_SIZE)
        finally:
            feed.disconnect()


def test_get_history_shanghai_market_bit_is_one():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(market=1, code="600000", periods=3)
        feed = _feed()
        try:
            feed.get_history("600000.SH", Frequency.M5, START.to_pydatetime(),
                             END.to_pydatetime(), AdjustType.NONE)
            assert hq.SCRIPT.args_of("get_security_bars")[0][:3] == (0, 1, "600000")
        finally:
            feed.disconnect()


def test_get_history_pages_back_until_start_reached():
    """区间起点在数据深处：按偏移翻页，直到够到 start（左端不截断=不留空洞）。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=BAR_PAGE_SIZE + 120, start="2020-01-01")
        feed = _feed()
        try:
            df = feed.get_history("000001.SZ", Frequency.D1,
                                  pd.Timestamp("2020-01-01").to_pydatetime(),
                                  pd.Timestamp("2020-01-10").to_pydatetime(), AdjustType.NONE)
        finally:
            feed.disconnect()
    assert len(hq.SCRIPT.args_of("get_security_bars")) == 2      # 第二页够到左端即停
    assert df["dt"].iloc[0] == pd.Timestamp("2020-01-01")
    assert df["dt"].iloc[-1] == pd.Timestamp("2020-01-10")
    assert df["dt"].is_monotonic_increasing


def test_get_history_filters_to_range():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=120, start="2024-01-02")
        feed = _feed()
        try:
            df = feed.get_history("000001.SZ", Frequency.D1,
                                  pd.Timestamp("2024-02-01").to_pydatetime(),
                                  pd.Timestamp("2024-02-10").to_pydatetime(), AdjustType.NONE)
        finally:
            feed.disconnect()
    assert df["dt"].min() == pd.Timestamp("2024-02-01")
    assert df["dt"].max() == pd.Timestamp("2024-02-10")
    assert len(df) == 10


def test_get_history_empty_returns_empty_table():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = []
        feed = _feed()
        try:
            df = feed.get_history("000001.SZ", Frequency.D1, START.to_pydatetime(),
                                  END.to_pydatetime(), AdjustType.NONE)
        finally:
            feed.disconnect()
    assert df.empty and list(df.columns) == HISTORY_COLUMNS


def test_adjust_requests_refused_not_fabricated():
    """复权口径红线：本地没有通达信复权因子 → 明确拒绝，且一次取数请求都不发。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=3)
        feed = _feed()
        try:
            for adjust in (AdjustType.FORWARD, AdjustType.BACKWARD):
                with pytest.raises(FeedCapabilityError) as ei:
                    feed.get_history("000001.SZ", Frequency.D1, START.to_pydatetime(),
                                     END.to_pydatetime(), adjust)
                assert "复权" in str(ei.value)
            assert hq.SCRIPT.methods("get_security_bars") == []
        finally:
            feed.disconnect()


def test_tick_freq_points_to_transactions():
    with fake_sdk("pytdx"):
        feed = _feed()
        try:
            with pytest.raises(FeedCapabilityError) as ei:
                feed.get_history("000001.SZ", Frequency.TICK, START.to_pydatetime(),
                                 END.to_pydatetime(), AdjustType.NONE)
            assert "get_transactions" in str(ei.value)
        finally:
            feed.disconnect()


def test_unsupported_freq_raises():
    with fake_sdk("pytdx"):
        feed = _feed()
        try:
            with pytest.raises(FeedCapabilityError):
                feed.get_history("000001.SZ", "13m", START.to_pydatetime(),
                                 END.to_pydatetime(), AdjustType.NONE)
        finally:
            feed.disconnect()


# --------------------------------------------------------------------------
# 分笔
# --------------------------------------------------------------------------
def test_get_transactions_normalized_ascending():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.transactions = hq.make_transactions(count=6)
        feed = _feed()
        try:
            df = feed.get_transactions("000001.SZ", count=500)
        finally:
            feed.disconnect()
    assert list(df.columns) == ["dt", "price", "volume", "num", "direction"]
    assert df["dt"].is_monotonic_increasing               # 源给倒序，归一化转正
    assert len(df) == 6
    assert df["volume"].iloc[0] == 10.0                   # 单位：手（不换算）


def test_get_transactions_empty_returns_empty_table():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.transactions = []
        feed = _feed()
        try:
            df = feed.get_transactions("000001.SZ")
        finally:
            feed.disconnect()
    assert df.empty and list(df.columns) == ["dt", "price", "volume", "num", "direction"]


def test_transactions_signature_with_category_first():
    """部分 pytdx 版本首参是 category：按签名适配，不猜一种就收工。"""
    class _CategoryFirstApi:
        def __init__(self) -> None:
            self.seen = None

        def get_transaction_data(self, category, market, code, start, count):
            self.seen = (category, market, code, start, count)
            return []

    api = _CategoryFirstApi()
    transport = TdxPytdxTransport(servers=[("10.0.0.1", 7709)])
    transport._api = api
    assert transport.transactions(0, "000001", 0, 100) == []
    assert api.seen == (0, 0, "000001", 0, 100)


def test_transactions_time_parts_combined_with_year_month_day():
    """旧版分笔返回没有 datetime 字符串，只有 time + year/month/day：要能组合出 dt。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.transactions = hq.make_transactions(count=3, with_datetime=False)
        feed = _feed()
        try:
            df = feed.get_transactions("000001.SZ")
        finally:
            feed.disconnect()
    assert len(df) == 3
    assert df["dt"].is_monotonic_increasing
    assert df["dt"].iloc[0].strftime("%H:%M:%S") == "09:30:00"


def test_transactions_without_any_date_field_drops_rows_not_guesses():
    """只有 HH:MM:SS 而完全没有日期字段：丢弃该行，不拿「今天」去补（宁可少不可错）。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.transactions = [{"price": 10.0, "vol": 5.0, "num": 1,
                                   "direction": "BUY", "time": "09:30:00"}]
        feed = _feed()
        try:
            df = feed.get_transactions("000001.SZ")
        finally:
            feed.disconnect()
    assert df.empty


# --------------------------------------------------------------------------
# 请求队列串行性（连接对象非线程安全）
# --------------------------------------------------------------------------
def test_requests_are_serialized_on_one_worker_thread():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=5)
        hq.SCRIPT.quotes = [hq.make_quotes()]
        hq.SCRIPT.slow = 0.01                            # 放大竞态窗口：并发进入会被 mock 计数
        feed = _feed()
        try:
            hq.SCRIPT.reset_calls()
            with cf.ThreadPoolExecutor(max_workers=8) as pool:
                futs = [pool.submit(feed.get_history, "000001.SZ", Frequency.D1,
                                    START.to_pydatetime(), END.to_pydatetime(), AdjustType.NONE)
                        for _ in range(12)]
                futs += [pool.submit(feed.get_transactions, "000001.SZ") for _ in range(4)]
                for f in futs:
                    f.result()
            assert len(hq.SCRIPT.methods("get_security_bars")) == 12
            assert len(hq.SCRIPT.thread_ids()) == 1          # 只有工作线程碰过连接对象
            assert hq.SCRIPT.overlaps == 0                   # 无并发进入（mock 侧计数）
        finally:
            hq.SCRIPT.slow = 0.0
            feed.disconnect()


def test_single_worker_thread_and_parallel_workers_refused():
    with fake_sdk("pytdx"):
        feed = _feed()
        assert feed._worker is not None and feed._worker.name == "tdx-worker"
        alive_workers = [t for t in threading.enumerate()
                         if t.name == "tdx-worker" and t.is_alive()]
        assert feed._worker in alive_workers
        feed.disconnect()
        assert not feed._worker.is_alive()
    with pytest.raises(ValueError):
        TdxAiDataFeed(max_workers=4)                     # 多工作线程 = 并发碰连接对象，禁止


def test_request_timeout_raises_connection_lost():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=3)
        hq.SCRIPT.slow = 0.5
        feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0,
                             request_timeout=0.05, start_watchdog=False, transport="pytdx")
        feed.connect()
        try:
            with pytest.raises(ConnectionLostError) as ei:
                feed.get_history("000001.SZ", Frequency.D1, START.to_pydatetime(),
                                 END.to_pydatetime(), AdjustType.NONE)
            assert "超时" in str(ei.value)
        finally:
            hq.SCRIPT.slow = 0.0
            feed.disconnect()


def test_requests_before_connect_fail_closed():
    feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0)
    with pytest.raises(ConnectionLostError):
        feed.get_transactions("000001.SZ")
    with pytest.raises(ConnectionLostError):
        feed.subscribe_realtime(["000001.SZ"], lambda t: None)
    assert feed.health_check() is False                  # 未连接不抛，只报不可用


# --------------------------------------------------------------------------
# 探活与断线
# --------------------------------------------------------------------------
def test_health_check_true_then_cached_60s():
    with fake_sdk("pytdx"):
        hq = _hq()
        feed = _feed()
        try:
            assert feed.health_check() is True
            n = len(hq.SCRIPT.methods("get_security_count"))
            assert feed.health_check() is True
            assert len(hq.SCRIPT.methods("get_security_count")) == n     # 缓存内不重复外呼
        finally:
            feed.disconnect()


def test_health_check_swallows_probe_error_and_returns_false():
    with fake_sdk("pytdx"):
        hq = _hq()
        feed = _feed()
        try:
            hq.SCRIPT.fail_methods = {"get_security_count"}
            feed._health_cache = (0.0, False)             # 绕过缓存：探活必须真外呼
            assert feed.health_check() is False
        finally:
            hq.SCRIPT.fail_methods = set()
            feed.disconnect()


def test_mid_call_disconnect_is_retried_once_then_reported():
    """断线必须抛 ConnectionLostError（宪法），内部最多重试 1 次且重试用重连后的连接。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=BAR_PAGE_SIZE * 2, start="2020-01-01")
        hq.SCRIPT.fail_after = 1                          # 第 1 次之后所有取数都抛
        feed = _feed()
        before = hq.SCRIPT.instantiated
        try:
            with pytest.raises(ConnectionLostError):
                feed.get_history("000001.SZ", Frequency.D1,
                                 pd.Timestamp("2020-01-01").to_pydatetime(),
                                 pd.Timestamp("2020-01-02").to_pydatetime(), AdjustType.NONE)
        finally:
            feed.disconnect()
    assert hq.SCRIPT.instantiated > before                # 重试走的是重连后的新连接对象
    assert len(hq.SCRIPT.methods("get_security_bars")) == 3   # 首页 + 断线页 + 1 次重试
    assert hq.SCRIPT.methods("connect") == ["connect", "connect"]


def test_server_list_tried_in_order():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.connect_fails = 2                       # 前两台拒连 → 第三台成功
        feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709), ("10.0.0.2", 7709),
                                      ("10.0.0.3", 7709)], min_interval=0.0,
                             start_watchdog=False, transport="pytdx")
        feed.connect()
        try:
            assert feed.describe()["server"] == ("10.0.0.3", 7709)
            assert [h for h, _ in hq.SCRIPT.connect_attempts] == ["10.0.0.1", "10.0.0.2",
                                                                  "10.0.0.3"]
        finally:
            feed.disconnect()


def test_all_servers_refused_raises_connection_lost():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.connect_ok = False
        feed = TdxAiDataFeed(servers=[("10.0.0.1", 7709)], min_interval=0.0,
                             transport="pytdx")
        with pytest.raises(ConnectionLostError) as ei:
            feed.connect()
        assert "试连失败" in str(ei.value)
        assert feed.describe()["connected"] is False


# --------------------------------------------------------------------------
# 实时：轮询合成 Tick + 引用计数
# --------------------------------------------------------------------------
def test_subscribe_polls_and_delivers_ticks():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001", price=11.5)]
        got: list = []
        gate = threading.Semaphore(0)

        def cb(t) -> None:
            got.append(t)
            gate.release()

        feed = _feed(poll_interval=0.03)
        try:
            feed.subscribe_realtime(["000001.SZ"], cb)
            assert gate.acquire(timeout=3.0)
            tick = got[-1]
            assert tick.symbol == "000001.SZ" and tick.last == 11.5
            assert tick.volume == 20000.0                 # 快照量纲同样是手
            assert len(tick.bids) == 5 and len(tick.asks) == 5
            assert hq.SCRIPT.args_of("get_security_quotes")[0] == ((0, "000001"),)
        finally:
            feed.disconnect()


def test_reference_counting_single_external_poll_and_unsubscribe():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001"),
                            hq.make_quotes(market=1, code="600000")]
        feed = _feed(poll_interval=30.0)                  # 轮询线程活着但不自己抢节拍
        try:
            feed.subscribe_realtime(["000001.SZ"], lambda t: None)
            feed.subscribe_realtime(["000001.SZ"], lambda t: None)   # 第二个策略订同一标的
            feed.subscribe_realtime(["600000.SH"], lambda t: None)
            assert feed.describe()["refcounts"] == {"000001.SZ": 2, "600000.SH": 1}
            hq.SCRIPT.reset_calls()
            assert feed._poll_once() == 2                 # 两份订阅 → 两条 Tick
            both = [a for a in hq.SCRIPT.args_of("get_security_quotes") if len(a) == 2]
            assert both == [((0, "000001"), (1, "600000"))]        # 两个订阅 → 外部只一份请求
            feed.unsubscribe_realtime(["000001.SZ"])
            assert feed.describe()["refcounts"] == {"600000.SH": 1}
            assert feed._poll_thread.is_alive()           # 仍有引用 → 不停轮询
            feed.unsubscribe_realtime(["600000.SH"])
            assert feed.describe()["polling"] is False    # 引用计数归零 → 停轮询
            assert feed._tick_thread is None
        finally:
            feed.disconnect()


def test_unsubscribe_leaves_other_subscribers_intact():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001")]
        feed = _feed(poll_interval=30.0)
        try:
            feed.subscribe_realtime(["000001.SZ", "600000.SH"], lambda t: None)
            feed.unsubscribe_realtime(["600000.SH"])
            assert feed._poll_symbols() == ["000001.SZ"]
            assert feed._poll_thread.is_alive()
        finally:
            feed.disconnect()


def test_subscribe_rejects_unknown_symbol_before_polling():
    with fake_sdk("pytdx"):
        feed = _feed()
        try:
            with pytest.raises(SymbolError):
                feed.subscribe_realtime(["830001.BJ"], lambda t: None)
            assert feed.describe()["subscribed"] == []
            assert feed.describe()["polling"] is False
        finally:
            feed.disconnect()


def test_callback_exception_isolated():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001")]
        got: list = []

        def bad_cb(t) -> None:
            raise RuntimeError("策略回调炸了")

        feed = _feed(poll_interval=30.0)
        try:
            feed.subscribe_realtime(["000001.SZ"], bad_cb)
            feed.subscribe_realtime(["000001.SZ"], got.append)
            feed._poll_once()
            for _ in range(200):
                if got:
                    break
                threading.Event().wait(0.01)
            assert got and got[0].symbol == "000001.SZ"    # 前一个回调抛了也不影响后一个
        finally:
            feed.disconnect()


def test_snapshot_without_data_yields_no_ticks():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = None                            # 真返回在无数据时给 None/空
        feed = _feed(poll_interval=30.0)
        try:
            feed.subscribe_realtime(["000001.SZ"], lambda t: None)
            assert feed._poll_once() == 0
        finally:
            feed.disconnect()


def test_disconnect_stops_poller_and_closes_transport():
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes()]
        feed = _feed()
        feed.subscribe_realtime(["000001.SZ"], lambda t: None)
        feed.disconnect()
        assert feed.describe()["connected"] is False
        assert feed.describe()["polling"] is False
        assert "disconnect" in hq.SCRIPT.methods()
        feed.disconnect()                                  # 幂等


def test_poller_marks_lost_on_disconnect_and_watchdog_replays_subscription():
    """宪法义务：断线可被 health_check 探测 + 重连成功后重放订阅集合。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001")]
        got: list = []
        feed = _feed(poll_interval=30.0)
        try:
            feed.subscribe_realtime(["000001.SZ"], got.append)
            hq.SCRIPT.reset_calls()
            hq.SCRIPT.fail_methods = {"get_security_count", "get_security_quotes"}
            assert feed.watchdog.check_once() is False     # 探活失败 → 判死
            assert feed.watchdog.health.state == "lost"
            hq.SCRIPT.fail_methods = set()                 # 外部恢复
            before = hq.SCRIPT.instantiated
            assert feed.watchdog.recover_once() is True
            assert hq.SCRIPT.instantiated > before         # 重连重建连接对象
            args = hq.SCRIPT.args_of("get_security_quotes")
            assert args and args[-1] == ((0, "000001"),)   # 订阅集合重放
            assert feed.watchdog.health.state == "ok"
            for _ in range(200):
                if got:
                    break
                threading.Event().wait(0.01)
            assert got and got[-1].symbol == "000001.SZ"   # 重放后 1 个周期内就有数
        finally:
            feed.disconnect()


def test_poll_thread_only_marks_lost_never_reconnects():
    """线程纪律：轮询线程不重连（重连归 watchdog），只 mark_lost。"""
    with fake_sdk("pytdx"):
        hq = _hq()
        hq.SCRIPT.quotes = [hq.make_quotes()]
        feed = _feed(poll_interval=0.02)
        try:
            feed.subscribe_realtime(["000001.SZ"], lambda t: None)
            hq.SCRIPT.fail_methods = {"get_security_quotes", "get_security_count"}
            for _ in range(200):
                if feed.watchdog.health.state == "lost":
                    break
                threading.Event().wait(0.02)
            assert feed.watchdog.health.state == "lost"
            assert feed.watchdog.health.reconnects == 0       # 恢复动作归 watchdog，不在这条线程
        finally:
            hq.SCRIPT.fail_methods = set()
            feed.disconnect()


# --------------------------------------------------------------------------
# 快照 → Tick
# --------------------------------------------------------------------------
def test_quote_to_tick_five_levels():
    with fake_sdk("pytdx"):
        hq = _hq()
        rec = hq.make_quotes(market=1, code="600000", price=12.0)
        tick = quote_to_tick(rec, pd.Timestamp("2024-01-02 09:31").to_pydatetime())
    assert tick.symbol == "600000.SH"
    assert tick.last == 12.0 and tick.open == pytest.approx(11.95)
    assert tick.bids[0] == (11.99, 100.0)
    assert tick.asks[0] == (12.01, 200.0)


def test_quote_to_tick_dirty_record_skipped():
    with fake_sdk("pytdx"):
        hq = _hq()
        bad = hq.make_quotes()
        bad.pop("code")
        assert quote_to_tick(bad, pd.Timestamp("2024-01-02").to_pydatetime()) is None


# --------------------------------------------------------------------------
# 官方传输层（假 SDK 走同一套归一化）
# --------------------------------------------------------------------------
def test_official_transport_history_snapshot_and_probe():
    with fake_sdk("pytdx", "TdxAiData"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=4)
        hq.SCRIPT.quotes = [hq.make_quotes(market=0, code="000001")]
        transport = TdxOfficialTransport()
        transport.open()
        try:
            bars = normalize_bars(transport.bars("1d", 0, "000001", 0, 100), Frequency.D1)
            assert len(bars) == 4 and list(bars.columns) == HISTORY_COLUMNS
            tick = quote_to_tick(transport.quotes([(0, "000001")])[0],
                                 pd.Timestamp("2024-01-02").to_pydatetime())
            assert tick.symbol == "000001.SZ"
            assert transport.probe() == 1000
            assert transport.resolved["connect"] == "connect"
        finally:
            transport.close()


def test_official_transport_history_via_feed():
    """官方传输层的周期取值是字符串（需在线核对），适配器按传输层切换取值。"""
    with fake_sdk("pytdx", "TdxAiData"):
        hq = _hq()
        hq.SCRIPT.bars = hq.make_bars(periods=6)
        feed = _feed(transport=None)                     # 自动选路 → 官方优先
        try:
            df = feed.get_history("000001.SZ", Frequency.D1, START.to_pydatetime(),
                                  END.to_pydatetime(), AdjustType.NONE)
            assert len(df) == 6
            period_used = hq.SCRIPT.args_of("official.get_kline")[0][2]
            assert period_used == "1d" and feed.describe()["transport"] == "TdxAiData"
        finally:
            feed.disconnect()


def test_financial_capability_refused():
    with fake_sdk("pytdx"):
        feed = _feed()
        try:
            with pytest.raises(FeedCapabilityError):
                feed.get_financial_data("000001.SZ")
        finally:
            feed.disconnect()
