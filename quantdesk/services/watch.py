"""盯盘服务：自选池实时行情 + 当日主动买卖量（A股 / 港股，只读）。

为什么存在
----------
产品诉求变了：用户不交易，只想**每天开盘就能看到真实实时动态**，并且要看懂——
所以这里同时负责三件事：

1. **取数**：腾讯批量快照（五档 + 内外盘，一次请求覆盖整池）+ 东财逐笔/分时/个股字段；
2. **算当日买卖量**：主口径用官方内外盘（两源实测逐位相等），逐笔只做**明细与分层**；
   方向码 `1/2` 谁是买谁是卖**不硬编码**——每天用官方内外盘自动校准并留痕，接口改了会自己发现；
3. **说人话**：给前端的一切实带 `unit`/`source`/`caveat`，让小白也能知道这个数字是哪儿来的。

口径纪律（写死在这里，别在页面里解释）
--------------------------------------
- A股量单位=手（1 手=100 股），港股量单位=股；金额单位 A股=元、港股=港元。
- 逐笔合计可能 **大于** 官方总量（含盘后成交），因此买卖量以官方内外盘为准，
  逐笔差异原样上报 `tick_vs_official_pct`，不做"抹平"处理。
- 大单分层按**单笔成交额**（东财口径）：小 <4 万，中 4~20 万，大 20~100 万，特大 ≥100 万。
- 收盘后五档为 0、逐笔停在最后时间戳属正常，`session` 字段会明说现在是哪个时段。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any

import pandas as pd

from quantdesk.core.errors import ConnectionLostError, QuantDeskError
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS
from quantdesk.core.symbols import board_of, normalize
from quantdesk.core.timeutil import now_ms
from quantdesk.store import repo

log = logging.getLogger(__name__)

__all__ = ["WatchService", "get_watch_service", "session_of"]

# 分层阈值：单笔成交额（元 / 港元），与东财「小/中/大/特大」口径对齐
_BUCKET_EDGES = (40_000.0, 200_000.0, 1_000_000.0)
_BUCKET_NAMES = ("small", "medium", "big", "huge")

_FLOW_TTL_S = 20.0          # 单标的逐笔重算的最小间隔（details 是重请求，别按 3s 打）
# 一次取全日逐笔的条数上限：买卖量与明细分页共用这一份，A股全日约 4~5 千笔，港股更多些
_TICK_FETCH_COUNT = 6000

# 各市场交易时段窗（本地时区）：(状态, 起点, 终点, 说明)；"HH:MM" 按字符串比较，一律补全到分钟
_CN_WINDOWS = [("pre_open", "09:15", "09:30", "集合竞价（9:15-9:25 可撤单，9:20-9:25 不可撤单）"),
               ("open", "09:30", "11:30", "盘中（上午连续竞价）"),
               ("lunch", "11:30", "13:00", "午间休市"),
               ("open", "13:00", "15:00", "盘中（下午连续竞价）"),
               ("post_close", "15:00", "15:59", "已收盘（15:00-15:30 盘后定价、大宗交易仍可能成交）")]
_HK_WINDOWS = [("pre_open", "09:00", "09:30", "港股开市前时段（竞价）"),
               ("open", "09:30", "12:00", "盘中（上午）"),
               ("lunch", "12:00", "13:00", "午间休市"),
               ("open", "13:00", "16:00", "盘中（下午）"),
               ("post_close", "16:00", "16:59", "已收盘（收市竞价约至 16:10，数据随后定格）")]
_WINDOWS = {"CN": _CN_WINDOWS, "HK": _HK_WINDOWS}


# 各源把量单位写成 lot/share（机器码），盯盘接口对外统一成中文单位 + 保留原码：
# "详细数据都能看"的前提是单位不会让人猜——猜错一次就差 100 倍。
_UNIT_CN = {"lot": "手", "share": "股", "手": "手", "股": "股"}


def _normalize_unit(row: dict) -> dict:
    raw = row.get("volume_unit")
    if raw:
        row["volume_unit_code"] = raw
        row["volume_unit"] = _UNIT_CN.get(str(raw), str(raw))
    return row


@dataclass
class FlowResult:
    """当日主动买卖量结果（含口径与差异说明，前端照抄即可，不在页面里二次解释）。"""

    symbol: str
    name: str = ""
    market: str = ""
    trade_date: int = 0
    unit: str = ""
    currency: str = ""
    last: float = 0.0
    pct_chg: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    outer_volume: float | None = None
    inner_volume: float | None = None
    outer_pct: float | None = None                 # 主动买入占比（%）
    tick_buy_volume: float = 0.0
    tick_sell_volume: float = 0.0
    tick_neutral_volume: float = 0.0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    net_amount: float = 0.0
    buckets: dict[str, dict[str, float]] = field(default_factory=dict)
    minutes: list[dict] = field(default_factory=list)
    side_map: dict[str, str] = field(default_factory=dict)
    calibrated: bool = False
    tick_vs_official_pct: float | None = None      # 逐笔合计相对官方总量的偏差
    flow_source: str = ""
    caveat: str = ""
    ticks_total: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["side_map"] = self.side_map
        return d


def _in_range(hhmm: str, start: str, end: str) -> bool:
    return start <= hhmm < end


def session_of(market: str, now: datetime | None = None) -> dict:
    """当前处于哪个交易时段（只认本地时间；节假日在 trading_day 字段里另说）。"""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    weekend = now.weekday() >= 5
    windows = _WINDOWS.get(market, _WINDOWS["CN"])
    state, note = "closed", "未开盘"
    for st, lo, hi, desc in windows:
        if lo <= hhmm < hi:
            state, note = st, desc
            break
    if weekend:
        state, note = "weekend", "周末休市"
    return {"market": market, "state": state, "note": note, "local_time": now.strftime("%H:%M:%S"),
            "weekday": now.weekday(), "weekend": weekend}


# 默认方向码约定（东财 push2 历史上一贯如此：2=外盘/主动买，1=内盘/主动卖，4=中性）。
# 只在校准不成立时兜底用，并且一定会带上 calibrated=False，界面据此标注"未校准"。
_DEFAULT_SIDE_MAP = {"1": "sell", "2": "buy", "4": "neutral"}


def calibrate_side_map(df: pd.DataFrame, outer: float | None, inner: float | None,
                       total: float) -> tuple[dict[str, str], bool]:
    """用官方内外盘反推逐笔方向码语义，返回 (映射, 是否校准成功)。

    方向码是会变的（不同接口版本 1/2 谁买谁卖不一样），硬编码等于赌。
    这里对两种候选映射算误差，取小的那个；误差大到离谱就当没校准。
    没有官方口径可对时，退回默认约定并把 calibrated 置 False——给一个标注过
    "未经确认"的可用分层，比给一堆 0 更有用，也比假装校准过更诚实。
    """
    if df.empty:
        return (dict(_DEFAULT_SIDE_MAP), False)
    if outer is None or inner is None or total <= 0:
        return (dict(_DEFAULT_SIDE_MAP), False)
    by_code = df.groupby("side_code")["volume"].sum().to_dict()
    codes = sorted(c for c in by_code if c in ("1", "2"))
    if len(codes) < 2:
        return ({**{c: "unknown" for c in codes}, "4": "neutral"}, False)
    a, b = codes[0], codes[1]
    cand_a = {a: "buy", b: "sell", "4": "neutral"}
    cand_b = {a: "sell", b: "buy", "4": "neutral"}

    def err(m: dict[str, str]) -> float:
        buy = sum(v for c, v in by_code.items() if m.get(c) == "buy")
        sell = sum(v for c, v in by_code.items() if m.get(c) == "sell")
        return abs(buy - outer) + abs(sell - inner)

    ea, eb = err(cand_a), err(cand_b)
    chosen, ok = (cand_a, ea <= eb) if ea <= eb else (cand_b, True)
    best, worst = min(ea, eb), max(ea, eb)
    # 两个候选误差几乎一样＝区分不出来（例如买卖量本来接近），此时不声称校准
    confident = worst > 0 and best / max(worst, 1.0) < 0.6 and best / total < 0.08
    return chosen, bool(ok and confident)


def aggregate_flow(df: pd.DataFrame, side_map: dict[str, str]) -> dict:
    """按校准后的方向码汇总买/卖量、买/卖额与大小单分层。"""
    if df.empty:
        return {}
    side = df["side_code"].map(lambda c: side_map.get(c, "unknown"))
    out: dict[str, float] = {
        "tick_buy_volume": float(df.loc[side == "buy", "volume"].sum()),
        "tick_sell_volume": float(df.loc[side == "sell", "volume"].sum()),
        "tick_neutral_volume": float(df.loc[side == "neutral", "volume"].sum()),
        "buy_amount": float(df.loc[side == "buy", "amount"].sum()),
        "sell_amount": float(df.loc[side == "sell", "amount"].sum()),
    }
    buckets: dict[str, dict[str, float]] = {}
    for i, name in enumerate(_BUCKET_NAMES):
        lo = _BUCKET_EDGES[i - 1] if i else 0.0
        hi = _BUCKET_EDGES[i] if i < len(_BUCKET_EDGES) else float("inf")
        mask = (df["amount"] >= lo) & (df["amount"] < hi)
        buckets[name] = {
            "buy_volume": float(df.loc[mask & (side == "buy"), "volume"].sum()),
            "sell_volume": float(df.loc[mask & (side == "sell"), "volume"].sum()),
            "buy_amount": float(df.loc[mask & (side == "buy"), "amount"].sum()),
            "sell_amount": float(df.loc[mask & (side == "sell"), "amount"].sum()),
            "ticks": int(mask.sum()),
        }
    out["buckets"] = buckets
    return out


def minute_series(df: pd.DataFrame, side_map: dict[str, str]) -> list[dict]:
    """分钟级买/卖量（盯盘图的柱状来源）。"""
    if df.empty:
        return []
    side = df["side_code"].map(lambda c: side_map.get(c, "unknown"))
    tmp = df[["dt", "volume"]].copy()
    tmp["side"] = side.values
    tmp["min"] = pd.to_datetime(tmp["dt"]).dt.floor("min")
    rows = []
    for minute, g in tmp.groupby("min", sort=True):
        rows.append({"dt": f"{minute:%H:%M}",
                     "buy": float(g.loc[g["side"] == "buy", "volume"].sum()),
                     "sell": float(g.loc[g["side"] == "sell", "volume"].sum()),
                     "neutral": float(g.loc[g["side"] == "neutral", "volume"].sum())})
    return rows


class WatchService:
    """盯盘中枢：池管理 + 实时快照 + 当日买卖量 + 收盘定格。"""

    def __init__(self, bus: EventBus | None = None, db_path=None,
                 tx_feed: Any = None, em_feed: Any = None) -> None:
        self.bus = bus or SYSTEM_BUS
        self.db_path = db_path
        self._tx = tx_feed
        self._em = em_feed
        self._lock = threading.RLock()
        self._live: dict[str, dict] = {}          # symbol → 最新快照
        self._flow: dict[str, FlowResult] = {}    # symbol → 最近一次买卖量
        self._flow_at: dict[str, float] = {}
        self._tick_cache: dict[str, tuple[float, pd.DataFrame]] = {}   # symbol → (时刻, 逐笔表)
        self._detail_cache: dict[str, tuple[float, dict]] = {}         # symbol → (时刻, 详情)
        self._calibration: dict[str, tuple[dict, bool]] = {}   # (side_map, calibrated)
        self._calibration_date = 0
        self._pool: list[str] = []
        self.stats = {"snapshots": 0, "flow_calls": 0, "errors": 0, "stale": 0}
        self.last_refresh_at = 0
        self.last_error = ""

    # ---------- 通道（惰性取，未就绪就如实报缺） ----------
    def _feeds(self) -> tuple[Any, Any]:
        with self._lock:
            if self._tx is None or self._em is None:
                from quantdesk.services.channels import get_hub

                hub = get_hub()
                self._tx = self._tx or hub.feed("tx_quote")
                self._em = self._em or hub.feed("em_quote")
            return self._tx, self._em

    def feed_status(self) -> dict:
        tx, em = self._feeds()
        return {"tx_quote": {"ready": tx is not None,
                             "connected": bool(getattr(tx, "_connected", False))},
                "em_quote": {"ready": em is not None,
                             "connected": bool(getattr(em, "_connected", False))}}

    def history_feed(self) -> Any:
        """给 API 用的历史 K 线通道（腾讯源覆盖 A股+港股；不暴露内部装配对象）。"""
        return self._feeds()[0]

    # ---------- 池 ----------
    def pool(self) -> list[str]:
        with self._lock:
            if self._pool:
                return list(self._pool)
        rows = repo.watchlist_list(db_path=self.db_path)
        out: list[str] = []
        for r in rows:
            try:
                out.append(normalize(r.symbol))
            except QuantDeskError:
                log.warning("自选股里有无法识别的代码，盯盘跳过: %s", r.symbol)
        if not out:
            # 空池就不外呼——盯盘页打开必须先是"有东西在看"，而不是"请先添加自选"
            from quantdesk.config import get_settings

            csv = str(getattr(get_settings(), "watch_default_symbols", "") or "")
            out = [normalize(s) for s in csv.split(",") if s.strip()]
            log.info("自选股为空，盯盘使用默认池 %d 只（A股 4 + 港股 4）", len(out))
        with self._lock:
            self._pool = out
        return list(out)

    def set_pool(self, symbols: list[str]) -> list[str]:
        normed = [normalize(s) for s in symbols]
        with self._lock:
            self._pool = normed
        return list(normed)

    def add(self, symbol: str, name: str = "") -> dict:
        internal = normalize(symbol)
        tx, _ = self._feeds()
        shown = name
        if not shown and tx is not None:
            try:
                shown = tx.get_snapshot([internal]).get(internal, {}).get("name", "")
            except QuantDeskError as e:
                log.info("加自选时取名称失败（不阻塞添加）: %s", e)
        repo.watchlist_add(internal, name=shown, note="盯盘", db_path=self.db_path)
        with self._lock:
            if internal not in self._pool:
                self._pool.append(internal)
        return {"symbol": internal, "name": shown, "board": board_of(internal).value}

    def remove(self, symbol: str) -> bool:
        internal = normalize(symbol)
        ok = repo.watchlist_remove(internal, db_path=self.db_path)
        with self._lock:
            self._pool = [s for s in self._pool if s != internal]
            self._live.pop(internal, None)
            self._flow.pop(internal, None)
        return ok

    # ---------- 实时快照 ----------
    def _decorate_row(self, row: dict, em) -> dict:
        """给一行快照补上盯盘要的派生字段，并在腾讯不给内外盘时（港股）用东财官方值补齐。

        这段口径逻辑只写一遍：整池刷新与单标的按需取数（`_ensure_live`）都走这里，
        否则两条路径会各自演化出不同的"主口径"。
        """
        row["stale"] = False
        row["fetched_at"] = now_ms()
        _normalize_unit(row)
        if em is not None and row.get("outer_volume") is None:
            # 港股腾讯不给内外盘 → 用东财个股快照补（f49 官方累计主动买量）
            row["outer_probed"] = True          # 探过了，别每次调用都再补一轮
            try:
                q = em.get_quote(row.get("symbol", ""))
                row["outer_volume"] = q.get("outer_volume")
                row["inner_volume"] = q.get("inner_volume")
                row["turnover_rate"] = q.get("turnover_rate")
                row["avg_price"] = q.get("avg_price")
                row["flow_extra_source"] = "em_quote"
            except QuantDeskError as e:
                log.info("东财补字段失败 %s: %s", row.get("symbol"), e)
        return row

    def _ensure_live(self, sym: str) -> dict:
        """取一只标的的快照，缓存里没有（或没探过内外盘）就当场外呼一次。

        买卖量的官方口径必须在调用点取到：冷启动的进程、不在池里的标的、
        详情先于列表加载——过去这里直接读空 dict，主口径就静默退化成逐笔聚合，
        而界面上仍写着"主口径=官方内外盘"。
        """
        with self._lock:
            row = self._live.get(sym)
        if row is not None and (row.get("outer_volume") is not None or row.get("outer_probed")):
            return row
        tx, em = self._feeds()
        if tx is None:
            return row or {}
        try:
            fresh = tx.get_snapshot([sym]).get(sym)
        except QuantDeskError as e:
            log.info("单标的快照失败 %s: %s", sym, e)
            return row or {}
        if not fresh:
            return row or {}
        fresh = self._decorate_row(fresh, em)
        with self._lock:
            self._live[sym] = fresh
            self.stats["snapshots"] += 1
        return fresh

    def refresh(self, symbols: list[str] | None = None, *, publish: bool = True) -> list[dict]:
        """拉一整池快照（腾讯一次请求），并入 live 缓存。失败保留旧值并标记 stale。"""
        syms = symbols or self.pool()
        if not syms:
            return []
        tx, em = self._feeds()
        if tx is None:
            self.last_error = "腾讯行情通道未就绪：盯盘池无法刷新"
            self.stats["errors"] += 1
            return self._stale_copy(syms)
        try:
            snap = tx.get_snapshot(syms)
        except QuantDeskError as e:
            self.last_error = f"快照刷新失败: {e}"
            self.stats["errors"] += 1
            self.stats["stale"] += 1
            return self._stale_copy(syms)
        now = now_ms()
        rows: list[dict] = [self._decorate_row(row, em) for row in snap.values()]
        with self._lock:
            self._live.update({r["symbol"]: r for r in rows})
            self.stats["snapshots"] += len(rows)
            self.last_refresh_at = now
            self.last_error = ""
        if publish:
            self._publish("watch_update", {"count": len(rows),
                                           "symbols": [r["symbol"] for r in rows]})
        return rows

    def _stale_copy(self, syms: list[str]) -> list[dict]:
        """断线兜底：把缓存里的旧快照标成 stale 后返回（盯盘页要能看到"最后已知值"）。"""
        with self._lock:
            out = []
            for s in syms:
                row = self._live.get(s)
                if row is None:
                    continue
                row = {**row, "stale": True}
                self._live[s] = row
                out.append(row)
            return out

    def refresh_auto(self, symbols: list[str] | None = None, *, force: bool = False) -> dict:
        """带时段保护的刷新：休市时不重复外呼，但第一次进来一定要给得出数据。

        盯盘页是开着不动的：没这道闸，用户通宵挂着就是每 5 秒打一次公开接口，
        本机 IP 会被反爬冷却——自动盯盘的前提是"只在盘中自动"。
        但收盘后打开页面也得看到当日真实数字，所以缓存为空时先种一次（此后不再外呼）。
        """
        sessions = {"CN": session_of("CN"), "HK": session_of("HK")}
        live_now = any(s["state"] in ("open", "pre_open", "post_close") for s in sessions.values())
        syms = symbols or self.pool()
        with self._lock:
            seeded = bool(self._live)
        if force or live_now or not seeded:
            rows = self.refresh(syms)
            return {"rows": rows, "polled": True, "sessions": sessions,
                    "reason": "" if (force or live_now) else "首次进入：取一次当日快照，此后休市不再外呼"}
        with self._lock:
            cached = [self._live[s] for s in syms if s in self._live]
        return {"rows": cached, "polled": False, "sessions": sessions,
                "reason": "两个市场均休市，返回当日最后一次快照（未外呼）"}

    def live(self, symbol: str | None = None) -> Any:
        with self._lock:
            if symbol:
                return self._live.get(normalize(symbol))
            return dict(self._live)

    def list_rows(self) -> list[dict]:
        """盯盘列表：快照 + 买卖量摘要（买卖占比、净额），按成交额降序。"""
        out = []
        for sym in self.pool():
            row = self._live.get(sym) or {}
            flow = self._flow.get(sym)
            out.append({
                "symbol": sym, "name": row.get("name", ""),
                "market": (sym.split(".")[1] if "." in sym else ""),
                "last": row.get("last", 0.0), "pct_chg": row.get("pct_chg"),
                "volume": row.get("volume"), "volume_unit": row.get("volume_unit"),
                "amount": row.get("amount"),
                "outer_volume": row.get("outer_volume"), "inner_volume": row.get("inner_volume"),
                "outer_pct": round(row["outer_volume"] / row["volume"] * 100, 2)
                             if row.get("outer_volume") and row.get("volume") else None,
                "bids": len(row.get("bids") or []), "asks": len(row.get("asks") or []),
                "stale": row.get("stale", not bool(row)),
                "net_amount": flow.net_amount if flow else None,
                "flow_source": flow.flow_source if flow else None,
                "fetched_at": row.get("fetched_at"),
            })
        return sorted(out, key=lambda r: (r.get("amount") or 0), reverse=True)

    # ---------- 当日买卖量 ----------
    def flow(self, symbol: str, *, force: bool = False) -> FlowResult:
        sym = normalize(symbol)
        with self._lock:
            cached = self._flow.get(sym)
            age = time.monotonic() - self._flow_at.get(sym, 0.0)
        if cached is not None and not force:
            # 盘中按 TTL 重算；休市时当日已定格，放宽到一小时——页面一直开着也不重复取逐笔
            limit = _FLOW_TTL_S if self.session_live() else 3600.0
            if age <= limit:
                return cached
        res = self._compute_flow(sym, force=force)
        with self._lock:
            self._flow[sym] = res
            self._flow_at[sym] = time.monotonic()
        self._publish("flow_update", {"symbol": sym, "outer_pct": res.outer_pct,
                                      "source": res.flow_source})
        return res

    def _ensure_calibration(self, df: pd.DataFrame, outer: float | None, inner: float | None,
                            total: float) -> tuple[dict[str, str], bool]:
        """方向码映射：当天校准成功一次就复用；没校准成功不写缓存（换个标的可能就成了）。

        把"这次没校准上"的结果缓存一整天，等于第一个盘前时段取到半截数据的失败
        污染后面全天的分层——所以要的是可信结论，不是任意结论。
        """
        today = int(datetime.now().strftime("%Y%m%d"))
        with self._lock:
            if self._calibration_date != today:
                self._calibration = {}
                self._calibration_date = today
            cached = self._calibration.get("global")
        if cached:
            return cached
        result = calibrate_side_map(df, outer, inner, total)
        if result[1]:
            with self._lock:
                self._calibration["global"] = result
            log.info("逐笔方向码校准结果: %s", result[0])
        else:
            log.warning("逐笔方向码未能与官方内外盘对齐（误差过大或数据不足），"
                        "本轮按默认约定分层并标注未校准: %s", result[0])
        return result

    def _transactions(self, sym: str, count: int, *, force: bool = False) -> pd.DataFrame:
        """逐笔取数带缓存：盘中 TTL 20s，休市放宽到 15 分钟。

        同一天同一份逐笔，详情区每刷一次就重取 5000 笔是最容易把本机打进冷却的动作；
        分页、筛买卖、算分层都该复用同一次取数。
        """
        limit = _FLOW_TTL_S if self.session_live() else 900.0
        with self._lock:
            hit = self._tick_cache.get(sym)
        if hit is not None and not force and time.monotonic() - hit[0] <= limit:
            return hit[1]
        _, em = self._feeds()
        if em is None:
            raise ConnectionLostError("东财逐笔通道未就绪")
        df = em.get_transactions(sym, count=count)
        with self._lock:
            self._tick_cache[sym] = (time.monotonic(), df)
        return df

    def _compute_flow(self, sym: str, *, force: bool = False) -> FlowResult:
        tx, em = self._feeds()
        today = int(datetime.now().strftime("%Y%m%d"))
        live = self._ensure_live(sym)
        res = FlowResult(symbol=sym, trade_date=today, name=live.get("name", ""),
                         market=sym.split(".")[1], unit="股" if sym.endswith(".HK") else "手",
                         currency="HKD" if sym.endswith(".HK") else "CNY")
        res.last = live.get("last", 0.0)
        res.pct_chg = live.get("pct_chg") or 0.0
        res.volume = live.get("volume", 0.0)
        res.amount = live.get("amount", 0.0)
        res.outer_volume = live.get("outer_volume")
        res.inner_volume = live.get("inner_volume")
        if res.outer_volume and res.volume:
            res.outer_pct = round(res.outer_volume / res.volume * 100, 2)
            res.flow_source = "official_outer_inner"
            # 港股的内外盘只有东财一个源（腾讯港股接口不给这两个字段），
            # 口径必须说清楚是单源，不能借 A股那句"两源实测一致"给自己抬精度
            res.caveat = ("买卖量主口径=官方外盘/内盘（A股两源实测逐位一致：腾讯 outer 与东财 f49）"
                          if not sym.endswith(".HK") else
                          "买卖量主口径=外盘/内盘（港股取东财 f49 单源：腾讯港股接口不给内外盘）")
        else:
            res.flow_source = "quote_only"
            res.caveat = "本标的暂未取到官方内外盘，买卖量仅由逐笔聚合，含盘后差异"

        if em is None:
            res.caveat += "；东财逐笔通道未就绪，无明细分层"
            res.updated_at = now_ms()
            return res

        try:
            df = self._transactions(sym, 5000, force=force)
        except QuantDeskError as e:
            self.stats["errors"] += 1
            res.caveat += f"；逐笔获取失败（{type(e).__name__}），本轮只报官方量"
            res.updated_at = now_ms()
            log.info("逐笔获取失败 %s: %s", sym, e)
            return self._finalize(res, df_empty=True)

        if df.empty:
            res.caveat += "；当日无逐笔（休市或新上市）"
            return self._finalize(res, df_empty=True)

        side_map, calibrated = self._ensure_calibration(df, res.outer_volume, res.inner_volume,
                                                        res.volume)
        agg = aggregate_flow(df, side_map)
        res.side_map = side_map
        res.calibrated = calibrated
        res.ticks_total = int(len(df))
        res.tick_buy_volume = agg["tick_buy_volume"]
        res.tick_sell_volume = agg["tick_sell_volume"]
        res.tick_neutral_volume = agg["tick_neutral_volume"]
        res.buy_amount = agg["buy_amount"]
        res.sell_amount = agg["sell_amount"]
        res.net_amount = round(res.buy_amount - res.sell_amount, 2)
        res.buckets = agg["buckets"]
        res.minutes = minute_series(df, side_map)
        tick_total = res.tick_buy_volume + res.tick_sell_volume + res.tick_neutral_volume
        if res.volume:
            res.tick_vs_official_pct = round((tick_total - res.volume) / res.volume * 100, 2)
        if res.outer_pct is None and tick_total:
            res.outer_pct = round(res.tick_buy_volume / tick_total * 100, 2)
            res.flow_source = "em_tick"
            if not calibrated:
                res.caveat += "；方向码未与官方对齐，买卖分层按默认约定（2=买/1=卖）给出"
        return self._finalize(res, df_empty=False)

    def _finalize(self, res: FlowResult, *, df_empty: bool) -> FlowResult:
        res.updated_at = now_ms()
        return res

    def session_live(self) -> bool:
        """A股/港股是否处于（或临近）可成交时段——休市时任何自动路径都不该外呼。"""
        return any(session_of(m)["state"] in ("open", "pre_open", "post_close")
                   for m in ("CN", "HK"))

    def _detail_ttl(self) -> float:
        """详情缓存时长：盘中 20s（要看动），休市 15min（当日已定格，别再打接口）。"""
        return _FLOW_TTL_S if self.session_live() else 900.0

    def detail(self, symbol: str, force: bool = False) -> dict:
        """单标的全量详情：快照 + 五档 + 买卖量 + 分时 + 个股字段（"详细数据都能看"）。"""
        sym = normalize(symbol)
        with self._lock:
            hit = self._detail_cache.get(sym)
        if hit is not None and not force and (time.monotonic() - hit[0]) <= self._detail_ttl():
            return hit[1]
        if force or self.session_live():
            self.refresh([sym], publish=False)
        row = self._ensure_live(sym)      # 冷缓存（新进程/收盘后首开）也要拿到快照，
        res = self.flow(sym, force=force)     # force 要一路传到取数层，否则「立即刷新」拿到的还是缓存
        tx, em = self._feeds()
        out = {"symbol": sym, "board": board_of(sym).value, "session": session_of(
            "HK" if sym.endswith(".HK") else "CN"),
            "snapshot": {k: v for k, v in row.items() if k not in ("bids", "asks")},
            "book": {"bids": row.get("bids") or [], "asks": row.get("asks") or []},
            "flow": res.to_dict(), "feeds": self.feed_status()}
        if em is not None:
            try:
                out["quote_extra"] = em.get_quote(sym)
            except QuantDeskError as e:
                out["quote_extra"] = {}
                out["quote_extra_error"] = str(e)
            try:
                tr = em.get_intraday(sym)
                out["intraday"] = [{"dt": f"{t:%H:%M}", "price": p, "avg_price": a,
                                    "volume": v}
                                   for t, p, a, v in zip(tr["dt"], tr["price"],
                                                          tr["avg_price"], tr["volume"])] \
                    if not tr.empty else []
            except QuantDeskError as e:
                out["intraday"] = []
                out["intraday_error"] = str(e)
        else:
            out["intraday"] = []
        with self._lock:
            self._detail_cache[sym] = (time.monotonic(), out)
        return out

    def ticks(self, symbol: str, limit: int = 500, offset: int = 0,
              side: str | None = None, *, force: bool = False) -> dict:
        """逐笔明细分页（全量可翻，按校准后的方向可过滤）。"""
        sym = normalize(symbol)
        with self._lock:
            res = self._flow.get(sym)
        side_map = (res.side_map if res else {}) or {}
        try:
            # 与买卖量共用同一份缓存取数：翻页/筛选不该每次都重新打一遍全日逐笔
            df = self._transactions(sym, _TICK_FETCH_COUNT, force=force)
        except QuantDeskError as e:
            return {"rows": [], "total": 0, "error": str(e)}
        if df.empty:
            return {"rows": [], "total": 0, "side_map": side_map}
        df = df.assign(direction=df["side_code"].map(lambda c: side_map.get(c, "unknown")))
        if side in ("buy", "sell", "neutral"):
            df = df[df["direction"] == side]
        total = int(len(df))
        page = df.iloc[::-1].iloc[offset:offset + min(int(limit), 2000)]     # 最新在前
        rows = [{"time": r["time"], "price": r["price"], "volume": r["volume"],
                 "deals": r["deals"], "amount": round(r["amount"], 2),
                 "direction": r["direction"], "side_code": r["side_code"]}
                for _, r in page.iterrows()]
        return {"rows": rows, "total": total, "side_map": side_map,
                "calibrated": bool(res and res.calibrated)}

    # ---------- 收盘定格 ----------
    def freeze(self, symbols: list[str] | None = None) -> dict:
        """把当日买卖量写进 daily_flow（盯盘页与复盘都读它）。"""
        syms = [normalize(s) for s in (symbols or self.pool())]
        # 定格是"一天一次"的重操作：先把快照拉新，否则调度任务在轮询之外单独跑时
        # 会拿空缓存写出 close=0、volume=0 的空行（复盘时看着像停牌，其实是没取数）
        self.refresh(syms, publish=False)
        saved, failed = 0, []
        for sym in syms:
            try:
                res = self.flow(sym, force=True)
            except QuantDeskError as e:
                failed.append({"symbol": sym, "error": str(e)})
                continue
            if res.volume <= 0 and not res.ticks_total:
                continue                                  # 停牌/无数据日不写空行
            repo.flow_upsert({
                "trade_date": res.trade_date, "symbol": res.symbol, "name": res.name,
                "market": res.market, "close": res.last, "pct_chg": res.pct_chg,
                "volume": res.volume, "amount": res.amount,
                "outer_volume": res.outer_volume, "inner_volume": res.inner_volume,
                "tick_buy_volume": res.tick_buy_volume,
                "tick_sell_volume": res.tick_sell_volume,
                "tick_neutral_volume": res.tick_neutral_volume,
                "buy_amount": res.buy_amount, "sell_amount": res.sell_amount,
                "big_buy_volume": (res.buckets.get("big", {}).get("buy_volume", 0.0)
                                   + res.buckets.get("huge", {}).get("buy_volume", 0.0)),
                "big_sell_volume": (res.buckets.get("big", {}).get("sell_volume", 0.0)
                                    + res.buckets.get("huge", {}).get("sell_volume", 0.0)),
                "side_map": json.dumps(res.side_map, ensure_ascii=False),
                "flow_source": res.flow_source,
                "staled": bool(res.outer_volume is None and not res.ticks_total),
            }, db_path=self.db_path)
            saved += 1
        self._publish("flow_frozen", {"saved": saved, "failed": len(failed)})
        return {"saved": saved, "failed": failed, "trade_date": int(datetime.now().strftime("%Y%m%d"))}

    def history(self, symbol: str | None = None, trade_date: int | None = None,
                limit: int = 60) -> list[dict]:
        if symbol:
            return repo.flow_history(normalize(symbol), limit=limit, db_path=self.db_path)
        return repo.flow_latest(trade_date or int(datetime.now().strftime("%Y%m%d")),
                                db_path=self.db_path)

    # ---------- 状态 ----------
    def status(self) -> dict:
        tx, em = self._feeds()
        return {"pool": self.pool(), "pool_size": len(self.pool()),
                "feeds": self.feed_status(),
                "session": {"CN": session_of("CN"), "HK": session_of("HK")},
                "last_refresh_at": self.last_refresh_at, "last_error": self.last_error,
                "stats": dict(self.stats),
                "calibration": {k: v[0] for k, v in self._calibration.items()},
                "calibrated": bool(self._calibration.get("global", (None, False))[1]),
                "flow_ready": bool(tx is not None),
                "poll_interval": getattr(tx, "_poll_interval", None),
                "note": "只读看盘：本模块不含任何下单路径"}

    def _publish(self, action: str, data: dict) -> None:
        try:
            self.bus.publish(Event(EventType.LOG, data={"action": action, **data},
                                   source="watch"))
        except Exception as e:  # noqa: BLE001 观测事件失败不影响盯盘
            log.debug("盯盘事件发布失败 %s: %s", action, e)


_service: WatchService | None = None
_service_lock = threading.Lock()


def get_watch_service() -> WatchService:
    global _service
    with _service_lock:
        if _service is None:
            _service = WatchService()
        return _service


def reset_watch_service_for_test() -> None:
    global _service
    with _service_lock:
        _service = None
