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

# 财务（AKShare 免账号可用）：首次取数几秒，日内不会变，所以按标的+表种缓存 5 分钟
FIN_REPORTS = ("indicator", "income", "balance", "cashflow")
FIN_PERIODS = 8
_FIN_TTL_S = 300.0
# 表种是谁给的、什么口径，必须写在大白话里——同一界面出现四种表，混淆一次就是一次错判
FIN_LABELS = {
    "indicator": "财务摘要（东财口径：每股/盈利/成长/风险等比率与常用金额）",
    "income": "利润表（新浪口径：科目逐项，金额为元）",
    "balance": "资产负债表（新浪口径：科目逐项，金额为元）",
    "cashflow": "现金流量表（新浪口径：科目逐项，金额为元）",
}


class MarketDataService:
    def __init__(self, store: ParquetStore | None = None, bus: EventBus | None = None,
                 db_path=None) -> None:
        self.store = store or ParquetStore()
        self.bus = bus
        self.db_path = db_path
        self._latest: dict[str, Tick] = {}
        self._last_pub: dict[str, float] = {}
        self._fin: dict[str, tuple[float, dict]] = {}
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

    def snapshot(self, symbol: str | None = None) -> dict | list | None:
        with self._lock:
            if symbol is None:
                return [self._tick_json(t) for s, t in sorted(self._latest.items())]
            t = self._latest.get(symbol)
        # 无行情返回 None（JSON null）——空对象会让前端把"无数据"误判为有效快照
        return self._tick_json(t) if t else None

    def snapshot_or_readonly(self, symbol: str) -> dict:
        """实时泵没有这只票的 tick 时，用只读盯盘源的盘口补一次。

        实时泵只推它订阅过的标的（策略池），行情面板选中的票常常不在里面。过去这里直接回
        null，界面永远写着"等待实时源接入"——而本机早就有免账号的公开盘口源在用。

        补的方式分两档：缓存里已经有就直接给；没有且**正在开盘**才外呼取一次。
        这个接口是 1s 轮询的自动路径，休市中外呼等于通宵打公开接口（宪法第 11 条③）。
        """
        hit = self.snapshot(symbol)
        if hit is not None:
            return {**hit, "source": "realtime_pump"}
        from quantdesk.services.watch import get_watch_service

        watch = get_watch_service()
        try:
            row = watch.cached_row(symbol)
            if not row and watch.session_live():
                row = watch.live_row(symbol)
        except Exception as e:  # noqa: BLE001 — 补盘口失败要让界面知道原因，不是静默空
            return {"symbol": symbol, "last": None, "bids": [], "asks": [],
                    "source": "none", "reason": f"只读行情源取快照失败：{e}"}
        if not row or row.get("last") is None:
            return {"symbol": symbol, "last": None, "bids": [], "asks": [],
                    "source": "none",
                    "reason": ("当前休市中，本机还没有该标的的行情快照（休市期间不自动取数）；"
                               "在实时盯盘页点「立即刷新」可强制取一次最新盘口")}
        ts = row.get("ts_ms") or row.get("fetched_at") or 0
        return {"symbol": row.get("symbol", symbol),
                "dt": pd.Timestamp(int(ts), unit="ms").isoformat() if ts else None,
                "last": row.get("last"), "open": row.get("open"),
                "high": row.get("high"), "low": row.get("low"),
                "volume": row.get("volume"), "amount": row.get("amount"),
                "bids": [list(b) for b in (row.get("bids") or [])],
                "asks": [list(a) for a in (row.get("asks") or [])],
                "prev_close": row.get("prev_close"),
                "stale": bool(row.get("stale")),
                "source": "tx_quote", "reason": ""}

    # ---------- 财务三表（免费源已可用，界面此前没有任何入口） ----------
    def financial(self, symbol: str, report_type: str = "indicator") -> dict:
        """财务报表：源返回"指标 × 报告期"宽表，这里转成前端好用的分组结构。

        三条口径纪律：
        * 缺失就是 null，不补 0（财报缺期很常见，补 0 会把"没披露"说成"零利润"）；
        * 首次外呼几秒，同标的同表种缓存 5 分钟——财务数据日内不变，
          而这是用户点一次就发一次请求的界面，不缓存等于把公开接口当本地库撞；
        * 源不可用如实抛，由路由换成可读的 503，不返回空表装成"这家公司没数据"。
        """
        import re
        import time as _t

        from quantdesk.core.symbols import normalize

        if report_type not in FIN_REPORTS:
            raise ValueError(f"report_type 只支持 {'/'.join(FIN_REPORTS)}，收到 {report_type}")
        sym = normalize(symbol)
        key = f"{sym}:{report_type}"
        now = _t.monotonic()
        with self._lock:
            hit = self._fin.get(key)
        if hit and hit[0] > now:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

        from quantdesk.services.channels import get_hub

        feed = get_hub().federated()
        df = feed.get_financial_data(sym, report_type=report_type)
        if df is None or df.empty:
            raise RuntimeError(f"{sym} 的 {report_type} 未返回任何行（源侧无数据或未就绪）")

        periods = sorted((c for c in df.columns if re.fullmatch(r"\d{8}", str(c))),
                         reverse=True)[:FIN_PERIODS]
        groups: list[dict] = []
        seen: dict[str, dict] = {}
        for _, row in df.iterrows():
            gname = str(row.get("选项") or "其他")
            item = {"indicator": str(row.get("指标") or ""),
                    "values": {p: (None if pd.isna(row[p]) else float(row[p])) for p in periods}}
            if gname not in seen:
                seen[gname] = {"name": gname, "items": []}
                groups.append(seen[gname])
            seen[gname]["items"].append(item)

        out = {"symbol": sym, "report_type": report_type, "periods": periods,
               "groups": groups,
               "source": getattr(feed, "last_financial_source", "") or "federated",
               "cached": False, "rows": int(len(df)),
               "note": (f"{FIN_LABELS[report_type]}；null=该期未披露（不是 0）。"
                        f"共 {len(df)} 项，界面展示最近 {len(periods)} 期")}
        with self._lock:
            self._fin[key] = (now + _FIN_TTL_S, out)
        return out

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
