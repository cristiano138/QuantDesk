"""通达信行情通道（双传输层 + 自动选择）：K 线 / 实时快照 / 分笔。

两条可落地的取数路径（本机装了哪条就用哪条）：
- ``TdxOfficialTransport``：通达信官方 AI 数据接口（SDK 模块名 ``TdxAiData``，随官方客户端
  安装、需申请开通）；
- ``TdxPytdxTransport``：开源 ``pytdx``（直连通达信行情服务器，免账号，社区最通用路径）。

``connect()`` 优先官方 SDK，官方缺失或连不上时自动降到 pytdx，实际用的是哪个传输层
在日志与 ``describe()`` 里如实上报；两者都缺 → ``import_optional`` 抛带安装引导的 AdapterError。
requires 声明的是**可选 SDK 全集**（二者择一即可工作），与注册表 ``deps_ready()`` 的
「全部存在才算就绪」语义不同——语义细节见类文档与 docs/integration/tdx_aidata.md。

线程纪律（本项目已知坑）：pytdx 与官方 SDK 的连接对象都**不是线程安全的**，多线程共用一个
API 实例会错包/串包。所以连接对象由一个专职工作线程独占，所有请求经 ``queue.Queue`` 串行
消费，调用方同步等待结果（带超时）；订阅回调由另一个投递线程驱动，SDK 侧线程只入队。

价格口径（宪法第 5 条 + 本项目已实锤的 P1 数据质量红线）：pytdx 只提供**未复权**数据，
且本地没有通达信复权因子来源，所以 ``adjust=FORWARD/BACKWARD`` 一律抛
``FeedCapabilityError``——绝不按比例伪造复权价（AKShare 的 qfq 口径分离已经证明：
猜出来的复权价会静默污染回测结果）。需要复权口径请走 tushare 精确 qfq 或 QMT。
"""

from __future__ import annotations

import inspect
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.adapters.base.optional import dependency_available, import_optional
from quantdesk.config import get_settings
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import AdapterError, ConnectionLostError, FeedCapabilityError
from quantdesk.core.models import Tick
from quantdesk.core.ratelimit import RateLimiter
from quantdesk.core.resilience import Backoff, ReconnectWatchdog
from quantdesk.core.symbols import from_internal, to_internal

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 传输层元数据
# --------------------------------------------------------------------------
_OFFICIAL_MODULE = "TdxAiData"
_PYTDX_MODULE = "pytdx.hq"

_INSTALL_HINT_BOTH = (
    "安装二者之一即可：① 免费开源的通达信行情组件（pytdx，免账号，直连通达信行情服务器）；"
    "② 通达信官方数据组件（TdxAiData，随官方客户端提供、需申请权限）。"
)

# 通达信公开行情服务器（vip）候选，连接时依次试连——不硬编码单一 IP。
# 这批地址来自公开资料，会随时间失效：失效时按 docs/integration/tdx_aidata.md 的
# 「服务器与换机策略」更新本常量（也可在实例化时用 servers= 覆盖）。
TDX_HQ_SERVERS: tuple[tuple[str, int], ...] = (
    ("119.147.212.81", 7709),
    ("124.71.187.122", 7709),
    ("122.51.120.217", 7709),
    ("110.41.154.217", 7709),
    ("111.229.247.189", 7709),
    ("115.238.56.198", 7709),
    ("115.238.90.35", 7709),
    ("119.147.212.81", 7727),
)

# pytdx get_security_bars 的 category 取值（**以 pytdx 文档为准，需在线核对**）：
#   0=5分钟  1=15分钟  2=30分钟  3=1小时  4=日线  5=周线  6=月线  7=1分钟
#   8=1分钟选  9=3分钟（另有版本 8/9 用作 1 分钟，取值以在线核对为准；本表只用下面这些）
PYTDX_BAR_CATEGORY: dict[Frequency, int] = {
    Frequency.M1: 7,
    Frequency.M5: 0,
    Frequency.M15: 1,
    Frequency.M30: 2,
    Frequency.H1: 3,
    Frequency.D1: 4,
    Frequency.W1: 5,
    Frequency.MO1: 6,
}
# 官方 SDK 的周期取值（同上，需在线核对）
OFFICIAL_PERIODS: dict[Frequency, str] = {
    Frequency.M1: "1m", Frequency.M5: "5m", Frequency.M15: "15m", Frequency.M30: "30m",
    Frequency.H1: "60m", Frequency.D1: "1d", Frequency.W1: "1w", Frequency.MO1: "1M",
}
# 日线族：pytdx 的 bar 时间带 15:00 收盘时刻，归一化时截到 00:00 与 AKShare/Tushare 对齐，
# 否则跨源对数会因 dt 不等而逐行失配（P1 口径红线的配套要求）。
_DAILY_FAMILY = frozenset({Frequency.D1, Frequency.W1, Frequency.MO1})

# pytdx 单次 get_security_bars 上限约 800 条；取小页避免触顶后偏移量算错
BAR_PAGE_SIZE = 700
# 分页上界：一次历史请求最多翻多少页（42000 根），防死循环
MAX_BAR_PAGES = 60

_TX_CATEGORY = 0     # 部分 pytdx 版本 get_transaction_data 首参为 category（0=分笔），需在线核对
_PROBE_MARKET = 0    # 探活取股票数量用的市场（0=深）

