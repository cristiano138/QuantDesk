"""D27 数据对齐专项（v3，最终口径）：多源数据一致性审计。

实测发现（写入进度日志的关键结论）：
- AKShare(东财) qfq 序列与 raw 序列在非除权日也存在日收益分离——
  部分标的被系统性放大（601899.SH 2019 年段实测 ~4.6×），部分标的接近一致
  （600000.SH/600519.SH 中位分离 ~0.006%）——属源端复权算法特性，非本地存储问题
  （现场重拉逐行一致已排除存储损坏）；
- 因此本日交付从「除权日检测」升级为「源口径审计」：
  逐标的量化 qfq/raw 日收益分离分布，给出该标的 qfq 口径能否用于回测的结论
  （中位分离 < 0.05% 视为可用），并落盘审计报告；
- 严格因子级交叉校验（raw × adj_factor vs qfq，误差 ≤0.1%）待 Tushare token
  （顺延项）；在拿到精确因子前，重除权标的的回测建议改用 raw 口径或更换源。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pandas as pd

from quantdesk.config import data_dir
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.store.parquet_store import ParquetStore

SYMBOLS = [
    "600000.SH", "000001.SZ", "600585.SH",
    "000651.SZ", "601318.SH", "000333.SZ",
    "600519.SH", "002415.SZ", "601899.SH", "300750.SZ",
]
USABLE_MEDIAN = 0.0005        # 中位日收益分离 < 0.05% → qfq 口径可用于回测
EXDIV_HINT = 0.002            # 分离 > 0.2% 的日子记为除权/异常候选（供人工核对）


def main() -> int:
    store = ParquetStore()
    out_dir = data_dir() / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"source_align_audit_{ts}.csv"

    all_ok = True
    rows: list[dict] = []
    print("== D27 数据对齐专项 v3（源口径审计）==")
    print(f"{'标的':<12}{'行数':>6}{'中位分离':>10}{'P95分离':>10}{'>0.2%天数':>9}{'结论':>8}")
    for sym in SYMBOLS:
        raw = store.read(sym, Frequency.D1, adjust=AdjustType.NONE)
        qfq = store.read(sym, Frequency.D1, adjust=AdjustType.FORWARD)
        if raw.empty or qfq.empty:
            print(f"{sym:<12}  缺双口径数据，跳过")
            all_ok = False
            continue
        df = raw[["dt", "close"]].rename(columns={"close": "raw"}).merge(
            qfq[["dt", "close"]].rename(columns={"close": "qfq"}), on="dt")
        df = df[(df["raw"] > 0) & (df["qfq"] > 0)].reset_index(drop=True)
        sep = (df["qfq"].pct_change() - df["raw"].pct_change()).abs().iloc[1:]
        med, p95 = float(sep.median()), float(sep.quantile(0.95))
        n_big = int((sep > EXDIV_HINT).sum())
        usable = med < USABLE_MEDIAN
        all_ok = all_ok and usable
        print(f"{sym:<12}{len(df):>6}{med:>10.6f}{p95:>10.4f}{n_big:>9}"
              f"{'qfq可用' if usable else 'qfq不可用':>8}")
        rows.append({
            "symbol": sym, "rows": len(df), "median_sep": round(med, 8),
            "p95_sep": round(p95, 6), "days_over_0.2pct": n_big,
            "qfq_usable_for_backtest": usable,
        })

    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else
                           ["symbol", "rows", "median_sep", "p95_sep",
                            "days_over_0.2pct", "qfq_usable_for_backtest"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n审计报告: {report_path}")
    print("说明: qfq 不可用标的在回测中建议使用 raw 口径（下单价格口径本就是 raw，"
          "宪法第 5 条兜底成立）；因子级精确校验待 Tushare token 后重跑 verify_sources.py")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
