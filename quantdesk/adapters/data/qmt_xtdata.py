"""miniQMT / QMT 行情通道（xtquant）—— 本项目主力数据源（宪法：QMT 为主力通道）。

为什么它是主力：数据由券商本地仓库提供（不限速、不反爬、可离线回放），且与交易通道同源，
行情口径与下单口径天然一致；AKShare/Tushare 退为兜底与交叉对数源。

线程模型（宪法线程纪律）：
- xtdata 的推送回调跑在 **SDK 自己的线程**上：`_on_push` 只做入队，绝不解析、绝不广播；
- 归一化与向下分发在适配器工作线程 `qmt-rt` 完成（`_drain_once` 是唯一消费入口，
  测试可用 `start_worker=False` 单步驱动，避免依赖线程时序）；
- 查询接口（get_market_data_ex / get_stock_list_in_sector / download_history_data）为同步
  阻塞式，由 services 层放线程池驱动；`download_history_data` 补历史时可能长时间阻塞，
  绝不能在事件循环/回调线程里调用（见 docs/integration/qmt.md 已知坑）。

单位与口径（交叉对数必读）：
- 成交量 volume 单位是「股」、成交额 amount 是「元」；AKShare 日线成交量是「手」
  （1 手 = 100 股）——两源对数前必须换算，否则 100 倍差会被误判成数据质量事故；
- 复权由 dividend_type 决定：none=原始市场价（下单口径）、front=前复权（回测口径）；
- 本适配器只做口径映射与列归一化，不做任何价格换算。

断线自愈：`self.watchdog`（ReconnectWatchdog）周期探活，判死退避重连，
重连成功后重放当前订阅清单（宪法：重连后必须自行重放订阅）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.adapters.base.optional import import_optional
from quantdesk.config import get_settings
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import (
    AdapterError,
    ConnectionLostError,
    FeedCapabilityError,
)
from quantdesk.core.events import EventBus
from quantdesk.core.models import Bar, Tick
from quantdesk.core.resilience import Backoff, ReconnectWatchdog
from quantdesk.core.symbols import from_internal
from quantdesk.core.timeutil import now_ms

log = logging.getLogger(__name__)

__all__ = ["QmtXtdataFeed"]

_INSTALL_HINT = (
    "xtquant 需券商 QMT 客户端自带的 whl：uv pip install xtquant（或安装券商提供的 "
    "xtquant-x.x.x.whl）；本项目不改 pyproject 主依赖，详见 docs/integration/qmt.md"
)
_LOGIN_GUIDE = "请登录 miniQMT 客户端（开启极简模式/独立交易）后重试"

# ---------- 参数映射表（置顶；接口面变动只改这里） ----------
_FREQ_TO_PERIOD: dict[Frequency, str] = {
    Frequency.M1: "1m",
    Frequency.M5: "5m",
    Frequency.M15: "15m",
    Frequency.M30: "30m",
    Frequency.H1: "60m",
    Frequency.D1: "1d",
    Frequency.W1: "1w",
    Frequency.MO1: "1M",
}
_PERIOD_TO_FREQ: dict[str, Frequency] = {v: k for k, v in _FREQ_TO_PERIOD.items()}
# 分钟族需要带时分秒的区间；日线族只用日期
_INTRADAY_PERIODS = frozenset({"1m", "5m", "15m", "30m", "60m"})
_DIVIDEND_TYPE: dict[str, str] = {
    AdjustType.NONE.value: "none",
    AdjustType.FORWARD.value: "front",
    AdjustType.BACKWARD.value: "back",
}
# 本项目需要的 tick 字段（xtdata 口径，真实字段名需在线核对——见接入卡清单第 1 条）
_LAST_FIELDS = ("lastPrice", "last", "close")
_SECTOR_PROBE = "沪深A股"          # 探活用板块列表：轻量、无区间参数、掉线必失败


def _parse_xt_time(value) -> pd.Timestamp | None:
    """xtdata 的 index/时间列形态：'YYYYMMDD'、'YYYYMMDDHHMMSS'，也可能是毫秒 int。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        # 毫秒级 epoch（>1e11）；秒级也兼容（xtdata 部分接口给秒）
        try:
            return pd.Timestamp(pd.to_datetime(float(value) / (1000.0 if value > 1e11
                                                               else 1.0), unit="s"))
        except (ValueError, OverflowError):
            return None
    s = str(value).strip()
    digits = s.split(".")[0] if len(s) not in (8, 14) else s
    if len(digits) == 8 and digits.isdigit():
        return pd.to_datetime(digits, format="%Y%m%d")
    if len(digits) == 14 and digits.isdigit():
        return pd.to_datetime(digits, format="%Y%m%d%H%M%S")
    try:
        return pd.to_datetime(s, errors="raise")
    except Exception:  # noqa: BLE001 解析不动的行直接丢，不让一天的脏行毁掉整个序列
        return None


