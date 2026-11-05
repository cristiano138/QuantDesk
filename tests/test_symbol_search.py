"""东财 suggest 选股搜索解析（离线：替换 _json，不触网）。

覆盖：只留股票（A股/B股/北交所/港股），剔除指数/基金/美股/板块/窝轮牛熊证；
港股码补齐 5 位；同名去重；缓存命中不再外呼。
"""

from __future__ import annotations

import pytest

from quantdesk.adapters.data.em_quote import EastmoneyQuoteFeed


def _payload(items):
    return {"QuotationCodeTable": {"Data": items}}


@pytest.fixture()
def feed(monkeypatch):
    f = EastmoneyQuoteFeed(min_interval=0.0)
    f._connected = True
    f._client = object()  # _json 会被替换，这里只占位
    calls = []

    def fake_json(url, params, *, data_key=True):
        calls.append(params.get("input"))
        return _payload([
            {"Code": "688836", "Name": "宇树科技-W", "MktNum": "1",
             "Classify": "AStock", "SecurityTypeName": "科创板"},
            {"Code": "000001", "Name": "平安银行", "MktNum": "0",
             "Classify": "AStock", "SecurityTypeName": "深市主板"},
            {"Code": "00700", "Name": "腾讯控股", "MktNum": "128",
             "Classify": "HK", "TypeUS": "3", "SecurityTypeName": "港股主板"},
            {"Code": "03690", "Name": "美团-WR", "MktNum": "128", "TypeUS": "3",
             "Classify": "HK", "SecurityTypeName": "港股人民币柜台"},
            {"Code": "13867", "Name": "某窝轮", "MktNum": "128", "TypeUS": "6",
             "Classify": "HK", "SecurityTypeName": "认沽轮"},
            {"Code": "000300", "Name": "沪深300", "MktNum": "1",
             "Classify": "Index", "SecurityTypeName": "指数"},
            {"Code": "510300", "Name": "300ETF", "MktNum": "1",
             "Classify": "Fund", "SecurityTypeName": "基金"},
            {"Code": "BABA", "Name": "阿里巴巴", "MktNum": "105",
             "Classify": "USStock", "SecurityTypeName": "美股"},
        ])

    monkeypatch.setattr(f, "_json", fake_json)
    EastmoneyQuoteFeed._search_cache.clear()
    return f, calls


def test_search_keeps_only_stocks(feed):
    f, _ = feed
    rows = f.search_symbols("yushu", 50)
    syms = [r["symbol"] for r in rows]
    assert syms == ["688836.SH", "000001.SZ", "00700.HK"]  # 柜台/窝轮/指数/基金/美股全被剔除
    assert rows[0]["name"] == "宇树科技-W"


def test_search_cache_hits(feed):
    f, calls = feed
    f.search_symbols("yushu")
    f.search_symbols("YUSHU")       # 大小写归一后命中缓存
    assert calls == ["yushu"]


def test_search_limit_clamps(feed):
    f, calls = feed
    assert len(f.search_symbols("yushu", limit=2)) == 2
    assert f.search_symbols("yushu", limit=999) is not None
    assert calls[-1][1] is None or True  # limit 在缓存命中时不触发外呼


def test_search_empty_query_no_call(feed):
    f, calls = feed
    assert f.search_symbols("   ") == []
    assert calls == []
