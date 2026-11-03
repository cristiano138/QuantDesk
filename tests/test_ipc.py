"""D11：ZMQ IPC 桥 + 策略子进程集成测试。

DoD：子进程收到主进程 bar 事件并回调 on_bar；本机往返延迟 P95 < 5ms。
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from quantdesk.core.models import Bar
from quantdesk.engine.runner import bar_to_dict, spawn_strategy_process
from quantdesk.ipc.bridge import BridgeMain

TEST_STRATEGY = '''"""IPC 测试策略：数 bar、回显延迟、第 5 根 bar 买入。"""

from quantdesk.engine.strategy_base import BaseStrategy


class IpcEchoStrategy(BaseStrategy):
    name = "ipc_echo"
    params = {"total": 100, "buy_at": 5}

    def on_start(self):
        self.count = 0
        self.ctx.subscribe(["600000.SH"])

    def on_bar(self, bar):
        self.count += 1
        # 回显：用于主进程测往返延迟（bar.amount 携带主进程发送时刻）
        self.ctx.log(f"echo:{bar.amount}:{self.count}")
        if self.count == int(self.params["buy_at"]):
            self.ctx.buy("600000.SH", 100, price=10.0)
        if self.count >= int(self.params["total"]):
            self.ctx.log("done")
'''


@pytest.fixture(scope="module")
def strategy_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("strat") / "ipc_echo_strategy.py"
    p.write_text(TEST_STRATEGY, encoding="utf-8")
    return p


def _wait_intent(bridge, tag, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = bridge.recv_intent(timeout_ms=200)
        if item and item[0] == tag:
            return item[1]
    raise TimeoutError(f"等待 {tag} 超时")


def test_child_receives_bars_and_submits_intent(strategy_file):
    """集成：子进程收到 bar → on_bar 回调 → buy 意图上行 → 主进程收到。"""
    bridge = BridgeMain()
    try:
        proc = spawn_strategy_process(str(strategy_file), {"total": 100, "buy_at": 5},
                                      "ipc_echo_1", bridge.event_url, bridge.order_url,
                                      heartbeat_interval=0.5)
        try:
            # 等子进程 ready（SUB 连接已建立）
            ready = _wait_intent(bridge, "ready")
            assert ready["strategy_id"] == "ipc_echo_1"
            time.sleep(0.3)                      # SUB 订阅传播

            # 发布 100 根 bar（dt 单调递增；amount 编码发送时刻 ns 用于延迟测量）
            base = datetime(2024, 1, 2)
            import pandas as pd

            dts = pd.bdate_range(base, periods=100)
            sent_ns = time.perf_counter_ns()
            for i, dt in enumerate(dts):
                bridge.publish("bar", bar_to_dict(Bar(
                    symbol="600000.SH", freq="1d", dt=dt.to_pydatetime(),
                    open=10 + i * 0.01, high=10.5 + i * 0.01, low=9.5 + i * 0.01,
                    close=10.2 + i * 0.01, volume=1000.0, amount=float(sent_ns + i))))
                if i % 20 == 0:
                    time.sleep(0.01)             # 让子进程喘口气，防止队列积压失序

            # 应收到第 5 根 bar 触发的 buy 意图
            intent = _wait_intent(bridge, "order_intent")
            assert intent["symbol"] == "600000.SH"
            assert intent["side"] == "buy" and intent["volume"] == 100
            assert intent["price"] == 10.0

            # 策略数满 100 根 → 发 done 日志（证明无事件丢失）；先排空途中的 echo 日志
            deadline = time.monotonic() + 15
            done = None
            while time.monotonic() < deadline:
                item = bridge.recv_intent(timeout_ms=200)
                if item and item[0] == "log":
                    if item[1]["message"] == "done":
                        done = item[1]
                        break
            assert done is not None, "未收到 done（事件丢失或子进程卡死）"
        finally:
            bridge.publish("control", {"action": "stop"})
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
            assert proc.exitcode == 0            # 优雅退出（收到 stop 指令）
    finally:
        bridge.close()


def test_round_trip_latency_p95(strategy_file):
    """往返延迟 P95 < 5ms（本机回环）。"""
    bridge = BridgeMain()
    try:
        proc = spawn_strategy_process(str(strategy_file), {"total": 10 ** 9, "buy_at": -1},
                                      "ipc_echo_2", bridge.event_url, bridge.order_url,
                                      heartbeat_interval=0)
        try:
            _wait_intent(bridge, "ready")
            time.sleep(0.3)
            import pandas as pd

            dts = pd.bdate_range(datetime(2024, 1, 2), periods=500)
            rtts = []
            sent = 0
            for i, dt in enumerate(dts):
                t0 = time.perf_counter_ns()
                bridge.publish("bar", bar_to_dict(Bar(
                    symbol="600000.SH", freq="1d", dt=dt.to_pydatetime(),
                    open=10, high=10.5, low=9.5, close=10.2, volume=1.0,
                    amount=float(t0))))
                # 收对应 echo（log: echo:<amount>:<count>）
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    item = bridge.recv_intent(timeout_ms=100)
                    if item and item[0] == "log" and item[1]["message"].startswith("echo:"):
                        _, amount_s, cnt = item[1]["message"].split(":")
                        if int(cnt) == i + 1:
                            rtts.append((time.perf_counter_ns() - int(float(amount_s))) / 1e6)
                            sent += 1
                            break
                else:
                    pytest.fail(f"第 {i + 1} 根 bar 回显超时")
            assert sent == 500
            rtts.sort()
            p95 = rtts[int(len(rtts) * 0.95)]
            assert p95 < 5.0, f"P95={p95:.3f}ms 超标（分布头尾: {rtts[0]:.3f}/{rtts[-1]:.3f}）"
        finally:
            bridge.publish("control", {"action": "stop"})
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
    finally:
        bridge.close()


def test_heartbeat_flows(strategy_file):
    """心跳信道：子进程定期上行 heartbeat。"""
    bridge = BridgeMain()
    try:
        proc = spawn_strategy_process(str(strategy_file), {"total": 10 ** 9, "buy_at": -1},
                                      "ipc_hb", bridge.event_url, bridge.order_url,
                                      heartbeat_interval=0.2)
        try:
            _wait_intent(bridge, "ready")
            beats = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(beats) < 2:
                item = bridge.recv_intent(timeout_ms=200)
                if item and item[0] == "heartbeat":
                    beats.append(item[1])
            assert len(beats) >= 2
            assert beats[0]["strategy_id"] == "ipc_hb"
            assert "session" in beats[0] and "ts" in beats[0]
        finally:
            bridge.publish("control", {"action": "stop"})
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
    finally:
        bridge.close()
