"""东方财富公开行情接口（A股 + 港股）——逐笔成交、分时曲线、个股全量快照。

与腾讯通道的分工：腾讯一次请求批量给五档与内外盘（盯盘池轮询主力）；
东财给**逐笔明细（带主动买卖方向）**、分时走势和个股扩展字段（股本/市值/PE/PB/换手/涨跌停），
港股的主动买卖量也来自这里。

口径与依据（2026-11-04 本机实测，非推断）：
- `details/get` 每行 7 字段：时间, 价格, 成交量(A股=手/港股=股), 笔数, 方向, 类型, 0；
  A股方向 `1/2/4`，**港股只有 `1/2`**（实测 00700 当日 18961 笔里无 `4`，中性笔不标注）。
  方向语义**不靠猜**：与官方内外盘对齐后自动判定（见 services/watch.py 的校准）。
  实测当日 600000.SH 逐笔合计 890953 手 > 官方总量 824086 手——逐笔含盘后成交，
  所以「当日买卖量」主口径用官方外盘/内盘，逐笔只做明细与分层，差异如实标注。
  单次返回行数没有「约 5000」这种上限：600000 全日 4654 行一次给完，
  00700 一次给 18961 行——头/尾两段并集是为覆盖而做，不是为了拼上限。
- `stock/get` 字段（fltt=2 直接给小数）：f43 最新 f44 高 f45 低 f46 开 f47 量 f48 额
  f49 外盘 f50 量比 f51/f52（**A股=涨停/跌停；港股=52周最高/最低**）f57 码 f58 名
  f60 昨收 f71 均价 f84 总股本 f85 流通股本 f92 每股净资产 f105 净利润 f116 总市值
  f117 流通市值 f162 市盈(动) f167 市净率 f168 换手率 f169 涨跌额 f170 涨跌幅
  f171 振幅 f198 所属板块码。
  上述每项都已与腾讯同一标的逐位核对（外盘 458432、均价 10.13、换手 0.25、振幅 1.78、
  涨跌停 11.10/9.08 全部相等；港股 f51/f52 的 656.7/375.2 与腾讯 52 周字段相等）。
  同一对下标在两个市场语义不同，`get_quote` 因此按市场分键输出 `limit_up/limit_down`
  与 `wk52_high/wk52_low`，不把 52 周区间冒充涨跌停。

非官方接口声明：东财行情页所用的公开 JSON 接口，无文档无 SLA。
另注意：本机 IP 被东财 push2 限流过（熔断提示需等待），所以本通道带限速与熔断退避。
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

_NAME = "em_quote"
_SNAPSHOT_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_DETAILS_URL = "https://push2.eastmoney.com/api/qt/stock/details/get"
_TRENDS_URL = "https://push2.eastmoney.com/api/qt/stock/trends2/get"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://quote.eastmoney.com/"}
_DEFAULT_INTERVAL = 0.35
_COOLDOWN_S = 60.0            # 连接被掐/超时后的退避时长（按主机分别计）
_SNAPSHOT_FIELDS = ("f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f60,f71,"
                    "f84,f85,f92,f105,f107,f116,f117,f162,f167,f168,f169,f170,f171,f198")
# 逐笔方向码（1/2 谁是买谁是卖由 services/watch.py 按官方内外盘校准后确定，此处只原样给出）
_SIDE_CODES = ("1", "2", "4")
# 东财 klt 的口径：分钟级是「分钟数」不是「秒数」（1/5/15/30/60），日周月是 101/102/103。
# 曾按秒写（900/1800/3600）——接口对不认识的 klt 直接回空表，而 klt=60 会被当成 60 分钟，
# 于是「请求 5 分钟线拿到 1 小时线」这种静默错周期只有真机外呼才能暴露。
_KLT = {Frequency.D1: "101", Frequency.W1: "102", Frequency.MO1: "103",
        Frequency.M1: "1", Frequency.M5: "5", Frequency.M15: "15",
        Frequency.M30: "30", Frequency.H1: "60"}
_FQT = {AdjustType.NONE: "0", AdjustType.FORWARD: "1", AdjustType.BACKWARD: "2"}


class EastmoneyQuoteFeed(DataFeedAdapter):
    """东财公开行情：逐笔成交 + 分时 + 个股全量快照（A股 / 港股）。"""

    name = _NAME
    label = "东方财富行情（A股/港股，只读，非官方软件）"
    prereq = ("无需账号与权限，联网即可使用。免费来源有访问频率限制，连续频繁请求会被临时限制，"
              "本来源会自动放慢并冷却。")
    official = False
    requires: tuple[str, ...] = ()
    default_enabled = True
    enable_flag = ""
    capabilities = frozenset({FeedCapability.REALTIME_TICK, FeedCapability.REALTIME_BAR,
                              FeedCapability.TRANSACTION})

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
        self._cooldown: dict[str, float] = {}      # host → 冷却截止（monotonic）
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "rate_limited": 0}

    # ---------- 连接 ----------
    def connect(self) -> None:
        with self._lock:
            if self._connected and self._client is not None:
                return
            self._client = httpx.Client(headers=_HEADERS, timeout=self.timeout,
                                        limits=httpx.Limits(max_connections=8))
            try:
                data = self._json(_SNAPSHOT_URL, {"secid": "0.000001",
                                                  "fields": "f43,f57", "invt": "2", "fltt": "2"})
            except ConnectionLostError as e:
                self._client.close()
                self._client = None
                raise ConnectionLostError(f"东财行情通道不可用：{e}") from e
            if not data or not data.get("f43"):
                self._client.close()
                self._client = None
                raise ConnectionLostError("东财行情通道不可用：探活请求返回空数据（可能被限流或接口变更）")
            self._connected = True
            log.info("东财行情通道已连接（A股/港股，只读）")

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
        if not self._connected:
            return False
        try:
            data = self._json(_SNAPSHOT_URL, {"secid": "0.000001", "fields": "f43",
                                              "invt": "2", "fltt": "2"})
            return bool(data and data.get("f43"))
        except Exception:  # noqa: BLE001
            return False

    # ---------- HTTP（按主机分别限流退避） ----------
    def _json(self, url: str, params: dict, *, data_key: bool = True) -> Any:
        """一次外呼：最多重试 1 次；只有反复失败的主机进入冷却（别把无关主机一起封锁）。

        空 data 属"这个标的没数据/接口变更"，抛 AdapterError 且不冷却——
        否则用户换一个代码就撞 60 秒墙，那是自找的难用。
        """
        client = self._client
        if client is None:
            raise ConnectionLostError("东财行情通道未连接，请先 connect()")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        left = self._cooldown.get(host, 0.0) - time.monotonic()
        if left > 0:
            raise ConnectionLostError(f"东财 {host} 限流冷却中，还需 {left:.0f}s（期间不重复请求）")
        self._rl.acquire()
        last_err: Exception | None = None
        for attempt in (0, 1):
            try:
                self.stats["requests"] += 1
                if attempt:
                    self.stats["retries"] += 1
                r = client.get(url, params=params)
                r.raise_for_status()
                payload = r.json()
                if not data_key:
                    return payload
                data = payload.get("data")
                if not data:
                    raise AdapterError("东财返回 data 为空（标的代码不存在或接口变更）")
                self._cooldown.pop(host, None)
                return data
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                self.stats["errors"] += 1
                fatal = e.__class__.__name__ in ("RemoteProtocolError", "ConnectError",
                                                 "ConnectTimeout", "ReadTimeout")
                if attempt == 0 and not fatal:
                    time.sleep(0.4)
                    continue
                if fatal:                      # 连接被掐/超时＝对端在挡，退避而不是硬打
                    self.stats["rate_limited"] += 1
                    self._cooldown[host] = time.monotonic() + _COOLDOWN_S
                break
        raise ConnectionLostError(
            f"东财行情外呼失败: {last_err.__class__.__name__}: {last_err}")

    # ---------- 快照 ----------
    @staticmethod
    def _secid(symbol: str) -> tuple[str, str]:
        internal = normalize(symbol)
        return internal, from_internal(internal, "em")

    def get_quote(self, symbol: str) -> dict:
        """个股全量快照（含外盘量、涨跌停、股本市值与财务摘要）。"""
        internal, secid = self._secid(symbol)
        d = self._json(_SNAPSHOT_URL, {"secid": secid, "fields": _SNAPSHOT_FIELDS,
                                       "invt": "2", "fltt": "2"}) or {}
        is_hk = internal.endswith(".HK")
        volume = float(d.get("f47") or 0.0)
        outer = d.get("f49")
        outer = float(outer) if outer is not None and str(outer) != "-" else None
        name_raw = d.get("f58") or ""
        name = name_raw if isinstance(name_raw, str) else str(name_raw)
        pct_raw = d.get("f170")

        def num(key: str) -> float | None:
            v = d.get(key)
            if v is None or (isinstance(v, str) and not v.replace(".", "").replace("-", "").isdigit()):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return {
            "symbol": internal, "secid": secid, "name": name.strip(),
            "last": num("f43") or 0.0, "prev_close": num("f60") or 0.0,
            "open": num("f46") or 0.0, "high": num("f44") or 0.0, "low": num("f45") or 0.0,
            "volume": volume, "volume_unit": "share" if is_hk else "lot",
            "amount": num("f48") or 0.0,
            "outer_volume": outer,                       # 主动买入累计量
            "inner_volume": (volume - outer) if (outer is not None and volume) else None,
            "avg_price": num("f71"), "vol_ratio": num("f50"),
            # f51/f52 在 A股是涨停/跌停，在港股（116.）实测是**52 周最高/最低**
            # （00700 当日 656.7 / 375.2，与腾讯 52 周字段逐位相同）。港股无涨跌停，
            # 所以这里按市场分键名输出——把 52 周区间当涨跌停显示，小白用户会当真。
            "limit_up": None if is_hk else num("f51"),
            "limit_down": None if is_hk else num("f52"),
            "wk52_high": num("f51") if is_hk else None,
            "wk52_low": num("f52") if is_hk else None,
            "turnover_rate": num("f168"), "pct_chg": num("f170"),
            "change": num("f169"), "amplitude": num("f171"),
            "pe_dynamic": num("f162"), "pb": num("f167"),
            "total_share": num("f84"), "float_share": num("f85"),
            "total_mv": num("f116"), "float_mv": num("f117"),
            "net_asset_ps": num("f92"), "net_profit": num("f105"),
            "board_code": str(d.get("f198") or ""),
            "is_hk": is_hk, "ts_ms": now_ms(),
            "pct_display": (pct_raw if isinstance(pct_raw, str) else None),
        }

    def to_tick(self, q: dict) -> Tick:
        return Tick(symbol=q["symbol"], dt=pd.Timestamp(q["ts_ms"], unit="ms").to_pydatetime(),
                    last=q["last"], open=q["open"], high=q["high"], low=q["low"],
                    volume=q["volume"], amount=q["amount"], bids=[], asks=[])

    # ---------- 逐笔成交（当日） ----------
    def get_transactions(self, symbol: str, count: int = 3000) -> pd.DataFrame:
        """当日逐笔明细。列：dt / price / volume / deals / side_code。

        `pos` 语义：0 从当日最早开始、负数从收盘端回溯；这里取两端并集去重，
        保证高成交标的（港股主板一次可回上万行）也覆盖到全日尾段。
        """
        _, secid = self._secid(symbol)
        vol_mult = 1.0 if secid.startswith("116.") else 100.0   # A股逐笔量单位=手；港股=股
        params = {"secid": secid, "fields1": "f1,f2,f3,f4",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57"}
        rows: list[str] = []
        for pos in (0, -max(int(count), 1)):
            try:
                d = self._json(_DETAILS_URL, {**params, "pos": str(pos)}) or {}
            except ConnectionLostError:
                if pos == 0:
                    raise
                continue                                     # 回溯段失败不影响已取到的头部
            rows += d.get("details") or []
        recs, seen = [], set()
        for line in rows:
            p = line.split(",")
            if len(p) < 5:
                continue
            key = (p[0], p[1], p[2], p[3], p[4])
            if key in seen:
                continue
            seen.add(key)
            try:
                recs.append({"time": p[0], "price": float(p[1]), "volume": float(p[2]),
                             "deals": float(p[3]) if p[3] else 0.0, "side_code": p[4].strip()})
            except ValueError:
                continue
        df = pd.DataFrame(recs, columns=["time", "price", "volume", "deals", "side_code"])
        if df.empty:
            return df
        df["dt"] = pd.to_datetime(f"{pd.Timestamp.now():%Y-%m-%d} " + df["time"],
                                  errors="coerce")
        df = df.sort_values("dt").drop_duplicates(subset=["dt", "price", "volume", "side_code"],
                                                  keep="last").reset_index(drop=True)
        df["amount"] = df["price"] * df["volume"] * vol_mult
        return df[["dt", "time", "price", "volume", "deals", "side_code", "amount"]]

    # ---------- 分时（1 分钟，当日） ----------
    def get_intraday(self, symbol: str) -> pd.DataFrame:
        """当日分时：dt / price / avg_price / volume / amount。"""
        _, secid = self._secid(symbol)
        d = self._json(_TRENDS_URL, {
            "secid": secid, "ndays": "1", "iscr": "0",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"}) or {}
        rows = []
        for line in d.get("trends") or []:
            p = line.split(",")
            if len(p) < 6:
                continue
            try:
                rows.append({"dt": pd.to_datetime(p[0]), "price": float(p[1]),
                             "avg_price": float(p[7]) if len(p) > 7 and p[7] else float(p[1]),
                             "volume": float(p[5]), "amount": float(p[6])})
            except (TypeError, ValueError):
                continue
        return pd.DataFrame(rows, columns=["dt", "price", "avg_price", "volume", "amount"])

    # ---------- 历史 K 线（分钟/日/周/月） ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        """日/周/月与 1/5/15/30/60 分钟 K 线。

        注：历史 K 线走 push2his（与实时 push2 不同主机）。本机曾出现 push2his 断连，
        失败一律抛 ConnectionLostError，由 FederatedFeed 降级到 akshare/tushare。
        """
        if freq not in _KLT:
            raise AdapterError(f"东财通道不提供 {freq} 周期")
        _, secid = self._secid(symbol)
        data = self._json("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
            "secid": secid, "klt": _KLT[freq], "fqt": _FQT[adjust],
            "beg": f"{start:%Y%m%d}", "end": f"{end:%Y%m%d}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"}) or {}
        recs = []
        for line in data.get("klines") or []:
            p = line.split(",")
            if len(p) < 6:
                continue
            recs.append({"dt": pd.to_datetime(p[0]), "open": float(p[1]), "close": float(p[2]),
                         "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
                         "amount": float(p[6]) if len(p) > 6 else 0.0})
        df = pd.DataFrame(recs, columns=["dt", "open", "high", "low", "close", "volume", "amount"])
        return df.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)

    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        raise AdapterError(
            f"东财行情通道不提供财报明细（capabilities 不含 FINANCIAL）：symbol={symbol}；"
            "财务数据请用 akshare / ths_ifind / tushare")

    # ---------- 实时订阅（轮询实现） ----------
    def subscribe_realtime(self, symbols: list[str],
                           callback: Callable[[Tick | Bar], None]) -> None:
        with self._lock:
            self._subs |= {normalize(s) for s in symbols}
            self._callback = callback
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._poll_loop,
                                                name="em-quote-poll", daemon=True)
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
                syms, cb = sorted(self._subs), self._callback
            if not syms or cb is None:
                self._stop.wait(1.0)
                continue
            for s in syms:
                try:
                    cb(self.to_tick(self.get_quote(s)))
                except ConnectionLostError as e:
                    log.warning("东财轮询失败 %s: %s", s, e)
                    break                        # 限流了就整轮退避，别逐只撞
                except (AdapterError, ValueError) as e:
                    log.warning("东财快照异常 %s: %s", s, e)
                    continue
            self._stop.wait(self._poll_interval)

    def set_poll_interval(self, seconds: float) -> None:
        self._poll_interval = max(0.5, float(seconds))

    def describe(self) -> dict:
        now = time.monotonic()
        return {"name": self.name, "connected": self._connected, "stats": dict(self.stats),
                "cooldown": {h: round(t - now, 1) for h, t in self._cooldown.items() if t > now},
                "subscribed": len(self._subs), "poll_interval": self._poll_interval,
                "official": False,
                "note": "公开网页接口，无文档无 SLA；逐笔含盘后成交，故买卖量主口径取官方内外盘"}


ADAPTER = EastmoneyQuoteFeed
