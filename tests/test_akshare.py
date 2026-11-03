"""D04：AKShare 适配器单测（列名映射/能力守卫，离线不触网）。

真实数据拉取见 test_akshare_network.py（@network，默认跳过）。
"""

import pandas as pd
import pytest

from quantdesk.adapters.data.akshare_feed import AkshareFeed, normalize_history
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import FeedCapabilityError
from quantdesk.store.parquet_store import HISTORY_COLUMNS


def test_capabilities_no_realtime():
    assert FeedCapability.HISTORY_BAR in AkshareFeed.capabilities
    assert FeedCapability.FINANCIAL in AkshareFeed.capabilities
    assert FeedCapability.REALTIME_TICK not in AkshareFeed.capabilities


def test_normalize_history_maps_columns():
    raw = pd.DataFrame({
        "日期": ["2024-01-02", "2024-01-03"],
        "开盘": [10.0, 10.1], "收盘": [10.05, 10.2], "最高": [10.2, 10.3],
        "最低": [9.9, 10.0], "成交量": [10000, 11000], "成交额": [100000, 110000],
    })
    out = normalize_history(raw, _colmap())
    assert list(out.columns) == HISTORY_COLUMNS
    assert str(out["dt"].dtype).startswith("datetime64")
    assert out["dt"].is_monotonic_increasing


def test_normalize_history_extra_and_missing_columns():
    raw = pd.DataFrame({
        "日期": ["2024-01-02"], "开盘": [10.0], "收盘": [10.05], "最高": [10.2],
        "最低": [9.9], "成交量": [10000], "成交额": [100000], "振幅": [3.0],
    })
    out = normalize_history(raw, _colmap())  # 额外列被忽略
    assert list(out.columns) == HISTORY_COLUMNS

    bad = raw.drop(columns=["收盘"])
    with pytest.raises(FeedCapabilityError):
        normalize_history(bad, _colmap())


def test_normalize_history_empty():
    out = normalize_history(pd.DataFrame(), _colmap())
    assert out.empty and list(out.columns) == HISTORY_COLUMNS


def test_get_history_rejects_tick_before_network():
    feed = AkshareFeed(min_interval=0.0)
    with pytest.raises(FeedCapabilityError):
        feed.get_history("000001.SZ", Frequency.TICK, "2024-01-01", "2024-01-31",
                         AdjustType.NONE)


def test_get_financial_data_rejects_unknown_report_type():
    feed = AkshareFeed(min_interval=0.0)
    with pytest.raises(FeedCapabilityError):
        feed.get_financial_data("000001.SZ", report_type="unknown")


def _colmap():
    from quantdesk.adapters.data.akshare_feed import _DAILY_COLMAP

    return dict(_DAILY_COLMAP)
