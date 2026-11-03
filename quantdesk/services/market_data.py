"""行情数据服务（D18）：历史读取（Parquet 直读，切周期不外呼）+ tick 快照缓存与节流广播。"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pandas as pd

from quantdesk.core.enums import AdjustType, Frequency
from quantdesk.core.events import Event, EventType, EventBus
from quantdesk.core.models import Tick
from quantdesk.store import repo
from quantdesk.store.parquet_store import ParquetStore

__all__ = ["MarketDataService", "get_market_service"]

_TICK_BROADCAST_INTERVAL = 0.25     # WS 广播节流：每标的 ≥250ms（≈4/s），合并取最新


class MarketDataService:
    def __init__(self, store: ParquetStore | None = None, bus: EventBus | None = None,
                 db_path=None) -> None:
        self.store = store or ParquetStore()
        self.bus = bus
        self.db_path = db_path
        self._latest: dict[str, Tick] = {}
        self._last_pub: dict[str, float] = {}
        self._lock = threading.RLock()

    # ---------- 历史（宪法：切周期走 Parquet 缓存，不重复外呼） ----------
    def history(self, symbol: str, freq: str = "1d",
                start: str | None = None, end: str | None = None,
                adjust: str = "forward") -> pd.DataFrame:
        f = Frequency(freq)
        a = AdjustType(adjust)
        return self.store.read(symbol, f, start=start, end=end, adjust=a)

    def freqs_available(self, symbol: str) -> list[str]:
        out = []
        for f in Frequency:
            rng = self.store.range_of(symbol, f, AdjustType.FORWARD)
            if rng is not None:
                out.append(f.value)
        return out

    # ---------- 快照 ----------
    def on_tick(self, tick: Tick) -> None:
        """实时源回调入口（D14 接入后由运行时调用）：更新缓存 + 节流广播。"""
        with self._lock:
            self._latest[tick.symbol] = tick
            now = time.monotonic()
            if now - self._last_pub.get(tick.symbol, 0.0) < _TICK_BROADCAST_INTERVAL:
                return                     # 节流窗口内合并（丢弃旧、保最新）
            self._last_pub[tick.symbol] = now
        self._publish(tick)

    def snapshot(self, symbol: str | None = None) -> dict | list:
        with self._lock:
            if symbol is None:
                return [self._tick_json(t) for s, t in sorted(self._latest.items())]
            t = self._latest.get(symbol)
        return self._tick_json(t) if t else {}

    # ---------- 自选股 ----------
    def watchlist(self) -> list[dict]:
        return [
            {"symbol": r.symbol, "name": r.name, "note": r.note}
            for r in repo.watchlist_list(db_path=self.db_path)
        ]

    def watchlist_add(self, symbol: str, name: str = "", note: str = "") -> None:
        repo.watchlist_add(symbol, name=name, note=note, db_path=self.db_path)

    def watchlist_remove(self, symbol: str) -> bool:
        return repo.watchlist_remove(symbol, db_path=self.db_path)

    # ---------- 内部 ----------
    def _publish(self, tick: Tick) -> None:
        if self.bus is None:
            return
        self.bus.publish(Event(EventType.TICK, data=self._tick_json(tick), source="market"))

    @staticmethod
    def _tick_json(t: Tick) -> dict:
        return {
            "symbol": t.symbol, "dt": t.dt.isoformat(), "last": t.last,
            "open": t.open, "high": t.high, "low": t.low,
            "volume": t.volume, "amount": t.amount,
            "bids": [list(b) for b in t.bids], "asks": [list(a) for a in t.asks],
        }


_service: MarketDataService | None = None
_service_lock = threading.Lock()


def get_market_service() -> MarketDataService:
    global _service
    with _service_lock:
        if _service is None:
            _service = MarketDataService()
        return _service
