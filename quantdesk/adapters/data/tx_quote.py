"""腾讯财经公开行情接口（A股 + 港股）——五档盘口、快照、K 线。

定位：只读看盘主源。它一次 HTTP 请求可同时返回**多只**标的的全量五档与内外盘，
适合盯盘池的高频轮询（港股同样支持，且与东财口径可互相校验）。

口径与依据（2026-11-04 本机实测，非推断）：
- A股：`成交量` 单位「手」（与 AKShare 日线一致）；`成交额` 字段单位「万元」；
  `外盘/内盘` = 主动买入/主动卖出累计量（手），实测与东财个股快照 f49 **逐位相等**
  （600000 当日 458432 手，内盘 365654 = 总量 − 外盘，两源一致）。
- 港股：`成交量/成交额` 单位「股 / 港元」（港股无统一整手概念，不做换算）；
  内外盘字段接口给 0（不提供），主动买卖量改用东财逐笔聚合，见 services/watch.py。
- 收盘后 A股五档仍保留挂单量（实测 600000 收盘后 bid/ask 各 5 档非空），港股给 0——
  两者都不代表断线，新鲜度看 `quote_time` 而不是看档位空不空。

非官方接口声明：腾讯公开行情网页所用的 JSON/文本接口，**无文档、无 SLA、可能随时变更**。
字段布局按市场分表（_TX_LAYOUT_A / _TX_LAYOUT_HK），改口径只改这两张表，不动逻辑。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from quantdesk.adapters.base.datafeed import DataFeedAdapter
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import AdapterError, ConnectionLostError
from quantdesk.core.models import Bar, Tick
from quantdesk.core.ratelimit import RateLimiter
from quantdesk.core.symbols import from_internal, normalize
from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

_NAME = "tx_quote"
_QUOTES_URL = "https://qt.gtimg.cn/q="
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://gu.qq.com/"}
_MAX_BATCH = 60                 # 单次请求最多带多少只（接口能更多，收敛一点防被裁包）
_DEFAULT_INTERVAL = 0.25

# 腾讯行情串按 ~ 切分后的字段下标（0 基，已用真实返回钉死；None=该市场不提供/未核对）
_TX_LAYOUT_A = {
    "name": 1, "code": 2, "last": 3, "prev_close": 4, "open": 5,
    "volume": 6, "outer": 7, "inner": 8,
    "bid": 9, "ask": 19, "ts": 30, "high": 33, "low": 34,
    "amount": 37, "amount_unit": 10000.0,       # 万元 → 元
    "total_mv": 44, "mv_unit": 1e8, "float_mv": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "avg_price": 51,
}
_TX_LAYOUT_HK = {
    "name": 1, "code": 2, "last": 3, "prev_close": 4, "open": 5,
    "volume": 6, "outer": None, "inner": None,
    "bid": 9, "ask": 19, "ts": 30, "chg": 31, "pct": 32,
    "high": 33, "low": 34, "amount": 37, "amount_unit": 1.0,   # 已是「元（港元）」
    "total_mv": 44, "mv_unit": 1e8, "float_mv": 45,
    "wk52_high": 48, "wk52_low": 49,
}
_FREQ_PARAM = {Frequency.D1: "day", Frequency.W1: "week", Frequency.MO1: "month"}
_ADJUST_PARAM = {AdjustType.NONE: "", AdjustType.FORWARD: "qfq", AdjustType.BACKWARD: "hfq"}

# 科创板（含 CDR）在腾讯串里给的是「股」，其余 A股给「手」——2026-11-05 跨源核对
# 122 只实测：主板/创业/北交 与东财「手」逐位相等，688/689 一律差 100 倍。
# 换算判据用成交额反推：成交额 ÷ (量×100) 落在 [最低, 最高] 里才是手，否则是股。
_KECHUANG_PREFIX = ("688", "689")


def _lot_divisor(code6: str, is_hk: bool) -> float:
    """把原始成交量换算成「手」的除数（港股不用手，返回 1）。"""
    if is_hk:
        return 1.0
    return 100.0 if code6.startswith(_KECHUANG_PREFIX) else 1.0


def _f(parts: list[str], idx: int | None, default: float = 0.0) -> float:
    """按下标取数值；缺失/空串/非数字一律回默认（接口偶尔给空位）。"""
    if idx is None or idx >= len(parts):
        return default
    raw = parts[idx].strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return default


def _levels(parts: list[str], start: int, n: int = 5) -> list[tuple[float, float]]:
    """五档：(价, 量)。量为 0 的档位丢弃（收盘后整档为空属正常）。"""
    out: list[tuple[float, float]] = []
    for i in range(n):
        p, v = _f(parts, start + i * 2), _f(parts, start + i * 2 + 1)
        if p > 0 and v > 0:
            out.append((p, v))
    return out


class TencentQuoteFeed(DataFeedAdapter):
    """腾讯公开行情：批量五档快照 + K 线（A股 / 港股）。"""

    name = _NAME
    label = "腾讯行情（A股/港股，只读，非官方软件）"
    prereq = ("无需账号与权限，联网即可使用。这是腾讯行情页所用的公开数据，"
              "字段可能随对方调整而变化。")
    official = False                     # 公开网页接口，非交易所官方授权 → UI 红字标注
    requires: tuple[str, ...] = ()       # httpx 已是主依赖，不是可选 SDK
    default_enabled = True
    enable_flag = ""
    capabilities = frozenset({FeedCapability.REALTIME_TICK, FeedCapability.HISTORY_BAR})

    def __init__(self, *, bus: Any = None, min_interval: float = _DEFAULT_INTERVAL,
                 timeout: float = 8.0, event_bus: Any = None) -> None:
        self.bus = bus or event_bus
        self.timeout = float(timeout)
        self._rl = RateLimiter(min_interval)
        self._client: httpx.Client | None = None
        self._connected = False
        self._lock = threading.RLock()
        self._subs: set[str] = set()
        self._callback: Callable[[Tick | Bar], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._poll_interval = 3.0
        self._last_ok_at = 0.0
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "rows": 0}

    # ---------- 连接 ----------
    def connect(self) -> None:
        with self._lock:
            if self._connected and self._client is not None:
                return
            self._client = httpx.Client(headers=_HEADERS, timeout=self.timeout,
                                        limits=httpx.Limits(max_connections=8))
            if not self._probe():
                self._client.close()
                self._client = None
                raise ConnectionLostError(
                    "腾讯行情通道不可用：无法从 qt.gtimg.cn 取到样例快照。"
                    "检查本机出站网络/代理/防火墙（需访问 https://qt.gtimg.cn）。")
            self._connected = True
            log.info("腾讯行情通道已连接（A股/港股，只读）")

    def _probe(self) -> bool:
        try:
            return bool(self._raw_quotes(["sz000001"]))
        except Exception as e:  # noqa: BLE001 探活失败一律按不可用
            log.warning("腾讯行情探活失败: %s", e)
            return False

    def disconnect(self) -> None:
        self.stop_polling()
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                finally:
                    self._client = None
            self._connected = False
            self._subs.clear()
            self._callback = None

    def health_check(self) -> bool:
        if not self._connected or self._client is None:
            return False
        try:
            return bool(self._raw_quotes(["sz000001"]))
        except Exception:  # noqa: BLE001
            return False

    # ---------- HTTP ----------
    def _get(self, url: str, *, params: dict | None = None, decode: str | None = None) -> str:
        """一次外呼，最多重试 1 次（宪法：重试风暴归上游路由管）。"""
        client = self._client
        if client is None:
            raise ConnectionLostError("腾讯行情通道未连接，请先 connect()")
        self._rl.acquire()
        last_err: Exception | None = None
        for attempt in (0, 1):
            try:
                self.stats["requests"] += 1
                if attempt:
                    self.stats["retries"] += 1
                r = client.get(url, params=params)
                r.raise_for_status()
                text = r.content.decode(decode, "ignore") if decode else r.text
                self._last_ok_at = time.time()
                return text
            except httpx.HTTPError as e:
                last_err = e
                self.stats["errors"] += 1
                if attempt == 0:
                    time.sleep(0.3)
        raise ConnectionLostError(f"腾讯行情外呼失败: {last_err.__class__.__name__}: {last_err}")

    def _raw_quotes(self, tx_codes: list[str]) -> dict[str, list[str]]:
        """批量取行情串，返回 {腾讯代码: 字段列表}。"""
        out: dict[str, list[str]] = {}
        for i in range(0, len(tx_codes), _MAX_BATCH):
            chunk = tx_codes[i:i + _MAX_BATCH]
            text = self._get(_QUOTES_URL + ",".join(chunk), decode="gbk")
            for line in text.splitlines():
                if '="' not in line:
                    continue
                key = line.split("=", 1)[0].strip()          # v_sh600000
                body = line.split('="', 1)[1].strip().strip('";')
                code = key[2:] if key.startswith("v_") else key
                parts = body.split("~")
                if len(parts) > 30 and parts[2].strip():
                    out[code] = parts
                    self.stats["rows"] += 1
        return out

    # ---------- 归一 ----------
    @staticmethod
    def _tx_code(symbol: str) -> str:
        return from_internal(normalize(symbol), "tx")

    def _parse(self, tx_code: str, parts: list[str]) -> dict:
        is_hk = tx_code.startswith("hk")
        layout = _TX_LAYOUT_HK if is_hk else _TX_LAYOUT_A
        code6 = parts[layout["code"]].strip() if len(parts) > layout["code"] else ""
        div = _lot_divisor(code6, is_hk)
        last = _f(parts, layout["last"])
        prev = _f(parts, layout["prev_close"])
        vol = _f(parts, layout["volume"]) / div          # 统一成：A股=手，港股=股
        amount = _f(parts, layout["amount"]) * layout.get("amount_unit", 1.0)
        outer = (_f(parts, layout["outer"]) / div) if layout.get("outer") is not None else None
        inner = (_f(parts, layout["inner"]) / div) if layout.get("inner") is not None else None
        ts_raw = parts[layout["ts"]] if layout.get("ts") is not None and \
            layout["ts"] < len(parts) else ""
        return {
            "symbol": (f"{parts[2].strip().zfill(5)}.HK" if is_hk
                       else normalize(parts[2].strip())),
            "tx_code": tx_code,
            "name": parts[layout["name"]].strip() if len(parts) > layout["name"] else "",
            "last": last, "prev_close": prev, "open": _f(parts, layout["open"]),
            "high": _f(parts, layout["high"]), "low": _f(parts, layout["low"]),
            "volume": vol,                                  # 统一口径：A股=手，港股=股
            "volume_unit": "share" if is_hk else "lot",
            "raw_volume_unit": "share" if (is_hk or div > 1.0) else "lot",
            "amount": amount,                               # 元 / 港元
            "outer_volume": outer, "inner_volume": inner,   # 主动买/主动卖（None=该市场不提供）
            "bids": _levels(parts, layout["bid"], 5),
            "asks": _levels(parts, layout["ask"], 5),
            "total_mv": _f(parts, layout.get("total_mv")) * layout.get("mv_unit", 1.0),
            "float_mv": _f(parts, layout.get("float_mv")) * layout.get("mv_unit", 1.0),
            # 腾讯对无涨跌停（港股/退市标的）给 -1 占位——不能把 -1 当涨停价输出
            "limit_up": (lambda v: v if v > 0 else None)(_f(parts, layout.get("limit_up"))),
            "limit_down": (lambda v: v if v > 0 else None)(_f(parts, layout.get("limit_down"))),
            "avg_price": _f(parts, layout.get("avg_price")) or None,
            "pct_chg": (last / prev - 1.0) * 100 if prev > 0 and last > 0 else None,
            "quote_time": ts_raw.strip(),
            "ts_ms": now_ms(),
        }

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """批量快照（盯盘池一次拿全）。键为内部代码，取不到/解析失败的标的直接缺席。"""
        if not symbols:
            return {}
        wanted = {self._tx_code(s): normalize(s) for s in symbols}
        raw = self._raw_quotes(list(wanted))
        out: dict[str, dict] = {}
        for tx_code, parts in raw.items():
            internal = wanted.get(tx_code)
            if internal is None:
                continue
            try:
                row = self._parse(tx_code, parts)
            except Exception as e:  # noqa: BLE001 单标的解析失败不影响整批
                log.warning("腾讯快照解析失败 %s: %s", tx_code, e)
                continue
            row["symbol"] = internal
            out[internal] = row
        return out

    def to_tick(self, row: dict) -> Tick:
        return Tick(symbol=row["symbol"], dt=pd.Timestamp(row["ts_ms"], unit="ms").to_pydatetime(),
                    last=row["last"], open=row["open"], high=row["high"], low=row["low"],
                    volume=row["volume"], amount=row["amount"],
                    bids=row["bids"], asks=row["asks"])

    # ---------- 历史 K 线 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        """日/周/月 K（腾讯按 count 返回，这里按区间估算条数并截断到 [start,end]）。

        注意：该接口不返回成交额，`amount` 统一填 0.0——需要金额的口径请用 akshare/tushare。
        """
        if freq not in _FREQ_PARAM:
            raise AdapterError(f"腾讯通道不提供 {freq} 周期（仅 1d/1w/1M）；"
                               "分钟级请用实时订阅或 akshare")
        s_days = max((pd.Timestamp(end) - pd.Timestamp(start)).days, 1)
        count = min(max(int(s_days * 1.6) + 10, 10), 800)
        params = {"param": f"{self._tx_code(symbol)},{_FREQ_PARAM[freq]},"
                           f"{start:%Y-%m-%d},{end:%Y-%m-%d},{count},"
                           f"{_ADJUST_PARAM.get(adjust, '')}"}
        data = self._get(_KLINE_URL, params=params).strip()
        import json

        try:
            payload = json.loads(data)
        except ValueError as e:
            raise AdapterError(f"腾讯 K 线返回不是合法 JSON: {e}") from e
        node = (payload.get("data") or {}).get(self._tx_code(symbol)) or {}
        key = f"{_ADJUST_PARAM.get(adjust, '')}{_FREQ_PARAM[freq]}"
        rows = node.get(key) or node.get(_FREQ_PARAM[freq]) or []
        recs = []
        for r in rows:
            if len(r) < 6:
                continue
            recs.append({"dt": pd.Timestamp(r[0]), "open": float(r[1]), "close": float(r[2]),
                         "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                         "amount": 0.0})
        df = pd.DataFrame(recs, columns=["dt", "open", "high", "low", "close", "volume", "amount"])
        if df.empty:
            return df
        df = df[(df["dt"] >= pd.Timestamp(start)) & (df["dt"] <= pd.Timestamp(end))]
        return df.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)

    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        raise AdapterError(
            f"腾讯行情通道不提供财务数据（capabilities 不含 FINANCIAL）：symbol={symbol}；"
            "财务口径请用 akshare / ths_ifind / tushare")

    # ---------- 实时订阅（轮询实现：接口无推送能力） ----------
    def subscribe_realtime(self, symbols: list[str],
                           callback: Callable[[Tick | Bar], None]) -> None:
        with self._lock:
            self._subs |= {normalize(s) for s in symbols}
            self._callback = callback
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._poll_loop,
                                                name="tx-quote-poll", daemon=True)
                self._thread.start()

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        with self._lock:
            self._subs -= {normalize(s) for s in symbols}
            if not self._subs:
                self.stop_polling()

    def stop_polling(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                syms = sorted(self._subs)
                cb = self._callback
            if not syms or cb is None:
                self._stop.wait(1.0)
                continue
            try:
                snap = self.get_snapshot(syms)
            except ConnectionLostError as e:
                log.warning("腾讯轮询失败（下个周期重试）: %s", e)
                self._stop.wait(self._poll_interval)
                continue
            for row in snap.values():
                try:
                    cb(self.to_tick(row))
                except Exception as e:  # noqa: BLE001 单标的回调异常不影响整批
                    log.error("腾讯快照回调异常 %s: %s", row.get("symbol"), e)
            self._stop.wait(self._poll_interval)

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_interval = max(0.5, float(seconds))

    # ---------- 观测 ----------
    def describe(self) -> dict:
        return {"name": self.name, "connected": self._connected,
                "last_ok_at": self._last_ok_at, "stats": dict(self.stats),
                "subscribed": len(self._subs), "poll_interval": self._poll_interval,
                "official": False,
                "note": "公开网页接口，无文档无 SLA；港股不提供内外盘（主动买卖量走东财逐笔）"}


ADAPTER = TencentQuoteFeed
