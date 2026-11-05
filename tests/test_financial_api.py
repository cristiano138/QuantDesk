"""财务数据：三表真的分开、缺期不补 0、缓存不重复外呼、坏通道不会静默消失。

这一轮接入时踩到两件事，都钉成用例：
① AKShare 的 get_financial_data 过去把 income/balance/cashflow 三种都返回同一张东财摘要，
   界面挂三个页签就是假拆分（数字一模一样）；现在三表走新浪、摘要走东财，内容必须真的不同。
② 适配器文件里一个语法错误让整条通道从注册表消失，界面只剩"无可用财务源: None"——
   装载失败必须登记成看得见的坏通道。
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantdesk.adapters.data.akshare_feed import AkshareFeed
from quantdesk.config import get_settings
from quantdesk.core.errors import FeedCapabilityError
from quantdesk.services import market_data as mod
from quantdesk.services.market_data import FIN_REPORTS, MarketDataService
from quantdesk.store.db import init_db, reset_engine
from quantdesk.store.parquet_store import ParquetStore


# ---------------- 适配器：真三表 ----------------

class _FakeAk:
    """假 akshare：三表各自返回不同的科目集合，摘要返回 指标×期 的宽表。"""

    @staticmethod
    def stock_financial_report_sina(stock: str, symbol: str) -> pd.DataFrame:
        assert stock.startswith(("sh", "sz", "bj")), f"新浪口径要市场前缀，收到 {stock}"
        if symbol == "利润表":
            return pd.DataFrame({"报告日": ["20260930", "20251231"],
                                 "营业收入": ["138133000000", "120000000000"],
                                 "营业支出": ["90000000000", "--"],
                                 "营业外收入": ["", ""]})
        if symbol == "资产负债表":
            return pd.DataFrame({"报告日": ["20260930", "20251231"],
                                 "资产总计": ["9000000000000", "8800000000000"],
                                 "负债合计": ["8400000000000", "8200000000000"]})
        return pd.DataFrame({"报告日": ["20260930", "20251231"],
                             "经营活动产生的现金流量净额": ["1000", "2000"]})

    @staticmethod
    def stock_financial_abstract(symbol: str) -> pd.DataFrame:
        return pd.DataFrame({"选项": ["常用指标", "盈利能力"],
                             "指标": ["归母净利润", "净资产收益率"],
                             "20260930": [4.0e10, 0.11], "20251231": [3.9e10, 0.12]})


@pytest.fixture()
def feed(monkeypatch):
    f = AkshareFeed()
    monkeypatch.setattr(f, "_ak", lambda: _FakeAk)
    monkeypatch.setattr(f, "_call", lambda fn: fn())     # 跳过限速与熔断，只测编组
    return f


@pytest.mark.parametrize("rt", ["income", "balance", "cashflow"])
def test_three_statements_return_their_own_columns(feed, rt):
    df = feed.get_financial_data("600000.SH", report_type=rt)
    assert not df.empty
    assert df["report_type"].unique().tolist() == [rt]
    assert {"symbol", "report_type", "选项", "指标", "20260930", "20251231"} <= set(df.columns)
    assert df["选项"].str.contains("科目").all()


def test_income_and_balance_do_not_share_the_same_numbers(feed):
    inc = feed.get_financial_data("600000.SH", report_type="income")
    bal = feed.get_financial_data("600000.SH", report_type="balance")
    assert set(inc["指标"]) != set(bal["指标"])
    assert "营业收入" in set(inc["指标"]) and "资产总计" in set(bal["指标"])


def test_empty_or_undisclosed_cells_become_none_not_zero(feed):
    """缺期/未披露在界面要显示「—」；补 0 会把"没披露"说成"零利润"。"""
    df = feed.get_financial_data("600000.SH", report_type="income")
    cell = df.loc[df["指标"] == "营业支出"].iloc[0]
    assert cell["20260930"] == 90000000000.0
    assert pd.isna(cell["20251231"])                       # "--" → NaN，不是 0
    dropped = df[df["指标"] == "营业外收入"]
    assert dropped.empty                                   # 几期全空 → 不占屏


def test_hk_is_refused_with_a_reachable_reason(feed):
    with pytest.raises(FeedCapabilityError) as e:
        feed.get_financial_data("00700.HK", report_type="income")
    assert "只覆盖 A股" in str(e.value) and "iFinD" in str(e.value)


def test_unknown_report_type_is_rejected(feed):
    with pytest.raises(FeedCapabilityError):
        feed.get_financial_data("600000.SH", report_type="forecast")


# ---------------- 服务层：转置、缓存、校验 ----------------

@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_engine()
    init_db(tmp_path / "f.db")
    service = MarketDataService(store=ParquetStore(root=tmp_path / "p"), bus=None,
                                db_path=tmp_path / "f.db")
    monkeypatch.setattr(mod, "_service", service)
    yield service
    reset_engine()
    get_settings.cache_clear()


class _StubHub:
    def __init__(self, feed):
        self.feed = feed
        self.calls = 0

    def federated(self):
        class _Fed:
            name = "federated"

            def __init__(self, inner):
                self.inner = inner
                self.last_financial_source = ""

            def get_financial_data(self, symbol, report_type="income"):
                self.last_financial_source = self.inner.last_source = "akshare"
                return self.inner.get_financial_data(symbol, report_type)

        self.calls += 1
        return _Fed(self.feed)


def test_financial_groups_periods_and_labels(svc, feed, monkeypatch):
    hub = _StubHub(feed)
    monkeypatch.setattr("quantdesk.services.channels.get_hub", lambda: hub)
    doc = svc.financial("600000.SH", report_type="income")
    assert doc["periods"][:2] == ["20260930", "20251231"]
    assert doc["groups"][0]["name"] == "利润表科目"
    assert doc["source"] == "akshare"
    assert "新浪" in doc["note"] and doc["cached"] is False


def test_financial_is_cached_per_symbol_and_type(svc, feed, monkeypatch):
    real = feed.get_financial_data
    hits = []

    def counting(sym, report_type="income"):
        hits.append((sym, report_type))
        return real(sym, report_type)

    monkeypatch.setattr(feed, "get_financial_data", counting)
    hub = _StubHub(feed)
    monkeypatch.setattr("quantdesk.services.channels.get_hub", lambda: hub)
    first = svc.financial("600000.SH", "income")
    again = svc.financial("600000.SH", "income")
    other = svc.financial("600000.SH", "balance")
    assert again["cached"] is True and other["cached"] is False
    assert hits == [("600000.SH", "income"), ("600000.SH", "balance")]  # 第二次走缓存


def test_bad_report_type_is_value_error(svc):
    with pytest.raises(ValueError):
        svc.financial("600000.SH", "forecast")


# ---------------- API 契约 ----------------

def test_financial_endpoint_shape_and_errors(svc, feed, monkeypatch):
    from quantdesk.app import app

    monkeypatch.setattr("quantdesk.services.channels.get_hub", lambda: _StubHub(feed))
    # 不起 lifespan：这里只验路由编组，起 lifespan 会把真通道装配拉进来（替身没有 start）
    client = TestClient(app)
    body = client.get("/api/market/financial",
                      params={"symbol": "600000.SH", "report_type": "income"}).json()
    assert {"symbol", "report_type", "periods", "groups", "source", "note"} <= set(body)
    bad = client.get("/api/market/financial",
                     params={"symbol": "600000.SH", "report_type": "bogus"})
    assert bad.status_code == 400 and "report_type" in bad.json()["detail"]
    hk = client.get("/api/market/financial",
                    params={"symbol": "00700.HK", "report_type": "income"})
    assert hk.status_code == 503 and "只覆盖 A股" in hk.json()["detail"]


def test_all_report_types_are_documented(svc):
    """界面按 FIN_REPORTS 渲染页签：加了表种却没写中文口径，界面就会露出原始 key。"""
    from quantdesk.services.market_data import FIN_LABELS

    assert set(FIN_LABELS) == set(FIN_REPORTS)
    for rt in FIN_REPORTS:
        assert "（" in FIN_LABELS[rt], f"{rt} 的口径说明没写来源"


# ---------------- 注册表：装载失败不许静默 ----------------

def test_broken_adapter_module_stays_visible(monkeypatch):
    import importlib
    import pkgutil
    from types import SimpleNamespace

    from quantdesk import adapters as reg

    reg.reset_registry_for_test()
    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name.endswith("broken_demo"):
            raise SyntaxError("invalid syntax (broken_demo.py, line 42)")
        return real_import(name, *a, **kw)

    def fake_iter_modules(path=None, *a, **kw):
        if path and str(path[0]).replace("\\", "/").endswith("adapters/data"):
            return [SimpleNamespace(name="broken_demo", ispkg=False)]
        return []

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    rows = reg.describe_adapters()
    reg.reset_registry_for_test()

    broken = [r for r in rows if "装载失败" in r["name"]]
    assert len(broken) == 1
    assert broken[0]["status"] == "unready"
    assert "invalid syntax" in broken[0]["load_error"]
    # 键与其它通道同构，前端不必为它加新分支
    assert {"name", "kind", "label", "prereq", "official", "missing",
            "capabilities", "deps_ready"} <= set(broken[0])
