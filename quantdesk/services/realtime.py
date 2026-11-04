"""实时行情泵（D14/D15 缺的最后一里）。

数据流（宪法 3.5）：主源回调 → FederatedFeed（引用计数/归一化）→ 本泵 →
① market_data 快照（前端 WS）② PaperBroker 撮合（模拟盘）③ 策略子进程下行
（tick / 形成中的 bar）④ 订单成交回报回投策略进程。

三条设计口径：
1. **bar 是「形成中的当前 bar」**：日线策略在盘中必须能看到今天的 bar 在变（与回测
   「history 含当前 bar」同语义，子进程侧按 dt upsert，不会把缓冲刷满重复行）；
2. **历史种子**：策略订阅时先把本地日线（默认 250 根）灌进子进程缓冲，
   否则 dual_ma 启动后 history() 是空的，要等够周期数才第一次决策——这会让
   「同一份策略代码跑通三种模式」在实时模式下名不副实；
3. 泵只搬运不改数：价格口径、复权、撮合规则都在各自层，这里一律透传。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS
from quantdesk.core.models import Bar, Tick
from quantdesk.engine.runner import bar_to_dict, order_to_dict, tick_from_dict
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)

__all__ = ["RealtimePump", "get_pump"]

_EMIT_MIN_INTERVAL = 1.0          # 同一 (symbol, freq) 的 bar 下行最小间隔（秒）
_SEED_BARS = 250                  # 订阅时回灌的历史根数（够均线类策略立刻决策）
_FREQ_AGG = {Frequency.M1: "%Y-%m-%d %H:%M", Frequency.D1: "%Y-%m-%d",
             Frequency.W1: "%Y-%GW%W", Frequency.MO1: "%Y-%m"}


def _bucket_key(dt: datetime, freq: Frequency) -> str:
    return dt.astimezone().strftime(_FREQ_AGG.get(freq, "%Y-%m-%d"))


def _floor_dt(dt: datetime, freq: Frequency) -> datetime:
    """bar 的时间取所属周期的起点。

    必须是「起点」而不是「最后一笔 tick 的时间」：同一根 bar 的多次更新才会带同一个
    dt，子进程才能按 dt upsert；否则每推一次就多一根 bar，历史缓冲会被同一天的
    几十根重复行刷满，均线算出来全是错的。
    """
    local = dt.astimezone() if dt.tzinfo else dt
    if freq is Frequency.D1:
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if freq is Frequency.W1:
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    if freq is Frequency.MO1:
        return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    minute = local.replace(second=0, microsecond=0)
    if freq is Frequency.M1:
        return minute
    step = {Frequency.M5: 5, Frequency.M15: 15, Frequency.M30: 30,
            Frequency.H1: 60}[freq]
    return minute.replace(minute=minute.minute // step * step)


class RealtimePump:
    def __init__(self, hub=None, market=None, trading=None, manager=None,
                 bus: EventBus | None = None, store: ParquetStore | None = None,
                 seed_bars: int = _SEED_BARS) -> None:
        self.hub = hub
        self.market = market
        self.trading = trading
        self.manager = manager
        self.bus = bus or SYSTEM_BUS
        self.store = store or ParquetStore()
        self.seed_bars = int(seed_bars)
        self._fed = None                       # 组合适配器（第一次用到时装配）
        self._symbol_strategies: dict[str, set[str]] = defaultdict(set)
        self._strategy_symbols: dict[str, set[str]] = {}
        self._strategy_freq: dict[str, Frequency] = {}
        self._watched: set[str] = set()
        self._buckets: dict[tuple[str, Frequency], dict] = {}
        self._last_emit: dict[tuple[str, Frequency], float] = {}
        self._lock = threading.RLock()
        self._sub_ids: list[int] = []
        self.ticks_seen = 0
        self.bars_emitted = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """接实时源 + 订阅回报事件。无实时源时只挂回报桥（不报错，状态如实上报）。"""
        if not self._sub_ids:
            self._sub_ids = [
                self.bus.subscribe(EventType.ORDER, self._on_order_event),
                self.bus.subscribe(EventType.TRADE, self._on_trade_event),
            ]
        for sym in self._watchlist_symbols():
            self.watch([sym])
        return self

    def stop(self) -> None:
        for sub_id in self._sub_ids:
            self.bus.unsubscribe(sub_id)
        self._sub_ids = []
        fed = self._federated()
        if fed is not None:
            fed.unsubscribe_realtime(sorted(self._watched), subscriber="panel")
        self._watched.clear()

    def _on_order_event(self, event: Event) -> None:
        """委托回报回投策略进程（order_id 携带 strategy_id 归属）。"""
        o = event.data
        sid = getattr(o, "strategy_id", "") or ""
        sup = self._supervisor(sid) if sid else None
        if sup is None:
            return
        sup.deliver("order", order_to_dict(o))

    def _on_trade_event(self, event: Event) -> None:
        t = event.data
        oid = getattr(t, "order_id", "")
        order = None
        if self.trading is not None:
            order = self.trading.get_order(oid)
        sid = getattr(order, "strategy_id", "") if order is not None else ""
        sup = self._supervisor(sid) if sid else None
        if sup is None:
            return
        sup.deliver("trade", {"trade_id": t.trade_id, "order_id": oid,
                              "symbol": t.symbol,
                              "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                              "price": t.price, "volume": t.volume, "amount": t.amount,
                              "traded_at": t.traded_at})

    # ---------- 订阅登记 ----------
    def _federated(self):
        if self.hub is None:
            return None
        if self._fed is None:
            self._fed = self.hub.federated()
        return self._fed

    def _watchlist_symbols(self) -> list[str]:
        if self.market is None:
            return []
        try:
            return [w["symbol"] for w in self.market.watchlist()]
        except Exception as e:  # noqa: BLE001 面板初始化失败不影响交易链路
            log.warning("读取自选股失败: %s", e)
            return []

    def watch(self, symbols: list[str], subscriber: str = "panel") -> None:
        """行情面板/快照需求：订阅但不投策略。"""
        fed = self._federated()
        if fed is None or not symbols:
            return
        with self._lock:
            new = [s for s in symbols if s not in self._watched]
            if not new:
                return
            self._watched |= set(new)
        try:
            fed.subscribe_realtime(new, self._on_item, subscriber=subscriber)
        except Exception as e:  # noqa: BLE001 无实时源时如实记录（面板继续用历史数据）
            log.info("实时订阅未就绪（%s）: %s", subscriber, e)
            with self._lock:
                self._watched -= set(new)

    def register_strategy(self, strategy_id: str, symbols: list[str],
                          freq: str | Frequency = Frequency.D1) -> None:
        """策略订阅：登记扇出表 + 向主源订阅 + 回灌历史种子。"""
        f = Frequency(freq) if not isinstance(freq, Frequency) else freq
        with self._lock:
            self._strategy_symbols.setdefault(strategy_id, set()).update(symbols)
            self._strategy_freq[strategy_id] = f
            for s in symbols:
                self._symbol_strategies[s].add(strategy_id)
        self.watch(symbols, subscriber=f"strategy:{strategy_id}")
        self._seed_history(strategy_id, symbols, f)

    def unregister_strategy(self, strategy_id: str) -> None:
        """策略停止/崩溃：注销扇出表与主源引用（计数归零才真退订）。"""
        with self._lock:
            symbols = self._strategy_symbols.pop(strategy_id, set())
            self._strategy_freq.pop(strategy_id, None)
            for s in symbols:
                subs = self._symbol_strategies.get(s)
                if subs:
                    subs.discard(strategy_id)
                    if not subs:
                        self._symbol_strategies.pop(s, None)
        fed = self._federated()
        if fed is not None and symbols:
            fed.drop_subscriber(f"strategy:{strategy_id}")

    def _seed_history(self, strategy_id: str, symbols: list[str], freq: Frequency) -> None:
        """把本地日线尾部若干根灌进子进程缓冲，策略启动即可决策。"""
        sup = self._supervisor(strategy_id)
        if sup is None:
            return
        for sym in symbols:
            try:
                df = self.store.read(sym, Frequency.D1, adjust=AdjustType.FORWARD)
            except Exception as e:  # noqa: BLE001 无本地数据不阻断启动
                log.info("历史种子读取失败 %s: %s", sym, e)
                continue
            if df.empty:
                continue
            tail = df.tail(self.seed_bars)
            for row in tail.to_dict("records"):
                bar = Bar(symbol=sym, freq=freq, dt=row["dt"].to_pydatetime(),
                          open=float(row["open"]), high=float(row["high"]),
                          low=float(row["low"]), close=float(row["close"]),
                          volume=float(row["volume"]), amount=float(row["amount"]))
                sup.deliver("bar", bar_to_dict(bar))

    # ---------- 行情入口 ----------
    def _on_item(self, item) -> None:
        """主源回调 → 快照 / 撮合 / 聚合下行。绝不在此阻塞。"""
        if isinstance(item, Tick):
            self.ticks_seen += 1
            if self.market is not None:
                self.market.on_tick(item)
            if self.trading is not None and self._should_match_paper():
                self.trading.paper.on_tick(item)      # 内部自带锁与撮合
            self._aggregate(item)
        elif isinstance(item, Bar):
            self._fanout_bar(item)

    def _should_match_paper(self) -> bool:
        from quantdesk.config import get_settings

        return bool(get_settings().simulation_only)

    def aggregate_tick(self, tick: Tick) -> None:
        """显式注入 tick（测试与手工回放入口，与主源回调同一路径）。"""
        self._on_item(tick)

    def _aggregate(self, tick: Tick) -> None:
        freqs = set(self._strategy_freq.values())
        if not freqs:
            return
        for f in freqs:
            key = (tick.symbol, f)
            bucket_id = _bucket_key(tick.dt, f)
            cur = self._buckets.get(key)
            if cur is None or cur["id"] != bucket_id:
                if cur is not None:
                    self._emit(key, self._bar_of(key, cur))     # 上一根收尾
                self._buckets[key] = {"id": bucket_id, "dt": _floor_dt(tick.dt, f),
                                      "open": tick.last, "high": tick.last,
                                      "low": tick.last, "close": tick.last,
                                      "volume": max(tick.volume, 0.0),
                                      "amount": max(tick.amount, 0.0)}
                self._emit(key, self._bar_of(key, self._buckets[key]))
                continue
            prev_vol = cur["volume"]
            cur.update(close=tick.last,
                       high=max(cur["high"], tick.last), low=min(cur["low"], tick.last))
            cur["volume"] = max(cur["volume"], tick.volume)
            cur["amount"] = max(cur["amount"], tick.amount)
            if cur["open"] == 0 and prev_vol == 0:
                cur["open"] = tick.last
            self._emit(key, self._bar_of(key, cur), throttled=True)

    def _bar_of(self, key: tuple[str, Frequency], cur: dict) -> Bar:
        return Bar(symbol=key[0], freq=key[1], dt=cur["dt"], open=cur["open"],
                   high=cur["high"], low=cur["low"], close=cur["close"],
                   volume=cur["volume"], amount=cur["amount"])

    def _emit(self, key, bar: Bar, throttled: bool = False) -> None:
        now = time.monotonic()
        if throttled and now - self._last_emit.get(key, 0.0) < _EMIT_MIN_INTERVAL:
            return
        self._last_emit[key] = now
        self._fanout_bar(bar)

    def _fanout_bar(self, bar: Bar) -> None:
        """bar 下行给订了该标的的策略进程 + 广播到事件总线（节流后，量可控）。"""
        with self._lock:
            targets = set(self._symbol_strategies.get(bar.symbol, ()))
        for sid in targets:
            sup = self._supervisor(sid)
            if sup is not None and self._strategy_freq.get(sid) == bar.freq:
                sup.deliver("bar", bar_to_dict(bar))
        self.bars_emitted += 1
        self.bus.publish(Event(EventType.BAR, data=bar_to_dict(bar), source="realtime"))

    # ---------- 观测 ----------
    def _supervisor(self, strategy_id: str):
        if self.manager is None:
            return None
        getter = getattr(self.manager, "supervisor", None)
        return getter(strategy_id) if callable(getter) else None

    def status(self) -> dict:
        return {"watched": sorted(self._watched), "ticks_seen": self.ticks_seen,
                "bars_emitted": self.bars_emitted,
                "strategies": {sid: {"symbols": sorted(syms),
                                     "freq": f.value if hasattr(f, "value") else str(f)}
                               for sid, syms in self._strategy_symbols.items()},
                "realtime_source": (self.hub.realtime_channel_name() if self.hub else "")}


_pump: RealtimePump | None = None
_pump_lock = threading.Lock()


def get_pump() -> RealtimePump:
    global _pump
    with _pump_lock:
        if _pump is None:
            from quantdesk.services.channels import get_hub
            from quantdesk.services.market_data import get_market_service
            from quantdesk.services.strategy_mgr import get_strategy_manager
            from quantdesk.services.trading import get_trading_service

            manager = get_strategy_manager()
            _pump = RealtimePump(hub=get_hub(), market=get_market_service(),
                                 trading=get_trading_service(), manager=manager)
            manager.realtime = _pump          # 双向接线：策略订阅上行 → 泵扇出下行
        return _pump
