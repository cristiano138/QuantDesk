"""D05：数据服务测试（批量下载编排/缺口报告/日历回退标记）。"""

import pandas as pd

from quantdesk.adapters.data.federated import FederatedFeed
from quantdesk.core.calendar import TradingCalendar
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.services.data_store import DataStoreService
from quantdesk.store.parquet_store import ParquetStore
from tests.test_federated import DummyFeed, _df


def _svc(tmp_path, calendar=None, feeds=None):
    store = ParquetStore(root=tmp_path / "p")
    fed = FederatedFeed(feeds or [DummyFeed(df=_df())], store=store)
    return DataStoreService(fed, store, calendar), store


def test_batch_download_report(tmp_path):
    svc, store = _svc(tmp_path)
    progress: list[tuple] = []
    report = svc.batch_download(
        ["600000.SH", "000001.SZ"], Frequency.D1, "2024-01-01", "2024-01-31",
        AdjustType.FORWARD, progress=lambda *a: progress.append(a))
    assert set(report.ok) == {"600000.SH", "000001.SZ"}
    assert report.ok["600000.SH"] == 3
    assert not report.failed
    assert len(progress) == 2
    assert not store.read("600000.SH", Frequency.D1, adjust=AdjustType.FORWARD).empty


def test_batch_download_partial_failure(tmp_path):
    bad = DummyFeed(df=None)
    bad.get_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    svc, _ = _svc(tmp_path, feeds=[DummyFeed(df=_df()), bad])
    report = svc.batch_download(
        ["600000.SH", "000001.SZ"], Frequency.D1, "2024-01-01", "2024-01-31",
        AdjustType.NONE)
    # FederatedFeed 顺序外呼：第一源成功 → 全部成功
    assert not report.failed


def test_gap_report_with_calendar(tmp_path):
    cal = TradingCalendar(tmp_path / "cal.db")
    cal.upsert([("20240102", 1), ("20240103", 1), ("20240104", 0), ("20240105", 1)])
    svc, store = _svc(tmp_path, calendar=cal)
    store.write("600000.SH", Frequency.D1, _df(days=3), AdjustType.NONE)  # 1/2,1/3,1/4? bdate 1/2-1/4
    g = svc.gap_report("600000.SH", Frequency.D1, "2024-01-02", "2024-01-05",
                       AdjustType.NONE)
    assert g["expected_days"] == 3  # 真日历 1/2,1/3,1/5
    assert g["used_fallback"] is False
    assert g["missing_days"] == 1   # 1/5 缺
    assert g["missing_sample"] == ["20240105"]


def test_gap_report_fallback_flag(tmp_path):
    svc, store = _svc(tmp_path, calendar=None)  # 无日历 → 工作日启发式
    store.write("000001.SZ", Frequency.D1, _df(days=3), AdjustType.NONE)
    g = svc.gap_report("000001.SZ", Frequency.D1, "2024-01-02", "2024-01-04",
                       AdjustType.NONE)
    assert g["used_fallback"] is True
    assert g["expected_days"] == 3
    assert g["missing_days"] == 0
    assert g["coverage"] == 1.0


def test_gap_report_empty_store(tmp_path):
    svc, _ = _svc(tmp_path)
    g = svc.gap_report("999999.SH", Frequency.D1, "2024-01-02", "2024-01-04")
    assert g["present_days"] == 0
    assert g["missing_days"] == 3
    assert g["coverage"] == 0.0
