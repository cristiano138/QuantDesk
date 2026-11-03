"""M1 交叉对数：AKShare vs Tushare 前复权日线（D05 验收）。

结论口径（宪法 C6）：
- 非除权日 |close_ak - close_ts| / close_ts ≤ 0.1% → 通过；
- |diff| > 1% 的日期单列为疑似除权日（exdiv_flag=1），除权日差异单列不算失败；
- 输出 data/logs/verify_sources_<ts>.csv + 控制台结论。

token 未配置 → 打印结构化引导并以退出码 2 结束（DoD 如实标注未通过，不虚报）。
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime

import pandas as pd

from quantdesk.adapters.data.akshare_feed import AkshareFeed
from quantdesk.adapters.data.tushare_feed import TushareFeed
from quantdesk.config import data_dir
from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.errors import AdapterError, ConfigError


def main() -> int:
    ap = argparse.ArgumentParser(description="M1 交叉对数（AKShare vs Tushare）")
    ap.add_argument("--symbols", default="600000.SH,000001.SZ,000858.SZ")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    ak = AkshareFeed(min_interval=1.0)
    try:
        ak.connect()
    except AdapterError as e:
        print(f"[未通过] AKShare 不可用: {e}")
        return 1

    ts_feed = TushareFeed()
    try:
        ts_feed.connect()
    except ConfigError as e:
        print(f"[未通过] {e}")
        print("补跑：配置 token 后重跑 scripts/verify_sources.py 即可，无需改代码。")
        return 2
    except AdapterError as e:
        print(f"[未通过] Tushare 连接失败: {e}")
        return 2

    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / f"verify_sources_{datetime.now():%Y%m%d_%H%M%S}.csv"

    verdicts: list[tuple[str, bool, float, int]] = []
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "dt", "ak_close", "tushare_close", "diff_pct", "exdiv_flag"])
        for sym in symbols:
            try:
                fa = ak.get_history(sym, Frequency.D1, args.start, args.end, AdjustType.FORWARD)
                ft = ts_feed.get_history(sym, Frequency.D1, args.start, args.end, AdjustType.FORWARD)
            except (AdapterError, ConfigError) as e:
                print(f"{sym}: 拉取失败 {e}")
                verdicts.append((sym, False, float("inf"), 0))
                continue
            if fa.empty or ft.empty:
                print(f"{sym}: 某源无数据（ak={len(fa)} 行, tushare={len(ft)} 行）")
                verdicts.append((sym, False, float("inf"), 0))
                continue
            m = fa[["dt", "close"]].merge(ft[["dt", "close"]], on="dt",
                                          suffixes=("_ak", "_ts"))
            m["diff_pct"] = (m["close_ak"] - m["close_ts"]).abs() / m["close_ts"] * 100
            m["exdiv_flag"] = (m["diff_pct"] > 1.0).astype(int)
            for r in m.itertuples(index=False):
                w.writerow([sym, f"{r.dt:%Y%m%d}", r.close_ak, r.close_ts,
                            round(r.diff_pct, 6), r.exdiv_flag])
            normal = m[m["exdiv_flag"] == 0]
            exdiv_n = int(m["exdiv_flag"].sum())
            mx = float(normal["diff_pct"].max()) if len(normal) else 0.0
            ok = mx <= 0.1
            verdicts.append((sym, ok, mx, exdiv_n))
            print(f"{sym}: 非除权日最大差异 {mx:.4f}%（阈值 0.1%），"
                  f"疑似除权日 {exdiv_n} 天（单列）→ {'通过' if ok else '未通过'}")

    print(f"对数报告: {out}")
    all_ok = len(verdicts) == len(symbols) and all(v[1] for v in verdicts)
    print("M1 交叉对数结论:", "通过" if all_ok else "未通过")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