# 官方 SDK 的类名/方法名未经本机验证：按候选名逐个探测，实际解析到的名字进 describe()
_OFFICIAL_API_NAMES = ("TdxAiDataAPI", "TdxAiDataApi", "TdxDataAPI", "TdxAPI", "Api")
_OFFICIAL_METHOD_NAMES: dict[str, tuple[str, ...]] = {
    "connect": ("connect", "login"),
    "close": ("disconnect", "close", "logout"),
    "probe": ("get_security_count", "get_stock_count", "get_server_time"),
    "bars": ("get_kline", "get_history_kline", "get_security_bars", "get_bar"),
    "quotes": ("get_snapshot", "get_realtime_quote", "get_security_quotes", "get_quote"),
    "transactions": ("get_transaction", "get_transaction_data", "get_deal_data"),
}
_ADJUST_REFUSED = (
    "通达信通道（{channel}）只有未复权数据，本机也没有通达信复权因子，"
    "因此拒绝 {adjust} 请求：宁可报错，不伪造复权价（宪法第 5 条价格口径）。"
    "需要复权口径请走 tushare 精确 qfq / QMT，或先接复权因子源再回来放开本分支。"
)

_REQUEST_TIMEOUT = 10.0       # 单次请求等待工作线程的兜底超时（秒）
_HEALTH_TTL = 60.0            # 探活结果缓存（秒），与 D14 口径一致
_DEFAULT_POLL_INTERVAL = 3.0  # pytdx 无推送：快照轮询周期（秒）


# --------------------------------------------------------------------------
# 纯函数：代码互转与列归一化（离线单测直接覆盖）
# --------------------------------------------------------------------------
def market_and_code(symbol: str) -> tuple[int, str]:
    """内部代码 → 通达信 ``(market, code)``。

    market 位与 ``core/symbols`` 的 "1.600000"/"0.000001" 同源：1=沪、0=深；
    北交所由 from_internal 直接拒（SymbolError），不在这里另立口径。
    """
    mkt, num = from_internal(symbol, "tdx").split(".")
    return int(mkt), num


def symbol_of_quote(rec: dict) -> str:
    """行情记录 → 内部代码（market/code 字段名兼容几种写法）。"""
    mkt = rec.get("market", rec.get("mkt"))
    code = rec.get("code", rec.get("security_code"))
    if mkt is None or not code:
        raise FeedCapabilityError(f"通达信行情记录缺 market/code 字段: {sorted(rec)[:8]}")
    return to_internal(f"{int(mkt)}.{str(code).strip()}", "tdx")


def bar_datetime(rec: dict) -> pd.Timestamp | None:
    """bar 时间解析：新版有 ``datetime`` 字符串（"2024-01-03 15:00"），
    旧版只有 year/month/day/hour/minute 整数字段——两种都要能解。"""
    raw = rec.get("datetime") or rec.get("dt") or rec.get("time")
    if raw:
        try:
            return pd.Timestamp(str(raw).strip())
        except (ValueError, TypeError):
            pass        # 解析不了就落到分段字段，不猜
    y, mo, d = rec.get("year"), rec.get("month"), rec.get("day")
    try:
        if not (y and mo and d):
            return None
        return pd.Timestamp(int(y), int(mo), int(d),
                            int(rec.get("hour") or 0), int(rec.get("minute") or 0))
    except (ValueError, TypeError):
        return None


def _num(rec: dict, *keys: str) -> float:
    """取第一个存在的数值字段（pytdx 用 vol，官方 SDK 可能用 volume）；缺失返回 NaN。"""
    for k in keys:
        v = rec.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("nan")
    return float("nan")


def _num0(rec: dict, *keys: str) -> float:
    """快照字段取值：缺失/NaN 归 0。

    K 线用 _num（缺列就是 NaN，让使用层看得见空洞）；快照 Tick 的字段是 0 还是 NaN
    会直接影响下游涨跌计算，统一按「源没给就是 0」的快照语义处理。
    """
    v = _num(rec, *keys)
    return 0.0 if v != v else v     # v != v 即 NaN


def normalize_bars(records: Iterable[dict] | None, freq: Frequency | None = None) -> pd.DataFrame:
    """源 K 线记录（dict 列表）→ HISTORY_COLUMNS，dt 升序去重。

    **成交量单位**：pytdx 的 ``vol`` 是「手」（1 手 = 100 股，日线/分钟线同口径），
    ``amount`` 是「元」。本函数**不做单位换算**，保持源口径如实透传——换算归使用层，
    与 AKShare 适配器的处理一致（口径混着改是最难查的数据 bug）。
    """
    if not records:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    rows = []
    for rec in records:
        dt = bar_datetime(rec)
        if dt is None:
            continue
        if freq in _DAILY_FAMILY:
            dt = dt.normalize()      # 日线族去掉 15:00 收盘时刻，与跨源 dt 对齐
        rows.append({"dt": dt,
                     "open": _num(rec, "open"), "high": _num(rec, "high"),
                     "low": _num(rec, "low"), "close": _num(rec, "close"),
                     "volume": _num(rec, "vol", "volume"), "amount": _num(rec, "amount")})
    if not rows:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    df = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    df["dt"] = pd.to_datetime(df["dt"])
    for c in HISTORY_COLUMNS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return (df.dropna(subset=["dt"]).drop_duplicates(subset=["dt"], keep="last")
            .sort_values("dt").reset_index(drop=True))


TX_COLUMNS = ["dt", "price", "volume", "num", "direction"]


def _tx_datetime(rec: dict) -> pd.Timestamp | None:
    """分笔时间：有日期字段就组合；只有 "HH:MM:SS" 而没有日期时返回 None 丢弃该行。

    为什么不猜日期：分笔缺日期就当今天用，会在历史区间里凭空造出成交明细
    （数据质量红线，宁可少不可错）。
    """
    raw = rec.get("datetime") or rec.get("dt")
    if raw:
        try:
            return pd.Timestamp(str(raw).strip())
        except (ValueError, TypeError):
            return None
    date_part = rec.get("date") or rec.get("traded_at")
    time_part = rec.get("time") or rec.get("tt")
    if date_part and time_part:
        try:
            return pd.Timestamp(f"{str(date_part).strip()} {str(time_part).strip()}")
        except (ValueError, TypeError):
            return None
    year = rec.get("year")
    if year and time_part:
        try:
            return pd.Timestamp(f"{int(year)}-{int(rec.get('month') or 1):02d}-"
                                f"{int(rec.get('day') or 1):02d} {str(time_part).strip()}")
        except (ValueError, TypeError):
            return None
    return None


