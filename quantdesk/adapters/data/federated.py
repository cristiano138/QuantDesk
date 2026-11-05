"""FederatedFeed —— services 层唯一直接使用的行情源（ADR-4）。

- 历史：本地 Parquet 优先 → miss 按优先级外呼 → 成功回写 Parquet；
- 财务：按降级链逐源尝试（FeedCapabilityError → 下一个源）；
- 实时：主源单一（ADR-5 宁停勿错，断线不换源以免两套 tick 口径混喂策略），
  订阅按 subscriber 做引用计数，多策略订同一标的对外只一份订阅，计数归零才真退订；
  策略停止/崩溃走 drop_subscriber() 统一注销；重连后 resync_realtime() 重放订阅集合；
- 本地命中即返回（W1 简化，不校验区间覆盖完整性）——完整性由 gap_report 兜底，
  缺口补拉走 DataStoreService.batch_download（write 幂等去重）。
"""

from __future__ import annotations

import logging
import threading
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
    label = "多来源自动切换"

    def __init__(self, history_feeds: list[DataFeedAdapter],
                 financial_feeds: list[DataFeedAdapter] | None = None,
                 store: ParquetStore | None = None,
                 realtime_feed: DataFeedAdapter | None = None):
        self.history_feeds = list(history_feeds)
        self.financial_feeds = list(financial_feeds) if financial_feeds else list(history_feeds)
        self.store = store
        self.realtime_feed = realtime_feed
        self._connected = False
        # 订阅引用计数：symbol → {subscriber_id}，计数归零才真的退订（多个策略订同一票，
        # 对外部只维持一份订阅——D14 DoD）
        self._refs: dict[str, set[str]] = {}
        self._callbacks: dict[str, dict[str, Callable[[Tick | Bar], None]]] = {}
        self._rt_lock = threading.RLock()
        # 观测用：上一次财务调用真正应答的源名（界面要写"数据来自哪"，不能靠猜）
        self.last_financial_source = ""

    # ---------- 能力 ----------
    @property
    def capabilities(self) -> frozenset[FeedCapability]:
        caps: set[FeedCapability] = set()
        for f in (*self.history_feeds, *self.financial_feeds, self.realtime_feed):
            if f is not None:
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
        with self._rt_lock:
            self._refs.clear()
            self._callbacks.clear()
        for f in self._unique_feeds():
            try:
                f.disconnect()
            except Exception:  # noqa: BLE001 幂等清理
                log.debug("子源 disconnect 异常（忽略）: %s", getattr(f, "name", f))
        self._connected = False

    def health_check(self) -> bool:
        return any(f.health_check() for f in self._unique_feeds())

    def _unique_feeds(self) -> list[DataFeedAdapter]:
        seen: dict[int, DataFeedAdapter] = {}
        for f in (*self.history_feeds, *self.financial_feeds, self.realtime_feed):
            if f is not None:
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
                df = feed.get_financial_data(symbol, report_type)
                # 谁答的要能回显：界面写"数据来自哪"不能靠猜（口径可追溯）
                self.last_financial_source = getattr(feed, "name", "unknown")
                return df
            except FeedCapabilityError as e:
                last = e
                log.info("财务降级: %s 不可用 → 下一源", getattr(feed, "name", feed))
                continue
        raise FeedCapabilityError(
            f"无可用财务源（symbol={symbol}, report_type={report_type}）: {last}")

    # ---------- 实时路由（ADR-5：主源单一，宁停勿错） ----------
    def subscribe_realtime(self, symbols: list[str],
                           callback: Callable[[Tick | Bar], None],
                           subscriber: str = "default") -> None:
        """按订阅方登记引用；某标的第一个订阅者到来才真的向主源订阅一份。

        subscriber 是策略/服务标识（进程内唯一），用于停止/崩溃时精确注销。
        """
        if self.realtime_feed is None:
            raise FeedCapabilityError(
                "无实时行情主源：在设置里接入 miniQMT 或通达信通道，"
                "并确认 feed_priority_realtime 指向已就绪的实时源")
        new: list[str] = []
        with self._rt_lock:
            table = self._callbacks.setdefault(subscriber, {})
            for s in symbols:
                table[s] = callback
                refs = self._refs.setdefault(s, set())
                if not refs:
                    new.append(s)
                refs.add(subscriber)
        if new:
            self.realtime_feed.subscribe_realtime(new, self._dispatch)
            log.info("实时订阅 %d 个标的（主源 %s）", len(new), self.realtime_feed.name)

    def unsubscribe_realtime(self, symbols: list[str],
                             subscriber: str = "default") -> None:
        """注销引用；计数归零的标的一次性向主源退订。"""
        gone: list[str] = []
        with self._rt_lock:
            table = self._callbacks.get(subscriber, {})
            for s in symbols:
                table.pop(s, None)
                refs = self._refs.get(s)
                if refs:
                    refs.discard(subscriber)
                    if not refs:
                        self._refs.pop(s, None)
                        gone.append(s)
            if not table:
                self._callbacks.pop(subscriber, None)
        if gone and self.realtime_feed is not None:
            self.realtime_feed.unsubscribe_realtime(gone)

    def drop_subscriber(self, subscriber: str) -> None:
        """策略停止/崩溃的统一注销入口（宪法：两种路径都要把引用放掉）。"""
        with self._rt_lock:
            symbols = list(self._callbacks.get(subscriber, {}))
        if symbols:
            self.unsubscribe_realtime(symbols, subscriber=subscriber)

    def active_symbols(self) -> list[str]:
        with self._rt_lock:
            return sorted(self._refs)

    def subscriber_count(self, symbol: str) -> int:
        with self._rt_lock:
            return len(self._refs.get(symbol, ()))

    def resync_realtime(self) -> None:
        """重连后重放当前订阅集合（适配器自己会重放；这里是组合适配器的兜底义务）。"""
        with self._rt_lock:
            symbols = sorted(self._refs)
        if symbols and self.realtime_feed is not None:
            self.realtime_feed.subscribe_realtime(symbols, self._dispatch)
            log.info("实时订阅已重放：%d 个标的", len(symbols))

    def _dispatch(self, item: Tick | Bar) -> None:
        """主源回调 → 按订阅方分发。回调线程只分发，绝不做阻塞操作。"""
        symbol = getattr(item, "symbol", "")
        with self._rt_lock:
            targets = [cb for subs in self._callbacks.values()
                       for s, cb in subs.items() if s == symbol]
        for cb in targets:
            try:
                cb(item)
            except Exception:  # noqa: BLE001 单订阅方异常不影响他人
                log.exception("实时回调异常（已隔离）: %s", symbol)


ADAPTER = FederatedFeed
