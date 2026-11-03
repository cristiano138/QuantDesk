"""D11 基准脚本：IPC 事件往返延迟测量（DoD：本机 P95 < 5ms）。

用法：uv run python scripts/bench_ipc.py [--n 500]
"""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime

import pandas as pd

from quantdesk.core.models import Bar
from quantdesk.engine.runner import bar_to_dict, spawn_strategy_process
from quantdesk.ipc.bridge import BridgeMain

STRATEGY = '''"""IPC 延迟基准策略：收到 bar 立即回显。"""

from quantdesk.engine.strategy_base import BaseStrategy


class BenchStrategy(BaseStrategy):
    name = "bench"
    params = {}

    def on_start(self):
        self.count = 0
        self.ctx.subscribe(["BENCH.SH"])

    def on_bar(self, bar):
        self.count += 1
        self.ctx.log(f"echo:{bar.amount}:{self.count}")
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    args = ap.parse_args()

    from pathlib import Path

    f = Path(__file__).parent / "_bench_strategy.py"
    f.write_text(STRATEGY, encoding="utf-8")
    bridge = BridgeMain()
    proc = spawn_strategy_process(str(f), {}, "bench", bridge.event_url,
                                  bridge.order_url, heartbeat_interval=0)
    try:
        # 等 ready
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            item = bridge.recv_intent(timeout_ms=200)
            if item and item[0] == "ready":
                break
        time.sleep(0.3)

        dts = pd.bdate_range(datetime(2024, 1, 2), periods=args.n)
        rtts = []
        for i, dt in enumerate(dts):
            t0 = time.perf_counter_ns()
            bridge.publish("bar", bar_to_dict(Bar(
                symbol="BENCH.SH", freq="1d", dt=dt.to_pydatetime(),
                open=10, high=10.5, low=9.5, close=10.2, volume=1.0, amount=float(t0))))
            dl = time.monotonic() + 2
            while time.monotonic() < dl:
                item = bridge.recv_intent(timeout_ms=100)
                if item and item[0] == "log" and item[1]["message"].startswith("echo:"):
                    _, amount_s, cnt = item[1]["message"].split(":")
                    if int(cnt) == i + 1:
                        rtts.append((time.perf_counter_ns() - int(float(amount_s))) / 1e6)
                        break
        bridge.publish("control", {"action": "stop"})
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
        bridge.close()
        f.unlink(missing_ok=True)

    rtts.sort()
    n = len(rtts)
    print(f"样本 {n} 往返")
    print(f"  min  = {rtts[0]:.3f} ms")
    print(f"  P50  = {rtts[n // 2]:.3f} ms")
    print(f"  P95  = {rtts[int(n * 0.95)]:.3f} ms")
    print(f"  P99  = {rtts[int(n * 0.99)]:.3f} ms")
    print(f"  max  = {rtts[-1]:.3f} ms")
    print(f"  mean = {statistics.mean(rtts):.3f} ms")
    ok = rtts[int(n * 0.95)] < 5.0
    print("结论:", "通过（P95 < 5ms）" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
