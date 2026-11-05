"""回测引擎（D07）：虚拟时钟 + 事件循环 + HistoricalDataFeed（包装 parquet_store）。

时序与无未来函数约定（日线回测标准口径，与 backtrader 默认一致）：
- 策略在 on_bar(T) 中提交的委托进入待撮合队列；
- 下一根同标的 bar（T+1）先撮合待撮合队列（开盘价优先），再回调 on_bar；
- 权益按当根 bar 收盘价逐 bar 盯市。

T+1：交易日切换先释放可用持仓，再撮合，再回调策略（当日买次日可卖）。
撮合走 engine/matching（纯函数），组合状态走 PositionBook；本模块负责落账。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

import pandas as pd

from quantdesk.core.enums import AdjustType, Frequency, OrderStatus, OrderType, Side
from quantdesk.core.models import Bar, Order, Trade
from quantdesk.core.timeutil import now_ms
from quantdesk.engine.matching import (
    MatchConfig,
    PositionBook,
    affordable_volume,
    apply_fill,
    match_on_bar,
)
from quantdesk.engine.strategy_base import BaseStrategy, StrategyContext
from quantdesk.store.parquet_store import HISTORY_COLUMNS, ParquetStore

__all__ = ["BacktestConfig", "BacktestResult", "BacktestEngine", "HistoricalDataFeed"]


class HistoricalDataFeed:
    """历史回放器：包装 ParquetStore，按 [start, end] 装载各标的本地数据（前复权）。"""

    def __init__(self, store: ParquetStore | None = None) -> None:
        self.store = store or ParquetStore()

    def load(self, symbols: list[str], freq: Frequency | str,
             start: str | None = None, end: str | None = None,
             adjust: AdjustType = AdjustType.FORWARD) -> dict[str, pd.DataFrame]:
        f = Frequency(freq)
        out: dict[str, pd.DataFrame] = {}
        for s in symbols:
            df = self.store.read(s, f, start=start, end=end, adjust=adjust)
            out[s] = df
        return out


@dataclass
class BacktestConfig:
    symbols: list[str]
    freq: Frequency | str = Frequency.D1
    start: str | None = None
    end: str | None = None
    initial_cash: float = 1_000_000.0
    adjust: AdjustType = AdjustType.FORWARD     # 宪法：回测与指标用前复权
    slippage: float = 0.0
    volume_limit_ratio: float = 1.0
    order_ttl_bars: int = 1                     # 未成交委托存活 bar 数（按同标的计）


@dataclass
class BacktestResult:
    equity: pd.DataFrame                        # dt / equity / cash / market_value
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    bars: int = 0
    config: dict = field(default_factory=dict)
    cancelled: bool = False                     # 用户中途取消（bars 为部分结果）
    planned_bars: int = 0                       # 本次原计划要处理的 bar 数（取消说明用）


class _PendingOrder:
    __slots__ = ("order", "attempts")

    def __init__(self, order: Order) -> None:
        self.order = order
        self.attempts = 0


class _BacktestContext(StrategyContext):
    """回测版 StrategyContext：history 直读本地；buy/sell 进 paper 路径（经 matching）。"""

    def __init__(self, engine: "BacktestEngine") -> None:
        self._engine = engine

    def subscribe(self, symbols: list[str]) -> None:
        self._engine.subscribed.update(symbols)

    def history(self, symbol: str, freq: Frequency | str = Frequency.D1,
                bars: int = 120) -> pd.DataFrame:
        eng = self._engine
        f = Frequency(freq)
        if f == eng.freq:
            df = eng._data.get(symbol)          # 引擎内存数据（已按 [start,end] 装载）
            if df is None or df.empty:
                return pd.DataFrame(columns=HISTORY_COLUMNS)
            return df[df["dt"] <= eng.clock].tail(bars).reset_index(drop=True)
        # 其他周期：直读本地库（无数据返回空表，由策略兜底）
        df = eng.feed.store.read(symbol, f, adjust=eng.cfg.adjust)
        if df.empty:
            return df
        return df[df["dt"] <= eng.clock].tail(bars).reset_index(drop=True)

    def buy(self, symbol: str, volume: int, price: float | None = None) -> Order:
        return self._engine._submit(Side.BUY, symbol, volume, price)

    def sell(self, symbol: str, volume: int, price: float | None = None) -> Order:
        return self._engine._submit(Side.SELL, symbol, volume, price)

    def get_position(self, symbol: str) -> Position | None:
        return self._engine.book.position(symbol)

    def log(self, message: str) -> None:
        self._engine._log(f"[{self._engine.strategy.name}] {message}")

    def now(self) -> datetime:
        return self._engine.clock


class BacktestEngine:
    """回测主引擎。用法：

        eng = BacktestEngine(MyStrategy(params={...}), BacktestConfig(symbols=["600000.SH"]))
        result = eng.run()
    """

    def __init__(self, strategy: BaseStrategy, config: BacktestConfig,
                 store: ParquetStore | None = None) -> None:
        self.strategy = strategy
        self.cfg = config
        self.freq = Frequency(config.freq)
        self.feed = HistoricalDataFeed(store)
        self.match_cfg = MatchConfig(slippage=config.slippage,
                                     volume_limit_ratio=config.volume_limit_ratio)
        # 状态
        self.clock: datetime = datetime(1970, 1, 1)
        self.cash: float = config.initial_cash
        self.book = PositionBook(strategy_id=strategy.name)
        self.subscribed: set[str] = set()
        self._data: dict[str, pd.DataFrame] = {}
        self._pending: list[_PendingOrder] = []
        self._orders: list[Order] = []
        self._trades: list[Trade] = []
        self._logs: list[str] = []
        self._last_close: dict[str, float] = {}
        self._last_date: pd.Timestamp | None = None
        self._seq = 0

    # ---------- 对外 ----------
    def run(self, on_progress=None, should_stop=None) -> BacktestResult:
        """执行回测。

        on_progress(done, total)：供服务层发布进度事件（异步队列化的前提）。
        节流由调用方决定——引擎只负责按 bar 汇报，不做 I/O。

        should_stop() -> bool：取消检查钩子（服务层传 threading.Event.is_set）。
        在**每根 bar 处理的开头**检查，命中就停下——一根 bar 之内不做半截撮合，
        所以拿到的是一个「截至上一根 bar 的完整状态」，成交/委托/持仓不会自相矛盾。
        取消不是异常：正常收尾（策略 on_stop、未成交委托按撤单处理），
        结果里 cancelled=True，由服务层决定记 cancelled 状态而不是 completed。
        """
        self._data = self.feed.load(self.cfg.symbols, self.freq,
                                    self.cfg.start, self.cfg.end, self.cfg.adjust)
        stream = self._merge_stream()
        total = sum(len(df) for df in self._data.values()) or 1
        self.strategy.bind(_BacktestContext(self))
        self.strategy.on_start()

        equity_rows: list[dict] = []
        n = 0
        cancelled = False
        last_dt: pd.Timestamp | None = None
        for dt, symbol, row in stream:
            if should_stop is not None and should_stop():
                cancelled = True
                self._log(f"收到取消请求，已在第 {n} 根 bar 后安全停下（计划 {total} 根）")
                break
            if dt < self.clock:
                raise ValueError(f"bar 时间非单调: {dt} < {self.clock}（symbol={symbol}）")
            self.clock = dt.to_pydatetime()
            last_dt = dt
            self._maybe_new_trading_day(dt)
            bar = Bar(symbol=symbol, freq=self.freq, dt=self.clock,
                      open=float(row["open"]), high=float(row["high"]),
                      low=float(row["low"]), close=float(row["close"]),
                      volume=float(row["volume"]), amount=float(row["amount"]))
            self._match_pending(bar)          # 先撮合上一根 bar 提交的委托
            self.strategy.on_bar(bar)         # 再驱动策略（可能提交新委托）
            self._last_close[symbol] = bar.close
            equity_rows.append(self._equity_row(dt))
            n += 1
            if on_progress is not None and (n % 25 == 0 or n == total):
                on_progress(n, total)

        self.strategy.on_stop()
        # 收尾：未成交委托按撤单处理（回测结束语义；取消的运行时说明是取消而非跑完）
        end_note = "回测取消未成交撤单" if cancelled else "回测结束未成交撤单"
        for po in self._pending:
            o = po.order
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PART_FILLED):
                o.status = OrderStatus.CANCELLED
                o.message = (o.message or "") + end_note
                o.updated_at = int(last_dt.timestamp() * 1000) if last_dt is not None else 0
        equity = pd.DataFrame(equity_rows)
        if not equity.empty:
            equity = equity.rename(columns={"dt": "dt"})
        return BacktestResult(
            equity=equity,
            orders=self._orders,
            trades=self._trades,
            logs=self._logs,
            bars=n,
            cancelled=cancelled,
            planned_bars=total,
            config={
                "symbols": list(self.cfg.symbols), "freq": self.freq.value,
                "start": self.cfg.start, "end": self.cfg.end,
                "initial_cash": self.cfg.initial_cash, "adjust": self.cfg.adjust.value,
                "slippage": self.cfg.slippage, "strategy": self.strategy.name,
                "params": dict(self.strategy.params),
            },
        )

    # ---------- 内部 ----------
    def _merge_stream(self) -> list[tuple[pd.Timestamp, str, pd.Series]]:
        """多标的合并事件流：按 dt 升序、同 dt 按标的列表序（稳定）；逐标的内严格升序。"""
        frames: list[pd.DataFrame] = []
        order_of = {s: i for i, s in enumerate(self.cfg.symbols)}
        for s in self.cfg.symbols:
            df = self._data.get(s)
            if df is None or df.empty:
                continue
            df = df.copy()
            df["_symbol"] = s
            df["_rank"] = order_of[s]
            frames.append(df)
        if not frames:
            return []
        all_df = pd.concat(frames, ignore_index=True)
        all_df = all_df.sort_values(["dt", "_rank"], kind="stable").reset_index(drop=True)
        # 逐标的严格升序校验（数据损坏防线）
        for s, grp in all_df.groupby("_symbol"):
            dts = grp["dt"].tolist()
            if any(b <= a for a, b in zip(dts, dts[1:])):
                raise ValueError(f"{s} 存在重复/乱序 dt，拒绝回放")
        return [(r["dt"], r["_symbol"], r) for _, r in all_df.iterrows()]

    def _maybe_new_trading_day(self, dt: pd.Timestamp) -> None:
        d = dt.normalize()
        if self._last_date is not None and d != self._last_date:
            self.book.on_new_trading_day()     # T+1 释放（日历驱动，D09 复用同一语义）
        self._last_date = d

    def _log(self, msg: str) -> None:
        self._logs.append(f"{self.clock:%Y-%m-%d %H:%M} {msg}")

    def _next_order_id(self) -> str:
        self._seq += 1
        return f"BT{self._seq:06d}"

    def _submit(self, side: Side, symbol: str, volume: int,
                price: float | None) -> Order:
        """委托提交（回测简化版闸口：资金与 T+1 可用预检；正式 RiskGate 在 D16）。"""
        if volume <= 0:
            o = self._mk_order(side, symbol, 0, 0.0, OrderType.LIMIT)
            o.status = OrderStatus.REJECTED
            o.message = "volume 必须 > 0"
            self._orders.append(o)
            return o
        if side == Side.SELL and volume > self.book.available(symbol):
            o = self._mk_order(side, symbol, volume, price or 0.0, OrderType.LIMIT)
            o.status = OrderStatus.REJECTED
            o.message = f"T+1 可用不足（available={self.book.available(symbol)}）"
            self._orders.append(o)
            self._log(f"SELL {symbol} {volume} 被拒：{o.message}")
            return o
        otype = OrderType.LIMIT if price is not None else OrderType.MARKET
        o = self._mk_order(side, symbol, volume, price or 0.0, otype)
        self._pending.append(_PendingOrder(o))
        self._orders.append(o)
        return o

    def _mk_order(self, side: Side, symbol: str, volume: int, price: float,
                  otype: OrderType) -> Order:
        return Order(order_id=self._next_order_id(), symbol=symbol, side=side,
                     price=price, volume=volume, order_type=otype,
                     strategy_id=self.strategy.name, status=OrderStatus.SUBMITTED,
                     created_at=now_ms(), updated_at=now_ms())

    def _match_pending(self, bar: Bar) -> None:
        """撮合待撮合队列（仅同标的）；TTL 到期未成交部分撤单。

        买入资金不足时按可负担量缩量成交（成交量向下取整到整手，
        连一手都买不起则整单拒绝）——回测简化口径，正式 RiskGate 在 D16。
        """
        still: list[_PendingOrder] = []
        for po in self._pending:
            o = po.order
            if o.symbol != bar.symbol:
                still.append(po)
                continue
            po.attempts += 1
            r = match_on_bar(o, bar, self.match_cfg)
            if r.filled:
                fill_vol = r.filled_volume
                side = o.side.value if isinstance(o.side, Side) else str(o.side)
                if side == Side.BUY.value:
                    fill_vol = self._affordable_volume(r.fill_price, fill_vol)
                if fill_vol <= 0:
                    o.status = OrderStatus.REJECTED
                    o.message = (o.message or "") + "资金不足拒绝"
                    self._log(f"{o.symbol} 委托 {o.order_id} 资金不足拒绝")
                    continue
                if fill_vol < r.filled_volume:
                    # 资金不足：订单有效量对齐到可负担量（消息留痕），剩余意图作废
                    o = replace(o, volume=o.filled_volume + fill_vol,
                                message=(o.message or "")
                                + f"资金不足缩量至 {o.filled_volume + fill_vol} 股；")
                    r = match_on_bar(o, bar, self.match_cfg)
                    if not r.filled:           # 理论不达：缩量后重撞失败则保留原结果防御
                        still.append(po)
                        continue
                for t in r.trades:
                    self.cash += self.book.apply_trade(t, fee=r.fee)
                    self._trades.append(t)
                o = apply_fill(o, r)
                po.order = o
                self._sync_order(o)            # apply_fill 纯函数：订单簿同步新快照
                self._log(f"{o.side} {o.symbol} 成交 {r.filled_volume}@{r.fill_price}"
                          f" fee={r.fee:.2f}")
            if o.filled_volume >= o.volume:
                continue                       # 完全成交，出队
            if po.attempts >= max(self.cfg.order_ttl_bars, 1):
                o.status = OrderStatus.CANCELLED
                o.message = (o.message or "") + f"{po.attempts} 根 bar 未成交撤单"
                self._log(f"{o.symbol} 委托 {o.order_id} 未成交撤单")
            else:
                still.append(po)
        self._pending = [p for p in still if p.order.status
                         in (OrderStatus.SUBMITTED, OrderStatus.PART_FILLED)]

    def _sync_order(self, o: Order) -> None:
        """apply_fill 返回新快照（纯函数），把订单簿中的同单替换为新对象。"""
        self._orders = [o if x.order_id == o.order_id else x for x in self._orders]

    def _affordable_volume(self, price: float, desired: int) -> int:
        """现金可负担的最大成交量（含费用，整手取整）——共用实现见 matching。"""
        return affordable_volume(self.cash, price, desired, self.match_cfg.fee)

    def _equity_row(self, dt: pd.Timestamp) -> dict:
        market_value = self.book.market_value(self._last_close)
        equity = self.cash + market_value
        return {"dt": dt, "equity": round(equity, 2), "cash": round(self.cash, 2),
                "market_value": round(market_value, 2)}
