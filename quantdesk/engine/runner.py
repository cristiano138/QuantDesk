"""策略子进程装载与 IPC 版上下文（D11）。

- multiprocessing（Windows spawn）：策略文件必须可独立 import（模块顶层不要有副作用）；
  子进程从文件路径动态装载策略类；
- IPCStrategyContext：策略只看得见 ctx——
  history 读子进程内缓存（订阅流累积，返回与回测版同列的 DataFrame）；
  buy/sell 经 PUSH 上行订单意图；get_position 读最近一次 POSITION 广播缓存；
  log 上行为 LOG 意图。
- 适配器实例永不进入策略进程（宪法第 6 条）：本模块只传递 URL 字符串。
"""

from __future__ import annotations

import importlib.util
import multiprocessing
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from quantdesk.core.enums import Frequency
from quantdesk.core.models import Bar, Order, Position, Tick, Trade
from quantdesk.engine.strategy_base import BaseStrategy, StrategyContext
from quantdesk.ipc.bridge import BridgeClient
from quantdesk.store.parquet_store import HISTORY_COLUMNS

__all__ = ["load_strategy_from_file", "IPCStrategyContext", "spawn_strategy_process"]


# ---------- 策略装载 ----------

def load_strategy_from_file(path: str, params: dict | None = None) -> BaseStrategy:
    """从策略文件路径装载 BaseStrategy 实例（动态 importlib，隔离命名）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"策略文件不存在: {path}")
    mod_name = f"quantdesk_user_strategy_{p.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, p)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    for obj in vars(module).values():
        if (isinstance(obj, type) and issubclass(obj, BaseStrategy)
                and obj is not BaseStrategy
                and getattr(obj, "__module__", "") == mod_name):
            return obj(params=dict(params or {}))
    raise ValueError(f"{path} 中未找到 BaseStrategy 子类")


# ---------- Bar/Tick ↔ dict ----------

def bar_to_dict(b: Bar) -> dict:
    # freq 必须走 .value：str(Frequency.D1) 会得到 "Frequency.D1"，
    # 子进程 bar_from_dict 拿到的周期就成了脏值（回测/实盘同构在 IPC 层断裂）
    return {"symbol": b.symbol,
            "freq": b.freq.value if hasattr(b.freq, "value") else str(b.freq),
            "dt": b.dt.isoformat(),
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume, "amount": b.amount}


def bar_from_dict(d: dict) -> Bar:
    return Bar(symbol=d["symbol"], freq=d.get("freq", "1d"),
               dt=datetime.fromisoformat(d["dt"]), open=d["open"], high=d["high"],
               low=d["low"], close=d["close"], volume=d["volume"], amount=d["amount"])


def tick_from_dict(d: dict) -> Tick:
    return Tick(symbol=d["symbol"], dt=datetime.fromisoformat(d["dt"]), last=d["last"],
                open=d.get("open", d["last"]), high=d.get("high", d["last"]),
                low=d.get("low", d["last"]), volume=d.get("volume", 0.0),
                amount=d.get("amount", 0.0),
                bids=[tuple(x) for x in d.get("bids", [])],
                asks=[tuple(x) for x in d.get("asks", [])])


def order_to_dict(o) -> dict:
    return {
        "order_id": o.order_id, "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "price": o.price, "volume": o.volume,
        "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "filled_volume": o.filled_volume, "avg_filled_price": o.avg_filled_price,
        "message": o.message, "strategy_id": o.strategy_id,
        "created_at": o.created_at, "updated_at": o.updated_at,
    }


def order_from_dict(d: dict) -> Order:
    from quantdesk.core.enums import OrderStatus, OrderType, Side

    return Order(order_id=d["order_id"], symbol=d["symbol"], side=Side(d["side"]),
                 price=d["price"], volume=d["volume"], order_type=OrderType(d["order_type"]),
                 status=OrderStatus(d["status"]), filled_volume=d.get("filled_volume", 0),
                 avg_filled_price=d.get("avg_filled_price", 0.0),
                 message=d.get("message", ""), strategy_id=d.get("strategy_id", ""),
                 created_at=d.get("created_at", 0), updated_at=d.get("updated_at", 0))


def trade_from_dict(d: dict) -> Trade:
    from quantdesk.core.enums import Side

    return Trade(trade_id=d["trade_id"], order_id=d["order_id"], symbol=d["symbol"],
                 side=Side(d["side"]), price=d["price"], volume=d["volume"],
                 amount=d["amount"], traded_at=d["traded_at"])


# ---------- IPC 版上下文 ----------

class IPCStrategyContext(StrategyContext):
    """策略子进程的 ctx：缓存订阅流 + 意图上行（接口与回测版一致）。"""

    def __init__(self, client: BridgeClient, strategy_id: str,
                 history_maxlen: int = 2000) -> None:
        self._client = client
        self._strategy_id = strategy_id
        self._strategy: BaseStrategy | None = None
        self._bars: dict[str, deque] = {}
        self._ticks: dict[str, Tick] = {}
        self._positions: list[dict] = []
        self._history_maxlen = history_maxlen

    # 事件回调（子进程收循环调用；runner 持有本对象）
    def on_event(self, tag: str, data: dict) -> bool | None:
        """返回 False 表示收到停止控制指令。"""
        if tag == "bar":
            bar = bar_from_dict(data)
            buf = self._bars.setdefault(bar.symbol, deque(maxlen=self._history_maxlen))
            # 按 dt upsert：实时下行推的是「形成中的当前 bar」，同一根会被多次更新；
            # 直接 append 会把历史缓冲刷满重复行，均线类策略算出来的就是错的。
            if buf and buf[-1].dt == bar.dt:
                buf[-1] = bar
            else:
                buf.append(bar)
            if self._strategy is not None:
                self._strategy.on_bar(bar)
        elif tag == "tick":
            tick = tick_from_dict(data)
            self._ticks[tick.symbol] = tick
            if self._strategy is not None:
                self._strategy.on_tick(tick)
        elif tag == "position":
            self._positions = data.get("positions", [])
        elif tag == "order":
            if self._strategy is not None:
                self._strategy.on_order(order_from_dict(data))
        elif tag == "trade":
            if self._strategy is not None:
                self._strategy.on_trade(trade_from_dict(data))
        elif tag == "control":
            if data.get("action") == "stop":
                return False
        return None

    # ---------- ctx API ----------
    def subscribe(self, symbols: list[str]) -> None:
        self._client.send("subscribe", {"symbols": list(symbols)})

    def history(self, symbol: str, freq: Frequency | str = Frequency.D1,
                bars: int = 120):
        """子进程内缓存的历史（订阅流累积），结构与回测版一致（HISTORY_COLUMNS）。

        pandas 懒加载：不调用 history 的策略子进程启动更快（导入期缩短）。
        """
        import pandas as pd

        buf = self._bars.get(symbol)
        if not buf:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        rows = [bar_to_dict(b) for b in list(buf)[-bars:]]
        return pd.DataFrame(rows, columns=HISTORY_COLUMNS)

    def buy(self, symbol: str, volume: int, price: float | None = None) -> dict:
        return self._submit("buy", symbol, volume, price)

    def sell(self, symbol: str, volume: int, price: float | None = None) -> dict:
        return self._submit("sell", symbol, volume, price)

    def _submit(self, side: str, symbol: str, volume: int, price: float | None) -> dict:
        intent = {"symbol": symbol, "side": side, "volume": int(volume),
                  "price": price, "order_type": "limit" if price is not None else "market",
                  "strategy_id": self._strategy_id, "ts": time.time()}
        self._client.send("order_intent", intent)
        return intent

    def get_position(self, symbol: str) -> Position | None:
        for p in self._positions:
            if p.get("symbol") == symbol:
                return Position(
                    symbol=p["symbol"], volume=int(p.get("volume", 0)),
                    available=int(p.get("available", 0)),
                    avg_cost=float(p.get("avg_cost", 0.0)),
                    market_value=float(p.get("market_value", 0.0)),
                    strategy_id=p.get("strategy_id", self._strategy_id),
                )
        return None

    def log(self, message: str) -> None:
        self._client.send("log", {"message": message, "ts": time.time()})

    def now(self) -> datetime:
        return datetime.now()


# ---------- 子进程入口 ----------

def _child_main(strategy_path: str, params: dict, strategy_id: str,
                event_url: str, order_url: str, heartbeat_interval: float) -> None:
    """策略子进程主函数（spawn target）。"""
    strategy = load_strategy_from_file(strategy_path, params)
    client = BridgeClient(strategy_id=strategy_id, heartbeat_interval=heartbeat_interval)
    client.connect(event_url, order_url)
    ctx = IPCStrategyContext(client, strategy_id)
    ctx._strategy = strategy    # noqa: SLF001 — 回调目标装配（仅子进程内部）
    strategy.bind(ctx)          # type: ignore[arg-type]
    strategy.on_start()
    client.send("ready", {"strategy_id": strategy_id})
    try:
        client.run_loop(ctx.on_event)
    finally:
        try:
            strategy.on_stop()
        finally:
            client.close()


def spawn_strategy_process(strategy_path: str, params: dict, strategy_id: str,
                           event_url: str, order_url: str,
                           heartbeat_interval: float = 5.0) -> multiprocessing.Process:
    """spawn 策略子进程（Windows 安全：target 为模块级函数，参数全可 pickle）。"""
    p = multiprocessing.Process(
        target=_child_main,
        args=(strategy_path, params, strategy_id, event_url, order_url,
              heartbeat_interval),
        name=f"strategy-{strategy_id}",
        daemon=True,
    )
    p.start()
    return p
