"""历史数据批量下载 CLI（D04/D05）。

用法：
  uv run python scripts/download_history.py --symbols 600000.SH,000001.SZ --start 2019-01-01
  uv run python scripts/download_history.py                 # 默认 scripts/symbols_sample.txt（50 只）

输出：Parquet 入库（幂等去重）+ 缺口报告 CSV（data/logs/gap_report_<ts>.csv）。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from quantdesk.adapters.data.akshare_feed import AkshareFeed
from quantdesk.adapters.data.federated import FederatedFeed
from quantdesk.adapters.data.tushare_feed import TushareFeed
from quantdesk.config import data_dir, get_settings
from quantdesk.core.calendar import TradingCalendar
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.services.data_store import DataStoreService
from quantdesk.store.parquet_store import ParquetStore


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QuantDesk 历史数据批量下载")
    p.add_argument("--symbols", default="", help="逗号分隔内部代码，如 600000.SH,000001.SZ")
    p.add_argument("--symbols-file", default="", help="每行一个内部代码的文本文件")
    p.add_argument("--freq", default="1d", help="周期（W1: 1d）")
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--adjust", default="qfq", choices=["none", "qfq", "hfq"])
    return p.parse_args()


def load_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    f = Path(args.symbols_file) if args.symbols_file \
        else Path(__file__).parent / "symbols_sample.txt"
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def main() -> int:
    args = parse_args()
    freq = Frequency(args.freq)
    adjust = {"none": AdjustType.NONE, "qfq": AdjustType.FORWARD,
              "hfq": AdjustType.BACKWARD}[args.adjust]
    symbols = load_symbols(args)
    if not symbols:
        print("未指定标的（--symbols 或 --symbols-file）")
        return 1

    store = ParquetStore()
    feeds = [AkshareFeed(), TushareFeed()]
    feed = FederatedFeed(history_feeds=feeds, financial_feeds=feeds, store=store)
    feed.connect()  # tushare 无 token 会告警跳过，不阻塞（AKShare 兜底）

    calendar = None
    try:
        calendar = TradingCalendar(data_dir() / "quantdesk.db")
        token = get_settings().tushare_token
        if token:
            try:
                calendar.ensure_range(args.start, args.end, token)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 交易日历拉取失败（用启发式）: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 日历不可用（用工作日启发式）: {e}")

    svc = DataStoreService(feed, store, calendar)

    def progress(i: int, n: int, sym: str, rows: int, err: str | None) -> None:
        if err is None:
            print(f"[{i}/{n}] {sym}: {rows} 行")
        else:
            print(f"[{i}/{n}] {sym}: 失败 {err}")

    print(f"开始下载 {len(symbols)} 只标的 {args.start} ~ {args.end} freq={freq.value} "
          f"adjust={adjust.value}")
    report = svc.batch_download(symbols, freq, args.start, args.end, adjust, progress)

    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / f"gap_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "expected_days", "present_days", "missing_days",
                    "coverage", "used_fallback", "missing_sample"])
        for sym in symbols:
            g = svc.gap_report(sym, freq, args.start, args.end, adjust)
            w.writerow([g["symbol"], g["expected_days"], g["present_days"],
                        g["missing_days"], g["coverage"], g["used_fallback"],
                        "|".join(g["missing_sample"][:5])])

    print(f"\n完成：成功 {len(report.ok)} 只 / 失败 {len(report.failed)} 只，"
          f"共 {report.total_rows} 行")
    if report.failed:
        print("失败明细：")
        for s, e in report.failed.items():
            print(f"  {s}: {e}")
    print(f"缺口报告: {out}")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
