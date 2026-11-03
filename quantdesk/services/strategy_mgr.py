"""策略管理服务（D12）：CRUD / 启停 / 生命周期门禁。

生命周期门禁（宪法）：无 completed 模拟运行记录 → 实盘启动拒绝（API 层映射 403）。
模拟盘（paper）启动不受门禁限制——先用模拟盘跑出记录再谈实盘。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from quantdesk.core.events import SYSTEM_BUS
from quantdesk.engine.supervisor import StrategySupervisor
from quantdesk.store import repo
from quantdesk.store.db import init_db

__all__ = ["StrategyManager", "StrategyGateError"]

STRATEGIES_ROOT = Path(__file__).resolve().parent.parent.parent / "strategies"


class StrategyGateError(PermissionError):
    """生命周期门禁拦截（映射 API 403）。"""


class StrategyManager:
    """策略文件/登记/启停编排。"""

    def __init__(self, db_path=None, event_bus=None, strategies_root: Path | None = None,
                 gate_check: bool = True) -> None:
        self.db_path = db_path
        self.bus = event_bus
        self.root = strategies_root or STRATEGIES_ROOT
        self.gate_check = gate_check
        self._supervisors: dict[str, StrategySupervisor] = {}
        self._lock = threading.RLock()
        init_db(self.db_path) if db_path else init_db()

    # ---------- CRUD ----------
    def list_files(self) -> list[str]:
        """strategies/ 下全部策略文件（相对路径，/ 分隔）。"""
        if not self.root.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*.py")
            if p.name != "__init__.py" and not p.name.startswith("_")
        )

    def read_file(self, rel_path: str) -> str:
        p = self._resolve(rel_path)
        return p.read_text(encoding="utf-8")

    def write_file(self, rel_path: str, code: str) -> str:
        """保存策略代码（新建或覆盖）；返回规范化相对路径。"""
        if not rel_path.endswith(".py") or rel_path.startswith("_"):
            raise ValueError("策略文件必须以 .py 结尾且不以下划线开头")
        p = self._resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")
        return p.relative_to(self.root).as_posix()

    def _resolve(self, rel_path: str) -> Path:
        p = (self.root / rel_path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"路径越界: {rel_path}")
        return p

    # ---------- 启停 ----------
    def start(self, rel_path: str, mode: str = "paper", params: dict | None = None,
              strategy_id: str | None = None) -> dict:
        """启动策略（子进程 + 守护）。mode=paper|live；live 过门禁。"""
        if mode not in ("paper", "live"):
            raise ValueError(f"未知运行模式: {mode}")
        path = self._resolve(rel_path)
        if not path.exists():
            raise FileNotFoundError(f"策略文件不存在: {rel_path}")
        if mode == "live":
            self.gate(rel_path)                    # 生命周期门禁（无记录 → 拒绝）
        sid = strategy_id or f"{Path(rel_path).stem}"
        with self._lock:
            old = self._supervisors.get(sid)
            if old is not None and old.status in ("running", "restarting"):
                return {"strategy_id": sid, "status": old.status, "already": True}
            old = self._supervisors.get(sid)
            if old is not None:
                old.shutdown()
            sup = StrategySupervisor(
                strategy_path=str(path), params=dict(params or {}), strategy_id=sid,
                db_path=self.db_path, event_bus=self.bus, on_intent=self._route_intent)
            sup.start()
            self._supervisors[sid] = sup
            return {"strategy_id": sid, "status": sup.status, "mode": mode}

    def stop(self, strategy_id: str) -> bool:
        with self._lock:
            sup = self._supervisors.pop(strategy_id, None)
        if sup is None:
            return False
        sup.stop()
        sup.shutdown()
        return True

    def status(self, strategy_id: str) -> dict | None:
        with self._lock:
            sup = self._supervisors.get(strategy_id)
        if sup is None:
            return None
        return {
            "strategy_id": sup.strategy_id,
            "status": sup.status,
            "last_heartbeat_age_s": round(max(time.monotonic() - sup.last_heartbeat, 0), 2)
            if sup.last_heartbeat else None,
            "restarts_last_hour": len([t for t in sup.restarts
                                       if t > time.monotonic() - 3600]),
            "recent_logs": list(sup.last_logs),
        }

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._supervisors)
        for sid in ids:
            self.stop(sid)

    # ---------- 生命周期门禁 ----------
    def gate(self, rel_path_or_name: str) -> None:
        """无 completed 模拟运行记录 → 抛 StrategyGateError（实盘启动 403）。"""
        if not self.gate_check:
            return
        name = Path(rel_path_or_name).stem
        row = self._has_completed_paper_run(name)
        if not row:
            raise StrategyGateError(
                f"策略 {name} 无 completed 模拟/回测运行记录——按生命周期门禁禁止直接实盘；"
                f"请先完成回测或模拟盘运行")

    def _has_completed_paper_run(self, strategy_name: str) -> bool:
        import sqlmodel

        from quantdesk.store.models_db import BacktestRunRow

        with repo.get_session(self.db_path) as s:
            row = s.exec(
                sqlmodel.select(BacktestRunRow)
                .where(BacktestRunRow.strategy_name == strategy_name)
                .where(BacktestRunRow.status == "completed")
            ).first()
            return row is not None

    def _route_intent(self, tag: str, data: dict) -> None:
        """上行意图路由（订单路由到 RiskGate 在 D16 接线；本日记录+广播）。"""
        if self.bus is not None:
            from quantdesk.core.events import Event, EventType

            self.bus.publish(Event(EventType.ORDER if tag == "order_intent" else EventType.LOG,
                                   data=data, source="strategy"))


_manager: StrategyManager | None = None
_manager_lock = threading.Lock()


def get_strategy_manager() -> StrategyManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = StrategyManager(event_bus=SYSTEM_BUS)
        return _manager
