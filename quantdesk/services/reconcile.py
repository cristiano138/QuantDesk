"""对账服务（宪法：先落库后广播 + 差异即事故）。

实盘通道（miniQMT / 掘金 / easytrader）的回报是「尽力而为」的：回调丢一帧、
程序在回报到达前被杀、GUI 自动化轮询窗口错开——都会让本地库和券商真实状态分叉。
分叉本身不可怕，可怕的是**没人知道**。所以本服务按固定周期（默认 30s，可配）
拿通道全量快照与本地 SQLite 逐项比对，差异落库 + 广播 RISK 事件 + 前端红条，
宁可误报也不漏报（D22 口径）。

模拟盘同样跑对账：PaperBroker 的内存簿必须与库一致，这条自检能在重启恢复类
缺陷第一次出现时就暴露，而不是等到实盘。
"""

from __future__ import annotations

import logging
import threading

from quantdesk.core.enums import OrderStatus, Side
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS
from quantdesk.core.models import Account
from quantdesk.core.timeutil import now_ms
from quantdesk.store import repo

log = logging.getLogger(__name__)

__all__ = ["ReconcileService", "KINDS"]

# 差异类型（前端按 kind 分组展示，运维按 kind 决定处置动作）
KINDS = {
    "order_missing_local": "券商有委托、本地无记录（回报丢失或落库失败）",
    "order_state_mismatch": "本地委托状态与券商不一致（状态机漂移）",
    "trade_missing_local": "券商有成交、本地无成交记录（资金/持仓必然算错）",
    "position_mismatch": "持仓数量或可用量与券商不一致（T+1 语义或漏成交）",
    "account_mismatch": "资金与券商不一致（超容差）",
}

_FILLED_STATES = (OrderStatus.FILLED, OrderStatus.PART_FILLED)
_TERMINAL_STATES = (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
                    OrderStatus.FAILED, OrderStatus.EXPIRED_CONFIRM)


def _side(v) -> str:
    return v.value if isinstance(v, Side) else str(v)


def _status(v) -> str:
    return v.value if isinstance(v, OrderStatus) else str(v)


