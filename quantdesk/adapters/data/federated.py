"""FederatedFeed —— services 层唯一直接使用的行情源（ADR-4）。

- 历史：本地 Parquet 优先 → miss 按优先级外呼 → 成功回写 Parquet；
- 财务：按降级链逐源尝试（FeedCapabilityError → 下一个源）；
- 实时：W3（D14）接入 qmt 主实时源 + 订阅引用计数；W1 调用即抛 FeedCapabilityError；
- 本地命中即返回（W1 简化，不校验区间覆盖完整性）——完整性由 gap_report 兜底，
  缺口补拉走 DataStoreService.batch_download（write 幂等去重）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import (
    AdapterError,
    ConfigError,
    ConnectionLostError,
    FeedCapabilityError,
)
from quantdesk.core.models import Bar, Tick
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)


class FederatedFeed(DataFeedAdapter):
    name = "federated"

    def __init__(self, history_feeds: list[DataFeedAdapter],
                 financial_feeds: list[DataFeedAdapter] | None = None,
                 store: ParquetStore | None = None):
        self.history_feeds = list(history_feeds)
        self.financial_feeds = list(financial_feeds) if financial_feeds else list(history_feeds)
        self.store = store
        self._connected = False

    # ---------- 能力 ----------
    @property
    def capabilities(self) -> frozenset[FeedCapability]:
        caps: set[FeedCapability] = set()
        for f in (*self.history_feeds, *self.financial_feeds):
            caps |= set(f.capabilities)
        return frozenset(caps)

    # ---------- 生命周期 ----------
    def connect(self) -> None:
        for f in self._unique_feeds():
            try:
                f.connect()
            except (AdapterError, ConfigError) as e:
                log.warning("子源 connect 失败（跳过）: %s: %s", getattr(f, "name", f), e)
        self._connected = True

    def disconnect(self) -> None:
        for f in self._unique_feeds():
            try:
                f.disconnect()
            except Exception:  # noqa: BLE001 幂等清理
                log.debug("子源 disconnect 异常（忽略）: %s", getattr(f, "name", f))
        self._connected = False

    def health_check(self) -> bool:
        return any(f.health_check() for f in self.history_feeds)

    def _unique_feeds(self) -> list[DataFeedAdapter]:
        seen: dict[int, DataFeedAdapter] = {}
        for f in (*self.history_feeds, *self.financial_feeds):
            seen.setdefault(id(f), f)
        return list(seen.values())

    # ---------- 历史 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        if self.store is not None:
            local = self.store.read(symbol, freq, start, end, adjust)
            if not local.empty:
                return local
        last_error: Exception | None = None
        attempted = False
        for feed in self.history_feeds:
            if FeedCapability.HISTORY_BAR not in feed.capabilities:
                continue
            attempted = True
            try:
                df = feed.get_history(symbol, freq, start, end, adjust)
            except (AdapterError, ConfigError) as e:
                last_error = e
                log.warning("历史外呼失败: %s %s: %s", getattr(feed, "name", feed), symbol, e)
                continue
            if df is not None and not df.empty:
                if self.store is not None:
                    try:
                        self.store.write(symbol, freq, df, adjust)
                    except Exception as e:  # noqa: BLE001 回写失败不影响返回
                        log.error("Parquet 回写失败 %s: %s", symbol, e)
                return df
        # 语义区分：空 DataFrame = 问到了但无数据；全部子源报错 = 没问到 → 抛错
        # （避免批量下载把"源不可用"误标为"成功 0 行"）
        if attempted and last_error is not None:
            raise ConnectionLostError(
                f"全部历史源不可用（symbol={symbol}）: {last_error}") from last_error
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    # ---------- 财务（降级链） ----------
    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        last: Exception | None = None
        for feed in self.financial_feeds:
            if FeedCapability.FINANCIAL not in feed.capabilities:
                continue
            try:
                return feed.get_financial_data(symbol, report_type)
            except FeedCapabilityError as e:
                last = e
                log.info("财务降级: %s 不可用 → 下一源", getattr(feed, "name", feed))
                continue
        raise FeedCapabilityError(
            f"无可用财务源（symbol={symbol}, report_type={report_type}）: {last}")

    # ---------- 实时（W3 提供） ----------
    def subscribe_realtime(self, symbols: list[str], callback: Callable[[Tick | Bar], None]) -> None:
        raise FeedCapabilityError("实时路由在 W3（D14）提供：qmt 主实时源 + 订阅引用计数")

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        raise FeedCapabilityError("实时路由在 W3（D14）提供")


ADAPTER = FederatedFeed
