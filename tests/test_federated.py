"""D05：FederatedFeed 测试（Parquet 优先/外呼回写/财务降级链/实时守卫）。"""

import pandas as pd
import pytest

from quantdesk.adapters.data.federated import FederatedFeed
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import AdapterError, FeedCapabilityError
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore


def _df(close: float = 10.0, start: str = "2024-01-02", days: int = 3) -> pd.DataFrame:
    dts = pd.bdate_range(start, periods=days)
    return pd.DataFrame({"dt": dts, "open": close, "high": close, "low": close,
                         "close": close, "volume": 100.0, "amount": 1000.0})


class DummyFeed:
    """脚本化假源：df 非空则返回；raise_if 设定时抛对应异常。"""

    name = "dummy"
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL})

    def __init__(self, df=None, raise_if: Exception | None = None, fin_df=None):
        self.df = df
        self.raise_if = raise_if
        self.fin_df = fin_df
        self.connected = False
        self.calls = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def health_check(self) -> bool:
        return self.connected

    def get_history(self, symbol, freq, start, end, adjust=AdjustType.FORWARD):
        self.calls += 1
        if self.raise_if is not None:
            raise self.raise_if
        return self.df if self.df is not None else pd.DataFrame(columns=HISTORY_COLUMNS)

    def get_financial_data(self, symbol, report_type="income"):
        if self.fin_df is None:
            raise FeedCapabilityError("dummy 无财务")
        return self.fin_df

    def subscribe_realtime(self, symbols, callback) -> None:
        raise FeedCapabilityError("dummy 无实时")


def test_local_hit_no_external_call(tmp_path):
    store = ParquetStore(root=tmp_path / "p")
    store.write("600000.SH", Frequency.D1, _df(), AdjustType.FORWARD)
    dummy = DummyFeed(df=_df(close=99.0))
    fed = FederatedFeed([dummy], store=store)
    out = fed.get_history("600000.SH", Frequency.D1, "2024-01-01", "2024-01-31",
                          AdjustType.FORWARD)
    assert dummy.calls == 0          # 本地命中不外呼
    assert float(out["close"].iloc[0]) == 10.0


def test_miss_calls_feed_and_writes_back(tmp_path):
    store = ParquetStore(root=tmp_path / "p")
    dummy = DummyFeed(df=_df())
    fed = FederatedFeed([dummy], store=store)
    out = fed.get_history("600000.SH", Frequency.D1, "2024-01-01", "2024-01-31",
                          AdjustType.FORWARD)
    assert dummy.calls == 1
    assert not out.empty
    # 回写生效：第二次调用走本地
    out2 = fed.get_history("600000.SH", Frequency.D1, "2024-01-01", "2024-01-31",
                           AdjustType.FORWARD)
    assert dummy.calls == 1
    assert not out2.empty


def test_all_feeds_fail_returns_empty(tmp_path):
    fed = FederatedFeed([DummyFeed(raise_if=AdapterError("x")),
                         DummyFeed(raise_if=AdapterError("y"))],
                        store=ParquetStore(root=tmp_path / "p"))
    out = fed.get_history("600000.SH", Frequency.D1, "2024-01-01", "2024-01-31")
    assert out.empty and list(out.columns) == HISTORY_COLUMNS


def test_connect_tolerates_child_failure():
    bad = DummyFeed(raise_if=AdapterError("connect fail"))
    good = DummyFeed(df=_df())
    fed = FederatedFeed([bad, good])
    fed.connect()  # 不应抛异常
    assert good.connected
    fed.disconnect()


def test_financial_degradation_chain():
    no_fin = DummyFeed(df=_df())                      # FINANCIAL 能力但无数据 → 降级
    has_fin = DummyFeed(fin_df=pd.DataFrame({"k": [1]}))
    fed = FederatedFeed([no_fin, has_fin])
    out = fed.get_financial_data("600000.SH", "income")
    assert not out.empty

    fed2 = FederatedFeed([DummyFeed(), DummyFeed()])
    with pytest.raises(FeedCapabilityError):
        fed2.get_financial_data("600000.SH", "income")


def test_realtime_guard():
    fed = FederatedFeed([DummyFeed(df=_df())])
    with pytest.raises(FeedCapabilityError):
        fed.subscribe_realtime(["600000.SH"], lambda e: None)
    with pytest.raises(FeedCapabilityError):
        fed.unsubscribe_realtime(["600000.SH"])


def test_capabilities_union():
    fed = FederatedFeed([DummyFeed(df=_df())])
    assert FeedCapability.HISTORY_BAR in fed.capabilities
    assert FeedCapability.FINANCIAL in fed.capabilities
