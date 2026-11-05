"""两个公开行情通道（tx_quote / em_quote）的离线单测：假 httpx transport，不碰外网。

为什么单独加这一层：services/watch 的用例用假通道对象，测的是"服务怎么用数据"；
本文件测"适配器把请求拼成什么、把返回拆成什么"——参数映射与单位口径。
本轮就是靠真机外呼才发现东财 `klt` 把分钟数写成秒数（请求 5 分钟线静默拿到 1 小时线，
M15/M30/H1 直接空表）。这类"接口不认识参数时不报错、只回空"的缺陷，
只有把 URL 参数钉成断言才能在 CI 里锁住。

覆盖通道 SOP 第 8 节里适用于公开 HTTP 源的部分：
探活失败、限速与冷却、单位缩放、字段布局按市场分表、批量分片、
能力边界（不支持的周期/财务一律抛错，不静默返回空表）。
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from quantdesk.adapters.data import em_quote as em_mod
from quantdesk.adapters.data import tx_quote as tx_mod
from quantdesk.adapters.data.em_quote import EastmoneyQuoteFeed
from quantdesk.adapters.data.tx_quote import TencentQuoteFeed
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.errors import AdapterError, ConnectionLostError

# ---------------- 外呼替身 ----------------

class Recorder:
    """记下每次外呼的 URL + 参数；按 URL 子串返回预置响应，或一律抛异常。"""

    def __init__(self, replies=None, raise_exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.replies = replies or {}
        self.raise_exc = raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((url, dict(request.url.params)))
        if self.raise_exc is not None:
            raise self.raise_exc
        for key, reply in self.replies.items():
            if key in url:
                if isinstance(reply, str):
                    return httpx.Response(200, content=reply.encode("gbk"))
                return httpx.Response(200, json=reply)
        return httpx.Response(200, json={"data": None})

    def matched(self, substring: str) -> list[tuple[str, dict]]:
        return [(u, p) for (u, p) in self.calls if substring in u]


@pytest.fixture()
def offline(monkeypatch):
    """把 httpx.Client 焊上 MockTransport：连 connect() 也不会真出网。"""
    real = httpx.Client

    def apply(handler):
        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return real(*args, **kwargs, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(httpx, "Client", factory)

    return apply


def _feed(cls, rec: Recorder, **kw):
    """已"连上"的实例（直接换 _client，避开 connect 的探活；限速器关掉免得用例睡觉）。"""
    feed = cls(min_interval=0.0, **kw)
    feed._client = httpx.Client(transport=httpx.MockTransport(rec.handler))
    feed._connected = True
    return feed


def _tx_line(prefix: str, fields: dict[int, str]) -> str:
    """按下标摆字段造一行行情串（下标取自适配器里的 _TX_LAYOUT_*，不靠手数对齐）。"""
    parts = [""] * 60
    for idx, value in fields.items():
        parts[idx] = value
    return f'v_{prefix}="' + "~".join(parts) + '"'


_A_LEVELS = {9 + i * 2: f"{10.00 + i / 100:.2f}" for i in range(5)} | \
            {10 + i * 2: str(100 + i) for i in range(5)} | \
            {19 + i * 2: f"{10.20 + i / 100:.2f}" for i in range(5)} | \
            {20 + i * 2: str(200 + i) for i in range(5)}

# ---------------- 腾讯：批量快照解析与单位口径 ----------------

def test_tx_snapshot_parses_units_and_levels():
    line = _tx_line("sh600000", {
        1: "浦发银行", 2: "600000", 3: "10.13", 4: "10.10", 5: "10.10",
        6: "824086", 7: "458432", 8: "365654",          # 量(手)/外盘/内盘
        30: "20261104161420", 33: "10.20", 34: "10.00",
        37: "83449.05",                                  # 成交额，单位「万元」
        47: "11.11", 48: "9.09", 51: "10.13", **_A_LEVELS})
    rec = Recorder(replies={"qt.gtimg.cn": line})
    row = _feed(TencentQuoteFeed, rec).get_snapshot(["600000.SH"])["600000.SH"]

    assert row["name"] == "浦发银行" and row["last"] == 10.13
    assert row["volume_unit"] == "lot" and row["volume"] == 824086.0
    assert row["raw_volume_unit"] == "lot"               # 主板本来就是手，不许再除
    assert row["amount"] == pytest.approx(83449.05 * 10000)   # 归一成「元」
    assert row["outer_volume"] == 458432.0 and row["inner_volume"] == 365654.0
    assert row["limit_up"] == 11.11 and row["limit_down"] == 9.09
    assert row["avg_price"] == 10.13
    assert row["bids"] == [(10.00, 100.0), (10.01, 101.0), (10.02, 102.0),
                           (10.03, 103.0), (10.04, 104.0)]
    assert row["asks"][0] == (10.20, 200.0)
    assert row["quote_time"] == "20261104161420"


_HK_LEVELS = {9 + i * 2: f"{400 + i:.2f}" for i in range(5)} | \
             {10 + i * 2: str(1000 + i) for i in range(5)} | \
             {19 + i * 2: f"{401 + i:.2f}" for i in range(5)} | \
             {20 + i * 2: str(2000 + i) for i in range(5)}


def test_tx_kechuang_volume_is_shares_and_gets_converted():
    """科创板（688/689）腾讯给的是「股」，适配器必须换算成手。

    实测依据：2026-11-05 跨源核对 122 只，主板/创业/北交的量与东财「手」逐位相等，
    688/689 一律差 100 倍；用成交额 ÷ 量 反推，只有当成股时均价才落在当日高低区间内。
    不换算的后果是电脑端盯盘把科创板成交量与买卖量都放大 100 倍。
    """
    line = _tx_line("sh688775", {
        1: "影石创新", 2: "688775", 3: "110.80", 4: "103.63", 5: "103.90",
        6: "11701851", 7: "6000000", 8: "5701851",      # 这里三个数都是「股」
        30: "20261105161420", 33: "112.40", 34: "103.63",
        37: "128074.37",                                 # 万元
        47: "124.36", 48: "82.90", 51: "109.45", **_A_LEVELS})
    rec = Recorder(replies={"qt.gtimg.cn": line})
    row = _feed(TencentQuoteFeed, rec).get_snapshot(["688775.SH"])["688775.SH"]

    assert row["raw_volume_unit"] == "share" and row["volume_unit"] == "lot"
    assert row["volume"] == 117018.51                    # 11,701,851 股 ÷ 100
    assert row["outer_volume"] == 60000.0 and row["inner_volume"] == 57018.51
    # 换算后均价必须落在当日高低区间里，这是判据本身
    assert row["low"] <= row["amount"] / (row["volume"] * 100) <= row["high"]


def test_tx_hk_has_no_outer_inner_and_uses_shares():
    """港股：接口不给内外盘 → 置 None（不是 0）；量单位=股；成交额不乘万。"""
    line = _tx_line("hk00700", {
        1: "腾讯控股", 2: "00700", 3: "400.00", 4: "398.00", 5: "399.00",
        6: "12345678", 30: "2026/11/04 16:08:31", 33: "402.00", 34: "397.00",
        37: "4938271200", 44: "40000", 45: "38000", **_HK_LEVELS})
    rec = Recorder(replies={"qt.gtimg.cn": line})
    row = _feed(TencentQuoteFeed, rec).get_snapshot(["00700.HK"])["00700.HK"]

    assert row["symbol"] == "00700.HK" and row["name"] == "腾讯控股"
    assert row["volume_unit"] == "share" and row["volume"] == 12345678.0
    assert row["outer_volume"] is None and row["inner_volume"] is None
    assert row["amount"] == pytest.approx(4938271200.0)
    assert row["limit_up"] is None                    # 港股无涨跌停，不填 0


def test_tx_batch_chunks_at_sixty():
    rec = Recorder(replies={"qt.gtimg.cn": _tx_line("sh600000", {2: "600000", 3: "10"})})
    _feed(TencentQuoteFeed, rec).get_snapshot([f"{600000 + i}.SH" for i in range(61)])
    assert len(rec.calls) == 2          # 61 只分两片；一片塞太多会被接口裁包


def test_tx_rejects_minute_freq_and_financials():
    """能力边界要抛错——空表在下游长得像"这个标的没数据"，会把缺陷藏起来。"""
    feed = _feed(TencentQuoteFeed, Recorder())
    with pytest.raises(AdapterError):
        feed.get_history("600000.SH", Frequency.M1, pd.Timestamp("2026-11-01"),
                         pd.Timestamp("2026-11-04"))
    with pytest.raises(AdapterError):
        feed.get_financial_data("600000.SH")


def test_tx_connect_probe_failure_raises_and_leaves_no_client(offline):
    offline(Recorder(raise_exc=httpx.ConnectError("no route to host")).handler)
    feed = TencentQuoteFeed(min_interval=0.0)
    with pytest.raises(ConnectionLostError):
        feed.connect()
    assert feed._client is None and not feed._connected


def test_tx_connect_probe_ok(offline):
    offline(Recorder(replies={"qt.gtimg.cn": _tx_line("sz000001", {2: "000001", 3: "11"})}).handler)
    feed = TencentQuoteFeed(min_interval=0.0)
    feed.connect()
    assert feed._connected and feed.health_check() is True


# ---------------- 东财：参数映射（本轮真实缺陷所在） ----------------

def test_em_klt_maps_minutes_not_seconds():
    """klt：分钟级是「分钟数」1/5/15/30/60，日周月是 101/102/103。

    写成秒（900/1800/3600）时接口不认识→回空表；M5→"60" 更坏：它认识 60，
    于是"请求 5 分钟线"静默拿到 1 小时线。周期映射错是最贵的一类 bug，
    因为图上看起来一切正常。
    """
    seen: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        klt = str(request.url.params["klt"])
        seen.add(klt)
        return httpx.Response(200, json={"data": {"klines": [
            "2026-11-04 10:00,10.0,10.1,10.2,9.9,100,1000,0.1"]}})

    feed = EastmoneyQuoteFeed(min_interval=0.0)
    feed._client = httpx.Client(transport=httpx.MockTransport(handler))
    feed._connected = True
    for freq, expect in [(Frequency.M1, "1"), (Frequency.M5, "5"), (Frequency.M15, "15"),
                         (Frequency.M30, "30"), (Frequency.H1, "60"), (Frequency.D1, "101"),
                         (Frequency.W1, "102"), (Frequency.MO1, "103")]:
        feed.get_history("600000.SH", freq, pd.Timestamp("2026-10-01"),
                         pd.Timestamp("2026-11-04"))
        assert expect in seen, f"{freq.value} 应发 klt={expect}，实际发过 {sorted(seen)}"
    assert len(seen) == 8, "映射表出现重复值说明有人在猜周期"
    assert {"900", "1800", "3600"}.isdisjoint(seen)


def test_em_fqt_maps_adjust_types():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params["fqt"]))
        return httpx.Response(200, json={"data": {"klines": []}})

    feed = EastmoneyQuoteFeed(min_interval=0.0)
    feed._client = httpx.Client(transport=httpx.MockTransport(handler))
    feed._connected = True
    for adjust, expect in [(AdjustType.NONE, "0"), (AdjustType.FORWARD, "1"),
                           (AdjustType.BACKWARD, "2")]:
        feed.get_history("600000.SH", Frequency.D1, pd.Timestamp("2026-01-01"),
                         pd.Timestamp("2026-11-04"), adjust)
        assert seen[-1] == expect, f"{adjust} 应发 fqt={expect}"


def test_em_secid_and_field_list():
    rec = Recorder(replies={"stock/get": {"data": {
        "f43": 10.13, "f47": 824086, "f49": 458432, "f51": 11.11, "f52": 9.09,
        "f58": "浦发银行", "f60": 10.10, "f71": 10.13, "f168": 0.25, "f171": 1.78}}})
    feed = _feed(EastmoneyQuoteFeed, rec)
    q = feed.get_quote("600000.SH")
    assert q["secid"] == "1.600000" and q["volume_unit"] == "lot"
    assert q["outer_volume"] == 458432.0
    assert q["inner_volume"] == 824086.0 - 458432.0      # 接口不给内盘，减法口径
    sent = rec.matched("stock/get")[0][1]["fields"]
    for code in ("f43", "f47", "f49", "f51", "f84", "f116", "f162", "f198"):
        assert code in sent, f"面板要用的 {code} 没在请求字段里"


def test_em_hk_limit_fields_are_52week_not_price_limits():
    """东财对 `116.` 标的**复用 f51/f52 给 52 周最高/最低**，不是涨跌停（港股没有涨跌停）。

    实测 00700 当日 f51/f52 = 656.7 / 375.2，而最新价 401.6——如果按 A股语义直接
    当成涨停/跌停显示，界面上会出现「涨停 656.70」这种把小白吓到的数。
    所以出参按市场分键：`.HK` 只给 wk52_*，A股只给 limit_*。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        secid = str(request.url.params["secid"])
        d = ({"f43": 401.6, "f47": 18961, "f49": 9994, "f51": 656.7, "f52": 375.2}
             if secid.startswith("116.") else
             {"f43": 10.13, "f47": 824086, "f49": 458432, "f51": 11.11, "f52": 9.09})
        return httpx.Response(200, json={"data": d})

    feed = EastmoneyQuoteFeed(min_interval=0.0)
    feed._client = httpx.Client(transport=httpx.MockTransport(handler))
    feed._connected = True

    hk = feed.get_quote("00700.HK")
    assert hk["limit_up"] is None and hk["limit_down"] is None
    assert hk["wk52_high"] == 656.7 and hk["wk52_low"] == 375.2

    cn = feed.get_quote("600000.SH")
    assert cn["limit_up"] == 11.11 and cn["limit_down"] == 9.09
    assert cn["wk52_high"] is None and cn["wk52_low"] is None


