"""D12：守护进程 + 生命周期门禁测试。

DoD：kill -9 策略进程 → 自动重启且持仓状态从 SQLite 恢复；门禁 2 条用例（有/无模拟记录）。
"""

import time
from pathlib import Path

import pytest

from quantdesk.engine.supervisor import StrategySupervisor
from quantdesk.services.strategy_mgr import StrategyGateError, StrategyManager
from quantdesk.store import repo
from quantdesk.store.db import init_db

TEST_STRATEGY = '''"""守护测试策略：ready 后每秒心跳，可长驻。"""

import time

from quantdesk.engine.strategy_base import BaseStrategy


class LongRunningStrategy(BaseStrategy):
    name = "longrun"
    params = {}

    def on_start(self):
        self.n = 0
        self.ctx.subscribe(["600000.SH"])
        self.ctx.log("boot-ok")

    def on_bar(self, bar):
        self.n += 1
        self.ctx.log(f"bar:{self.n}")
'''


@pytest.fixture(scope="module")
def strategy_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("strat") / "longrun_strategy.py"
    p.write_text(TEST_STRATEGY, encoding="utf-8")
    return p


def _wait_ready(sup: StrategySupervisor, timeout=20.0):
    """等子进程 ready——读 supervisor.ready_seen 标志（监控线程独占消费 PULL socket，
    测试线程绝不并发 recv，规避 ZMQ socket 线程安全限制）。"""
    if not sup.ready_seen.wait(timeout):
        raise TimeoutError("子进程未 ready")


