"""策略管理服务（D12）：CRUD / 启停 / 生命周期门禁。

生命周期门禁（宪法）：无 completed 模拟运行记录 → 实盘启动拒绝（API 层映射 403）。
模拟盘（paper）启动不受门禁限制——先用模拟盘跑出记录再谈实盘。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from quantdesk.core.events import SYSTEM_BUS
from quantdesk.engine.supervisor import StrategySupervisor
from quantdesk.store import repo
from quantdesk.store.db import init_db

log = logging.getLogger(__name__)

__all__ = ["StrategyManager", "StrategyGateError"]

STRATEGIES_ROOT = Path(__file__).resolve().parent.parent.parent / "strategies"


class StrategyGateError(PermissionError):
    """生命周期门禁拦截（映射 API 403）。"""


class StrategyManager:
    """策略文件/登记/启停编排。

    两处与运行时的接线（此前缺失，导致「策略跑了但什么都不发生」）：
    - order_intent 上行 → RiskGate.submit（订单唯一咽喉，宪法第 2 条）；
    - ctx.subscribe 上行 → 实时泵登记扇出（行情下行，宪法 3.5 事件流）。
    """

    def __init__(self, db_path=None, event_bus=None, strategies_root: Path | None = None,
                 gate_check: bool = True, realtime=None, trading=None) -> None:
        self.db_path = db_path
        self.bus = event_bus
        self.root = strategies_root or STRATEGIES_ROOT
        self.gate_check = gate_check
        self.realtime = realtime              # RealtimePump（get_pump() 装配时回填）
        self._trading = trading               # TradingService（惰性取，含 RiskGate）
        self._supervisors: dict[str, StrategySupervisor] = {}
        self._freq: dict[str, str] = {}
        self._lock = threading.RLock()
        init_db(self.db_path) if db_path else init_db()

    @property
    def trading(self):
        if self._trading is None:
            from quantdesk.services.trading import get_trading_service

            self._trading = get_trading_service()
        return self._trading

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
              strategy_id: str | None = None, freq: str = "1d") -> dict:
        """启动策略（子进程 + 守护）。mode=paper|live；live 过门禁。

        freq 决定实时下行聚合口径（默认日线，与回测默认一致）。
        """
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
                self._detach_realtime(sid)
            sup = StrategySupervisor(
                strategy_path=str(path), params=dict(params or {}), strategy_id=sid,
                db_path=self.db_path, event_bus=self.bus, on_intent=self._route_intent,
                on_subscribe=self._on_subscribe)
            sup.start()
            self._supervisors[sid] = sup
            self._freq[sid] = freq
            self.trading.gate.restore_pending()    # 实盘待确认单不因策略重启而丢
            return {"strategy_id": sid, "status": sup.status, "mode": mode, "freq": freq}

    def stop(self, strategy_id: str) -> bool:
        with self._lock:
            sup = self._supervisors.pop(strategy_id, None)
            self._freq.pop(strategy_id, None)
        if sup is None:
            return False
        self._detach_realtime(strategy_id)
        sup.stop()
        sup.shutdown()
        return True

    def supervisor(self, strategy_id: str) -> StrategySupervisor | None:
        """实时泵/回报桥接用（按 id 取守护器投递下行事件）。"""
        with self._lock:
            return self._supervisors.get(strategy_id)

    def running_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._supervisors)

    # ---------- 运行时接线 ----------
    def _on_subscribe(self, strategy_id: str, symbols: list[str]) -> None:
        """策略 ctx.subscribe() 上行 → 实时泵登记扇出（含历史种子回灌）。"""
        if self.realtime is None:
            return
        freq = self._freq.get(strategy_id, "1d")
        try:
            self.realtime.register_strategy(strategy_id, symbols, freq=freq)
        except Exception as e:  # noqa: BLE001 行情源缺失不杀策略进程（状态灯会如实报红）
            log.warning("实时订阅登记失败 %s: %s", strategy_id, e)

    def _detach_realtime(self, strategy_id: str) -> None:
        if self.realtime is not None:
            try:
                self.realtime.unregister_strategy(strategy_id)
            except Exception as e:  # noqa: BLE001 注销失败只影响订阅回收
                log.warning("实时订阅注销失败 %s: %s", strategy_id, e)

    def status(self, strategy_id: str) -> dict | None:
        with self._lock:
            sup = self._supervisors.get(strategy_id)
        if sup is None:
            return None
        return {
            "strategy_id": sup.strategy_id,
            "status": sup.status,
            "subscriptions": sorted(sup.subscriptions),
            "freq": self._freq.get(sup.strategy_id, "1d"),
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
        """上行意图路由：order_intent 必经 RiskGate（订单唯一咽喉），其余广播。

        策略进程拿不到 broker 适配器（宪法第 6 条），它只能发意图；这里是意图
        进入闸门的唯一入口——绕过它直连券商的路径在架构上不存在。
        """
        if tag == "order_intent":
            from quantdesk.core.enums import OrderStatus, OrderType, Side
            from quantdesk.core.models import Order
            from quantdesk.core.timeutil import now_ms

            price = data.get("price")
            try:
                intent = Order(
                    order_id=self.trading.gate.new_order_id(),
                    symbol=str(data["symbol"]), side=Side(str(data["side"])),
                    price=float(price) if price else 0.0,
                    volume=int(data["volume"]),
                    order_type=OrderType(str(data.get("order_type") or "limit")),
                    strategy_id=str(data.get("strategy_id") or "unknown"),
                    status=OrderStatus.PENDING_CONFIRM,
                    created_at=now_ms(), updated_at=now_ms())
            except (KeyError, ValueError, TypeError) as e:
                self._emit_log(f"订单意图非法已丢弃: {e}（{data}）")
                return
            out = self.trading.gate.submit(intent)
            if self.bus is not None:
                from quantdesk.core.events import Event, EventType

                self.bus.publish(Event(EventType.ORDER, data=out, source="strategy"))
            return
        if tag == "subscribe":
            return                                    # 已在 supervisor 侧登记，无需广播
        self._emit_log(str(data.get("message", data)))

    def _emit_log(self, message: str) -> None:
        if self.bus is not None:
            from quantdesk.core.events import Event, EventType

            self.bus.publish(Event(EventType.LOG, data={"message": message},
                                   source="strategy"))


_manager: StrategyManager | None = None
_manager_lock = threading.Lock()


def get_strategy_manager() -> StrategyManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = StrategyManager(event_bus=SYSTEM_BUS)
        return _manager