def test_em_hk_transactions_have_no_neutral_code():
    """港股逐笔方向码只有 1/2（实测 18961 笔无 `4`）：中性桶为空是数据边界，不是解析漏了。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"details": [
            "09:30:00,400.00,100,3,1,,0", "09:30:01,400.20,50,2,2,,U"]}})

    feed = EastmoneyQuoteFeed(min_interval=0.0)
    feed._client = httpx.Client(transport=httpx.MockTransport(handler))
    feed._connected = True
    df = feed.get_transactions("00700.HK", count=10)
    assert set(df["side_code"]) == {"1", "2"}          # 原样透传，不替接口猜出第三个值
    assert df["amount"].iloc[0] == pytest.approx(400.0 * 100)


def test_em_non_numeric_placeholder_becomes_none():
    """某些标的换手/市盈返回 "-"：转 None，前端显示"—"，不能当 0（0 是"没有换手"）。"""
    rec = Recorder(replies={"stock/get": {"data": {"f43": 10.0, "f47": 100,
                                                   "f168": "-", "f162": "-"}}})
    q = _feed(EastmoneyQuoteFeed, rec).get_quote("600000.SH")
    assert q["turnover_rate"] is None and q["pe_dynamic"] is None


def test_em_hk_vs_cn_tick_amount_multiplier():
    """逐笔量：A股单位「手」→ 算金额乘 100；港股（secid 前缀 116.）单位「股」→ 乘 1。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"details": ["09:30:00,400.00,100,3,2,,0"]}})

    for sym, mult in [("00700.HK", 1.0), ("600000.SH", 100.0)]:
        feed = EastmoneyQuoteFeed(min_interval=0.0)
        feed._client = httpx.Client(transport=httpx.MockTransport(handler))
        feed._connected = True
        df = feed.get_transactions(sym, count=10)
        assert df["amount"].iloc[0] == pytest.approx(400.0 * 100 * mult), sym


