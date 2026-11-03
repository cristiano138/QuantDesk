"""D04/D05：AKShare 真实数据拉取（@network，默认跳过；-m network 显式运行）。"""

import pytest

from quantdesk.adapters.data.akshare_feed import AkshareFeed
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.store.parquet_store import HISTORY_COLUMNS


@pytest.mark.network
def test_akshare_real_daily_qfq():
    feed = AkshareFeed(min_interval=0.0)
    try:
        feed.connect()
        df = feed.get_history("000001.SZ", Frequency.D1, "2025-01-01", "2025-01-31",
                              AdjustType.FORWARD)
    except Exception as e:  # noqa: BLE001 网络不可用 → 跳过而非红
        pytest.skip(f"网络/接口不可用: {e}")
    assert not df.empty
    assert list(df.columns) == HISTORY_COLUMNS
    assert df["dt"].is_monotonic_increasing
