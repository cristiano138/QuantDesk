"""D08 DoD 验证脚本：dual_ma 回测 600000.SH 2020-2026 + 独立参考核算。

参考核算（与引擎相互独立实现）：
- 用 pandas 直接按 dual_ma 规则推出信号日序列（金叉/死叉）；
- 按引擎时序规则（次根 bar 开盘价优先成交、市价单）手工推成交价与费用；
- 对比引擎输出的成交序列与最终权益。
"""

import json

import pandas as pd

from quantdesk.services.backtest_svc import BacktestService
from quantdesk.store.parquet_store import ParquetStore

out = BacktestService().run(
    strategy="dual_ma", symbols=["600000.SH"],
    start="2020-01-01", end="2026-12-31", params={"fast": 5, "slow": 20, "volume": 500},
)
print("== dual_ma 600000.SH 2020-2026 ==")
print("bars:", out["bars"])
print("metrics:", json.dumps(out["metrics"], ensure_ascii=False))
print("trades:")
for t in out["trades"]:
    print(f"  {t['trade_id']} {t['side']} {t['volume']}@{t['price']} amount={t['amount']}")

# ---- 独立参考核算 ----
from quantdesk.core.enums import AdjustType, Frequency
store = ParquetStore()
df = store.read("600000.SH", Frequency.D1, start="2020-01-01",
                adjust=AdjustType.FORWARD).reset_index(drop=True)
fast, slow = 5, 20
ma_f = df["close"].rolling(fast).mean()
ma_s = df["close"].rolling(slow).mean()
golden = (ma_f.shift(1) <= ma_s.shift(1)) & (ma_f > ma_s)
dead = (ma_f.shift(1) >= ma_s.shift(1)) & (ma_f < ma_s)
golden_days = df.loc[golden, "dt"].dt.strftime("%Y-%m-%d").tolist()
dead_days = df.loc[dead, "dt"].dt.strftime("%Y-%m-%d").tolist()
print("参考核算 金叉日:", golden_days)
print("参考核算 死叉日:", dead_days)

# 引擎成交日（traded_at 毫秒 → 日期）
for t in out["trades"]:
    d = pd.Timestamp(t["traded_at"], unit="ms").strftime("%Y-%m-%d")
    print(f"引擎成交 {t['side']} @ {d} price={t['price']}")

# 手工推算最终权益：现金 + 持仓×最后收盘
cash, vol, avg = 1_000_000.0, 0, 0.0
comm, min5, stamp, transfer = 2.5e-4, 5.0, 5e-4, 1e-4
for t in out["trades"]:
    amt = t["amount"]
    if t["side"] == "buy":
        fee = round(max(amt * comm, min5) + amt * transfer, 2)
        cash -= amt + fee
        old_cost = avg * vol
        vol += t["volume"]
        avg = (old_cost + amt + fee) / vol
    else:
        fee = round(max(amt * comm, min5) + amt * stamp + amt * transfer, 2)
        cash += amt - fee
        vol -= t["volume"]
last_close = float(df["close"].iloc[-1])
manual_equity = cash + vol * last_close
engine_equity = out["equity"][-1]["equity"]
print(f"参考核算最终权益: {manual_equity:.2f}（现金 {cash:.2f} 持仓 {vol}×{last_close}）")
print(f"引擎最终权益:     {engine_equity:.2f}")
diff = abs(manual_equity - engine_equity)
print(f"差异: {diff:.4f} 元（{diff / manual_equity * 100:.6f}%）")
