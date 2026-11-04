"""QMT/miniQMT 行情通道（xtquant.xtdata）离线单测——全程走 tests/mocks/xtquant 假 SDK。

覆盖面（对应宪法条款）：
- 历史：列归一化 + dt 升序 + 区间格式 + freq/dividend_type 映射 + 无数据返回空表 +
  「内部最多重试 1 次」+ 复权口径不可用时的回退留痕；
- 实时：SDK 推送线程只入队 → 工作线程归一化（Tick 五档 / Bar），退订与重连重放；
- 运维：探活缓存与异常吞掉、未连接拒订阅、无 SDK 时的登录引导、财务能力明确拒绝。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

from quantdesk.adapters import describe_adapters
from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS
from quantdesk.adapters.base.optional import dependency_available
from quantdesk.adapters.data.qmt_xtdata import QmtXtdataFeed, normalize_history, to_tick
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import ConnectionLostError, FeedCapabilityError
from quantdesk.core.models import Bar, Tick
from tests.mocks import fake_sdk

_FULL_TICK = {
    "time": 1700000000000,
    "lastPrice": 10.50, "open": 10.20, "high": 10.60, "low": 10.10,
    "volume": 1_234_500.0, "amount": 12_900_000.0,
    "bidPrice": [10.49, 10.48, 10.47, 10.46, 10.45],
    "bidVol": [100, 200, 300, 400, 500],
    "askPrice": [10.50, 10.51, 10.52, 10.53, 10.54],
    "askVol": [10, 20, 30, 40, 50],
}


def _daily_frame(index: list[str], n: int) -> pd.DataFrame:
    """构造 xtdata 风格的宽表：时间在 index（字符串），并夹带多余列。"""
    return pd.DataFrame({
        "open": [1.0] * n, "high": [2.0] * n, "low": [0.5] * n,
        "close": [1.5 + i for i in range(n)],
        "volume": [100] * n, "amount": [150.0] * n,
        "period": ["1d"] * n,            # 多余列必须被丢掉
    }, index=index)


@contextmanager
def feed_ctx(start_worker: bool = False, **kwargs):
    """假 SDK 环境 + 已连接的 feed（退出必 disconnect，不给后续用例留线程）。"""
    with fake_sdk("xtquant"):
        import xtquant
        xtquant.reset()
        feed = QmtXtdataFeed(start_worker=start_worker, **kwargs)
        try:
            yield feed, xtquant
        finally:
            feed.disconnect()


@contextmanager
def connected_feed(start_worker: bool = False, **kwargs):
    with feed_ctx(start_worker=start_worker, **kwargs) as (feed, xtquant):
        feed.connect()
        yield feed, xtquant


# ==================== 历史 K 线 ====================
def test_history_normalizes_columns_and_sorts_ascending():
    with connected_feed() as (feed, xtquant):
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(
            ["20200103", "20200102", "20200101"], 3)
        out = feed.get_history("600000.SH", Frequency.D1,
                               datetime(2020, 1, 1), datetime(2020, 1, 3))
    assert list(out.columns) == HISTORY_COLUMNS
    assert str(out["dt"].dtype) == "datetime64[ns]"
    assert out["dt"].is_monotonic_increasing
    assert out["dt"].iloc[0] == pd.Timestamp("2020-01-01")
    assert out["dt"].iloc[-1] == pd.Timestamp("2020-01-03")


def test_history_intraday_parses_14_digit_index_with_time():
    with connected_feed() as (feed, xtquant):
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(
            ["20200102093500", "20200102093400"], 2)
        out = feed.get_history("600000.SH", Frequency.M1,
                               datetime(2020, 1, 2, 9, 30), datetime(2020, 1, 2, 10, 0))
    assert [str(x) for x in out["dt"]] == ["2020-01-02 09:34:00", "2020-01-02 09:35:00"]
    assert out["close"].tolist() == [2.5, 1.5]          # 行随 dt 一起重排（原表是倒序）


def test_history_no_rows_returns_empty_frame_not_raise():
    with connected_feed() as (feed, xtquant):
        xtquant.DATA["history"] = {}                     # 本地仓库没有该标的
        out = feed.get_history("600000.SH", Frequency.D1,
                               datetime(2020, 1, 1), datetime(2020, 1, 3))
    assert out.empty and list(out.columns) == HISTORY_COLUMNS


@pytest.mark.parametrize(("freq", "period", "stamp_len"), [
    (Frequency.M1, "1m", 14), (Frequency.M5, "5m", 14), (Frequency.M15, "15m", 14),
    (Frequency.M30, "30m", 14), (Frequency.H1, "60m", 14), (Frequency.D1, "1d", 8),
    (Frequency.W1, "1w", 8), (Frequency.MO1, "1M", 8),
])
def test_freq_map_and_range_format(freq, period, stamp_len):
    with connected_feed() as (feed, xtquant):
        feed.get_history("600000.SH", freq, datetime(2020, 1, 1, 9, 31),
                         datetime(2020, 1, 3, 15, 0))
        call = xtquant.find("get_market_data_ex")
    assert call["kwargs"]["period"] == period
    assert len(call["kwargs"]["start_time"]) == stamp_len


@pytest.mark.parametrize(("adjust", "dividend"), [
    (AdjustType.NONE, "none"), (AdjustType.FORWARD, "front"), (AdjustType.BACKWARD, "back"),
])
def test_dividend_type_mapping(adjust, dividend):
    with connected_feed() as (feed, xtquant):
        feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                         datetime(2020, 1, 2), adjust=adjust)
        call = xtquant.find("get_market_data_ex")
    assert call["kwargs"]["dividend_type"] == dividend


def test_tick_freq_is_not_history_capability():
    with connected_feed() as (feed, xtquant):
        with pytest.raises(FeedCapabilityError) as ei:
            feed.get_history("600000.SH", Frequency.TICK, datetime(2020, 1, 1),
                             datetime(2020, 1, 2))
    assert "tick" in str(ei.value).lower()


def test_unsupported_dividend_value_falls_back_to_front_with_warning(caplog):
    with connected_feed() as (feed, xtquant):
        with caplog.at_level("WARNING"):
            feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                             datetime(2020, 1, 2), adjust="front_ratio")
        call = xtquant.find("get_market_data_ex")
    assert call["kwargs"]["dividend_type"] == "front"
    assert any("front_ratio" in r.getMessage() for r in caplog.records)


def test_sdk_rejected_dividend_retries_once_with_front():
    with connected_feed() as (feed, xtquant):
        xtquant.SCRIPT["reject_dividend"] = {"back"}
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(["20200101"], 1)
        out = feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                               datetime(2020, 1, 2), adjust=AdjustType.BACKWARD)
        divs = [c["kwargs"]["dividend_type"] for c in xtquant.calls_of("get_market_data_ex")]
    assert not out.empty
    # 后复权尝试（含适配器内那一次重试）失败后，必须以 front 再走一次，而不是直接报错
    assert divs[-1] == "front"
    assert "back" in divs


def test_transient_sdk_error_is_retried_exactly_once():
    with connected_feed() as (feed, xtquant):
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(["20200101"], 1)
        xtquant.SCRIPT["fail"] = {"get_market_data_ex": 1}
        out = feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                               datetime(2020, 1, 2))
        assert len(xtquant.calls_of("get_market_data_ex")) == 2      # 1 次原始 + 1 次重试
        assert not out.empty

        xtquant.reset()
        feed.connect()
        xtquant.SCRIPT["fail"] = {"get_market_data_ex": 2}           # 再失败就不试了
        with pytest.raises(ConnectionLostError):
            feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                             datetime(2020, 1, 2))
        assert len(xtquant.calls_of("get_market_data_ex")) == 2


def test_history_failure_message_carries_login_guidance():
    with connected_feed() as (feed, xtquant):
        xtquant.SCRIPT["fail"] = {"get_market_data_ex": 5}
        with pytest.raises(ConnectionLostError) as ei:
            feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                             datetime(2020, 1, 2))
    assert "miniQMT" in str(ei.value)


def test_download_history_runs_before_read_and_incrementally():
    with connected_feed() as (feed, xtquant):
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(["20200101"], 1)
        feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1), datetime(2020, 1, 2))
        methods = [c["method"] for c in xtquant.CALLS]
        dl = xtquant.find("download_history_data")
    assert methods.index("download_history_data") < methods.index("get_market_data_ex")
    assert dl["kwargs"]["incrementally"] is True


def test_download_falls_back_when_incrementally_unsupported():
    """老版本 xtdata 没有 incrementally 参数：适配器必须换签名再试，而不是整体失败。"""
    with connected_feed() as (feed, xtquant):
        xtquant.SCRIPT["download_rejects_incrementally"] = True
        xtquant.DATA["history"]["600000.SH"] = _daily_frame(["20200101"], 1)
        out = feed.get_history("600000.SH", Frequency.D1, datetime(2020, 1, 1),
                               datetime(2020, 1, 2))
        downloads = xtquant.calls_of("download_history_data")
    assert not out.empty
    assert len(downloads) == 2
    assert "incrementally" in downloads[0]["kwargs"]
    assert "incrementally" not in downloads[1]["kwargs"]


def test_normalize_history_is_pure_and_drops_unparsable_rows():
    df = _daily_frame(["20200101", "bad-date", "20200102"], 3)
    out = normalize_history(df, "1d")
    assert len(out) == 2                                   # 解析不动的行被丢掉
    assert list(out.columns) == HISTORY_COLUMNS


def test_normalize_history_missing_column_is_structured_error():
    with pytest.raises(FeedCapabilityError):
        normalize_history(pd.DataFrame({"open": [1.0]}, index=["20200101"]), "1d")


# ==================== 探活 ====================
def test_health_check_cached_for_60s_and_probe_bypasses_cache():
    with connected_feed() as (feed, xtquant):
        assert feed.health_check() is True
        assert feed.health_check() is True
        assert len(xtquant.calls_of("get_stock_list_in_sector")) == 1   # 第二次吃缓存

        xtquant.SCRIPT["sector_raises"] = True
        assert feed.health_check() is True                 # 缓存期内仍报上次结果
        assert feed._probe() is False                      # 守护线程走的是实时探活

        feed.health_ttl_s = 0.0
        assert feed.health_check() is False                # 缓存过期后如实反映


def test_health_check_swallows_sdk_exception_and_returns_false():
    with connected_feed() as (feed, xtquant):
        xtquant.SCRIPT["sector_raises"] = True
        xtquant.SCRIPT["fail"] = {"get_stock_list_in_sector": 3}
        feed.health_ttl_s = 0.0
        assert feed.health_check() is False                # 不抛，只报状态


# ==================== 实时 ====================
def test_realtime_push_only_enqueues_on_sdk_thread_then_normalizes_on_drain():
    with connected_feed() as (feed, xtquant):
        got: list = []
        feed.subscribe_realtime(["600000.SH"], got.append)
        fired = xtquant.xtdata.fire_quote("600000.SH", "tick", {"600000.SH": [_FULL_TICK]})
        assert fired == 1
        assert got == []                                   # 回调线程绝不向下分发
        assert feed._push_q.qsize() == 1                   # 只入队
        assert feed._drain_once(0) == 1
    tick = got[0]
    assert isinstance(tick, Tick) and tick.symbol == "600000.SH"
    assert tick.last == 10.50 and tick.open == 10.20 and tick.high == 10.60
    assert len(tick.bids) == 5 and len(tick.asks) == 5
    assert tick.bids[0] == (10.49, 100.0)
    assert tick.asks[-1] == (10.54, 50.0)


def test_realtime_push_accepts_dict_shaped_payload():
    """SDK 推送有 {code: [rec]} 与 {code: rec} 两种形态，都得吃下（版本差异不静默丢数据）。"""
    with connected_feed() as (feed, xtquant):
        got: list = []
        feed.subscribe_realtime(["000001.SZ"], got.append)
        xtquant.xtdata.fire_quote("000001.SZ", "tick", {"000001.SZ": _FULL_TICK})
        feed._drain_once(0)
    assert len(got) == 1 and got[0].symbol == "000001.SZ"


def test_realtime_minute_push_becomes_bar():
    with connected_feed() as (feed, xtquant):
        got: list = []
        feed.subscribe_realtime(["600000.SH"], got.append)
        xtquant.xtdata.fire_quote("600000.SH", "1m", {"600000.SH": [{
            "time": 1700000000000, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1,
            "volume": 100.0, "amount": 110.0}]})
        feed._drain_once(0)
    bar = got[0]
    assert isinstance(bar, Bar) and bar.freq is Frequency.M1
    assert bar.close == 1.1


def test_realtime_delivery_runs_on_worker_thread_not_sdk_thread():
    """宪法线程纪律的端到端证明：慢消费者也不会卡住 SDK 推送线程。"""
    with connected_feed(start_worker=True) as (feed, xtquant):
        entered, release = threading.Event(), threading.Event()
        seen: list[tuple[str, object]] = []

        def slow_cb(item):
            entered.set()
            assert release.wait(timeout=5.0), "消费者被允许阻塞（在工作线程上）"
            seen.append((threading.current_thread().name, item))

        feed.subscribe_realtime(["600000.SH"], slow_cb)
        xtquant.xtdata.fire_quote("600000.SH", "tick", {"600000.SH": [_FULL_TICK]})
        assert entered.wait(timeout=5.0)                   # 推送调用本身没有被阻塞
        assert seen == []                                  # 分发还没发生在测试线程
        release.set()
        for _ in range(100):
            if seen:
                break
            threading.Event().wait(0.02)
        assert seen and isinstance(seen[0][1], Tick)
        assert seen[0][0] == "qmt-rt"                      # 归一化+分发在工作线程，不是推送线程


def test_unsubscribe_realtime_clears_handles_and_calls_sdk():
    with connected_feed() as (feed, xtquant):
        feed.subscribe_realtime(["600000.SH", "000001.SZ"], lambda e: None)
        assert len(feed._handles) == 4                     # 2 标的 × (tick + 1m)
        before = xtquant.xtdata.sub_count()
        feed.unsubscribe_realtime(["600000.SH"])
        unsub = xtquant.find("unsubscribe_quote")
        assert len(unsub["args"]) == 2                     # 该标的的 tick + 1m 两个句柄
        assert all(isinstance(s, int) for s in unsub["args"])
        assert len(feed._handles) == 2
        assert xtquant.xtdata.sub_count() == before - 2    # 假 SDK 侧句柄也释放了
        feed.unsubscribe_realtime(["000001.SZ"])
        assert feed._handles == {}
        assert xtquant.xtdata.sub_count() == 0


def test_subscribe_realtime_registers_both_periods_per_symbol():
    with connected_feed() as (feed, xtquant):
        feed.subscribe_realtime(["600000.SH"], lambda e: None)
        subs = [(c["kwargs"]["stock_code"], c["kwargs"]["period"])
                for c in xtquant.calls_of("subscribe_quote")]
    assert subs == [("600000.SH", "tick"), ("600000.SH", "1m")]


def test_subscribe_before_connect_fails_closed():
    with fake_sdk("xtquant"):
        feed = QmtXtdataFeed()
        try:
            with pytest.raises(ConnectionLostError) as ei:
                feed.subscribe_realtime(["600000.SH"], lambda e: None)
        finally:
            feed.disconnect()
    assert "miniQMT" in str(ei.value)


# ==================== 断线自愈 ====================
def test_watchdog_lost_then_reconnect_replays_subscription_list():
    with connected_feed() as (feed, xtquant):
        feed.subscribe_realtime(["600000.SH", "000001.SZ"], lambda e: None)
        xtquant.CALLS.clear()

        xtquant.SCRIPT["sector_raises"] = True             # 客户端被登出
        assert feed.watchdog.check_once() is False
        assert feed.watchdog.health.state == "lost"

        xtquant.SCRIPT.pop("sector_raises")
        xtquant.CALLS.clear()
        assert feed.watchdog.recover_once() is True        # 退避后重连
        replayed = [(c["kwargs"]["stock_code"], c["kwargs"]["period"])
                    for c in xtquant.calls_of("subscribe_quote")]
        assert feed.watchdog.health.state == "ok"
        assert len(feed._handles) == 4                     # 句柄表已重建（可继续退订/再重放）
    assert sorted(replayed) == sorted([("600000.SH", "tick"), ("600000.SH", "1m"),
                                       ("000001.SZ", "tick"), ("000001.SZ", "1m")])


def test_watchdog_interval_and_backoff_come_from_settings():
    from quantdesk.config import get_settings

    s = get_settings()
    with connected_feed() as (feed, _xtquant):
        assert feed.watchdog is not None
        assert feed.watchdog.interval == s.watchdog_interval_s
        assert feed.watchdog.backoff.cap == s.backoff_cap_s


def test_reconnect_without_subscription_is_noop():
    with connected_feed() as (feed, xtquant):
        assert feed._resubscribe_all() == 0


# ==================== 能力/元数据 ====================
def test_financial_capability_explicitly_absent():
    with connected_feed() as (feed, xtquant):
        assert FeedCapability.FINANCIAL not in feed.capabilities
        with pytest.raises(FeedCapabilityError) as ei:
            feed.get_financial_data("600000.SH")
    assert "tushare" in str(ei.value)


def test_registry_meta_for_qmt_feed():
    rows = {r["name"]: r for r in describe_adapters()}
    row = rows["qmt_xtdata"]
    assert row["kind"] == "data" and row["official"] is True
    assert row["requires"] == ["xtquant"]
    assert set(row["capabilities"]) == {"history_bar", "realtime_tick", "realtime_bar"}
    assert "miniQMT" in row["label"] and row["prereq"]


def test_connect_without_sdk_gives_login_guidance():
    """没装 xtquant（或客户端没跑）时报的必须是「怎么办」，不是裸 ImportError 堆栈。"""
    if dependency_available("xtquant"):
        pytest.skip("本机存在真实 xtquant，无法验证「缺 SDK」分支")
    feed = QmtXtdataFeed()
    try:
        with pytest.raises(ConnectionLostError) as ei:
            feed.connect()
    finally:
        feed.disconnect()
    msg = str(ei.value)
    assert "miniQMT" in msg and "xtquant" in msg


def test_to_tick_zero_price_levels_are_dropped():
    """停牌/集合竞价时买一价为 0：这类档位不能进盘口，否则下游会拿 0 元挂单做决策。"""
    rec = dict(_FULL_TICK, bidPrice=[0.0, 10.48, 0, 0, 0], bidVol=[100, 200, 0, 0, 0])
    tick = to_tick("600000.SH", rec)
    assert tick.bids == [(10.48, 200.0)]
