"""D04：Parquet 存储测试（schema 校验/增量去重/年月分文件）。"""

import pandas as pd
import pytest

from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore, SchemaError


def _daily_df(start: str, days: int, close0: float = 10.0) -> pd.DataFrame:
    dts = pd.bdate_range(start, periods=days)
    return pd.DataFrame({
        "dt": dts,
        "open": [close0 + i * 0.1 for i in range(days)],
        "high": [close0 + i * 0.1 + 0.2 for i in range(days)],
        "low": [close0 + i * 0.1 - 0.1 for i in range(days)],
        "close": [close0 + i * 0.1 + 0.1 for i in range(days)],
        "volume": [10000.0 + i for i in range(days)],
        "amount": [100000.0 + i * 10 for i in range(days)],
    })


@pytest.fixture()
def store(tmp_path):
    return ParquetStore(root=tmp_path / "parquet")


def test_roundtrip_and_column_order(store):
    df = _daily_df("2024-01-02", 10)
    store.write("600000.SH", Frequency.D1, df, AdjustType.FORWARD)
    out = store.read("600000.SH", Frequency.D1, adjust=AdjustType.FORWARD)
    assert list(out.columns) == HISTORY_COLUMNS
    assert len(out) == 10
    assert out["dt"].is_monotonic_increasing
    assert float(out["close"].iloc[-1]) == pytest.approx(10.0 + 9 * 0.1 + 0.1)


def test_incremental_no_duplicate_rows(store):
    """DoD：断点重跑增量无重复行。"""
    df = _daily_df("2024-01-02", 10)
    store.write("000001.SZ", Frequency.D1, df, AdjustType.NONE)
    assert len(store.read("000001.SZ", Frequency.D1, adjust=AdjustType.NONE)) == 10
    # 重跑同样数据
    store.write("000001.SZ", Frequency.D1, df, AdjustType.NONE)
    assert len(store.read("000001.SZ", Frequency.D1, adjust=AdjustType.NONE)) == 10
    # 追加新日期（增量）
    more = _daily_df("2024-01-16", 5)
    store.write("000001.SZ", Frequency.D1, more, AdjustType.NONE)
    assert len(store.read("000001.SZ", Frequency.D1, adjust=AdjustType.NONE)) == 15


def test_overlapping_range_keeps_latest(store):
    df = _daily_df("2024-01-02", 5)
    store.write("600519.SH", Frequency.D1, df, AdjustType.NONE)
    patched = df.copy()
    patched.loc[patched.index[0], "close"] = 99.0
    store.write("600519.SH", Frequency.D1, patched, AdjustType.NONE)  # 同 dt 覆盖
    out = store.read("600519.SH", Frequency.D1, adjust=AdjustType.NONE)
    assert len(out) == 5
    assert float(out["close"].iloc[0]) == 99.0


def test_schema_error_missing_column(store):
    bad = _daily_df("2024-01-02", 3).drop(columns=["close"])
    with pytest.raises(SchemaError):
        store.write("600000.SH", Frequency.D1, bad, AdjustType.NONE)


def test_schema_error_bad_dt(store):
    bad = _daily_df("2024-01-02", 3)
    bad["dt"] = "not-a-date"
    with pytest.raises(Exception):
        store.write("600000.SH", Frequency.D1, bad, AdjustType.NONE)


def test_yearly_partition_files(store):
    store.write("600000.SH", Frequency.D1, _daily_df("2023-06-01", 10), AdjustType.NONE)
    store.write("600000.SH", Frequency.D1, _daily_df("2024-06-03", 10), AdjustType.NONE)
    d = store.root / "SH" / "600000.SH" / "1d" / "none"
    assert (d / "2023.parquet").exists()
    assert (d / "2024.parquet").exists()


def test_minute_monthly_partition_files(store):
    dts = pd.date_range("2024-01-03 09:35", periods=5, freq="5min").tolist() + \
          pd.date_range("2024-02-05 09:35", periods=5, freq="5min").tolist()
    df = pd.DataFrame({
        "dt": dts, "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05,
        "volume": 100.0, "amount": 1005.0,
    })
    store.write("000001.SZ", Frequency.M5, df, AdjustType.NONE)
    d = store.root / "SZ" / "000001.SZ" / "5m" / "none"
    assert (d / "202401.parquet").exists()
    assert (d / "202402.parquet").exists()


def test_read_range_filter_and_empty(store):
    df = _daily_df("2024-01-02", 20)
    store.write("600000.SH", Frequency.D1, df, AdjustType.NONE)
    out = store.read("600000.SH", Frequency.D1, start="2024-01-10", end="2024-01-15",
                     adjust=AdjustType.NONE)
    assert len(out) == 4  # 工作日 1/10、1/11、1/12、1/15（1/13、1/14 为周末）
    empty = store.read("999999.SH", Frequency.D1, adjust=AdjustType.NONE)
    assert empty.empty and list(empty.columns) == HISTORY_COLUMNS


def test_range_of(store):
    assert store.range_of("600000.SH", Frequency.D1, AdjustType.NONE) is None
    store.write("600000.SH", Frequency.D1, _daily_df("2024-01-02", 10), AdjustType.NONE)
    lo, hi = store.range_of("600000.SH", Frequency.D1, AdjustType.NONE)
    assert f"{lo:%Y-%m-%d}" == "2024-01-02"


def test_store_root_resolves_data_dir_at_construction(tmp_path, monkeypatch):
    """事故回归锁：ParquetStore 曾在模块顶层绑定 data_dir，夹具改目录不生效。

    后果不是测试红，而是测试把合成行情写进真实 data/parquet（覆盖同年真实 K 线）。
    构造时才取目录，任何改 data_dir 的夹具都必须能改到存储根。
    """
    from quantdesk import config as cfg
    from quantdesk.store.parquet_store import ParquetStore

    monkeypatch.setattr(cfg, "data_dir", lambda: tmp_path)
    assert ParquetStore().root == tmp_path / "parquet"
    # 已构造的实例仍指旧根：写入只落在自己那份 root 下，不会回写真实目录
    (tmp_path / "parquet").mkdir(parents=True, exist_ok=True)