def test_em_empty_data_is_adapter_error_without_cooldown():
    """`data` 为空 = 标的没数据/代码写错 → 抛 AdapterError 且**不冷却**：
    否则用户敲错一个代码就把整源锁 60 秒。"""
    feed = _feed(EastmoneyQuoteFeed, Recorder(replies={}))
    with pytest.raises(AdapterError):
        feed.get_quote("999999.SZ")
    assert feed.describe()["cooldown"] == {}


def test_em_connection_drop_cools_down_that_host():
    """连接被掐 = 对端在挡 → 按主机冷却，冷却期内一次都不再外呼（把限流打成封禁是自找的）。"""
    rec = Recorder(raise_exc=httpx.RemoteProtocolError("peer closed connection"))
    feed = _feed(EastmoneyQuoteFeed, rec)
    with pytest.raises(ConnectionLostError):
        feed.get_quote("600000.SH")
    assert feed.stats["rate_limited"] == 1
    before = len(rec.calls)
    with pytest.raises(ConnectionLostError) as e:
        feed.get_quote("600000.SH")
    assert "冷却" in str(e.value)
    assert len(rec.calls) == before
    assert feed.describe()["cooldown"]            # 面板能如实看到还剩几秒


def test_em_transactions_merge_head_and_tail():
    """逐笔取 pos=0 与 pos=-count 两段的并集（单次上限约 5000 行，全日要两头拼），重叠去重。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pos = str(request.url.params["pos"])
        calls.append(pos)
        rows = (["09:30:00,10.00,100,2,1,,0", "09:31:00,10.01,50,1,2,,0"] if pos == "0"
                else ["15:29:00,10.10,30,1,0,,0", "09:31:00,10.01,50,1,2,,0"])
        return httpx.Response(200, json={"data": {"details": rows}})

    feed = EastmoneyQuoteFeed(min_interval=0.0)
    feed._client = httpx.Client(transport=httpx.MockTransport(handler))
    feed._connected = True
    df = feed.get_transactions("600000.SH", count=2)
    assert calls == ["0", "-2"]
    assert len(df) == 3
    assert df["dt"].is_monotonic_increasing
    assert df["time"].tolist() == ["09:30:00", "09:31:00", "15:29:00"]   # 含盘后段，如实保留


def test_em_intraday_has_avg_price():
    rec = Recorder(replies={"trends2": {"data": {"trends": [
        "2026-11-04 09:30,10.10,10.13,10.13,10.10,1000,10130,10.13",
        "2026-11-04 09:31,10.13,10.12,10.14,10.12,900,9108,10.125"]}}})
    df = _feed(EastmoneyQuoteFeed, rec).get_intraday("600000.SH")
    assert list(df.columns) == ["dt", "price", "avg_price", "volume", "amount"]
    assert df["avg_price"].tolist() == [10.13, 10.125]


def test_em_financial_not_supported():
    feed = _feed(EastmoneyQuoteFeed, Recorder())
    with pytest.raises(AdapterError):
        feed.get_financial_data("600000.SH")


# ---------------- 注册表可见性（前端零改动的根据） ----------------

def test_both_channels_registered_unofficial_and_ready():
    from quantdesk.adapters import describe_adapters

    meta = {r["name"]: r for r in describe_adapters()}
    for name in ("tx_quote", "em_quote"):
        assert name in meta, f"{name} 未出现在通道全景——面板看不到它"
        assert meta[name]["official"] is False, "公开网页接口必须带非官方标注"
        assert meta[name]["status"] == "ready", meta[name]   # 无可选 SDK，装好即就绪


def test_adapters_ask_for_no_credentials():
    """两通道都不该向用户要凭据：凭据清单里没有它们 → 通道页不会渲染出空的凭据表单。"""
    from quantdesk.services.channels import credential_spec

    channels = {c["channel"] for c in credential_spec()}
    for name in ("tx_quote", "em_quote"):
        assert name not in channels, f"{name} 是免账号公开接口，不该出现在凭据清单里"


def test_em_module_wires_the_feed_class():
    assert tx_mod.ADAPTER is TencentQuoteFeed and em_mod.ADAPTER is EastmoneyQuoteFeed