def normalize_transactions(records: Iterable[dict] | None) -> pd.DataFrame:
    """分笔记录 → [dt, price, volume, num, direction]（dt 升序）。

    成交量单位同 K 线：pytdx 的 ``vol`` 是「手」，不做换算。
    """
    if not records:
        return pd.DataFrame(columns=TX_COLUMNS)
    rows = []
    for rec in records:
        dt = _tx_datetime(rec)
        if dt is None:
            continue
        rows.append({"dt": dt, "price": _num(rec, "price"), "volume": _num(rec, "vol", "volume"),
                     "num": _num(rec, "num"), "direction": str(rec.get("direction") or "")})
    if not rows:
        return pd.DataFrame(columns=TX_COLUMNS)
    df = pd.DataFrame(rows, columns=TX_COLUMNS)
    df["dt"] = pd.to_datetime(df["dt"])
    return df.sort_values("dt").reset_index(drop=True)


def quote_to_tick(rec: dict, ts: datetime) -> Tick | None:
    """快照记录 → Tick（五档盘口；缺字段按空档处理，不填 0 价免得看起来像有挂单）。

    pytdx 快照无时间戳字段，dt 取轮询采集时刻——这也是本通道实时口径的固有延迟
    （一个轮询周期），不是交易所推送时刻。
    """
    try:
        symbol = symbol_of_quote(rec)
    except Exception as e:  # noqa: BLE001 脏记录跳过，不拖垮整批快照
        log.warning("通达信快照无法解析（跳过该标的）: %s", e)
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for i in range(1, 6):
        bp, bv = rec.get(f"bid{i}"), rec.get(f"bid_vol{i}", rec.get(f"bid_vol_{i}"))
        ap, av = rec.get(f"ask{i}"), rec.get(f"ask_vol{i}", rec.get(f"ask_vol_{i}"))
        if bp not in (None, 0) and float(bp) > 0:
            bids.append((float(bp), float(bv or 0)))
        if ap not in (None, 0) and float(ap) > 0:
            asks.append((float(ap), float(av or 0)))
    return Tick(symbol=symbol, dt=ts,
                last=_num0(rec, "price", "last"),
                open=_num0(rec, "open"),
                high=_num0(rec, "high"),
                low=_num0(rec, "low"),
                volume=_num0(rec, "vol", "volume"),   # 单位：手
                amount=_num0(rec, "amount"),
                bids=bids, asks=asks)


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------
class TdxPytdxTransport:
    """pytdx 传输层：开源、免账号，直连通达信行情服务器。

    实例只在**工作线程**内被触碰（连接对象非线程安全）。
    """

    channel = "pytdx"
    is_official = False

    def __init__(self, servers: Iterable[tuple[str, int]] | None = None,
                 timeout: float = 5.0, heartbeat: bool = False) -> None:
        self._servers = list(servers) if servers else list(TDX_HQ_SERVERS)
        self._timeout = float(timeout)
        self._heartbeat = bool(heartbeat)
        self._api = None
        self.server: tuple[str, int] | None = None

    def open(self) -> None:
        if self._api is not None:          # 幂等：已连接不重复握手
            return
        mod = import_optional(_PYTDX_MODULE, channel="通达信 pytdx 传输层",
                              install_hint="pip install pytdx（" + _INSTALL_HINT_BOTH + "）")
        api_cls = getattr(mod, "TdxHq_API", None)
        if api_cls is None:
            raise AdapterError("pytdx.hq 里没有 TdxHq_API（pytdx 版本或包结构不符），"
                               "需在线核对 pytdx 版本")
        api = api_cls(heartbeat=self._heartbeat)
        failures = []
        for host, port in self._servers:
            try:
                if self._try_connect(api, host, port):
                    self._api = api
                    self.server = (host, port)
                    log.info("pytdx 已连上行情服务器 %s:%s（试连失败 %d 台）",
                             host, port, len(failures))
                    return
            except Exception as e:  # noqa: BLE001 单台失败继续下一台
                self._safe_disconnect(api)
            failures.append(f"{host}:{port}")
        raise ConnectionLostError(
            f"pytdx 全部行情服务器试连失败（{', '.join(failures)}）；"
            "行情服务器列表会失效，按 docs/integration/tdx_aidata.md 更新 TDX_HQ_SERVERS "
            "或实例化时传 servers=")

    def _try_connect(self, api: Any, host: str, port: int) -> bool:
        """pytdx 各版本 connect 形参不一（有的没有 timeout），按签名兼容调用——需在线核对。"""
        try:
            params = inspect.signature(api.connect).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {"timeout": self._timeout} if "timeout" in params else {}
        return bool(api.connect(host, port, **kwargs))

    @staticmethod
    def _safe_disconnect(api: Any) -> None:
        try:
            api.disconnect()
        except Exception:  # noqa: BLE001 试连阶段的清理失败不影响换机
            pass

    def close(self) -> None:
        api, self._api = self._api, None
        self.server = None
        if api is not None:
            self._safe_disconnect(api)

    def probe(self) -> int:
        """探活：取（深市）股票数量——轻量、无副作用，>0 视为活着。"""
        return int(self._exec("get_security_count", _PROBE_MARKET))

    def bars(self, category: int, market: int, code: str, start: int, count: int) -> list:
        return self._exec("get_security_bars", category, market, code, start, count) or []

    def quotes(self, pairs: list[tuple[int, str]]) -> list:
        return self._exec("get_security_quotes", pairs) or []

    def transactions(self, market: int, code: str, start: int, count: int) -> list:
        api = self._require("get_transaction_data")
        fn = getattr(api, "get_transaction_data", None)
        if fn is None:
            raise FeedCapabilityError(
                "本机 pytdx 无 get_transaction_data（分笔接口名随版本变化，需在线核对）")
        names = list(inspect.signature(fn).parameters)
        # 部分版本首参是 category（0=分笔）；两种形态都覆盖，避免版本差异变成运行期猜谜
        args = ((market, code, start, count) if names and names[0] != "category"
                else (_TX_CATEGORY, market, code, start, count))
        return self._exec("get_transaction_data", *args) or []

    # ---------- 内部 ----------
    def _require(self, op: str) -> Any:
        if self._api is None:
            raise ConnectionLostError(f"pytdx 未连接（op={op}）：先 connect()")
        return self._api

    def _exec(self, method: str, *args: Any) -> Any:
        """调用 SDK；任何异常都归一为 ConnectionLostError 并**丢弃连接对象**。

        丢弃是刻意的：坏 socket 复用会让「重试一次」变成重试同一个错误。
        """
        api = self._require(method)
        fn = getattr(api, method, None)
        if fn is None:
            raise FeedCapabilityError(f"pytdx 缺少接口 {method}（需在线核对 SDK 版本）")
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self._api = None
            raise ConnectionLostError(
                f"pytdx {method} 调用失败（连接已作废，将重连后重试）: "
                f"{e.__class__.__name__}: {e}") from e


