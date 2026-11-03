"""回测服务（D08）：指标计算 + 回测编排 + 运行落库（backtest_runs 表）。

指标口径（全部写明，测试锁定）：
- 总收益 = 末期净值 / 期初净值 - 1
- 年化收益 = (1 + 总收益)^(252 / 收益间隔数) - 1（收益间隔数 = bar 数 - 1）
- 最大回撤 = max(1 - 净值 / 历史峰值)
- Sharpe = sqrt(252) × mean(r - rf/252) / std(r, ddof=1)；std=0 时为 0
- 胜率 / 盈亏比：按 FIFO 平仓回合核算，成本含买入费用、收入扣卖出费用
- 换手（年化）= Σ成交金额 / 平均净值 / 年数（年数 = bar 数 / 252）
"""

from __future__ import annotations

import importlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from quantdesk.core.enums import Side
from quantdesk.core.models import Trade
from quantdesk.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from quantdesk.engine.matching import FeeCalculator
from quantdesk.engine.strategy_base import BaseStrategy
from quantdesk.store import repo
from quantdesk.store.parquet_store import ParquetStore

__all__ = ["compute_metrics", "equity_to_json", "available_strategies",
           "load_strategy", "BacktestService"]

_STRATEGY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRADING_DAYS = 252


def _publish_log(message: str) -> None:
    """向进程级事件枢纽发布 LOG（/ws/events 下行）。"""
    from quantdesk.core.events import SYSTEM_BUS, Event, EventType

    SYSTEM_BUS.publish(Event(EventType.LOG, data={"message": message}, source="backtest"))


# ---------- 指标 ----------

def equity_to_json(equity: pd.DataFrame) -> list[dict]:
    """equity curve（DataFrame → JSON 安全结构）。"""
    if equity is None or equity.empty:
        return []
    out = equity.copy()
    out["dt"] = pd.to_datetime(out["dt"]).map(lambda t: t.isoformat())
    return out.round(4).to_dict(orient="records")


def _round_trips(trades: list[Trade], fee: FeeCalculator | None = None) -> list[float]:
    """FIFO 平仓回合盈亏（含费用）。"""
    fee = fee or FeeCalculator()
    book: dict[str, dict] = {}
    pnls: list[float] = []
    for t in sorted(trades, key=lambda x: (x.traded_at, x.trade_id)):
        side = t.side.value if isinstance(t.side, Side) else str(t.side)
        e = book.setdefault(t.symbol, {"vol": 0, "cost": 0.0})
        if side == Side.BUY.value:
            e["vol"] += t.volume
            e["cost"] += t.amount + fee.buy_fee(t.amount)
        else:
            if e["vol"] <= 0:
                continue
            avg = e["cost"] / e["vol"]
            vol = min(t.volume, e["vol"])
            pnl = (t.amount - fee.sell_fee(t.amount)) - avg * vol
            pnls.append(round(pnl, 2))
            e["vol"] -= vol
            e["cost"] = avg * e["vol"]
    return pnls


def compute_metrics(equity: pd.DataFrame, trades: list[Trade],
                    rf: float = 0.0) -> dict:
    """绩效指标（口径见模块 docstring）。空数据返回零值结构。"""
    zero = {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
            "sharpe": 0.0, "win_rate": 0.0, "profit_loss_ratio": None,
            "turnover": 0.0, "trade_count": len(trades or [])}
    if equity is None or len(equity) < 2:
        return zero
    nav = equity["equity"].astype(float).reset_index(drop=True)
    if nav.iloc[0] <= 0:
        return zero
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
    intervals = max(len(nav) - 1, 1)
    annual_return = float((1 + total_return) ** (_TRADING_DAYS / intervals) - 1)
    peak = nav.cummax()
    max_drawdown = float((1 - nav / peak).max())
    ret = nav.pct_change().dropna()
    std = float(ret.std(ddof=1)) if len(ret) > 1 else 0.0
    sharpe = 0.0 if std == 0 else float(
        math.sqrt(_TRADING_DAYS) * (ret.mean() - rf / _TRADING_DAYS) / std)
    pnls = _round_trips(trades or [])
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = round(len(wins) / len(pnls), 4) if pnls else 0.0
    pl_ratio: float | None = None
    if wins and losses:
        pl_ratio = round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 4)
    elif wins and not losses:
        pl_ratio = None                            # 无亏损回合，比值无定义
    years = max(len(nav) / _TRADING_DAYS, 1e-9)
    turnover = round(float(sum(t.amount for t in trades or []))
                     / float(nav.mean()) / years, 4)
    return {
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4),
        "win_rate": win_rate,
        "profit_loss_ratio": pl_ratio,
        "turnover": turnover,
        "trade_count": len(trades or []),
    }


# ---------- 策略发现与装载 ----------

def available_strategies() -> list[dict]:
    """扫描 strategies/ 包下所有策略模块，返回 [{name, module, params}]。"""
    out: dict[str, dict] = {}
    for mod_name in _strategy_modules():
        try:
            cls = load_strategy(mod_name.split(".")[-1])
        except Exception:                          # noqa: BLE001 — 单模块损坏不拖垮枚举
            continue
        out[cls.name] = {"name": cls.name, "module": mod_name,
                         "params": dict(cls.params)}
    return sorted(out.values(), key=lambda d: d["name"])


