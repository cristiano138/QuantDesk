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

    # ---------- 补齐（自动盯盘的每日 K 线） ----------
    def backfill(self, symbols: list[str], freq: Frequency, start, end,
                 adjust: AdjustType = AdjustType.FORWARD) -> DownloadReport:
        """将源侧数据补进本地库：只写本地缺的 dt，并且写入后立刻自检。

        为什么不直接用 batch_download 覆盖：公开免费源的字段不齐（腾讯日线没有成交额，
        amount 恒为 0），而 akshare/tushare 有。每日自动任务如果把已有行覆盖成 0，
        一个月后没人能发现本地库的成交额已经被打空——这是数据资产上的静默劣化。
        所以这里只允许两种写：本地没有的 dt、以及能把 amount=0 升级成有金额的 dt。
        """
        report = DownloadReport(started_at=int(pd.Timestamp.now().timestamp() * 1000))
        for sym in symbols:
            try:
                fetched = self.feed.get_history(sym, freq, start, end, adjust)
                if fetched is None or fetched.empty:
                    report.ok[sym] = 0
                    continue
                local = self.store.read(sym, freq, adjust=adjust)
                combined = self._merge_candidate(fetched, local)
                if combined.empty:
                    report.ok[sym] = 0
                    continue
                written = self.store.write(sym, freq, combined, adjust)
                problem = self._selfcheck(sym, freq, adjust, local, combined)
                if problem:
                    report.failed[sym] = problem
                    log.error("K 线补齐自检未通过 %s: %s", sym, problem)
                    continue
                report.ok[sym] = written
            except Exception as e:  # noqa: BLE001 单标的失败不中断整批
                report.failed[sym] = repr(e)
                log.error("K 线补齐失败 %s: %s", sym, e)
        report.finished_at = int(pd.Timestamp.now().timestamp() * 1000)
        return report

    @staticmethod
    def _merge_candidate(fetched: pd.DataFrame, local: pd.DataFrame) -> pd.DataFrame:
        """待写行 = 本地没有的 dt + 能把本地 amount=0 换成有金额的 dt。"""
        have = pd.to_datetime(local["dt"]) if not local.empty else pd.Series(dtype="datetime64[ns]")
        f_dt = pd.to_datetime(fetched["dt"])
        keep = ~f_dt.isin(set(have))
        if not local.empty:
            zero_amount = pd.to_numeric(local["amount"], errors="coerce").fillna(0.0)
            fixable = set(pd.to_datetime(local.loc[zero_amount <= 0.0, "dt"]))
            incoming_amount = pd.to_numeric(fetched["amount"], errors="coerce").fillna(0.0)
            keep |= f_dt.isin(fixable) & (incoming_amount > 0.0)
        return fetched.loc[keep].copy()

    def _selfcheck(self, symbol: str, freq: Frequency, adjust: AdjustType,
                   before: pd.DataFrame, written: pd.DataFrame) -> str:
        """写完就复核：重读本地，确认新行确实在、dt 仍单调不重复、原有行一行没少。"""
        after = self.store.read(symbol, freq, adjust=adjust)
        dts = pd.to_datetime(after["dt"])
        if len(dts) > 1 and not dts.is_monotonic_increasing:
            return "自检失败：dt 不单调"
        dup = int(dts.duplicated().sum())
        if dup:
            return f"自检失败：本地出现 {dup} 个重复 dt"
        before_days = set(pd.to_datetime(before["dt"])) if not before.empty else set()
        lost = before_days - set(dts)
        if lost:
            return f"自检失败：原有 {len(lost)} 个交易日丢失（补齐不该删数据）"
        added = set(pd.to_datetime(written["dt"]))
        missing_new = added - set(dts)
        if missing_new:
            return f"自检失败：声称写入的 {len(missing_new)} 行没落库"
        return ""

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
