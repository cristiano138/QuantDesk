"""D05：Tushare 适配器单测（mock pro 对象，离线；复权计算/能力守卫/token 引导）。"""

import pandas as pd
import pytest

from quantdesk.adapters.data.tushare_feed import TushareFeed
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.errors import ConfigError, FeedCapabilityError
from quantdesk.store.parquet_store import HISTORY_COLUMNS


def _daily_raw() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ"] * 3,
        "trade_date": ["20240103", "20240102", "20240101"],  # tushare 返回降序
        "open": [10.0, 10.1, 10.2],
        "high": [10.5, 10.6, 10.7],
        "low": [9.9, 10.0, 10.1],
        "close": [10.2, 10.3, 10.4],
        "vol": [1000.0, 1100.0, 1200.0],
        "amount": [10200.0, 11330.0, 12480.0],
    })


def _factor_raw() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ"] * 3,
        "trade_date": ["20240101", "20240102", "20240103"],
        "adj_factor": [100.0, 100.0, 101.0],  # 最后一天除权，因子上移
    })


class FakePro:
    def __init__(self):
        self.daily_calls: list[dict] = []

    def daily(self, **kw):
        self.daily_calls.append(kw)
        return _daily_raw()

    def adj_factor(self, **kw):
        return _factor_raw()


def _feed_with_mock() -> tuple[TushareFeed, FakePro]:
    feed = TushareFeed(min_interval=0.0)
    pro = FakePro()
    feed._pro = pro
    return feed, pro


def test_get_history_none_adjust_sorted():
    feed, _ = _feed_with_mock()
    df = feed.get_history("000001.SZ", Frequency.D1, "2024-01-01", "2024-01-03",
                          AdjustType.NONE)
    assert list(df.columns) == HISTORY_COLUMNS
    assert df["dt"].is_monotonic_increasing  # 内部规范：dt 升序
    assert len(df) == 3


def test_get_history_qfq_computation():
    """qfq = 原始价 × factor / 最新 factor。最新 factor=101（20240103 除权）。"""
    feed, _ = _feed_with_mock()
    df = feed.get_history("000001.SZ", Frequency.D1, "2024-01-01", "2024-01-03",
                          AdjustType.FORWARD)
    # 20240101: close 10.4 × 100/101 ≈ 10.297；20240103: 10.2 × 101/101 = 10.2
    assert float(df["close"].iloc[0]) == pytest.approx(10.4 * 100 / 101, rel=1e-9)
    assert float(df["close"].iloc[-1]) == pytest.approx(10.2, rel=1e-9)


def test_get_history_hfq_computation():
    feed, _ = _feed_with_mock()
    df = feed.get_history("000001.SZ", Frequency.D1, "2024-01-01", "2024-01-03",
                          AdjustType.BACKWARD)
    assert float(df["close"].iloc[0]) == pytest.approx(10.4 * 100.0, rel=1e-9)


def test_get_history_rejects_minute():
    feed, _ = _feed_with_mock()
    with pytest.raises(FeedCapabilityError):
        feed.get_history("000001.SZ", Frequency.M5, "2024-01-01", "2024-01-03")


def test_get_history_without_connect_raises():
    feed = TushareFeed(min_interval=0.0)
    with pytest.raises(Exception):
        feed.get_history("000001.SZ", Frequency.D1, "2024-01-01", "2024-01-03")


def test_connect_without_token_guidance():
    feed = TushareFeed(min_interval=0.0, token="")
    with pytest.raises(ConfigError) as exc:
        feed.connect()
    assert "https://tushare.pro" in str(exc.value)


def test_get_financial_data_unknown_type():
    feed, _ = _feed_with_mock()
    with pytest.raises(FeedCapabilityError):
        feed.get_financial_data("000001.SZ", report_type="nope")


def test_financial_passes_ts_code():
    class FinPro(FakePro):
        def __init__(self):
            super().__init__()
            self.calls: list[dict] = []

        def income(self, **kw):
            self.calls.append(kw)
            return pd.DataFrame({"ts_code": ["000001.SZ"], "end_date": ["20231231"]})

    feed = TushareFeed(min_interval=0.0)
    pro = FinPro()
    feed._pro = pro
    df = feed.get_financial_data("000001.SZ", "income")
    assert pro.calls and pro.calls[0]["ts_code"] == "000001.SZ"
    assert df.iloc[0]["report_type"] == "income"