class TdxOfficialTransport:
    """通达信官方 AI 数据接口（SDK 模块名 ``TdxAiData``）传输层。

    本机没装官方 SDK 时不会走到这里；类名/方法名/形参按候选探测，解析结果由
    ``resolved`` 如实上报（describe 里能看到实际调到了什么名字）。**所有候选名都需在线核对**。
    """

    channel = "TdxAiData"
    is_official = True

    def __init__(self, install_dir: str = "") -> None:
        self._install_dir = install_dir or get_settings().tdx_install_dir
        self._api = None
        self._names: dict[str, str] = {}        # op → 实际解析到的方法名
        self._forms: dict[str, str] = {}        # 方法名 → 实际生效的实参形态（在线核对用）

    @property
    def resolved(self) -> dict[str, str]:
        return dict(self._names)

    @property
    def server(self) -> None:
        return None     # 官方 SDK 自管登录/服务器，这里没有 host:port 概念

    def open(self) -> None:
        if self._api is not None:
            return
        mod = import_optional(_OFFICIAL_MODULE, channel="通达信官方 SDK",
                              install_hint="安装通达信官方 AI 数据 SDK（模块名 TdxAiData）；"
                                           "没有官方 SDK 就装 pytdx：" + _INSTALL_HINT_BOTH)
        api_cls = next((getattr(mod, n) for n in _OFFICIAL_API_NAMES
                        if getattr(mod, n, None) is not None), None)
        if api_cls is None:
            raise AdapterError(
                f"TdxAiData 模块里找不到已知的 API 类名（候选 {list(_OFFICIAL_API_NAMES)}）；"
                "官方 SDK 的真实类名需在线核对后补进本文件常量")
        api = api_cls()
        connect_name = self._resolve_api_name(api, "connect")
        if connect_name:
            # install_dir 为空时不给关键字形态：空目录对官方 SDK 没有意义，传了反而可能被拒
            values = {} if not self._install_dir else {"install_dir": self._install_dir}
            self._api = api
            self._call(connect_name, values)
        self._api = api
        for op in ("close", "probe", "bars", "quotes", "transactions"):
            self._resolve_api_name(api, op)      # 早失败：缺接口现在就报，而不是跑到一半
        log.info("通达信官方 SDK 已连接，解析到的接口名: %s", self._names)

    def close(self) -> None:
        api, self._api = self._api, None
        name = self._names.get("close")
        if api is not None and name:
            try:
                getattr(api, name)()
            except Exception as e:  # noqa: BLE001 清理失败不掩盖主流程
                log.warning("通达信官方 SDK 关闭异常（忽略）: %s", e)

    def probe(self) -> int:
        """探活：取股票数量（或服务器时间）——轻量、无副作用。"""
        name = self._require("probe")
        out = self._call(name, {})
        if isinstance(out, (int, float)):
            return int(out)
        if isinstance(out, dict):
            return int(out.get("count") or out.get("total") or (1 if out else 0))
        if isinstance(out, list):
            return len(out)
        return 1 if out else 0

    def bars(self, period: Any, market: int, code: str, start: int, count: int) -> list:
        name = self._require("bars")
        values = {"market": market, "code": code, "period": period, "start": start,
                  "count": count}
        return list(self._call(name, values) or [])

    def quotes(self, pairs: list[tuple[int, str]]) -> list:
        name = self._require("quotes")
        values = {"stock_list": pairs, "symbols": [f"{m}.{c}" for m, c in pairs]}
        return list(self._call(name, values) or [])

    def transactions(self, market: int, code: str, start: int, count: int) -> list:
        name = self._require("transactions")
        values = {"market": market, "code": code, "start": start, "count": count}
        return list(self._call(name, values) or [])

    # ---------- 内部 ----------
    def _require(self, op: str) -> str:
        name = self._names.get(op)
        if not name:
            raise FeedCapabilityError(
                f"通达信官方 SDK 未解析到 {op} 接口（候选名需在线核对）")
        return name

    def _resolve_api_name(self, api: Any, op: str) -> str | None:
        for cand in _OFFICIAL_METHOD_NAMES.get(op, ()):
            if getattr(api, cand, None) is not None:
                self._names.setdefault(op, cand)
                return self._names[op]
        if op != "close":
            raise AdapterError(
                f"通达信官方 SDK 缺少 {op} 接口（候选 {_OFFICIAL_METHOD_NAMES.get(op)}）；"
                "真实方法名需在线核对后补进 _OFFICIAL_METHOD_NAMES")
        return None

    def _call(self, name: str, values: dict[str, Any]) -> Any:
        """按候选实参形态逐个尝试官方 SDK 方法。

        官方 SDK 的真实形参名/顺序未在本机核对过（接入卡的在线核对清单第 1 条），
        所以这里不猜一次当结论：关键字形态、纯位置形态、省略 start 的位置形态都试一遍，
        哪个跑通就记进 ``_forms``（describe 里可见，便于回写真实签名）。
        """
        if self._api is None:
            raise ConnectionLostError(f"通达信官方 SDK 未连接（op={name}）")
        fn = getattr(self._api, name)
        tried: list[str] = []
        for kind, args, kwargs in self._candidate_forms(values):
            try:
                out = fn(*args, **kwargs)
            except TypeError as e:
                tried.append(f"{kind}: {e}")
                continue        # 形参不符：换下一种候选形态
            except Exception as e:  # noqa: BLE001 真调用失败 → 丢弃连接对象，交给上层重连重试
                self._api = None
                raise ConnectionLostError(
                    f"通达信官方 SDK {name} 调用失败（连接已作废）: "
                    f"{e.__class__.__name__}: {e}") from e
            self._forms[name] = kind
            return out
        raise AdapterError(f"通达信官方 SDK {name} 的候选调用形态全部不匹配；"
                           f"真实签名需在线核对（试过: {'; '.join(tried)}）")

    @staticmethod
    def _candidate_forms(values: dict[str, Any]) -> list[tuple[str, tuple, dict]]:
        """候选形态：互斥的键（如 stock_list/symbols）不组进同一个关键字包。"""
        out: list[tuple[str, tuple, dict]] = []
        exclusive = [k for k in values if k in ("stock_list", "symbols")]
        common = {k: v for k, v in values.items() if k not in exclusive}
        for extra in ([e for e in exclusive if values.get(e) is not None] + [None]):
            kwargs = dict(common)
            if extra:
                kwargs[extra] = values[extra]
            if kwargs:
                out.append((f"kwargs:{sorted(kwargs)}", (), kwargs))
        positional = [v for v in values.values() if v is not None]
        if positional:
            out.append((f"positional:{len(positional)}", tuple(positional), {}))
        short = [v for k, v in values.items() if k != "start" and v is not None]
        if 0 < len(short) < len(positional):
            out.append((f"positional-nostart:{len(short)}", tuple(short), {}))
        if not values:
            out.append(("noargs", (), {}))
        return out