def _strategy_modules() -> list[str]:
    mods: list[str] = []
    pkg_root = Path(__file__).resolve().parent.parent.parent / "strategies"
    if not pkg_root.exists():
        return mods
    for p in sorted(pkg_root.rglob("*.py")):
        if p.name.startswith("_"):
            continue
        rel = p.relative_to(pkg_root.parent).with_suffix("")
        mods.append(".".join(rel.parts))
    return mods


def load_strategy(name: str) -> type[BaseStrategy]:
    """按名字装载策略类。只允许 strategies/ 包内模块名（防任意 import）。"""
    if not _STRATEGY_NAME_RE.match(name or ""):
        raise ValueError(f"非法策略名: {name!r}")
    candidates = [f"strategies.examples.{name}", f"strategies.{name}"]
    last_err: Exception | None = None
    for mod_name in candidates:
        try:
            module = importlib.import_module(mod_name)
        except ModuleNotFoundError as e:
            last_err = e
            continue
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, BaseStrategy)
                    and obj is not BaseStrategy
                    and getattr(obj, "__module__", "") == module.__name__):
                return obj
        raise ValueError(f"模块 {mod_name} 中未找到 BaseStrategy 子类")
    raise ValueError(f"未找到策略 {name!r}（{last_err}）")


# ---------- 服务 ----------

class BacktestService:
    """同步小任务版回测（D08）；异步队列化在 D10 视前端需要再加。"""

    def __init__(self, store: ParquetStore | None = None, db_path=None) -> None:
        self.store = store or ParquetStore()
        self.db_path = db_path

    def run(self, *, strategy: str, symbols: list[str], params: dict | None = None,
            freq: str = "1d", start: str | None = None, end: str | None = None,
            initial_cash: float = 1_000_000.0, slippage: float = 0.0,
            rf: float = 0.0) -> dict:
        cls = load_strategy(strategy)
        strat = cls(params=dict(params or {}))
        cfg = BacktestConfig(symbols=list(symbols), freq=freq, start=start, end=end,
                             initial_cash=float(initial_cash), slippage=float(slippage))
        engine = BacktestEngine(strat, cfg, store=self.store)
        result = engine.run()
        if result.bars == 0:
            raise ValueError(f"本地无数据：{symbols} {freq} {start}~{end}"
                             f"（请先在数据管理页下载）")
        metrics = compute_metrics(result.equity, result.trades, rf=rf)
        equity_json = equity_to_json(result.equity)

        run_id = repo.backtest_create(
            strategy_name=cls.name, symbol=",".join(symbols), freq=freq,
            start=str(start or ""), end=str(end or ""),
            params_json=json.dumps(result.config | {"rf": rf}, ensure_ascii=False),
            db_path=self.db_path)
        repo.backtest_finish(run_id, metrics_json=json.dumps(metrics, ensure_ascii=False),
                             equity_json=json.dumps(equity_json), status="completed",
                             db_path=self.db_path)
        # 回测过程输出接事件流（D10：同步任务为完成后整体推送，异步流式在 M4 前补齐）
        _publish_log(f"回测 {run_id} 完成：{cls.name} {symbols} "
                     f"{len(result.trades)} 笔成交，总收益 {metrics['total_return']:.2%}")
        return {
            "run_id": run_id,
            "metrics": metrics,
            "equity": equity_json,
            "trades": [self._trade_json(t) for t in result.trades],
            "orders": [self._order_json(o) for o in result.orders],
            "logs": result.logs[-200:],
            "bars": result.bars,
            "config": result.config,
        }

    def get(self, run_id: int) -> dict | None:
        row = repo.backtest_get(run_id, db_path=self.db_path)
        if row is None:
            return None
        return {
            "run_id": row.id,
            "strategy": row.strategy_name,
            "symbol": row.symbol,
            "freq": row.freq,
            "start": row.start,
            "end": row.end,
            "params": json.loads(row.params_json or "{}"),
            "status": row.status,
            "metrics": json.loads(row.metrics_json or "{}"),
            "equity": json.loads(row.equity_json or "[]"),
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }

    # ---------- 序列化 ----------
    @staticmethod
    def _trade_json(t: Trade) -> dict:
        d = asdict(t)
        d["side"] = t.side.value if isinstance(t.side, Side) else str(t.side)
        return d

    @staticmethod
    def _order_json(o) -> dict:
        return {
            "order_id": o.order_id, "symbol": o.symbol,
            "side": o.side.value if isinstance(o.side, Side) else str(o.side),
            "price": o.price, "volume": o.volume,
            "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
            "status": o.status.value if hasattr(o.status, "value") else str(o.status),
            "filled_volume": o.filled_volume, "avg_filled_price": o.avg_filled_price,
            "message": o.message,
        }


_service: BacktestService | None = None


def get_backtest_service() -> BacktestService:
    """模块级单例（API 层使用）。"""
    global _service
    if _service is None:
        _service = BacktestService()
    return _service
