"""数据服务：批量下载编排 + 交易日缺口报告（D05）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from quantdesk.adapters.base.datafeed import DataFeedAdapter
from quantdesk.core.calendar import TradingCalendar
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)


@dataclass
class DownloadReport:
    ok: dict[str, int] = field(default_factory=dict)      # symbol -> 参与合并行数
    failed: dict[str, str] = field(default_factory=dict)  # symbol -> 错误摘要
    started_at: int = 0
    finished_at: int = 0

    @property
    def total_rows(self) -> int:
        return sum(self.ok.values())


class DataStoreService:
    def __init__(self, feed: DataFeedAdapter, store: ParquetStore,
                 calendar: TradingCalendar | None = None):
        self.feed = feed
        self.store = store
        self.calendar = calendar

    # ---------- 批量下载 ----------
    def batch_download(self, symbols: list[str], freq: Frequency, start, end,
                       adjust: AdjustType = AdjustType.FORWARD,
                       progress=None) -> DownloadReport:
        """逐标的经 FederatedFeed 拉取并入库；单标的失败不中断整批。"""
        report = DownloadReport(started_at=int(pd.Timestamp.now().timestamp() * 1000))
        total = len(symbols)
        for i, sym in enumerate(symbols, 1):
            try:
                df = self.feed.get_history(sym, freq, start, end, adjust)
                rows = self.store.write(sym, freq, df, adjust) \
                    if df is not None and not df.empty else 0
                report.ok[sym] = rows
                if progress:
                    progress(i, total, sym, rows, None)
            except Exception as e:  # noqa: BLE001 单标的失败不中断
                report.failed[sym] = repr(e)
                log.error("下载失败 %s: %s", sym, e)
                if progress:
                    progress(i, total, sym, 0, repr(e))
        report.finished_at = int(pd.Timestamp.now().timestamp() * 1000)
        return report

    # ---------- 缺口报告 ----------
    def _expected_days(self, start, end) -> tuple[list[str], bool]:
        """期望交易日：真日历优先；未覆盖/未配置退化为工作日启发式（带标记）。"""
        if self.calendar is not None:
            days, used_fallback = self.calendar.open_days_with_fallback(start, end)
            return days, used_fallback
        days: list[str] = []
        d = pd.Timestamp(start).date()
        last = pd.Timestamp(end).date()
        while d <= last:
            if d.weekday() < 5:
                days.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return days, True

    def gap_report(self, symbol: str, freq: Frequency, start, end,
                   adjust: AdjustType = AdjustType.NONE) -> dict:
        """对照期望交易日，报告本地 Parquet 的缺失日期。"""
        df = self.store.read(symbol, freq, start, end, adjust)
        present = {t.strftime("%Y%m%d") for t in pd.to_datetime(df["dt"])} \
            if not df.empty else set()
        expected, used_fallback = self._expected_days(start, end)
        missing = [d for d in expected if d not in present]
        coverage = 1.0 - (len(missing) / len(expected)) if expected else 1.0
        return {
            "symbol": symbol,
            "freq": freq.value,
            "adjust": adjust.value,
            "start": pd.Timestamp(start).strftime("%Y%m%d"),
            "end": pd.Timestamp(end).strftime("%Y%m%d"),
            "expected_days": len(expected),
            "present_days": len(present & set(expected)),
            "missing_days": len(missing),
            "missing_sample": missing[:10],
            "coverage": round(coverage, 4),
            "used_fallback": used_fallback,
        }