class ReconcileService:
    def __init__(self, hub=None, paper_broker=None, live_broker=None,
                 db_path=None, bus: EventBus | None = None,
                 interval: float | None = None, cash_tolerance: float = 0.01,
                 settings=None) -> None:
        self.hub = hub
        self.paper = paper_broker
        self.live = live_broker
        self.db_path = db_path
        self.bus = bus or SYSTEM_BUS
        self.tolerance = float(cash_tolerance)
        self.settings = settings
        self.interval = float(interval) if interval else self._cfg_interval()
        self.last_result: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _cfg(self):
        if self.settings is not None:
            return self.settings
        from quantdesk.config import get_settings

        return get_settings()

    def _cfg_interval(self) -> float:
        return float(getattr(self._cfg(), "reconcile_interval_s", 30.0))

    # ---------- 通道选择 ----------
    def _broker(self):
        """当前应被核对的通道：模拟盘核对 paper，实盘核对 hub 解析出的 live。"""
        if self.live is not None:
            return self.live
        if self._sim_only():
            return self.paper
        if self.hub is not None:
            return self.hub.live_broker()
        return None

    def _sim_only(self) -> bool:
        return bool(getattr(self._cfg(), "simulation_only", True))

    def _channel_name(self) -> str:
        b = self._broker()
        return getattr(b, "name", "") if b is not None else ""

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.interval = self._cfg_interval()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reconcile", daemon=True)
        self._thread.start()
        log.info("对账任务已启动（周期 %.0fs）", self.interval)

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._cfg_interval()):
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 守护循环不因一次失败而停摆
                log.exception("对账执行异常（继续运行）: %s", e)

    # ---------- 核对 ----------
    def run_once(self) -> dict:
        broker = self._broker()
        if broker is None:
            self.last_result = {"ran_at": now_ms(), "channel": "", "skipped": "无可用通道"}
            return self.last_result
        snapshot = self._snapshot(broker)
        diffs = self._compare(broker, snapshot)
        with self._lock:
            repo.reconcile_record(diffs, channel=self._channel_name(), db_path=self.db_path)
        self.last_result = {
            "ran_at": now_ms(), "channel": self._channel_name(),
            "diffs": len(diffs),
            "by_kind": {k: sum(1 for d in diffs if d["kind"] == k) for k in KINDS},
        }
        if diffs:
            log.error("对账差异 %d 项（通道 %s）: %s", len(diffs), self._channel_name(),
                      [d["ref"] for d in diffs][:10])
            self._publish({"action": "reconcile_diff", "channel": self._channel_name(),
                           "count": len(diffs), "diffs": diffs[:20]})
        return self.last_result

    @staticmethod
    def _snapshot(broker) -> dict:
        """优先用通道的专用快照接口（一次查询成套），否则按 ABC 逐项查。"""
        if hasattr(broker, "snapshot_for_reconcile"):
            orders, trades, positions, account = broker.snapshot_for_reconcile()
            return {"orders": orders, "trades": trades,
                    "positions": positions, "account": account}
        return {"orders": broker.get_orders(), "trades": broker.get_trades(),
                "positions": broker.get_position(), "account": broker.get_account()}

    def _compare(self, broker, snap: dict) -> list[dict]:
        diffs: list[dict] = []
        local_orders = repo.list_orders(limit=5000, db_path=self.db_path)
        by_id = {o.order_id: o for o in local_orders}
        by_broker_id = {o.broker_order_id: o for o in local_orders if o.broker_order_id}

        for bo in snap["orders"]:
            lo = by_id.get(bo.order_id) or by_broker_id.get(str(bo.broker_order_id))
            if lo is None:
                diffs.append({"kind": "order_missing_local", "ref": bo.order_id,
                              "detail": {"symbol": bo.symbol, "side": _side(bo.side),
                                         "volume": bo.volume, "status": _status(bo.status)}})
                continue
            if _status(lo.status) != _status(bo.status):
                # 只报「有意义」的漂移：本地仍挂单但券商已终结，或已终结状态不同
                if _status(bo.status) in _TERMINAL_STATES or \
                        _status(lo.status) in _FILLED_STATES:
                    diffs.append({"kind": "order_state_mismatch", "ref": lo.order_id,
                                  "detail": {"local": _status(lo.status),
                                             "broker": _status(bo.status),
                                             "filled": f"{lo.filled_volume}/{bo.filled_volume}"}})

        local_trades = repo.list_trades(limit=100_000, db_path=self.db_path)
        trade_ids = {t.trade_id for t in local_trades}
        broker_trade_ids = {t.broker_trade_id for t in local_trades if t.broker_trade_id}
        for bt in snap["trades"]:
            if bt.trade_id in trade_ids or (bt.broker_trade_id and
                                            bt.broker_trade_id in broker_trade_ids):
                continue
            diffs.append({"kind": "trade_missing_local", "ref": bt.trade_id,
                          "detail": {"symbol": bt.symbol, "price": bt.price,
                                     "volume": bt.volume, "order_id": bt.order_id}})

        for bp in snap["positions"]:
            lp = next((p for p in repo.latest_positions(db_path=self.db_path)
                       if p.symbol == bp.symbol), None)
            if lp is None:
                if bp.volume:
                    diffs.append({"kind": "position_mismatch", "ref": bp.symbol,
                                  "detail": {"local": "无记录", "broker_volume": bp.volume}})
                continue
            if lp.volume != bp.volume or lp.available != bp.available:
                diffs.append({"kind": "position_mismatch", "ref": bp.symbol,
                              "detail": {"local_volume": lp.volume, "broker_volume": bp.volume,
                                         "local_available": lp.available,
                                         "broker_available": bp.available}})

        ba: Account | None = snap.get("account")
        if ba is not None:
            # 通道自带数：cash + mv 必须 ≈ total_asset。这条不变式是 GUI/SDK 字段映射
            # 错误的照妖镜（easytrader 中文键、掘金字段变更都会在这里先炸出来），
            # 比「和昨天存过的资金比」更能当日报警用。
            delta = ba.cash + ba.market_value - ba.total_asset
            if abs(delta) > self.tolerance:
                diffs.append({"kind": "account_mismatch", "ref": "internal_consistency",
                              "detail": {"cash": ba.cash, "market_value": ba.market_value,
                                         "total_asset": ba.total_asset, "delta": round(delta, 4)}})
            if not self._sim_only() and snap["positions"]:
                mv_sum = sum(p.market_value for p in snap["positions"])
                if abs(mv_sum - ba.market_value) > max(self.tolerance, ba.market_value * 1e-4):
                    diffs.append({"kind": "account_mismatch", "ref": "market_value_sum",
                                  "detail": {"positions_sum": round(mv_sum, 2),
                                             "account_market_value": ba.market_value}})
        return diffs

    # ---------- 查询 ----------
    def recent_diffs(self, limit: int = 100, diffs_only: bool = False) -> list[dict]:
        """最近的对账行。默认含每轮无差异的审计行（"这一轮我核对过、没差异"也是事实）。

        要"只看差异"传 diffs_only=True——API 层默认就是这么给的，界面上列的是差异，
        不是心跳。服务层保持旧默认：改了它的默认语义等于给所有调用方埋一个惊喜。
        """
        return repo.reconcile_recent(limit=limit, db_path=self.db_path,
                                     diffs_only=diffs_only)

    def open_diffs(self, limit: int = 100, diffs_only: bool = True) -> list[dict]:
        """还没处置的差异（红条只统计这些——列出来没人认领，噪音会淹掉真事故）。

        diffs_only=True：每轮无差异也会落一条 kind="clean" 的审计行，它不是差异。
        不排除的话「待处置」按 30s 一轮心跳增长，一天两千多，真差异反而看不见。
        要审计流水（含每轮无差异记录）传 False——两个分支都尊重这个参数，
        不然前端那个「含每轮无差异记录」的勾在某些视图下点了没反应。
        """
        return repo.reconcile_recent(limit=limit, db_path=self.db_path,
                                     unresolved_only=True, diffs_only=diffs_only)

    def resolve(self, diff_id: int, note: str = "", resolve: bool = True) -> dict | None:
        row = repo.reconcile_resolve(diff_id, note=note, resolve=resolve, db_path=self.db_path)
        if row is not None:
            self._publish({"action": "reconcile_resolved", "diff": row})
        return row

    def status(self) -> dict:
        with self._lock:
            res = dict(self.last_result)
        res.update({"channel": self._channel_name(), "interval_s": self._cfg_interval(),
                    "running": self._thread is not None and self._thread.is_alive(),
                    "open": len(self.open_diffs(limit=500)),
                    "kinds": KINDS})
        return res

    def _publish(self, payload: dict) -> None:
        if self.bus is not None:
            self.bus.publish(Event(EventType.RISK, data=payload, source="reconcile"))


_service: ReconcileService | None = None
_service_lock = threading.Lock()


def get_reconcile_service() -> ReconcileService:
    global _service
    with _service_lock:
        if _service is None:
            # 与交易服务共用同一批通道连接：paper 簿（模拟盘自检）+ hub 解析的实盘通道
            from quantdesk.services.trading import get_trading_service

            trading = get_trading_service()
            _service = ReconcileService(hub=trading.hub, paper_broker=trading.paper,
                                        db_path=trading.paper.db_path)
        return _service
