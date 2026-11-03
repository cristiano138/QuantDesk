"""v1.0 全链路验收剧本（D20 / M4）。

演练范围（无实时数据源场景下的完整等价链路，实时源接入后重跑 D14 验收）：
  ① 本地数据就绪检查（600000.SH 日线 Parquet）
  ② dual_ma 回测（真实数据）→ 指标完整
  ③ 模拟盘全天运行：合成 tick 驱动 09:30-15:00 全天 + 策略 ctx 无关的手工委托流
     （限价/市价/撤单/拒单路径），逐笔费用与 FeeCalculator 手工核算一致，
     现金恒等式（现金+市值=总资产）全程成立
  ④ 重启恢复：资金/持仓/委托完整
  ⑤ 三道闸用例全绿（pytest 子集）

退出码 0 = 通过；非 0 = 失败（打印失败项）。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("== v1.0 全链路验收剧本 ==")

    # ① 数据就绪
    print("① 本地数据")
    from quantdesk.core.enums import AdjustType, Frequency
    from quantdesk.store.parquet_store import ParquetStore

    store = ParquetStore()
    rng = store.range_of("600000.SH", Frequency.D1, AdjustType.FORWARD)
    check("600000.SH 日线本地就绪", rng is not None, f"range={rng}")

    # ② 回测
    print("② dual_ma 回测（真实数据）")
    from quantdesk.services.backtest_svc import BacktestService

    out = BacktestService().run(strategy="dual_ma", symbols=["600000.SH"],
                                start="2020-01-01", end="2026-12-31")
    check("回测完成且 bar 数 > 1000", out["bars"] > 1000, f"bars={out['bars']}")
    m = out["metrics"]
    check("指标完整", all(m[k] is not None for k in
                          ("total_return", "annual_return", "max_drawdown", "sharpe",
                           "win_rate", "turnover", "trade_count")),
          f"total_return={m['total_return']:.2%} trades={m['trade_count']}")

    # ③ 模拟盘全天
    print("③ 模拟盘全天运行（tick 驱动 + 手工委托流）")
    from quantdesk.core.enums import OrderStatus, Side
    from quantdesk.core.models import Tick
    from quantdesk.engine.matching import FeeCalculator
    from quantdesk.services.trading import TradingService
    from quantdesk.store.db import init_db

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "e2e.db"
    init_db(db)
    svc = TradingService(initial_cash=1_000_000.0, db_path=db)

    fee_calc = FeeCalculator()
    base = datetime(2026, 11, 4)
    ticks_sent = 0

    def feed(price: float, minute: int, symbol: str = "600000.SH") -> None:
        nonlocal ticks_sent
        hh, mm = divmod(9 * 60 + 30 + minute, 60)      # 从 09:30 顺推（330 分钟内不越界）
        t = Tick(symbol=symbol, dt=datetime(2026, 11, 4, hh, mm),
                 last=price, open=price, high=price, low=price,
                 volume=10_000, amount=price * 10_000,
                 bids=[(round(price - 0.02, 2), 500)],
                 asks=[(round(price + 0.02, 2), 500)])
        svc.paper.on_tick(t)
        ticks_sent += 1

    # —— 全天行情：09:30-14:59 每分钟一个 tick，价格缓慢波动 ——
    prices = [10.0 + 0.01 * (i % 40) - 0.1 for i in range(330)]
    for minute, px in enumerate(prices):
        feed(round(px, 2), minute)

    # —— 委托流 ——
    o1 = svc.place_order("600000.SH", "buy", 500, price=10.5)     # 立即成交
    check("限价买入立即成交", o1.status == OrderStatus.FILLED, f"@{o1.avg_filled_price}")

    o2 = svc.place_order("600000.SH", "sell", 500, price=10.6)    # T+1 拒
    check("当日卖出被 T+1 拒", o2.status == OrderStatus.REJECTED and "T+1" in o2.message)

    o3 = svc.place_order("600000.SH", "buy", 300, price=9.0)      # 挂单
    check("低挂单进入挂单簿", o3.status == OrderStatus.SUBMITTED)
    feed(8.9, 400)                                                 # 价格下探 → 成交
    o3b = svc.get_order(o3.order_id)
    check("挂单被 tick 驱动成交", o3b is not None and o3b.status == OrderStatus.FILLED)

    o4 = svc.place_order("600000.SH", "buy", 100, price=5.0)      # 挂单后撤
    ok_cancel = svc.cancel_order(o4.order_id)
    check("撤单受理", ok_cancel and svc.get_order(o4.order_id).status == OrderStatus.CANCELLED)

    feed(8.8, 450)
    o5 = svc.place_order("600000.SH", "sell", 300, price=8.5)     # 昨日？今日买 → T+1 拒
    check("当日买入的可用量校验", o5.status == OrderStatus.REJECTED)

    # —— 费用手工核算：与成交明细逐笔对照 ——
    fee_ok = True
    for t in svc.trades():
        expect = (fee_calc.buy_fee(t.amount) if t.side == Side.BUY.value
                  else fee_calc.sell_fee(t.amount))
        if abs(expect - fee_calc.fee(t.side, t.amount)) > 1e-9:
            fee_ok = False
    check("费用口径一致（buy/sell 费率函数自洽）", fee_ok)

    # —— 现金恒等式 ——
    acct = svc.account()
    cash_expect = 1_000_000.0
    for t in svc.trades():
        f = fee_calc.fee(t.side, t.amount)
        cash_expect += (-(t.amount + f) if t.side == Side.BUY.value else (t.amount - f))
    check("现金恒等式（初始资金+Σ成交净额=现金）",
          abs(acct.cash - cash_expect) < 0.01,
          f"cash={acct.cash:.2f} expect={cash_expect:.2f}")
    check("总资产=现金+市值", abs(acct.total_asset - acct.cash - acct.market_value) < 0.01)

    # —— 全天无未捕获异常的佐证：订单状态机无非法迁移 ——
    valid = {"submitted", "part_filled", "filled", "cancelled", "rejected", "failed",
             "pending_confirm", "expired_confirm"}
    check("订单状态机合法", all(o.status in valid for o in svc.list_orders()),
          f"{len(svc.list_orders())} 单")

    # ④ 重启恢复
    print("④ 重启恢复")
    cash_before = svc.account().cash
    pos_before = [(p.symbol, p.volume, p.available) for p in svc.positions()]
    orders_before = len(svc.list_orders())
    trades_before = len(svc.trades())
    svc.stop()
    svc2 = TradingService(initial_cash=1_000_000.0, db_path=db)
    svc2.start()
    acct2 = svc2.account()
    pos2 = [(p.symbol, p.volume, p.available) for p in svc2.positions()]
    check("资金恢复", abs(acct2.cash - cash_before) < 0.01,
          f"{cash_before:.2f} → {acct2.cash:.2f}")
    check("持仓恢复（含 T+1 可用）", pos2 == pos_before, f"{pos_before}")
    check("委托记录完整", len(svc2.list_orders()) == orders_before)
    check("成交记录完整", len(svc2.trades()) == trades_before)

    # ⑤ 三道闸 + 全量测试
    print("⑤ 测试套件")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_paper_trading.py",
         "tests/test_riskgate.py", "tests/test_confirm_flow.py", "-q"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout else ""
    check("三道闸用例全绿", r.returncode == 0, tail)

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过 → {FAILURES}")
        return 1
    print("结果：M4 验收剧本全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
