"""D05：数据服务测试（批量下载编排/缺口报告/日历回退标记）。"""

import pandas as pd

from quantdesk.adapters.data.federated import FederatedFeed
from quantdesk.core.calendar import TradingCalendar
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.services.data_store import DataStoreService
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore
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


# ---------------- 每日自动 K 线补齐（只补缺，不覆盖） ----------------

class StubFeed:
    """可控日线源：腾讯日线的 amount 恒为 0，这里就是要演这个坑。"""

    name = "stub"

    def __init__(self, rows_by_symbol):
        self.rows = rows_by_symbol
        self.calls = []

    def get_history(self, symbol, freq, start, end, adjust=None):
        self.calls.append((symbol, adjust))
        recs = self.rows.get(symbol)
        if recs is None:
            raise RuntimeError(f"该标的无数据: {symbol}")
        return pd.DataFrame(recs, columns=HISTORY_COLUMNS)


def _bars(*days_amount, close=10.0):
    """(day, amount) → 宪法列 DataFrame。"""
    return pd.DataFrame([[pd.Timestamp(d), close, close, close, close, 100.0, a]
                         for d, a in days_amount], columns=HISTORY_COLUMNS)


def _svc_feed(tmp_path, feed):
    store = ParquetStore(root=tmp_path / "p")
    return DataStoreService(feed, store), store


def test_backfill_never_overwrites_existing_days(tmp_path):
    """本地已有的行绝不覆盖：akshare 口径（有成交额）在先，amount=0 的源后来只能补缺。"""
    feed = StubFeed({"00700.HK": _bars(("2024-01-02", 0.0), ("2024-01-03", 0.0),
                                        ("2024-01-04", 0.0))})
    svc, store = _svc_feed(tmp_path, feed)
    store.write("00700.HK", Frequency.D1, _bars(("2024-01-02", 9999.0), close=11.0),
                AdjustType.FORWARD)
    rep = svc.backfill(["00700.HK"], Frequency.D1, "2024-01-01", "2024-01-31", AdjustType.FORWARD)
    assert rep.ok["00700.HK"] == 2 and not rep.failed
    local = store.read("00700.HK", Frequency.D1, adjust=AdjustType.FORWARD)
    kept = local[local["dt"] == pd.Timestamp("2024-01-02")].iloc[0]
    assert kept["close"] == 11.0 and kept["amount"] == 9999.0   # 没被 amount=0 换掉
    assert len(local) == 3


def test_backfill_upgrades_zero_amount_rows(tmp_path):
    """反向也要成立：本地是 amount=0 的旧行，来了带金额的同日行，允许升级。"""
    svc, store = _svc_feed(tmp_path, StubFeed({"600000.SH": _bars(("2024-01-02", 5000.0),
                                                                   close=12.0)}))
    store.write("600000.SH", Frequency.D1, _bars(("2024-01-02", 0.0)), AdjustType.NONE)
    rep = svc.backfill(["600000.SH"], Frequency.D1, "2024-01-01", "2024-01-31", AdjustType.NONE)
    assert rep.ok["600000.SH"] == 1
    row = store.read("600000.SH", Frequency.D1, adjust=AdjustType.NONE).iloc[0]
    assert row["amount"] == 5000.0 and row["close"] == 12.0


def test_backfill_is_idempotent(tmp_path):
    svc, store = _svc_feed(tmp_path, StubFeed({"00700.HK": _bars(("2024-01-02", 0.0),
                                                                 ("2024-01-03", 0.0))}))
    args = (["00700.HK"], Frequency.D1, "2024-01-01", "2024-01-31", AdjustType.FORWARD)
    assert svc.backfill(*args).ok["00700.HK"] == 2
    assert svc.backfill(*args).ok["00700.HK"] == 0        # 没有新东西就不写
    assert len(store.read("00700.HK", Frequency.D1, adjust=AdjustType.FORWARD)) == 2


def test_backfill_records_per_symbol_failure(tmp_path):
    svc, _ = _svc_feed(tmp_path, StubFeed({"00700.HK": _bars(("2024-01-02", 0.0))}))
    rep = svc.backfill(["00700.HK", "999999.SZ"], Frequency.D1, "2024-01-01", "2024-01-31",
                       AdjustType.FORWARD)
    assert rep.ok["00700.HK"] == 1
    assert "999999.SZ" in rep.failed and "无数据" in rep.failed["999999.SZ"]


def test_selfcheck_flags_lost_days(tmp_path):
    """自检不是摆设：落库后如果原有的交易日不见了，必须报失败。"""
    svc, store = _svc_feed(tmp_path, StubFeed({}))
    before = _bars(("2024-01-02", 0.0))
    store.read = lambda *a, **k: pd.DataFrame(columns=HISTORY_COLUMNS)   # 模拟数据被吞
    assert "丢失" in svc._selfcheck("00700.HK", Frequency.D1, AdjustType.FORWARD, before, before)


def test_selfcheck_clean_when_state_good(tmp_path):
    svc, store = _svc_feed(tmp_path, StubFeed({}))
    good = _bars(("2024-01-02", 0.0), ("2024-01-03", 0.0))
    store.read = lambda *a, **k: good
    assert svc._selfcheck("00700.HK", Frequency.D1, AdjustType.FORWARD, good, good) == ""
