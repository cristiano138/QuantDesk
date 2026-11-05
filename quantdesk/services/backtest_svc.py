"""回测服务（D08）：指标计算 + 回测编排 + 运行落库（backtest_runs 表）
+ 运行明细落库（backtest_detail 表，成交/委托/日志/元信息分块，重启后可回看）
+ 取消（排队中直接作废 / 运行中在 bar 边界安全停下，状态 cancelled）。

指标口径（全部写明，测试锁定）：
- 总收益 = 末期净值 / 期初净值 - 1
- 年化收益 = (1 + 总收益)^(252 / 收益间隔数) - 1（收益间隔数 = bar 数 - 1）
- 最大回撤 = max(1 - 净值 / 历史峰值)
- Sharpe = sqrt(252) × mean(r - rf/252) / std(r, ddof=1)；std=0 时为 0
- 胜率 / 盈亏比：按 FIFO 平仓回合核算，成本含买入费用、收入扣卖出费用
- 换手（年化）= Σ成交金额 / 平均净值 / 年数（年数 = bar 数 / 252）
- 取消的运行不计算指标（metrics_json 保持空对象）：部分成交算出来的收益不代表一次跑完
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import queue
import re
import threading
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from quantdesk.core.enums import Frequency, Side
from quantdesk.core.errors import QuantDeskError
from quantdesk.core.models import Trade
from quantdesk.core.symbols import normalize
from quantdesk.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from quantdesk.engine.matching import FeeCalculator
from quantdesk.engine.strategy_base import BaseStrategy
from quantdesk.store import repo
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)

__all__ = ["compute_metrics", "equity_to_json", "available_strategies",
           "load_strategy", "BacktestService"]

_STRATEGY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRADING_DAYS = 252
_LOG_TAIL = 200          # 过程日志只留最后 200 条（与改造前进程内缓存同口径，总量如实上报）


def _publish_log(payload) -> None:
    """向进程级事件枢纽发布 LOG（/ws/events 下行）。

    进度类消息带结构（action/run_id/done/total），前端按 action 分派——
    纯文本消息仍然兼容（只填 message）。
    """
    from quantdesk.core.events import SYSTEM_BUS, Event, EventType

    data = dict(payload) if isinstance(payload, dict) else {"message": str(payload)}
    data.setdefault("message", "")
    SYSTEM_BUS.publish(Event(EventType.LOG, data=data, source="backtest"))


def _idle_run_note(strat: BaseStrategy, symbols: list[str]) -> tuple[str, list[str]] | None:
    """策略写死了交易哪只、而它不在本次喂给引擎的标的里 → 返回 (目标代码, 本次标的)。

    为什么需要这一步（本轮实测出来的缺陷）：示例策略把标的写在参数 `symbol` 里
    （dual_ma.on_bar 开头 `if bar.symbol != symbol: return`）。用户从盯盘池一次性带入
    十几只代码、参数 symbol 却还是默认的那只时，引擎照常逐根喂 bar、状态照样 completed，
    界面上是「0 成交 + 一条平线」——看起来像策略没信号，实际是配置不一致。
    这种判断必须由服务层给出并落进备注，不能让前端拿"成交为 0"去猜原因。

    只认参数里那个叫 `symbol` 的键（示例策略的约定），不吃其它形状的策略：
    策略没有这个参数就返回 None——猜错会把一次正常回测标成空跑，比不标更糟。
    """
    raw = str(getattr(strat, "params", {}).get("symbol", "") or "").strip()
    if not raw:
        return None
    try:
        target = normalize(raw)
        have = sorted({normalize(s) for s in symbols})
    except QuantDeskError:
        return None                       # 代码写法认不出就不下结论
    if target in have:
        return None
    return target, have


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
    """回测编排：同步 run()（小任务/脚本用）+ 异步 submit()（前端用）。

    异步队列的必要性：1655 根日线 ≈ 3s，多标的/分钟线会到几十秒——同步 POST 会
    占住请求线程、前端干等且无进度。现在 run_id 先落库（queued→running→completed/
    failed/cancelled/interrupted），worker 线程执行，进度经 WS 与轮询双通道可见，
    中途失败的运行也留痕。

    两条本轮补上的能力（此前的真实空缺）：
    ① 明细落库：成交/委托/日志/元信息按块写 backtest_detail 表，服务重启后
       GET /api/backtest/{run_id} 仍返回与重启前同构的明细（此前只活在本进程
       _results 内存里，保留最近 50 份，重启即不可回看）。
       顺序仍是「先落库后广播」：明细块 → 终态（含指标）→ 才发 WS 事件。
    ② 取消：request_cancel(run_id)——排队中的直接从队列摘掉标 cancelled；
       运行中的置 Event，引擎在 bar 边界停下，状态记 cancelled、指标留空
       （半截结果不许冒充 completed，明细与净值如实标 partial 供回看）。
       进程重启后库里悬挂的 queued|running 由 reconcile_orphans() 标 interrupted，
       不让一条早就不存在的运行永远挂着「运行中」。
    """

    #: 内存结果缓存里随明细一起留存的键（equity 太大不进缓存，一直从库读）
    _CACHED_KEYS = ("run_id", "metrics", "bars", "config", "trades", "orders",
                    "logs", "partial", "logs_total")

    def __init__(self, store: ParquetStore | None = None, db_path=None,
                 keep_results: int = 50, sweep_orphans: bool = True) -> None:
        self.store = store or ParquetStore()
        # 注意：db_path 只是「显式指定的库」，为 None 时各 repo 调用在调用点取默认库
        # （default_db_path 惰性取 data_dir）——模块顶层绑死路径会让测试夹具换目录失效。
        self.db_path = db_path
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._payloads: dict[int, dict] = {}
        self._progress: dict[int, dict] = {}
        self._results: dict[int, dict] = {}
        self._results_order: list[int] = []
        self._keep_results = keep_results
        self._cancels: dict[int, threading.Event] = {}
        self._lock = threading.RLock()
        if sweep_orphans:
            # 单例第一次被装配时就把上一任进程留下的悬挂运行结清（失败不阻断服务）
            try:
                self.reconcile_orphans()
            except Exception:                     # noqa: BLE001 库还没建等场景按无悬挂处理
                log.exception("回测悬挂运行清理失败（不阻断服务）")

    # ---------- 异步队列 ----------
    def submit(self, **kwargs) -> int:
        """入队并立刻返回 run_id（校验策略名在入队前做，坏请求不占队列）。"""
        cls = load_strategy(kwargs["strategy"])
        kwargs["strategy"] = cls.name
        run_id = repo.backtest_create(
            strategy_name=cls.name, symbol=",".join(kwargs.get("symbols") or []),
            freq=kwargs.get("freq", "1d"), start=str(kwargs.get("start") or ""),
            end=str(kwargs.get("end") or ""),
            params_json=json.dumps(kwargs.get("params") or {}, ensure_ascii=False),
            db_path=self.db_path)
        repo.backtest_set_status(run_id, "queued", db_path=self.db_path)
        with self._lock:
            self._payloads[run_id] = kwargs
            self._cancels[run_id] = threading.Event()
            self._progress[run_id] = {"run_id": run_id, "status": "queued",
                                      "done": 0, "total": 0, "percent": 0.0}
        self._ensure_worker()
        self._queue.put(run_id)
        return run_id

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._drain, name="backtest-worker",
                                            daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            run_id = self._queue.get()
            if run_id is None:
                return
            payload = self._payloads.pop(run_id, None)
            if payload is None:
                # 排队期间被取消：request_cancel 已把载荷摘掉、状态写成 cancelled，不执行
                continue
            try:
                out = self.run(run_id=run_id, **payload)
                self._store_result(run_id, out)
            except Exception as e:  # noqa: BLE001 单个任务失败不能带走 worker
                log.exception("异步回测失败 run_id=%s", run_id)
                self._set_progress(run_id, "failed", error=f"{e.__class__.__name__}: {e}")

    def _store_result(self, run_id: int, out: dict) -> None:
        with self._lock:
            self._results[run_id] = {k: out[k] for k in self._CACHED_KEYS if k in out}
            self._results_order.append(run_id)
            while len(self._results_order) > self._keep_results:
                old = self._results_order.pop(0)
                self._results.pop(old, None)
                self._progress.pop(old, None)
                self._cancels.pop(old, None)

    def _set_progress(self, run_id: int, status: str, **extra) -> None:
        with self._lock:
            cur = self._progress.setdefault(run_id, {"run_id": run_id})
            cur.update({"status": status, **extra})
            # percent 一律由 done/total 现算（取消时停在 14% 就写 14%，不许留旧值）；
            # completed 是唯一的 100%（cancelled 不是跑完，不给满进度）。
            if cur.get("total"):
                if status == "completed":
                    cur["percent"] = 100.0
                elif cur.get("done") is not None:
                    cur["percent"] = round(100.0 * cur["done"] / cur["total"], 1)

    def progress(self, run_id: int, row=None) -> dict:
        """进度（内存优先；本进程没有这条运行时回落到库，不再一律报 unknown）。

        为什么要回落：服务重启后内存进度表是空的，而用户回看历史运行、
        或前端在断线重连后拉 /progress，拿到的该是这条运行的真实终态，
        不是「unknown」——状态的事实源在库里，不在本进程记忆里。

        status 取值：queued / running / completed / failed / cancelled / interrupted
        —— 与库里同名状态一致（cancelled/interrupted 本轮新增），另有两个只在进度面
        出现的值：cancelling（取消已受理、引擎尚未走到 bar 边界，前端当 running 继续轮询）
        与 unknown（库里查不到这条运行）。
        row：调用方已取到运行行时传入，省一次重复查库。
        """
        with self._lock:
            cur = self._progress.get(run_id)
            if cur is not None:
                return dict(cur)
        if row is None:
            row = repo.backtest_get(run_id, db_path=self.db_path)
        if row is None:
            return {"run_id": run_id, "status": "unknown"}
        return {"run_id": run_id, "status": row.status,
                "done": None, "total": None,
                "percent": 100.0 if row.status == "completed" else 0.0,
                "source": "db"}

    def stop_worker(self) -> None:
        self._queue.put(None)

    # ---------- 取消 ----------
    def reconcile_orphans(self) -> list[int]:
        """把库里悬挂的 queued|running 标 interrupted（进程重启后队列已不存在）。"""
        ids = repo.backtest_reconcile_orphans(db_path=self.db_path)
        if ids:
            log.warning("回测悬挂运行标记 interrupted: %s", ids)
            _publish_log({"action": "backtest_interrupted", "run_ids": ids,
                          "message": f"服务重启：{len(ids)} 条未跑完的回测运行标记 interrupted"})
        return ids

    def request_cancel(self, run_id: int) -> dict | None:
        """请求取消一条运行。返回 None 表示库里没有这条运行（API 层转 404）。

        语义（对进程内队列与重启后的库记录都成立）：
        - queued 且载荷还在队列里 → 摘掉载荷，直接标 cancelled，根本不执行；
        - running（或刚被 worker 取走的 queued）→ 置 Event，引擎在 bar 边界停下，
          由 run() 收尾时写 cancelled；本调用不改状态，只保证标志位已置；
        - 库里是 queued|running 但本进程没有在跑它 → 说明服务重启过、执行体早没了，
          如实标 interrupted（这条永远不可能被「取消」，因为它并没有在跑）；
        - 已是终态（completed/failed/cancelled/interrupted）→ 幂等返回 changed=False，
          绝不把 completed 改写成 cancelled。
        """
        row = repo.backtest_get(run_id, db_path=self.db_path)
        if row is None:
            return None
        status = row.status
        with self._lock:
            ev = self._cancels.get(run_id)
            queued_payload = self._payloads.pop(run_id, None) if status == "queued" else None
            if ev is not None:
                ev.set()                          # 不管当前状态字样是什么，先置位（收敛竞态）

        if status in ("completed", "failed", "cancelled", "interrupted"):
            return {"run_id": run_id, "ok": False, "changed": False, "status": status,
                    "message": f"运行已是终态 {status}，无需取消"}

        if status == "queued" and queued_payload is not None:
            # 排队中：不执行就是最干净的取消，没有任何部分结果需要交代
            repo.backtest_set_status(run_id, "cancelled",
                                     remark="用户取消：排队中未执行", db_path=self.db_path)
            self._set_progress(run_id, "cancelled", message="排队中已取消，未执行")
            with self._lock:
                self._cancels.pop(run_id, None)
            _publish_log({"action": "backtest_cancelled", "run_id": run_id,
                          "message": f"回测 {run_id} 已在排队阶段取消（未执行）"})
            return {"run_id": run_id, "ok": True, "changed": True, "status": "cancelled",
                    "message": "已从队列移除，未执行"}

        if ev is not None:
            # 运行中：标志已置位，引擎在当前 bar 边界停下后由 run() 落 cancelled
            self._set_progress(run_id, "cancelling", message="已请求取消，等待当前 bar 结束")
            return {"run_id": run_id, "ok": True, "changed": False, "status": status,
                    "cancelling": True,
                    "message": "取消请求已受理，引擎将在当前 bar 边界安全停下（状态稍后落 cancelled）"}

        # 库里说它在跑，本进程却没有这条运行的执行体——服务重启导致的悬挂
        repo.backtest_set_status(run_id, "interrupted",
                                 remark="取消请求：服务已重启，该运行的执行进程不存在",
                                 db_path=self.db_path)
        return {"run_id": run_id, "ok": True, "changed": True, "status": "interrupted",
                "message": "本进程内不存在该运行（服务已重启），已如实标记 interrupted"}

    # ---------- 执行 ----------
    def run(self, *, strategy: str, symbols: list[str], params: dict | None = None,
            freq: str = "1d", start: str | None = None, end: str | None = None,
            initial_cash: float = 1_000_000.0, slippage: float = 0.0,
            rf: float = 0.0, run_id: int | None = None) -> dict:
        cls = load_strategy(strategy)
        strat = cls(params=dict(params or {}))
        cfg = BacktestConfig(symbols=list(symbols), freq=freq, start=start, end=end,
                             initial_cash=float(initial_cash), slippage=float(slippage))
        engine = BacktestEngine(strat, cfg, store=self.store)

        # 预检本地数据：没数据就不要建运行记录（异步队列里一条注定失败的 run 只会污染清单）
        probe_freq = Frequency(cfg.freq) if not isinstance(cfg.freq, Frequency) else cfg.freq
        if not any(not self.store.read(s, probe_freq, cfg.start, cfg.end, cfg.adjust).empty
                   for s in cfg.symbols):
            raise ValueError(f"本地无数据：{symbols} {freq} {start}~{end}"
                             f"（请先在数据管理页下载）")

        if run_id is None:
            run_id = repo.backtest_create(
                strategy_name=cls.name, symbol=",".join(symbols), freq=freq,
                start=str(start or ""), end=str(end or ""),
                params_json=json.dumps({"params": params or {}, "rf": rf},
                                       ensure_ascii=False), db_path=self.db_path)
        else:
            repo.backtest_set_status(run_id, "running", db_path=self.db_path)
        # 同步路径（直接 run()）也要可取消：没有事件就现建一个，
        # 这样 POST /api/backtest/{id}/cancel 对同步运行同样成立（不必非走队列）。
        with self._lock:
            stop_ev = self._cancels.setdefault(run_id, threading.Event())

        def _progress(done: int, total: int) -> None:
            self._set_progress(run_id, "running", done=done, total=total)
            _publish_log({"action": "backtest_progress", "run_id": run_id,
                          "done": done, "total": total, "message":
                              f"回测 {run_id} 进度 {done}/{total}"})

        try:
            result = engine.run(on_progress=_progress, should_stop=stop_ev.is_set)
            if result.bars == 0 and not result.cancelled:
                raise ValueError(f"本地无数据：{symbols} {freq} {start}~{end}"
                                 f"（请先在数据管理页下载）")

            metrics = compute_metrics(result.equity, result.trades, rf=rf)
            equity_json = equity_to_json(result.equity)
            trades_json = [self._trade_json(t) for t in result.trades]
            orders_json = [self._order_json(o) for o in result.orders]
            cancelled = bool(result.cancelled)

            # 空跑如实说明：只在「一根成交都没有」时下结论——有成交说明策略不只认这个参数，
            # 那时标它为空跑就是误报。这句话进 result.logs（不是事后塞进切片），
            # 这样 logs_total 与实存条数同源，界面上「显示 N 条 / 共 M 条」不会自相矛盾。
            warning = ""
            idle = _idle_run_note(strat, cfg.symbols)
            if idle and not result.trades:
                tgt, have = idle
                warning = (f"空跑提示：策略 {cls.name} 只交易参数 symbol={tgt}，"
                           f"而它不在本次标的（{'、'.join(have)}）里——"
                           f"{result.bars} 根 bar 里没有它认得的标的，0 成交是配置不一致，"
                           f"不是策略没有信号。改法：把 {tgt} 加进「标的」，"
                           f"或把参数 symbol 改成标的列表里的代码。")
                result.logs.insert(0, warning)
            logs_json = list(result.logs[-_LOG_TAIL:])

            # ① 明细先落库（宪法第 4 条：先落库后广播——状态字样与 WS 事件都不许跑在数据前面）
            detail_counts = self._persist_detail(run_id, result, trades_json, orders_json,
                                                 logs_json, cancelled=cancelled)
            # ② 再写终态：completed 带指标；cancelled 指标一律留空——
            #    半截成交算出来的收益不是「一次跑完的回测」，写进去就是冒充 completed。
            if cancelled:
                remark = (f"用户取消：已处理 {result.bars}/{result.planned_bars} 根 bar，"
                          f"成交/委托/净值为截至取消点的部分结果，指标不予计算")
                if warning:
                    remark = f"{remark}｜{warning}"
                repo.backtest_finish(run_id, metrics_json="{}",
                                     equity_json=json.dumps(equity_json, ensure_ascii=False),
                                     remark=remark, status="cancelled", db_path=self.db_path)
            else:
                repo.backtest_finish(run_id,
                                     metrics_json=json.dumps(metrics, ensure_ascii=False),
                                     equity_json=json.dumps(equity_json, ensure_ascii=False),
                                     remark=warning,
                                     status="completed", db_path=self.db_path)
        except Exception as e:  # noqa: BLE001 失败也要留痕（前端轮询能看到原因）
            repo.backtest_set_status(run_id, "failed",
                                     remark=f"{e.__class__.__name__}: {e}",
                                     db_path=self.db_path)
            self._set_progress(run_id, "failed", error=str(e))
            raise
        finally:
            with self._lock:
                self._cancels.pop(run_id, None)

        out = {
            "run_id": run_id,
            "metrics": metrics if not cancelled else {},
            "equity": equity_json,
            "trades": trades_json,
            "orders": orders_json,
            "logs": logs_json,
            "bars": result.bars,
            "config": result.config,
            "status": "cancelled" if cancelled else "completed",
            "partial": cancelled,                    # 明细是否为部分结果（取消）
            "logs_total": len(result.logs),          # 截断前总条数（logs 只存最后 200 条）
            "detail_source": "db",                   # 已落库，重启后同构可回看
            "detail_counts": detail_counts,
        }
        if cancelled:
            self._set_progress(run_id, "cancelled", done=result.bars,
                               total=result.planned_bars,
                               message=f"已取消：处理 {result.bars}/{result.planned_bars} 根 bar")
            _publish_log({"action": "backtest_cancelled", "run_id": run_id,
                          "done": result.bars, "total": result.planned_bars,
                          "message": f"回测 {run_id} 已取消："
                                     f"处理 {result.bars}/{result.planned_bars} 根 bar，"
                                     f"{len(result.trades)} 笔部分成交（指标不予计算）"})
            return out

        self._set_progress(run_id, "completed", done=result.bars, total=result.bars)
        _publish_log({"action": "backtest_done", "run_id": run_id, "message":
                      f"回测 {run_id} 完成：{cls.name} {symbols} "
                      f"{len(result.trades)} 笔成交，总收益 {metrics['total_return']:.2%}"})
        return out

    def _persist_detail(self, run_id: int, result: BacktestResult,
                        trades: list[dict], orders: list[dict], logs: list[str],
                        cancelled: bool) -> dict:
        """成交/委托/日志/元信息分块落 backtest_detail（体积与条数都在库里如实记）。"""
        parts = {
            "trades": trades,
            "orders": orders,
            "logs": logs,
            "meta": {
                "bars": int(result.bars),
                "planned_bars": int(result.planned_bars),
                "cancelled": bool(cancelled),
                "partial": bool(cancelled),
                "config": result.config,
                "logs_total": len(result.logs),
                "logs_stored": len(logs),
                "trades": len(trades),
                "orders": len(orders),
            },
        }
        written = repo.backtest_detail_write(run_id, parts, db_path=self.db_path)
        # 自检（写入后回读条数）：明细落库最怕「以为写了其实没写」——
        # 一次跑完几千笔成交却在重启后回看是空的，用户只能认为功能没做。
        counts = {k: int((v or {}).get("rows", 0)) for k, v in written.items() if k != "meta"}
        counts["bars"] = int(result.bars)
        back = repo.backtest_detail_stats(run_id, db_path=self.db_path)
        for k in ("trades", "orders"):
            if back.get(k, {}).get("rows", 0) != counts.get(k, 0):
                raise RuntimeError(f"回测 {run_id} 明细自检失败：{k} 落库 "
                                   f"{back.get(k, {}).get('rows', 0)} 条 ≠ 期望 "
                                   f"{counts.get(k, 0)} 条")
        return counts

    def get(self, run_id: int) -> dict | None:
        """单次运行详情：库里取状态/指标/净值，明细优先用本进程缓存、否则回读库。

        响应结构（键集合）在重启前后保持一致：trades/orders/logs/bars/config 恒在，
        没落过明细的历史运行给出空集合 + detail_source="none"，而不是让前端判空漂移。
        """
        row = repo.backtest_get(run_id, db_path=self.db_path)
        if row is None:
            return None
        out = {
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
            "remark": row.remark,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
            "progress": self.progress(run_id, row=row),
            "trades": [], "orders": [], "logs": [], "bars": 0, "config": {},
            "partial": row.status in ("cancelled", "interrupted"),
            "logs_total": 0,
            "detail_source": "none",
        }
        cached = self._results.get(run_id)
        if cached:
            out.update({k: cached[k] for k in self._CACHED_KEYS if k in cached
                        and k != "run_id"})
            out["detail_source"] = "memory"
            return out
        parts = repo.backtest_detail_read(run_id, db_path=self.db_path)
        if not parts:
            return out
        meta = parts.get("meta") or {}
        out.update({
            "trades": parts.get("trades", []),
            "orders": parts.get("orders", []),
            "logs": parts.get("logs", []),
            "bars": int(meta.get("bars", 0)),
            "config": meta.get("config", {}) or {},
            "partial": bool(meta.get("partial", row.status in ("cancelled", "interrupted"))),
            "logs_total": int(meta.get("logs_total", len(parts.get("logs", [])))),
            "detail_source": "db",
        })
        return out

    def list_runs(self, limit: int = 50) -> list[dict]:
        return repo.backtest_list(limit=limit, db_path=self.db_path)

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