def normalize_history(df, period: str | None = None) -> pd.DataFrame:
    """xtdata 返回的宽表 → HISTORY_COLUMNS（纯函数，便于离线单测）。

    xtdata 的 K 线把时间放在 **index**（字符串 'YYYYMMDD'/'YYYYMMDDHHMMSS'），
    列名已是 open/high/low/close/volume/amount，但会夹带 period 等额外列。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    out = df.copy()
    if "dt" not in out.columns:
        # 时间轴可能在 index，也可能在 'time'/'datetime' 列（不同版本/周期不一致）
        time_col = next((c for c in ("time", "datetime", "date") if c in out.columns), None)
        if time_col is not None:
            out = out.rename(columns={time_col: "dt"})
        elif isinstance(out.index, pd.MultiIndex):
            out["dt"] = [str(x[-1]) for x in out.index]
        else:
            out["dt"] = [str(x) for x in out.index]
    missing = [c for c in HISTORY_COLUMNS if c not in out.columns]
    if missing:
        raise FeedCapabilityError(
            f"xtdata 返回缺少列 {missing}（period={period}，接口/字段可能变动，"
            f"请核对 docs/integration/qmt.md 在线清单）")
    out["dt"] = out["dt"].map(_parse_xt_time)
    out = out[HISTORY_COLUMNS].dropna(subset=["dt"]).copy()
    for c in HISTORY_COLUMNS[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["dt"] = pd.to_datetime(out["dt"]).astype("datetime64[ns]")
    return out.sort_values("dt").reset_index(drop=True)


def _rec_get(rec, names):
    for n in names if isinstance(names, tuple) else (names,):
        v = rec.get(n) if isinstance(rec, dict) else getattr(rec, n, None)
        if v is not None:
            return v
    return None


def iter_push_records(payload) -> Iterator[tuple[str, dict]]:
    """SDK 推送 payload → (内部代码, 单条记录)。

    真实 xtdata 有两种形态：`{code: [record, ...]}`（订阅回调常见）与
    `{code: record}`（full tick 风格）。两种都吃，避免版本差异把推送静默丢掉。
    """
    if not isinstance(payload, dict):
        return
    for code, value in payload.items():
        if isinstance(value, dict):
            yield str(code), value
        elif isinstance(value, (list, tuple)):
            for rec in value:
                if isinstance(rec, dict):
                    yield str(code), rec
        elif isinstance(value, pd.DataFrame):
            for idx, rec in value.iterrows():
                rec = dict(rec)
                rec.setdefault("time", _parse_xt_time(idx))
                yield str(code), rec


def _levels(prices, vols, n: int = 5) -> list[tuple[float, float]]:
    """五档盘口：价格与量两个数组按下标配对，缺位丢弃（停牌/集合竞价时数组不足 5 档）。"""
    prices = list(prices or [])
    vols = list(vols or [])
    out: list[tuple[float, float]] = []
    for p, v in zip(prices[:n], vols[:n]):
        try:
            if float(p) > 0:
                out.append((float(p), float(v or 0)))
        except (TypeError, ValueError):
            continue
    return out


def _record_dt_ms(rec) -> int:
    """记录里的时间：xtdata 常见为毫秒 epoch；无则用本地当前时间兜底。"""
    raw = _rec_get(rec, ("time", "timestamp", "dt"))
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw if raw > 1e11 else raw * 1000)
    ts = _parse_xt_time(raw) if raw is not None else None
    return int(ts.value // 10 ** 6) if ts is not None else now_ms()


def to_tick(code: str, rec: dict) -> Tick:
    """full tick 记录 → Tick（含五档）。"""
    ms = _record_dt_ms(rec)
    return Tick(
        symbol=code,
        dt=datetime.fromtimestamp(ms / 1000),
        last=float(_rec_get(rec, _LAST_FIELDS) or 0.0),
        open=float(rec.get("open", 0.0) or 0.0),
        high=float(rec.get("high", 0.0) or 0.0),
        low=float(rec.get("low", 0.0) or 0.0),
        volume=float(rec.get("volume", rec.get("pvolume", 0.0)) or 0.0),
        amount=float(rec.get("amount", 0.0) or 0.0),
        bids=_levels(rec.get("bidPrice"), rec.get("bidVol")),
        asks=_levels(rec.get("askPrice"), rec.get("askVol")),
    )


def to_bar(code: str, period: str, rec: dict) -> Bar:
    """订阅回调的 K 线记录 → Bar。"""
    ms = _record_dt_ms(rec)
    return Bar(
        symbol=code,
        freq=_PERIOD_TO_FREQ.get(period, period),
        dt=datetime.fromtimestamp(ms / 1000),
        open=float(rec.get("open", 0.0) or 0.0),
        high=float(rec.get("high", 0.0) or 0.0),
        low=float(rec.get("low", 0.0) or 0.0),
        close=float(_rec_get(rec, ("close", "lastPrice")) or 0.0),
        volume=float(rec.get("volume", 0.0) or 0.0),
        amount=float(rec.get("amount", 0.0) or 0.0),
    )


class QmtXtdataFeed(DataFeedAdapter):
    """xtquant.xtdata 行情通道：历史 K + 实时 tick/分钟线（财务能力不提供）。"""

    name = "qmt_xtdata"
    label = "miniQMT 行情（xtquant，主力源）"
    prereq = "券商开通 QMT/miniQMT 权限 → 本机登录 miniQMT 客户端（极简模式）→ " \
             "设置页录入 userdata 路径；xtquant 由券商 whl 安装"
    official = True
    requires = ("xtquant",)
    default_enabled = True
    enable_flag = ""
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.REALTIME_TICK,
                              FeedCapability.REALTIME_BAR})

    def __init__(self, event_bus: EventBus | None = None,
                 realtime_periods: tuple[str, ...] = ("tick", "1m"),
                 health_ttl_s: float = 60.0, start_worker: bool = True) -> None:
        self.bus = event_bus
        self.realtime_periods = tuple(realtime_periods)
        self.health_ttl_s = float(health_ttl_s)
        self.start_worker = bool(start_worker)

        self._xt = None                       # xtquant.xtdata 模块（惰性导入）
        self._connected = False
        self._health_cache: tuple[float, bool] = (0.0, False)

        self._callback: Callable[[Tick | Bar], None] | None = None
        self._handles: dict[tuple[str, str], int] = {}   # (code, period) → subscribe_quote seq
        self._push_q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_worker = threading.Event()

        self.watchdog: ReconnectWatchdog | None = None   # ChannelHub 状态灯读这个属性
        self._lock = threading.RLock()

    # ================= 生命周期 =================
    def connect(self) -> None:
        """惰性导入 xtdata。失败必须是可路由的断线（ConnectionLostError）+ 引导。"""
        with self._lock:
            self._xt = self._import_xtdata()
            self._connected = True
            self._ensure_watchdog()
        if self.watchdog is not None:
            self.watchdog.start()             # 已在跑则内部直接返回

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            wd, self.watchdog = self.watchdog, None
            self._handles.clear()
        if wd is not None:
            wd.stop()
        self._stop_worker.set()
        self._push_q.put(None)                # 唤醒阻塞在 get 上的工作线程
        t, self._worker = self._worker, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    def health_check(self) -> bool:
        """轻量探活（缓存 60s，避免状态灯把 SDK 打满）；异常一律吞掉返回 False。"""
        now = time.monotonic()
        if now - self._health_cache[0] < self.health_ttl_s:
            return self._health_cache[1]
        ok = self._probe()
        self._health_cache = (now, ok)
        return ok

    def _probe(self) -> bool:
        """真实探活：取板块列表——掉线/未登录时必失败，且不抛（可探测语义）。"""
        xt = self._xt
        if xt is None or not self._connected:
            return False
        try:
            return bool(xt.get_stock_list_in_sector(_SECTOR_PROBE))
        except Exception as e:  # noqa: BLE001 探活只报状态，异常不外溢
            log.warning("QMT 行情探活失败: %s", e)
            return False

    # ================= 历史 K 线 =================
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        """先增量补本地仓库（download_history_data），再读 get_market_data_ex。

        无数据返回**空表**（不抛）；SDK 报错抛 ConnectionLostError（内部最多重试 1 次，
        重试风暴与降级归 FederatedFeed）。
        """
        period = self._period_of(freq)
        xt = self._require()
        code = from_internal(symbol, "qmt")
        s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
        fmt = "%Y%m%d%H%M%S" if period in _INTRADAY_PERIODS else "%Y%m%d"
        s, e = f"{s_ts:{fmt}}", f"{e_ts:{fmt}}"

        self._call(lambda: self._download(xt, code, period, s, e),
                   what=f"download_history_data({code},{period})")
        dividend = self._dividend_type(adjust)
        read = lambda div: xt.get_market_data_ex([], [code], period=period, start_time=s,
                                                 end_time=e, dividend_type=div,
                                                 fill_data=True)
        try:
            data = self._call(lambda: read(dividend), what=f"get_market_data_ex({code},{period})")
        except ConnectionLostError as e:
            # 老客户端的本地仓库可能没做前/后复权数据 → 回退 front 再试一次（留痕）
            if _is_dividend_error(e) and dividend != "front":
                log.warning("QMT 拒绝复权口径 %r（%s），回退 dividend_type=front", dividend, e)
                dividend = "front"
                data = self._call(lambda: read(dividend),
                                  what=f"get_market_data_ex({code},{period},front)")
            else:
                raise
        rows = (data or {}).get(code)
        if rows is None or (isinstance(rows, pd.DataFrame) and rows.empty):
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        return normalize_history(rows, period)

    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        raise FeedCapabilityError(
            f"xtdata 不提供财务报表（capabilities 不含 FINANCIAL）；"
            f"财务走 ths_ifind / tushare / akshare（symbol={symbol}, report_type={report_type}）")

    # ================= 实时订阅 =================
    def subscribe_realtime(self, symbols: list[str],
                           callback: Callable[[Tick | Bar], None]) -> None:
        """增量追加订阅；每个标的按 realtime_periods 建订阅句柄（可取消、可重放）。"""
        xt = self._require()
        if not self._connected:
            raise ConnectionLostError(f"QMT 行情通道未连接：{_LOGIN_GUIDE}")
        with self._lock:
            self._callback = callback
            for sym in symbols:
                code = from_internal(sym, "qmt")
                for period in self.realtime_periods:
                    if (code, period) not in self._handles:
                        self._subscribe_one(xt, code, period)
        self._ensure_worker()
        log.info("QMT 实时订阅：新增/复核 %d 个标的（当前句柄 %d 个）",
                 len(symbols), len(self._handles))

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        xt = self._xt
        with self._lock:
            dropped: list[int] = []
            for sym in symbols:
                code = from_internal(sym, "qmt")
                for period in self.realtime_periods:
                    seq = self._handles.pop((code, period), None)
                    if seq is not None:
                        dropped.append(seq)
        if xt is None or not dropped:
            return
        try:
            xt.unsubscribe_quote(dropped)
        except Exception as e:  # noqa: BLE001 退订失败只留痕：句柄已丢，重连时会重建
            log.warning("QMT 退订失败（忽略）: %s", e)

    def _subscribe_one(self, xt, code: str, period: str) -> int:
        seq = xt.subscribe_quote(stock_code=code, period=period, start_time="",
                                 end_time="", count=1,
                                 callback=self._make_push_handler(period))
        self._handles[(code, period)] = seq      # 重连重放与退订都靠这张表
        return seq

    def _make_push_handler(self, period: str) -> Callable:
        def _on_push(payload, _period: str = period) -> None:
            # 宪法线程纪律：SDK 推送线程内只入队，绝不解析/绝不广播/绝不阻塞
            self._push_q.put((_period, payload))
        return _on_push

    def _ensure_worker(self) -> None:
        if not self.start_worker:
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_worker.clear()
            self._worker = threading.Thread(target=self._worker_loop, name="qmt-rt",
                                            daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_worker.is_set():
            self._drain_once(0.5)

    def _drain_once(self, timeout: float = 0.0) -> int:
        """消费一条推送并分发（工作线程与测试单步驱动共用同一入口，便于确定性断言）。"""
        try:
            item = self._push_q.get(timeout=timeout) if timeout > 0 \
                else self._push_q.get_nowait()
        except queue.Empty:
            return 0
        if item is None:
            return 0
        period, payload = item
        cb = self._callback
        if cb is None:
            return 0
        n = 0
        for code, rec in iter_push_records(payload):
            try:
                cb(to_tick(code, rec) if period == "tick" else to_bar(code, period, rec))
                n += 1
            except Exception:  # noqa: BLE001 单条推送异常不拖垮工作线程
                log.exception("QMT 实时归一化失败 code=%s period=%s", code, period)
        return n

    # ================= 断线自愈 =================
    def _resubscribe_all(self) -> int:
        """宪法义务：重连成功后重放当前订阅清单。"""
        with self._lock:
            keys = list(self._handles)
            cb = self._callback
            self._handles.clear()
        if not keys:
            return 0
        if cb is None:
            log.warning("QMT 重连后无可用回调，跳过订阅重放（%d 个句柄待上层重订）", len(keys))
            return 0
        xt = self._require()
        replayed = 0
        for code, period in keys:
            try:
                self._subscribe_one(xt, code, period)
                replayed += 1
            except Exception as e:  # noqa: BLE001 单个标的重订失败不影响其他
                log.warning("QMT 订阅重放失败 %s/%s: %s", code, period, e)
        log.info("QMT 重连后订阅重放完成：%d/%d", replayed, len(keys))
        self._ensure_worker()
        return replayed

    # ================= 内部 =================
    @staticmethod
    def _import_xtdata():
        try:
            return import_optional("xtquant.xtdata", channel="miniQMT 行情",
                                   install_hint=_INSTALL_HINT)
        except AdapterError as e:
            # SDK 缺失/导入异常在行情通道上等价于「通道不可用」→ 交给路由层降级
            raise ConnectionLostError(f"QMT 行情通道不可用：{e}；{_LOGIN_GUIDE}") from e

    def _require(self):
        if self._xt is None:
            self._xt = self._import_xtdata()
        return self._xt

    @staticmethod
    def _period_of(freq) -> str:
        key = freq if isinstance(freq, Frequency) else None
        if key is None:
            try:
                key = Frequency(str(freq))
            except ValueError:
                raise FeedCapabilityError(f"xtdata 不支持周期 {freq!r}") from None
        period = _FREQ_TO_PERIOD.get(key)
        if period is None:
            raise FeedCapabilityError(
                f"xtdata 不支持周期 {key.value}（tick 请走实时订阅 subscribe_realtime）")
        return period

    @staticmethod
    def _dividend_type(adjust) -> str:
        key = adjust.value if isinstance(adjust, AdjustType) else str(getattr(adjust, "value", adjust))
        if key in _DIVIDEND_TYPE:
            return _DIVIDEND_TYPE[key]
        # front_ratio/back_ratio 这类比率复权口径 xtdata 没有 → 回退并留痕（不静默）
        log.warning("QMT 不支持复权口径 %r，回退 dividend_type=front"
                    "（需精确比率复权请走 Tushare 本地因子）", adjust)
        return "front"

    @staticmethod
    def _download(xt, code: str, period: str, start: str, end: str) -> None:
        """增量补本地仓库；老版本无 incrementally 参数 → 降级为全量补拉。"""
        try:
            xt.download_history_data(code, period, start, end, incrementally=True)
        except TypeError as e:
            if "incrementally" not in str(e):
                raise
            log.info("xtdata 版本不支持 incrementally 参数，按默认方式补拉: %s", e)
            xt.download_history_data(code, period, start, end)

    def _call(self, fn: Callable, *, what: str):
        """统一「内部最多重试 1 次」；最终失败归一为 ConnectionLostError 便于降级链识别。"""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("QMT 行情调用失败（%s），重试一次: %s", what, e)
            try:
                return fn()
            except Exception as e2:  # noqa: BLE001
                raise ConnectionLostError(
                    f"QMT 行情调用失败（{what}）: {e2.__class__.__name__}: {e2}；{_LOGIN_GUIDE}"
                ) from e2

    def _ensure_watchdog(self) -> None:
        if self.watchdog is not None:
            return
        s = get_settings()
        self.watchdog = ReconnectWatchdog(
            name=self.name, probe=self._probe, reconnect=self._reconnect,
            on_reconnected=self._resubscribe_all, bus=self.bus,
            interval=s.watchdog_interval_s,
            backoff=Backoff(base=s.backoff_base_s, cap=s.backoff_cap_s),
            probe_timeout_note=f"miniQMT 行情通道探活失败：{_LOGIN_GUIDE}")

    def _reconnect(self) -> None:
        """重连 = 重新导入并登录（客户端可能已重启，SDK 句柄必须换新）。"""
        self._xt = None
        self._connected = False
        self._health_cache = (0.0, False)
        self.connect()


def _is_dividend_error(e: Exception) -> bool:
    text = str(e)
    return "dividend" in text.lower() or "复权" in text


ADAPTER = QmtXtdataFeed