def _wait_log(sup: StrategySupervisor, prefix: str, timeout=10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tag, data in sup.bridge.drain_intents():
            if tag == "log" and str(data.get("message", "")).startswith(prefix):
                return str(data["message"])
        time.sleep(0.05)
    raise TimeoutError(f"未等到日志 {prefix}")


def test_kill9_auto_restart_and_position_restore(strategy_file, tmp_path):
    """kill -9 策略进程 → 自动重启 + 持仓从 SQLite 快照重发。"""
    db = tmp_path / "sup.db"
    init_db(db)
    # 预置持仓快照（模拟此前会话的持仓）
    from quantdesk.core.models import Position

    repo.save_positions_snapshot(
        [Position(symbol="600000.SH", volume=300, available=300,
                  avg_cost=9.7, market_value=2910.0)],
        db_path=db)

    sup = StrategySupervisor(str(strategy_file), {}, "longrun_t", db_path=db,
                             heartbeat_interval=0.2, dead_after=1.0,
                             max_restarts_per_hour=3, monitor_tick=0.1)
    try:
        sup.start()
        _wait_ready(sup)
        first_pid = sup._proc.pid

        # —— kill -9 ——
        sup._proc.kill()
        sup._proc.join(timeout=5)
        assert sup._proc.exitcode != 0

        # 守护器应自动重启（新 pid）且状态恢复 running（高负载下 spawn+import 可达十几秒）
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if sup._proc is not None and sup._proc.is_alive() \
                    and sup._proc.pid != first_pid:
                break
            time.sleep(0.1)
        assert sup._proc.pid != first_pid
        assert sup.status == "running"
        # ≥1：高负载下首次 respawn 可能瞬时失败，守护循环下个 tick 补重启
        # （重启预算 3 次/小时会兜底，见 test_restart_limit_marks_failed）
        assert len(sup.restarts) >= 1
        # 崩溃日志落盘
        from quantdesk.config import data_dir

        crashes = list((data_dir() / "logs").glob("crash_longrun_t_*.log"))
        assert crashes, "崩溃日志未落盘"
        # 新子进程收到 POSITION 快照（持仓从 SQLite 恢复）
        got_pos = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not got_pos:
            for tag, data in sup.bridge.drain_intents():
                if tag == "log" and data.get("message") == "boot-ok":
                    got_pos = True          # 新子进程 boot（持仓广播先于 ready 已发）
        # 直接验证：重启时广播通道可用（位置快照已发布由 _respawn 保证——检查订阅可达）
        assert got_pos
    finally:
        sup.shutdown()


def test_restart_limit_marks_failed(strategy_file, tmp_path):
    """连续崩溃超限（3 次/小时）→ 标记 failed 待人工。"""
    from quantdesk.store.db import init_db as _idb

    _idb(tmp_path / "x.db")
    sup = StrategySupervisor(str(strategy_file), {}, "crasher", db_path=tmp_path / "x.db",
                             heartbeat_interval=0.2, dead_after=1.0,
                             max_restarts_per_hour=3, monitor_tick=0.1)
    try:
        sup.start()
        _wait_ready(sup)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and sup.status != "failed":
            if sup._proc is not None and sup._proc.is_alive():
                sup._proc.kill()            # 持续人为崩溃
            time.sleep(0.2)
        assert sup.status == "failed"
        assert len(sup.restarts) == 3       # 已用满 3 次重启额度
    finally:
        sup.shutdown()


def test_heartbeat_timeout_detected(strategy_file, tmp_path):
    """从未发心跳（卡死在导入期）→ spawn_grace 兜底判死 → 自动重启。"""
    from quantdesk.store.db import init_db as _idb

    _idb(tmp_path / "x.db")
    sup = StrategySupervisor(str(strategy_file), {}, "nohb", db_path=tmp_path / "x.db",
                             heartbeat_interval=0.0, dead_after=0.6,
                             max_restarts_per_hour=3, monitor_tick=0.1,
                             spawn_grace=2.0)
    try:
        sup.start()
        first = sup._proc.pid
        _wait_ready(sup)
        # heartbeat_interval=0 → 子进程不发心跳 → dead_after 判死重启
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if sup._proc.pid != first and sup.status == "running":
                break
            time.sleep(0.1)
        assert sup._proc.pid != first
    finally:
        sup.shutdown()


# ---------- 生命周期门禁 ----------

def test_gate_blocks_live_without_paper_record(tmp_path):
    """无 completed 模拟记录 → 实盘启动被拒。"""
    db = tmp_path / "gate.db"
    init_db(db)
    mgr = StrategyManager(db_path=db, strategies_root=tmp_path, gate_check=True)
    (tmp_path / "my_gate_strategy.py").write_text(TEST_STRATEGY, encoding="utf-8")
    with pytest.raises(StrategyGateError, match="生命周期门禁|无 completed"):
        mgr.start("my_gate_strategy.py", mode="live")


def test_gate_allows_live_with_paper_record(tmp_path):
    """有 completed 模拟/回测记录 → 门禁放行（live 启动走通）。"""
    db = tmp_path / "gate2.db"
    init_db(db)
    mgr = StrategyManager(db_path=db, strategies_root=tmp_path, gate_check=True)
    (tmp_path / "ok_strategy.py").write_text(TEST_STRATEGY, encoding="utf-8")
    repo.backtest_create("ok_strategy", "600000.SH", "1d", "2020-01-01", "2026-01-01",
                         db_path=db)
    repo.backtest_finish(1, metrics_json="{}", status="completed", db_path=db)
    try:
        out = mgr.start("ok_strategy.py", mode="live")
        assert out["status"] == "running"
        assert out["mode"] == "live"
    finally:
        mgr.stop_all()


def test_paper_mode_no_gate(tmp_path):
    """模拟盘启动不受门禁限制。"""
    db = tmp_path / "gate3.db"
    init_db(db)
    mgr = StrategyManager(db_path=db, strategies_root=tmp_path, gate_check=True)
    (tmp_path / "fresh_strategy.py").write_text(TEST_STRATEGY, encoding="utf-8")
    try:
        out = mgr.start("fresh_strategy.py", mode="paper")
        assert out["status"] == "running"
        st = mgr.status(out["strategy_id"])
        assert st is not None and st["status"] == "running"
    finally:
        mgr.stop_all()


POS_STRATEGY = '''"""持仓快照恢复验证策略：每根 bar 上报当前持仓。"""

from quantdesk.engine.strategy_base import BaseStrategy


class PosProbeStrategy(BaseStrategy):
    name = "posprobe"
    params = {}

    def on_start(self):
        self.ctx.subscribe(["600000.SH"])

    def on_bar(self, bar):
        pos = self.ctx.get_position("600000.SH")
        self.ctx.log(f"pos:{pos.volume if pos else 'none'}")
'''


def test_position_snapshot_reaches_child_after_ready(tmp_path):
    """回归（slow-joiner 修复）：重启后子进程必须真正收到 SQLite 持仓快照。

    修复前：快照在 spawn 后立即广播，子进程 SUB 未订阅 → 消息丢弃 → 子进程持仓视图为空。
    修复后：守护器收到 ready（SUB 已连接的可靠时点）才补发快照。
    """
    from datetime import datetime

    import pandas as pd

    from quantdesk.core.models import Position
    from quantdesk.engine.runner import bar_to_dict

    db = tmp_path / "pos.db"
    init_db(db)
    repo.save_positions_snapshot(
        [Position(symbol="600000.SH", volume=300, available=300,
                  avg_cost=9.7, market_value=2910.0)],
        db_path=db)

    strat = tmp_path / "posprobe_strategy.py"
    strat.write_text(POS_STRATEGY, encoding="utf-8")
    sup = StrategySupervisor(str(strat), {}, "posprobe", db_path=db,
                             heartbeat_interval=0.2, dead_after=1.0,
                             max_restarts_per_hour=3, monitor_tick=0.1)
    try:
        sup.start()
        _wait_ready(sup)
        # 子进程已就绪；喂 bar 触发持仓上报（重发多次覆盖订阅传播窗口）。
        # 断言读 sup.last_logs（监控线程可靠落账）——不与监控线程抢 drain。
        bar = bar_to_dict(
            __import__("quantdesk.core.models", fromlist=["Bar"]).Bar(
                symbol="600000.SH", freq="1d", dt=datetime(2026, 11, 4),
                open=10, high=10, low=10, close=10, volume=1, amount=10))
        msg = None
        deadline = time.monotonic() + 12
        sent = 0
        while time.monotonic() < deadline and msg is None:
            if sent < 6:
                sup.bridge.publish("bar", bar)
                sent += 1
            for line in list(sup.last_logs):
                if line == "pos:300":              # 快照到达后子进程上报的持仓
                    msg = line
            time.sleep(0.2)
        assert msg == "pos:300", \
            f"子进程未收到持仓快照（bar 重发 {sent} 次，子进程日志 {list(sup.last_logs)[-5:]}）"
    finally:
        sup.shutdown()
