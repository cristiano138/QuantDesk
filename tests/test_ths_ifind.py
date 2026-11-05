"""同花顺 iFinD 适配器单测（全离线：假 SDK + tmp 目录，不触网、不消耗真实配额）。

覆盖：响应解析归一化 / 复权与周期参数映射 / 财务长表（三种结构抹平）/
会话失效自动重登（只重登一次）/ 配额计数与强缓存（命中不消耗）/ 日志脱敏 /
health_check 语义 / 能力守卫 / 实时回调只入队。
真实账号到位后的在线核对项见 docs/integration/ths_ifind.md。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from datetime import date, datetime

import pandas as pd
import pytest

import quantdesk.adapters.data.ths_ifind as m
from quantdesk.adapters import describe_adapters, get_adapter_class
from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import (
    AdapterError,
    ConfigError,
    ConnectionLostError,
    FeedCapabilityError,
)
from quantdesk.core.models import Tick
from tests.mocks import fake_sdk

SYMBOL = "600000.SH"
START = datetime(2024, 1, 2)
END = datetime(2024, 1, 4)
REFRESH_TOKEN = "REFRESH-SECRET-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_local_vault(monkeypatch):
    """凭据一律显式注入：保险库/Settings 查找在测试里恒为空，结果与本机配置无关。"""
    monkeypatch.setattr(m, "_secret_lookup", lambda key, attr: "")


@pytest.fixture()
def sdk():
    """挂载假 iFinDPy 并复位脚本（假模块绝不泄漏给其他用例）。"""
    with fake_sdk("iFinDPy"):
        import iFinDPy

        iFinDPy.reset()
        yield iFinDPy


def make_feed(sdk, tmp_path, tag="a", **kw) -> m.ThsIFindFeed:
    """离线可复现的 feed：零限速 + tmp 目录（不碰 data/）+ 显式凭据。"""
    opts = {"refresh_token": REFRESH_TOKEN, "min_interval": 0.0, "daily_quota": 100,
            "store_root": tmp_path / f"parquet_{tag}", "cache_dir": tmp_path / f"cache_{tag}"}
    opts.update(kw)
    feed = m.ThsIFindFeed(**opts)
    feed.connect()
    return feed


def calls_to(sdk, name: str) -> list[tuple]:
    return [c for c in sdk.CALLS if c[0] == name]


def last_args(sdk, name: str) -> tuple:
    """最近一次该函数调用的实参元组（假 SDK 的流水格式为 (func, args)）。"""
    return calls_to(sdk, name)[-1][1]


# ================= 元数据与注册表 =================
def test_adapter_metadata_matches_channel_contract():
    assert m.ThsIFindFeed.name == "ths_ifind"
    assert m.ThsIFindFeed.label == "同花顺 iFinD（付费）"
    assert m.ThsIFindFeed.requires == ("iFinDPy",)
    assert m.ThsIFindFeed.official is True
    assert "同花顺" in m.ThsIFindFeed.prereq and "设置页" in m.ThsIFindFeed.prereq
    assert m.ThsIFindFeed.capabilities == frozenset(
        {FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL,
         FeedCapability.REALTIME_TICK, FeedCapability.TRANSACTION})


def test_registry_discovers_ths_ifind():
    assert get_adapter_class("ths_ifind") is m.ThsIFindFeed
    row = next(r for r in describe_adapters() if r["name"] == "ths_ifind")
    assert row["kind"] == "data" and row["official"] is True
    assert "financial" in row["capabilities"] and "transaction" in row["capabilities"]


def test_missing_sdk_raises_guidance_error(tmp_path, monkeypatch):
    """无 whl 时不能崩，也不能静默：抛带安装引导的 AdapterError（顶层不 import 的回报）。"""
    monkeypatch.setitem(sys.modules, "iFinDPy", None)             # find_spec 也走不通
    feed = m.ThsIFindFeed(refresh_token=REFRESH_TOKEN, cache_dir=tmp_path,
                          store_root=tmp_path / "p")
    with pytest.raises(AdapterError) as ei:
        feed.connect()
    msg = str(ei.value)
    assert "iFinDPy" in msg and "whl" in msg


# ================= 历史：解析归一化（纯函数） =================
def test_parse_history_response_normalizes_columns_and_order():
    resp = {
        "errorcode": 0, "errmsg": "",
        "data": [{"thscode": SYMBOL,
                  "time": ["2024-01-03", "2024-01-02"],
                  "table": {"open": [10.1, 10.0], "high": [10.4, 10.2],
                            "low": ["9.9", 9.8], "close": [10.2, 10.1],
                            "volume": [120000, 100000], "amount": [1.21e6, "1.01e6"]}},
                 ],
    }
    out = m._parse_history_response(resp)
    assert list(out.columns) == HISTORY_COLUMNS
    assert str(out["dt"].dtype).startswith("datetime64")
    assert out["dt"].is_monotonic_increasing                    # 源乱序 → 排好序返回
    assert [t.strftime("%Y-%m-%d") for t in out["dt"]] == ["2024-01-02", "2024-01-03"]
    assert all(pd.api.types.is_float_dtype(out[c]) for c in HISTORY_COLUMNS[1:])
    assert out["amount"].iloc[0] == pytest.approx(1.01e6)       # 字符串数值也归成 float


def test_parse_history_response_empty_and_scalar_shapes():
    assert m._parse_history_response({"errorcode": 0, "data": []}).empty
    assert list(m._parse_history_response(None).columns) == HISTORY_COLUMNS
    # thstime 只在 table 内、字段为标量：仍归一化成功
    resp = {"data": [{"thscode": SYMBOL, "table": {
        "thstime": ["2024-01-02"], "open": 10.0, "high": 10.4, "low": 9.9,
        "close": 10.2, "volume": 100000, "amount": 1.0e6}}]}
    out = m._parse_history_response(resp)
    assert len(out) == 1 and out["open"].iloc[0] == pytest.approx(10.0)


def test_parse_history_response_missing_field_is_capability_error():
    resp = {"data": [{"thscode": SYMBOL, "time": ["2024-01-02"],
                      "table": {"open": [10.0], "high": [10.4]}}]}
    with pytest.raises(FeedCapabilityError) as ei:
        m._parse_history_response(resp)
    assert "close" in str(ei.value)


# ================= 历史：端到端（假 SDK） =================
def test_get_history_returns_sorted_history_columns(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    out = feed.get_history(SYMBOL, Frequency.D1, START, END, AdjustType.FORWARD)
    assert list(out.columns) == HISTORY_COLUMNS
    assert out["dt"].is_monotonic_increasing and len(out) == 3
    assert out["dt"].dtype.kind == "M"
    assert last_args(sdk, "THS_HistoryQuotes")[0] == SYMBOL       # 代码格式与内部一致


def test_get_history_maps_adjust_and_frequency(sdk, tmp_path):
    """复权/周期参数映射：CTP 与 Interval 随入参变化（口径常量置顶，核对后只改常量）。"""
    for tag, (freq, adjust, ctp, interval) in {
        "adj_d1": (Frequency.D1, AdjustType.NONE, "none", "D"),
        "adj_fw": (Frequency.D1, AdjustType.FORWARD, "front", "D"),
        "adj_bw": (Frequency.D1, AdjustType.BACKWARD, "back", "D"),
        "adj_m5": (Frequency.M5, AdjustType.FORWARD, "front", "5"),
    }.items():
        feed = make_feed(sdk, tmp_path, tag=tag)
        feed.get_history(SYMBOL, freq, START, END, adjust)
        p = last_args(sdk, "THS_HistoryQuotes")[2]
        assert p["CTP"] == ctp and p["Interval"] == interval
        assert p["CTP"] == m._CTP[adjust]


def test_get_history_rejects_unsupported_frequency(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    with pytest.raises(FeedCapabilityError):
        feed.get_history(SYMBOL, Frequency.TICK, START, END)
    assert calls_to(sdk, "THS_HistoryQuotes") == []              # 守卫在外呼之前


def test_get_history_empty_response_is_not_error(sdk, tmp_path):
    sdk.STATE["history_response"] = {"errorcode": 0, "errmsg": "", "data": []}
    feed = make_feed(sdk, tmp_path)
    out = feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert out.empty and list(out.columns) == HISTORY_COLUMNS


# ================= 配额 + 强缓存（DoD：命中不消耗） =================
def test_same_request_second_hit_does_not_consume_quota(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    first = feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert feed.quota_status()["used"] == 1
    second = feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert len(second) == len(first)
    assert feed.quota_status()["used"] == 1                      # 命中缓存：计数不变
    assert len(calls_to(sdk, "THS_HistoryQuotes")) == 1


def test_over_quota_served_by_parquet_strong_cache_no_request(sdk, tmp_path):
    """超阈值只读缓存不发请求，且计数不增加（跨实例：模拟重启后的新连接）。"""
    feed1 = make_feed(sdk, tmp_path, tag="shared", daily_quota=1)
    rows = len(feed1.get_history(SYMBOL, Frequency.D1, START, END))
    assert feed1.quota_status()["exceeded"] is True

    sdk.reset()                                                  # 只清调用流水，保留缓存盘
    sdk.STATE.update({"logged_in": False})
    feed2 = make_feed(sdk, tmp_path, tag="shared", daily_quota=1)
    out = feed2.get_history(SYMBOL, Frequency.D1, START, END)    # 命中 Parquet 强缓存
    assert len(out) == rows
    assert calls_to(sdk, "THS_HistoryQuotes") == []
    assert feed2.quota_status()["used"] == 1


def test_over_quota_without_cache_raises_with_guidance(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path, daily_quota=1)
    feed.get_history(SYMBOL, Frequency.D1, START, END)
    with pytest.raises(AdapterError) as ei:
        feed.get_history("000001.SZ", Frequency.D1, START, END)  # 该标的无缓存
    assert "配额" in str(ei.value)
    assert len(calls_to(sdk, "THS_HistoryQuotes")) == 1


def test_quota_counter_persisted_per_day(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    feed.get_history(SYMBOL, Frequency.D1, START, END)
    feed._quota_date = "2020-01-01"                              # 模拟跨天
    assert feed.quota_status()["date"] == date.today().isoformat()
    assert feed.quota_status()["used"] == 0                      # 当日重新计数


# ================= 会话：失效自动重登（最多 1 次） =================
def test_session_invalid_relogins_once_then_succeeds(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    assert feed._login_count == 1                                # connect 首登
    sdk.STATE["fail_times"] = 1
    out = feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert len(out) == 3
    assert len(sdk.LOGIN_CALLS) == 2                             # 首登 + 重登一次
    assert len(calls_to(sdk, "THS_HistoryQuotes")) == 2          # 重试一次（宪法上限 1 次）
    assert feed.quota_status()["used"] == 2


def test_session_invalid_after_relogin_gives_up_once(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    sdk.STATE["fail_times"] = 2                                  # 重登后仍失效
    with pytest.raises(ConnectionLostError):
        feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert len(sdk.LOGIN_CALLS) == 2                             # 不无限重登
    assert len(calls_to(sdk, "THS_HistoryQuotes")) == 2
    assert feed._logged_in is False                              # 通道标记断线，交上层降级


def test_daily_relogin_on_date_change(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    feed._login_date = date(2020, 1, 1)                          # 模拟跨自然日
    feed.get_history(SYMBOL, Frequency.D1, START, END)
    assert len(sdk.LOGIN_CALLS) == 2                             # 每日定时重登（跨天首调触发）


def test_refresh_token_login_flow_and_password_fallback(sdk, tmp_path):
    """refresh token 优先：先换 access_token 再登录；无 token 时走账号密码。"""
    feed = make_feed(sdk, tmp_path, tag="tok")
    assert len(sdk.TOKEN_CALLS) == 1
    assert sdk.LOGIN_CALLS[0][0] == "MOCKACCESSTOKEN-0123456789abcdef"
    assert REFRESH_TOKEN not in str(sdk.LOGIN_CALLS)               # 明文 token 不下传给登录接口

    n_token = len(sdk.TOKEN_CALLS)
    pw_feed = m.ThsIFindFeed(username="acct-user", password="pw-secret", min_interval=0.0,
                             store_root=tmp_path / "pw_p", cache_dir=tmp_path / "pw_c")
    pw_feed.connect()
    assert len(sdk.TOKEN_CALLS) == n_token                       # 无 token → 不换票
    assert ("acct-user", "pw-secret") in sdk.LOGIN_CALLS


def test_login_without_credentials_raises_config_guide(sdk, tmp_path):
    feed = m.ThsIFindFeed(min_interval=0.0, store_root=tmp_path / "nc_p",
                          cache_dir=tmp_path / "nc_c")
    with pytest.raises(ConfigError) as ei:
        feed.connect()
    assert "ifind.refresh_token" in str(ei.value)


def test_refresh_token_exchange_failure_is_connection_lost(sdk, tmp_path):
    sdk.STATE["access_token_errorcode"] = -1010
    feed = m.ThsIFindFeed(refresh_token=REFRESH_TOKEN, min_interval=0.0,
                          store_root=tmp_path / "at_p", cache_dir=tmp_path / "at_c")
    with pytest.raises(ConnectionLostError) as ei:
        feed.connect()
    assert "access_token" in str(ei.value)
    assert REFRESH_TOKEN not in str(ei.value)                    # 异常信息也不带明文


def test_token_resolution_is_single_choke_point(monkeypatch, tmp_path):
    """凭据取用收口顺序：构造参数 > 注入 callable > 保险库/Settings（主线程替换只改一处）。"""
    monkeypatch.setattr(m, "_secret_lookup", lambda key, attr: "VAULT-TOKEN-9999")
    common = {"min_interval": 0.0, "store_root": tmp_path / "tr_p", "cache_dir": tmp_path / "tr_c"}
    assert m.ThsIFindFeed(**common)._resolve_token() == "VAULT-TOKEN-9999"
    assert m.ThsIFindFeed(token_provider=lambda: "PROVIDER-7", **common)._resolve_token() \
        == "PROVIDER-7"
    assert m.ThsIFindFeed(refresh_token="CTOR-1",
                          token_provider=lambda: "PROVIDER-7", **common)._resolve_token() \
        == "CTOR-1"
    # 账号密码同口径：构造参数优先，其余走保险库键 ifind.username / ifind.password
    assert m.ThsIFindFeed(**common)._resolve_credentials() == ("VAULT-TOKEN-9999",
                                                               "VAULT-TOKEN-9999")
    assert m.ThsIFindFeed(username="u", password="p", **common)._resolve_credentials() == ("u", "p")


# ================= 日志脱敏（宪法：前 4 位 + ***） =================
def test_credentials_never_logged_in_plaintext(sdk, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    make_feed(sdk, tmp_path, tag="mask")
    text = caplog.text
    assert REFRESH_TOKEN not in text
    assert "MOCKACCESSTOKEN-0123456789abcdef" not in text
    assert "MOCK***" in text                                     # 只允许掩码形态


def test_login_failure_log_and_error_are_masked(sdk, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    sdk.STATE["access_token_errorcode"] = -1
    feed = m.ThsIFindFeed(refresh_token=REFRESH_TOKEN, min_interval=0.0,
                          store_root=tmp_path / "mf_p", cache_dir=tmp_path / "mf_c")
    with pytest.raises(ConnectionLostError) as ei:
        feed.connect()
    assert REFRESH_TOKEN not in str(ei.value) and REFRESH_TOKEN not in caplog.text
    assert "REFR***" in str(ei.value)


# ================= 财务：统一长表归一化（三种结构抹平） =================
def _shape_a():
    """利润表：FeedBatch，table = {指标: [各期值]}，time 为数组。"""
    return {"errorcode": 0, "data": [{"thscode": SYMBOL,
                                      "time": ["2022-12-31", "2023-12-31"],
                                      "updatetime": "2024-04-30 08:00:00",
                                      "table": {"ths_revenue_stock": [9.0, 10.0],
                                                "ths_net_profit_stock": [1.0, None]}}]}


def _shape_b():
    """资产负债表：tbl = [{'indicator','value','period','unit'}] 列表形态。"""
    return {"errorcode": 0, "data": [{"thscode": SYMBOL, "updatetime": "2024-04-30",
                                      "tbl": [{"indicator": "ths_total_assets_stock",
                                               "value": 100.0, "period": "2023-12-31",
                                               "unit": "亿元"},
                                              {"indicator": "ths_total_liability_stock",
                                               "value": "80.5", "period": "2023-12-31"}]}]}


def _shape_c():
    """现金流量表：tbl = 每期一个 dict（time + 指标键）。"""
    return {"errorcode": 0, "data": [{"thscode": SYMBOL, "updatetime": "2024-04-30",
                                      "tbl": [{"time": "2022-12-31",
                                               "ths_ncf_oa_stock": 5.0},
                                              {"time": "2023-12-31",
                                               "ths_ncf_oa_stock": [6.0]}]}]}


def test_financial_three_report_shapes_collapse_to_same_long_table(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    sdk.STATE["feed_batch_response"] = _shape_a()
    income = feed.get_financial_data(SYMBOL, "income")
    sdk.STATE["feed_batch_response"] = _shape_b()
    balance = feed.get_financial_data(SYMBOL, "balance")
    sdk.STATE["feed_batch_response"] = _shape_c()
    cashflow = feed.get_financial_data(SYMBOL, "cashflow")

    for df in (income, balance, cashflow):
        assert list(df.columns) == m.FINANCIAL_COLUMNS           # 列结构完全一致
        assert set(df["report_type"]) <= {"income", "balance", "cashflow"}
        assert df["indicator"].str.startswith("ths_").all()
        assert all(isinstance(v, float) for v in df["value"])     # 数值不伪造、统一成 float

    assert len(income) == 3                                       # 2 期营收 + 1 期净利润
    assert income[income["indicator"] == "ths_revenue_stock"]["value"].tolist() == [9.0, 10.0]
    assert balance["unit"].tolist() == ["亿元", ""]               # 源没给 unit 就留空
    assert sorted(cashflow["period"]) == ["2022-12-31", "2023-12-31"]
    assert income["update_time"].iloc[0] == "2024-04-30 08:00:00"


def test_financial_indicator_uses_basic_data_flat_record(sdk, tmp_path):
    """指标集走 THS_BasicData；无 table 时（指标直挂记录）也能归一化。"""
    sdk.STATE["basic_response"] = {"errorcode": 0, "data": [
        {"thscode": SYMBOL, "time": "2023-12-31", "updatetime": "2024-04-30",
         "ths_roe_avg_stock": 8.5, "ths_eps_basic_stock": "0.91"}]}
    feed = make_feed(sdk, tmp_path)
    out = feed.get_financial_data(SYMBOL, "indicator")
    assert list(out.columns) == m.FINANCIAL_COLUMNS
    assert set(out["indicator"]) == {"ths_roe_avg_stock", "ths_eps_basic_stock"}
    assert float(out[out["indicator"] == "ths_eps_basic_stock"]["value"].iloc[0]) == 0.91
    assert float(out[out["indicator"] == "ths_roe_avg_stock"]["value"].iloc[0]) == 8.5
    assert calls_to(sdk, "THS_BasicData") and calls_to(sdk, "THS_FeedBatch") == []


def test_financial_rejects_unknown_report_type(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    with pytest.raises(FeedCapabilityError):
        feed.get_financial_data(SYMBOL, "shareholder")


def test_financial_cache_hit_does_not_consume_quota(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    first = feed.get_financial_data(SYMBOL, "income")
    assert feed.quota_status()["used"] == 1
    again = feed.get_financial_data(SYMBOL, "income")
    assert len(again) == len(first)
    assert feed.quota_status()["used"] == 1                      # 进程内缓存
    assert len(calls_to(sdk, "THS_FeedBatch")) == 1

    sdk.reset()
    sdk.STATE["logged_in"] = False
    fresh = make_feed(sdk, tmp_path)                             # 新实例 → 磁盘长表缓存
    assert len(fresh.get_financial_data(SYMBOL, "income")) == len(first)
    assert calls_to(sdk, "THS_FeedBatch") == []
    assert fresh.quota_status()["used"] == 1                     # 配额计数纹丝不动


def test_financial_empty_result_not_cached(sdk, tmp_path):
    sdk.STATE["feed_batch_response"] = {"errorcode": 0, "data": []}
    feed = make_feed(sdk, tmp_path)
    out = feed.get_financial_data(SYMBOL, "income")
    assert out.empty and list(out.columns) == m.FINANCIAL_COLUMNS
    feed.get_financial_data(SYMBOL, "income")
    assert len(calls_to(sdk, "THS_FeedBatch")) == 2              # 空结果不固化成"暂无"


# ================= 实时：回调只入队 + 工作线程归一化 =================
def test_parse_realtime_snapshot_five_levels():
    raw = {"errorcode": 0, "data": [{"thscode": SYMBOL, "time": "2024-01-02 09:31:00",
                                     "table": {
                                         "latest": [10.1], "open": [10.0], "high": [10.2],
                                         "low": [9.9], "volume": [1000], "amount": [10100],
                                         "bidPrice1": [10.09], "bidPrice2": [10.08],
                                         "bidPrice3": [10.07], "bidPrice4": [10.06],
                                         "bidPrice5": [10.05],
                                         "bidVolume1": [500], "bidVolume2": [600],
                                         "bidVolume3": [700], "bidVolume4": [800],
                                         "bidVolume5": [900],
                                         "askPrice1": [10.11], "askPrice2": [10.12],
                                         "askPrice3": [10.13], "askPrice4": [10.14],
                                         "askPrice5": [10.15],
                                         "askVolume1": [100], "askVolume2": [200],
                                         "askVolume3": [300], "askVolume4": [400],
                                         "askVolume5": [500]}}]}
    tick = m._parse_realtime_snapshot(raw)
    assert isinstance(tick, Tick) and tick.symbol == SYMBOL
    assert len(tick.bids) == 5 and len(tick.asks) == 5
    assert tick.bids[0] == (10.09, 500.0) and tick.asks[-1] == (10.15, 500.0)
    assert tick.last == pytest.approx(10.1) and isinstance(tick.dt, datetime)


def test_parse_realtime_snapshot_missing_levels_not_fabricated():
    raw = {"data": {"thscode": SYMBOL, "time": "2024-01-02 09:31:00",
                    "latest": 10.1, "bidPrice1": 10.09, "bidVolume1": 500}}
    tick = m._parse_realtime_snapshot(raw)
    assert tick is not None and len(tick.bids) == 1 and tick.asks == []
    assert m._parse_realtime_snapshot({"data": []}) is None
    assert m._parse_realtime_snapshot("not a dict") is None


def test_subscribe_realtime_delivers_tick_off_callback_thread(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    got: queue.Queue = queue.Queue()
    feed.subscribe_realtime([SYMBOL], got.put)
    assert len(calls_to(sdk, "THS_RealTimeQuotes")) == 1
    enqueuer = feed._on_snapshot
    assert sdk.STATE["rt_callback"] == enqueuer                   # SDK 侧只拿到入队函数
    assert sdk.STATE["rt_callback"] != got.put                    # 用户回调绝不外传给 SDK

    assert sdk.push_realtime({"data": [{"thscode": SYMBOL, "time": "2024-01-02 09:31:00",
                                        "table": {"latest": [10.1], "bidPrice1": [10.09],
                                                  "bidVolume1": [500]}}]}) == 1
    tick = got.get(timeout=2.0)
    assert isinstance(tick, Tick) and tick.last == pytest.approx(10.1)
    assert threading.current_thread() is not feed._rt_worker      # 归一化不在 SDK 回调线程


def test_subscribe_realtime_incremental_and_unsubscribe(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    got: queue.Queue = queue.Queue()
    feed.subscribe_realtime([SYMBOL], got.put)
    feed.subscribe_realtime([SYMBOL, "000001.SZ"], got.put)       # 增量：只订新标的
    assert last_args(sdk, "THS_RealTimeQuotes")[0] == "000001.SZ"
    feed.unsubscribe_realtime([SYMBOL])
    assert len(calls_to(sdk, "THS_UnsubscribeRealTime")) == 1
    feed.unsubscribe_realtime(["000001.SZ"])
    assert last_args(sdk, "THS_UnsubscribeRealTime")[0] == "000001.SZ"
    assert feed._rt_worker is None                                # 全部退订后线程回收


def test_subscribe_realtime_capability_error_when_sdk_lacks_api(sdk, tmp_path, monkeypatch):
    for name in m._RT_FUNC_NAMES:
        if hasattr(sdk, name):
            monkeypatch.delattr(sdk, name)
    feed = make_feed(sdk, tmp_path)
    with pytest.raises(FeedCapabilityError):
        feed.subscribe_realtime([SYMBOL], lambda t: None)


# ================= 运维语义 =================
def test_health_check_true_and_cached_60s(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    assert feed.health_check() is True
    n = len(calls_to(sdk, "THS_BasicData"))
    assert feed.health_check() is True
    assert len(calls_to(sdk, "THS_BasicData")) == n              # 命中 60s 缓存，不再外呼


def test_health_check_swallows_errors_and_returns_false(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    assert feed.health_check() is True
    feed._health_cache = (0.0, False)
    sdk.STATE["raise_on_call"] = RuntimeError("sdk socket closed")
    assert feed.health_check() is False                          # 断线可探测
    sdk.STATE["raise_on_call"] = None
    feed._health_cache = (0.0, False)
    assert feed.health_check() is True


def test_sdk_exception_surfaces_as_connection_lost(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    sdk.STATE["raise_on_call"] = RuntimeError("connection reset")
    with pytest.raises(ConnectionLostError):
        feed.get_history(SYMBOL, Frequency.D1, START, END)


def test_disconnect_is_idempotent_and_stops_worker(sdk, tmp_path):
    feed = make_feed(sdk, tmp_path)
    feed.subscribe_realtime([SYMBOL], lambda t: None)
    feed.disconnect()
    feed.disconnect()
    assert sdk.STATE["logged_in"] is False and feed._rt_worker is None
    assert feed._logged_in is False
    # 重连可用（会话状态由 _login 重新建立）
    feed.connect()
    assert feed._logged_in is True