_TRANSPORT_BUILDERS = {
    "pytdx": lambda servers, timeout: TdxPytdxTransport(servers=servers, timeout=timeout),
    "official": lambda servers, timeout: TdxOfficialTransport(),
}


# --------------------------------------------------------------------------
# 适配器
# --------------------------------------------------------------------------
class _Subscription:
    """一次 subscribe_realtime 调用 = 一个订阅记录（回调 + 它关心的标的）。"""

    __slots__ = ("sid", "callback", "symbols")

    def __init__(self, sid: int, callback: Callable[[Tick], None], symbols: list[str]) -> None:
        self.sid = sid
        self.callback = callback
        self.symbols = list(symbols)


class _Request:
    """投给工作线程的一次 SDK 调用。"""

    __slots__ = ("op", "args", "kwargs", "done", "result", "error", "enqueued")

    def __init__(self, op: str, args: tuple, kwargs: dict) -> None:
        self.op = op
        self.args = args
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.enqueued = time.monotonic()


class _Stop:
    __slots__ = ()


_STOP_REQUEST = _Stop()
_STOP_TICK = _Stop()


class TdxAiDataFeed(DataFeedAdapter):
    """通达信行情适配器（官方 SDK / pytdx 二者择一）。

    ``requires`` 声明的是**可选 SDK 全集**，配 ``requires_any = True`` 告诉注册表
    「任一可用即就绪」——只装了 pytdx 时面板不再误报 unready。用 pytdx 时传输层
    是否为官方口径，由 ``describe()["transport_official"]`` 如实上报。
    """

    name = "tdx_aidata"
    label = "通达信行情（官方数据组件 / 免费开源版）"
    prereq = ("二者择一：① 使用免费开源的通达信行情组件（免账号，直连通达信行情服务器，"
              "非官方实现）；② 安装通达信官方数据组件（需申请权限）。"
              "官方优先，缺省自动改用免费版")
    requires = ("TdxAiData", "pytdx")
    requires_any = True
    official = True          # 存在官方路径；用 pytdx 时传输层口径由 describe()["transport_official"] 如实上报
    default_enabled = True
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.REALTIME_TICK,
                              FeedCapability.TRANSACTION})

    def __init__(self, *, poll_interval: float = _DEFAULT_POLL_INTERVAL,
                 request_timeout: float = _REQUEST_TIMEOUT, min_interval: float = 0.05,
                 servers: Iterable[tuple[str, int]] | None = None,
                 transport: str | None = None, start_watchdog: bool = True,
                 max_workers: int = 1) -> None:
        if max_workers != 1:
            raise ValueError("max_workers 只能是 1：通达信 SDK 连接对象非线程安全，必须串行")
        s = get_settings()
        self._poll_interval = float(poll_interval)
        self._request_timeout = float(request_timeout)
        self._limiter = RateLimiter(min_interval)
        self._servers = list(servers) if servers else None
        self._forced = transport
        self._start_watchdog = bool(start_watchdog)

        self._transport: Any = None
        self._transport_reason = ""
        self._connected = False
        self._queue: queue.Queue | None = None
        self._worker: threading.Thread | None = None

        self._lock = threading.RLock()
        self._subs: dict[int, _Subscription] = {}
        self._sub_seq = 0
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._tick_queue: queue.Queue = queue.Queue()
        self._tick_thread: threading.Thread | None = None

        self._health_cache: tuple[float, bool] = (0.0, False)
        self._watchdog = ReconnectWatchdog(
            name=self.name,
            probe=self._probe,                       # 探活走 _probe（不读缓存），否则刚发生的断线会被旧缓存盖掉
            reconnect=self._reconnect_transport,
            on_reconnected=self._replay_subscriptions,
            interval=s.watchdog_interval_s,
            backoff=Backoff(base=s.backoff_base_s, cap=s.backoff_cap_s))

    # ---------- 生命周期 ----------
    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._transport = None
            self._transport_reason = ""
            transport = self._select_transport()
            self._queue = queue.Queue()
            self._worker = threading.Thread(target=self._worker_loop, name="tdx-worker",
                                            daemon=True)
            self._worker.start()
            self._connected = True        # _request 依赖该标志，必须先置位再投 open
            err = self._open_transport(transport)
            if err is not None:
                fallback = self._fallback_transport(transport, err)
                err2 = self._open_transport(fallback) if fallback is not None else err
                if err2 is not None:
                    self._connected = False
                    self._transport = None
                    self._stop_worker()
                    raise err2 if fallback is not None else err
                transport = fallback
            self._transport = transport
            log.info("通达信通道就绪：传输层=%s%s（%s）", transport.channel,
                     f" @{transport.server}" if getattr(transport, "server", None) else "",
                     self._transport_reason or "首选")
            if self._start_watchdog:
                self._watchdog.start()

    def _open_transport(self, transport: Any) -> Exception | None:
        """经工作线程完成握手（连接对象只在 worker 线程里创建），失败返回异常不抛。"""
        self._transport = transport
        try:
            self._request("open")
        except Exception as e:  # noqa: BLE001 由调用方决定降级还是原样抛出
            return e
        return None

    def _fallback_transport(self, cur: Any, err: Exception) -> Any:
        """官方 SDK 在场但握手失败时唯一的降级：改用 pytdx（原因如实记进 describe）。"""
        if self._forced or not getattr(cur, "is_official", False):
            return None
        if not dependency_available("pytdx"):
            return None
        log.warning("通达信官方 SDK 连接失败（%s），改用 pytdx 传输层", err)
        self._transport_reason = (
            f"官方 SDK 连接失败已降级 pytdx：{err.__class__.__name__}: {err}")
        return TdxPytdxTransport(servers=self._servers)

    def disconnect(self) -> None:
        """幂等：未连接时什么都不做。

        收尾顺序有讲究：停线程的 join 必须放在锁外——轮询/投递线程也要拿这把锁，
        持锁 join 会让两边互等满超时（实测：断开会白等 31s）。
        """
        with self._lock:
            if not self._connected:
                return
            self._watchdog.stop()
            try:
                self._request("close", timeout=5.0)
            except Exception as e:  # noqa: BLE001 断开是清理动作，失败留痕不外抛
                log.warning("通达信传输层关闭异常（忽略）: %s", e)
            self._connected = False       # 置 False 后新请求一律 fail-closed
            self._transport = None
            worker = self._worker
        self._stop_poller()
        self._stop_worker()
        if worker is not None:
            worker.join(timeout=2.0)

    def health_check(self) -> bool:
        """轻量探活，结果缓存 60s；任何异常返回 False（供 UI 状态灯与守护线程用）。"""
        now = time.monotonic()
        if now - self._health_cache[0] < _HEALTH_TTL:
            return self._health_cache[1]
        ok = self._probe()
        self._health_cache = (now, ok)
        return ok

    def _probe(self) -> bool:
        if not self._connected or self._transport is None:
            return False
        try:
            return int(self._request("probe")) > 0
        except Exception as e:  # noqa: BLE001 探活吞异常（可探测语义）
            log.warning("通达信探活失败: %s", e)
            return False

    # ---------- 传输层选择 ----------
    def _select_transport(self) -> Any:
        """官方 SDK 优先，缺失（或连不上）降级 pytdx；两者都没有则抛带引导的 AdapterError。"""
        forced = (self._forced or "").strip().lower()
        official_mod = dependency_available(_OFFICIAL_MODULE)
        pytdx_mod = dependency_available("pytdx")
        if forced in _TRANSPORT_BUILDERS:
            if forced == "pytdx" and not pytdx_mod:
                raise AdapterError(f"通达信 pytdx 传输层不可用：pytdx 未安装。{_INSTALL_HINT_BOTH}")
            if forced == "official" and not official_mod:
                raise AdapterError(
                    f"通达信官方 SDK（TdxAiData）未安装：{_INSTALL_HINT_BOTH}")
            self._transport_reason = f"按 transport={forced} 强制指定"
            return _TRANSPORT_BUILDERS[forced](self._servers, 5.0)
        if official_mod:
            self._transport_reason = "官方 SDK 在用"
            return TdxOfficialTransport()
        if pytdx_mod:
            self._transport_reason = "官方 SDK 缺失，降级 pytdx"
            log.info("通达信官方 SDK（TdxAiData）未安装，使用开源 pytdx 传输层")
            return TdxPytdxTransport(servers=self._servers)
        raise AdapterError(
            f"通达信通道未就绪：TdxAiData 与 pytdx 都不存在（二者择一即可）。{_INSTALL_HINT_BOTH}")

    # ---------- 工作线程与请求队列 ----------
    def _worker_loop(self) -> None:
        """唯一触碰 SDK 连接对象的线程：请求串行消费（连接对象非线程安全）。"""
        q = self._queue
        assert q is not None
        while True:
            item = q.get()
            if item is _STOP_REQUEST or q is None:
                return
            req: _Request = item
            try:
                req.result = self._dispatch(req)
            except BaseException as e:  # noqa: BLE001 异常原样回传调用方
                req.error = e
            finally:
                req.done.set()

    def _dispatch(self, req: _Request) -> Any:
        if req.op == "open":
            self._transport.open()
            return None
        if req.op == "close":
            self._transport.close()
            return None
        if req.op == "probe":
            return self._transport.probe()
        self._limiter.acquire()
        try:
            return self._invoke(req.op, req.args)
        except ConnectionLostError as e:
            # 宪法 DataFeed 约定：单次调用内部最多重试 1 次（重连后重来），
            # 重试风暴与跨源降级归上层 FederatedFeed
            log.warning("通达信 %s 调用断线，重连后重试一次: %s", req.op, e)
            self._transport.open()
            return self._invoke(req.op, req.args)

    def _invoke(self, op: str, args: tuple) -> Any:
        fn = getattr(self._transport, op, None)
        if fn is None:
            raise FeedCapabilityError(f"通达信传输层不支持操作 {op}")
        return fn(*args)

    def _request(self, op: str, *args: Any, timeout: float | None = None) -> Any:
        """投递请求给工作线程并同步等待结果（带超时）。"""
        with self._lock:
            q, worker = self._queue, self._worker
            if not self._connected or q is None or worker is None or not worker.is_alive():
                raise ConnectionLostError(f"通达信通道未连接（op={op}）：先 connect()")
        req = _Request(op, args, {})
        q.put(req)
        if not req.done.wait(timeout if timeout is not None else self._request_timeout):
            # 结果不确定：不重发（同一连接对象上重发可能拿到上一条的包），由调用方按断线处理
            raise ConnectionLostError(
                f"通达信 {op} 请求超时（{self._request_timeout:.1f}s，工作线程未响应）")
        if req.error is not None:
            raise req.error
        return req.result

    def _stop_worker(self) -> None:
        q = self._queue
        if q is not None:
            q.put(_STOP_REQUEST)
        self._queue = None

    # ---------- 历史 K 线 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        """历史 K 线（**只提供未复权**）。

        adjust=FORWARD/BACKWARD 直接抛 FeedCapabilityError：pytdx 侧只有原始价，
        本地也没有通达信复权因子，任何「按比例折算」都是伪造数据（宪法第 5 条 +
        P1 复权口径红线）。校验在发任何 SDK 请求之前完成。
        """
        if adjust is not AdjustType.NONE:
            channel = getattr(self._transport, "channel", "未选择")
            raise FeedCapabilityError(_ADJUST_REFUSED.format(channel=channel, adjust=adjust.value))
        if freq is Frequency.TICK:
            raise FeedCapabilityError(
                "通达信通道历史逐笔请用 get_transactions()（K 线接口无 tick 周期）")
        if freq not in PYTDX_BAR_CATEGORY:
            raise FeedCapabilityError(f"通达信通道不支持周期 {freq}")
        market, code = market_and_code(symbol)          # 非法/北交所代码在这里就拒
        period = (OFFICIAL_PERIODS[freq] if self._transport_is_official()
                  else PYTDX_BAR_CATEGORY[freq])
        if self._transport is None:
            raise ConnectionLostError("通达信通道未连接：先 connect()")

        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        collected: list[dict] = []
        seen: set[pd.Timestamp] = set()
        offset = 0
        for _ in range(MAX_BAR_PAGES):
            page = self._request("bars", period, market, code, offset, BAR_PAGE_SIZE) or []
            if not page:
                break
            chunk = normalize_bars(page, freq)
            if chunk.empty:
                log.warning("通达信 K 线页无法解析（period=%s %s）：字段名需在线核对",
                            period, symbol)
                break
            if not (set(chunk["dt"]) - seen):
                break         # 整页都是已知时间戳：偏移语义与预期不符，停止翻页（防死循环）
            seen.update(chunk["dt"])
            collected.extend(chunk.to_dict("records"))
            if chunk["dt"].iloc[0] <= start_ts or len(chunk) < BAR_PAGE_SIZE:
                break                                   # 够到区间左端 / 数据取完
            offset += len(page)
        if not collected:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        df = pd.DataFrame(collected, columns=HISTORY_COLUMNS)
        df["dt"] = pd.to_datetime(df["dt"])
        df = df[(df["dt"] >= start_ts) & (df["dt"] <= end_ts)]
        return (df.drop_duplicates(subset=["dt"], keep="last").sort_values("dt")
                .reset_index(drop=True))

    def _transport_is_official(self) -> bool:
        return bool(getattr(self._transport, "is_official", False))

    # ---------- 分笔 ----------
    def get_transactions(self, symbol: str, count: int = 1000, start: int = 0) -> pd.DataFrame:
        """分笔成交（TRANSACTION 能力）：[dt, price, volume(手), num, direction]，dt 升序。

        无数据返回空表（不抛）。
        """
        market, code = market_and_code(symbol)
        if self._transport is None:
            raise ConnectionLostError("通达信通道未连接：先 connect()")
        recs = self._request("transactions", market, code, start, int(count)) or []
        return normalize_transactions(recs)

    # ---------- 实时（轮询快照合成 Tick）----------
    def subscribe_realtime(self, symbols: list[str],
                           callback: Callable[[Tick | Any], None]) -> None:
        """订阅实时快照（增量追加）。

        pytdx 没有推送能力，这里用轮询线程按 ``get_security_quotes`` 合成 Tick，
        周期 ``poll_interval``（默认 3s）——延迟与推送源不同，属本通道固有口径。
        同一标的被多个策略订阅时，外部只轮询一份（引用计数）。
        """
        if not callable(callback):
            raise TypeError("subscribe_realtime 需要 callback 可调用对象")
        with self._lock:
            if not self._connected:
                raise ConnectionLostError("通达信通道未连接：先 connect()")
            # 先全部校验（非法/北交所代码直接抛），再落订阅表——不留半截订阅
            for s in symbols:
                market_and_code(s)
            if not symbols:
                return
            self._sub_seq += 1
            self._subs[self._sub_seq] = _Subscription(self._sub_seq, callback, list(symbols))
            self._ensure_poller()

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        """从轮询集合移除；引用计数归零才真正停轮询线程。"""
        target = set(symbols)
        with self._lock:
            for sub in list(self._subs.values()):
                kept = [s for s in sub.symbols if s not in target]
                if len(kept) != len(sub.symbols):
                    sub.symbols = kept
                if not sub.symbols:
                    self._subs.pop(sub.sid, None)
            stop = not self._has_references()
        if stop:                      # 引用计数归零才停；join 不能在锁内（见 disconnect 注释）
            self._stop_poller()

    def _refcount(self, symbol: str) -> int:
        return sum(1 for sub in self._subs.values() if symbol in sub.symbols)

    def _has_references(self) -> bool:
        return any(bool(sub.symbols) for sub in self._subs.values())

    def _poll_symbols(self) -> list[str]:
        out: list[str] = []
        for sub in self._subs.values():
            for s in sub.symbols:
                if s not in out:
                    out.append(s)
        return out

    def _ensure_poller(self) -> None:
        if not self._has_references():
            return
        if self._tick_thread is None or not self._tick_thread.is_alive():
            self._tick_thread = threading.Thread(target=self._tick_loop, name="tdx-tick",
                                                 daemon=True)
            self._tick_thread.start()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(target=self._poll_loop, name="tdx-poller",
                                                 daemon=True)
            self._poll_thread.start()
            log.info("通达信轮询已启动（周期 %.1fs，标的 %d 个）",
                     self._poll_interval, len(self._poll_symbols()))

    def _stop_poller(self) -> None:
        """停轮询与投递线程（幂等）。调用方不得持 `self._lock`（要 join 这两条线程）。"""
        with self._lock:
            self._poll_stop.set()
            poll_thread, self._poll_thread = self._poll_thread, None
            tick_thread, self._tick_thread = self._tick_thread, None
        self._tick_queue.put(_STOP_TICK)      # 投递线程可能阻塞在 get()：用哨兵叫醒
        for t in (poll_thread, tick_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                self._poll_once()
            except ConnectionLostError as e:
                if self._poll_stop.is_set() or not self._connected:
                    return            # 正在断开：这里的失败是收尾噪声，不报断线
                # 轮询线程只标记断线，重连与重放归 watchdog（线程纪律：不在这里重连）
                self._watchdog.mark_lost(f"通达信轮询断线: {e}")
                self._poll_stop.wait(self._poll_interval)
            except Exception as e:  # noqa: BLE001 轮询异常不退出线程，留痕继续
                log.warning("通达信轮询异常: %s", e)
            else:
                self._poll_stop.wait(self._poll_interval)

    def _poll_once(self) -> int:
        """一轮快照请求 → Tick 入投递队列（这里只入队，回调在投递线程里跑）。"""
        symbols = self._poll_symbols()
        if not symbols:
            return 0
        pairs = [market_and_code(s) for s in symbols]
        records = self._request("quotes", pairs) or []
        ts = datetime.now()
        n = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            tick = quote_to_tick(rec, ts)
            if tick is None:
                continue
            self._tick_queue.put(tick)
            n += 1
        return n

    def _tick_loop(self) -> None:
        while True:
            item = self._tick_queue.get()
            if item is _STOP_TICK:
                return
            tick: Tick = item
            with self._lock:
                subs = list(self._subs.values())
            for sub in subs:
                if tick.symbol not in sub.symbols:
                    continue
                try:
                    sub.callback(tick)
                except Exception:  # noqa: BLE001 单个回调异常隔离，不影响其他订阅者
                    log.exception("通达信订阅回调异常（已隔离）sid=%s", sub.sid)

    # ---------- 断线自愈 ----------
    @property
    def watchdog(self) -> ReconnectWatchdog:
        return self._watchdog

    def _reconnect_transport(self) -> None:
        if self._transport is None:
            raise ConnectionLostError("通达信传输层不存在：先 connect()")
        self._transport.close()
        self._transport.open()
        self._health_cache = (time.monotonic(), True)   # 刚重连：缓存一次成功，避免重复探活

    def _replay_subscriptions(self) -> None:
        """宪法义务：重连成功后重放订阅。

        pytdx 是请求式快照，重连后把订阅集合原样交给轮询线程即可（集合本身在内存里，
        不依赖连接对象）；这里做两件事：确保轮询线程活着 + 立即采一轮，让前端 1 个周期内
        重新有数（不是等下一个 tick 才发现还活着）。
        """
        with self._lock:
            if not self._connected or not self._has_references():
                return
            self._ensure_poller()
        try:
            self._poll_once()
        except ConnectionLostError as e:
            log.warning("通达信重连后重放订阅仍失败: %s", e)

    # ---------- 财务（本通道不提供）----------
    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        raise FeedCapabilityError(
            f"通达信通道不提供财务数据（capabilities 不含 FINANCIAL）：symbol={symbol}")

    # ---------- 运维 ----------
    def describe(self) -> dict:
        """如实上报当前传输层（UI 接入向导与日志都读这里，不猜）。"""
        with self._lock:
            transport = getattr(self._transport, "channel", None)
            symbols = self._poll_symbols()
            return {
                "name": self.name,
                "label": self.label,
                "connected": self._connected,
                "transport": transport or "未选择",
                "transport_reason": self._transport_reason,
                "transport_official": bool(getattr(self._transport, "is_official", False)),
                "server": getattr(self._transport, "server", None),
                "resolved_methods": getattr(self._transport, "resolved", {}),
                "polling": self._poll_thread is not None and self._poll_thread.is_alive(),
                "poll_interval_s": self._poll_interval,
                "subscribed": symbols,
                "refcounts": {s: self._refcount(s) for s in symbols},
                "adjust_supported": ["none"],
                "volume_unit": "手（1 手 = 100 股，与 pytdx 源口径一致，未换算）",
            }

    @classmethod
    def deps_ready(cls) -> bool:
        """二者择一的就绪判定（注册表 requires 语义是「全部存在」，与本机实况不符时用这个）。"""
        return dependency_available(_OFFICIAL_MODULE) or dependency_available("pytdx")


ADAPTER = TdxAiDataFeed
