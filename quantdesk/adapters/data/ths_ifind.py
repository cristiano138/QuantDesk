"""同花顺 iFinD 数据适配器（付费专业源：财务主源 + 历史行情 + 实时快照）。

项目定位（宪法：feed_priority_financial = "ths_ifind,tushare,akshare"）：
- **财务降级链第一优先源**：iFinD 是三类报表 + 财务指标口径最完整的一档；
  代价是它的报表结构与其他源完全不同 → 本适配器负责归一化为统一长表
  （列定义见 FINANCIAL_COLUMNS），上层（FederatedFeed/存储）只见统一列；
- 历史行情：THS_HistoryQuotes，复权口径由服务端参数控制（不像 tushare 本地算因子）；
- 实时：THS_RealTimeQuotes 快照式订阅，回调线程只入队，归一化在工作线程做。

治理要点（宪法逐条对应）：
1. 顶层不 import iFinDPy（同花顺私有 whl，非主依赖）——方法内 import_optional，
   缺失即抛带安装引导的 AdapterError，注册表如实上报「未就绪」；
2. 单次调用内部最多重试 1 次：会话失效 → 重登一次 → 重试一次，再失败就抛
   ConnectionLostError；重试风暴与降级归 FederatedFeed；
3. iFinDPy 是**进程级单会话** SDK（登录态全局），多线程并发调用不安全：
   本适配器用 _api_lock 串行化一切 SDK 调用，重登与请求队列因此天然互斥
   （已知坑：重登线程与在途请求竞争登录态）；
4. 配额纪律（付费源核心）：当日调用计数落盘（跨重启不丢），超阈值**只读缓存不外呼**；
   同一请求第二次命中缓存时配额计数不增加（缓存分三层：进程内 → Parquet 强缓存 →
   财务长表缓存）；
5. 日志脱敏：账号/token 一律 secrets.mask()（前 4 位 + ***），明文永不进日志；
6. 凭据取用**单点收口** _resolve_token() / _resolve_credentials()：构造参数 →
   注入的 callable → 保险库（core/secrets.py，键 ifind.refresh_token 等）→
   settings 字段（不存在则空串）。主线程替换凭据来源只改 _secret_lookup 一个函数。

待在线核对（真实账号到位后按 docs/integration/ths_ifind.md 的清单逐条校准）：
指标名、CTP/Interval 参数取值、table 字段名、财务指标 ID、真实 errorcode 取值表。
本文件把这些全部放在顶部映射常量里，核对后只改常量不动逻辑。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.adapters.base.optional import import_optional
from quantdesk.config import get_settings
from quantdesk.core import secrets
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import (
    AdapterError,
    ConfigError,
    ConnectionLostError,
    FeedCapabilityError,
    QuantDeskError,
)
from quantdesk.core.models import Tick
from quantdesk.core.ratelimit import RateLimiter
from quantdesk.store.parquet_store import ParquetStore

log = logging.getLogger(__name__)

CHANNEL = "同花顺 iFinD"
SDK_MODULE = "iFinDPy"
INSTALL_HINT = (
    "安装同花顺随账号提供的行情组件（iFinDPy）；组件与账号绑定，换电脑需重新授权"
)
CREDENTIAL_GUIDE = (
    "iFinD 账号未配置：需先由同花顺开通试用或正式账号，再在设置页填写 "
    "iFinD 账号与密码（或授权 token），加密保存在本机。")

# ---------- 历史行情映射（待在线核对：只改这些常量） ----------
_HQ_INDICATORS = "thstime,open,high,low,close,volume,amount"
# 复权口径 → iFinD 复权参数 CTP 取值（官方文档口径：none/front/back/uniform 字符串族）
_CTP = {AdjustType.NONE: "none", AdjustType.FORWARD: "front", AdjustType.BACKWARD: "back"}
# 周期 → iFinD Interval 取值（日线族用 D/W/M，分钟族用分钟数字）
_INTERVAL = {Frequency.D1: "D", Frequency.W1: "W", Frequency.MO1: "M",
             Frequency.M1: "1", Frequency.M5: "5", Frequency.M15: "15",
             Frequency.M30: "30", Frequency.H1: "60"}
_DAILY_FREQS = frozenset({Frequency.D1, Frequency.W1, Frequency.MO1})
# HISTORY_COLUMNS → iFinD table 字段候选名（第一个命中即用）
_HQ_FIELDS: dict[str, tuple[str, ...]] = {
    "dt": ("thstime", "time"),
    "open": ("open",), "high": ("high",), "low": ("low",), "close": ("close",),
    "volume": ("volume",), "amount": ("amount",),
}
# 强缓存的区间覆盖判定容差（端点落在非交易日/停牌期时不至于误判为缺口）
_COVERAGE_TOL_DAYS = {True: 10, False: 4}    # dict[是否日线族, 容差天数]

# ---------- 财务映射（统一长表契约；指标 ID 待在线核对） ----------
FINANCIAL_COLUMNS = ["symbol", "report_type", "period", "indicator", "value",
                     "unit", "update_time"]
_FIN_REPORT_TYPE = ("income", "balance", "cashflow", "indicator")
# report_type → 报表指标集 ID（iFinD 财务类指标，在线核对后替换）
_FIN_SHEET = {
    "income": "ths_profit_sheet_stock",
    "balance": "ths_balance_sheet_stock",
    "cashflow": "ths_cash_flow_statement_stock",
    "indicator": "ths_fina_indicator_stock",
}
# report_type → THS_BasicData/THS_FeedBatch 功能参数
#   F1=报表类型码 F2=频率(101=年报) F3=数据类型 F4=截止日期 F5=返回条数
_FIN_FUNC_PARAMS = {
    "income": {"F1": "I100000351", "F2": "101", "F3": "101", "F4": "", "F5": "1000"},
    "balance": {"F1": "I100000352", "F2": "101", "F3": "101", "F4": "", "F5": "1000"},
    "cashflow": {"F1": "I100000353", "F2": "101", "F3": "101", "F4": "", "F5": "1000"},
    "indicator": {"F1": "I100000835", "F2": "101", "F3": "101", "F4": "", "F5": "1000"},
}
_META_KEYS = frozenset({"thscode", "time", "name", "updatetime", "unit", "table", "tbl"})

# ---------- 实时映射（快照式；接口名候选按可用性择一） ----------
_RT_FUNC_NAMES = ("THS_RealTimeQuotes", "THS_RealtimeQuotes", "THS_SubscribeSecurity")
_RT_UNSUB_FUNC_NAMES = ("THS_UnsubscribeRealTime", "THS_UnSubscribeRealTime",
                        "THS_CancelSubscribeSecurity")
_RT_LEVELS = {"bid": ("bidPrice", "bidVolume"), "ask": ("askPrice", "askVolume")}
_RT_FIELDS: dict[str, tuple[str, ...]] = {
    "last": ("latest", "last", "trade"),
    "open": ("open",), "high": ("high",), "low": ("low",),
    "volume": ("volume",), "amount": ("amount",),
}
_RT_TIME_KEYS = ("time", "thstime", "lastTime", "tradeTime")
_RT_INDICATORS = ",".join(
    ["rt_time"] + [v[0] for v in _RT_FIELDS.values()]
    + [f"{pfx}{i}" for pfx, _ in _RT_LEVELS.values() for i in range(1, 6)]
    + [f"{vol}{i}" for _, vol in _RT_LEVELS.values() for i in range(1, 6)]
)

# ---------- errorcode 语义（真实取值表待在线核对，见 docs 清单） ----------
_OK_CODE = 0
# 指示「未登录/会话失效」的 errorcode：命中即触发一次重登
_SESSION_CODES = frozenset({-1010, -1011, -1014, -1302})
_SESSION_KEYWORDS = ("未登录", "登录失效", "登录过期", "重新登录", "会话失效",
                     "session", "not login", "unauthorized", "token expire",
                     "access_token invalid")
# 需要按「通道断线」上报（health_check 可探测）的 errorcode
_CONNECTION_CODES = frozenset({-201, -301, -1010, -1011, -1014, -1302})

_DEFAULT_DAILY_QUOTA = 3000        # 保守默认：Settings 无 ifind_daily_quota 字段时使用
_DEFAULT_MIN_INTERVAL = 0.2        # 同上：Settings 无 ifind_min_interval 字段时使用
_MAX_MEM_FRAMES = 64               # 进程内缓存帧数上限（防长跑内存膨胀）
_FIN_CACHE_TTL = 6 * 3600.0        # 财务长表磁盘缓存有效期（秒）

__all__ = ["ThsIFindFeed", "ADAPTER", "FINANCIAL_COLUMNS"]


# ================= 纯函数：响应解析与归一化（离线单测直接覆盖） =================

def _errorcode(resp: Any) -> int:
    """取 errorcode（非 dict / 缺字段一律按未知错误码 -999999 处理，不静默当成功）。"""
    if isinstance(resp, dict):
        code = resp.get("errorcode")
        if isinstance(code, (int, float)):
            return int(code)
        return -999999
    return -999999


def _errmsg(resp: Any) -> str:
    if isinstance(resp, dict):
        return str(resp.get("errmsg") or resp.get("message") or "")
    return ""


def _is_session_invalid(resp: Any) -> bool:
    """会话失效判定：errorcode 命中集合，或 errmsg 命中关键字（真实码表待核对，双保险）。"""
    code = _errorcode(resp)
    if code == _OK_CODE:
        return False
    if code in _SESSION_CODES:
        return True
    msg = _errmsg(resp).lower()
    return any(k in msg for k in _SESSION_KEYWORDS)


def _as_list(value: Any, n: int) -> list[Any]:
    """iFinD 字段值可能是标量、也可能是与 time 等长的数组；统一成长度 n 的 list。"""
    if isinstance(value, (list, tuple)):
        vals = list(value)
    else:
        vals = [value] * n
    if len(vals) < n:
        vals = vals + [None] * (n - len(vals))
    return vals[:n]


def _records_of(resp: Any) -> list[dict]:
    """响应里的 data 归一为记录列表（兼容 dict / list / 缺失）。"""
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, (list, tuple)):
        return [r for r in data if isinstance(r, dict)]
    return []


def _times_of(rec: dict, table: dict) -> list[Any]:
    """时间点：优先记录级 time（iFinD HistoryQuotes 口径），退化到 table 内 thstime。"""
    for src in (rec.get("time"), table.get("thstime"), table.get("time")):
        if src is None:
            continue
        if isinstance(src, (list, tuple)):
            return list(src)
        return [src]
    return []


def _parse_history_response(resp: Any) -> pd.DataFrame:
    """THS_HistoryQuotes 响应 → HISTORY_COLUMNS 归一化（纯函数，无网络）。

    结构（待在线核对）::

        {'errorcode': 0, 'errmsg': '',
         'data': [{'thscode': '600000.SH',
                   'time': ['2024-01-02', '2024-01-03'],
                   'table': {'open': [10.0, 10.1], 'high': [...], ...}}]}

    兼容点：time 在记录级或 table 内、字段值为标量或数组、多标的多记录拼接。
    字段缺失一律抛 FeedCapabilityError（接口变动的信号），不静默补 0。
    """
    frames: list[pd.DataFrame] = []
    for rec in _records_of(resp):
        table = rec.get("table")
        if not isinstance(table, dict):
            table = rec                      # 退化：字段直接挂在记录上
        times = _times_of(rec, table)
        if not times:
            continue
        n = len(times)
        cols: dict[str, list[Any]] = {}
        missing: list[str] = []
        for field in HISTORY_COLUMNS[1:]:
            value = _first_present(table, _HQ_FIELDS[field])
            if value is None:
                missing.append(field)
                continue
            cols[field] = _as_list(value, n)
        if missing:
            raise FeedCapabilityError(
                f"{CHANNEL} 历史响应缺少字段 {missing}（table 字段名可能已变动，"
                f"检查 ths_ifind.py 顶部 _HQ_FIELDS 映射）")
        frames.append(pd.DataFrame({"dt": times, **cols}))
    if not frames:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return _finalize_history(out)


def _first_present(mapping: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _finalize_history(out: pd.DataFrame) -> pd.DataFrame:
    """严格 HISTORY_COLUMNS + dt datetime64[ns] 升序 + 数值列统一 float（宪法列契约）。

    数值列强制 float64：iFinD 把 volume 当整数返回，混 int64/float64 会让
    Parquet 分片间 dtype 漂移，下游合并/回测口径不稳。
    """
    out = out[HISTORY_COLUMNS].copy()
    out["dt"] = pd.to_datetime(out["dt"], errors="coerce")
    for c in HISTORY_COLUMNS[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out = out.dropna(subset=["dt"])
    out = out.drop_duplicates(subset=["dt"], keep="last")
    return out.sort_values("dt").reset_index(drop=True)


def _coerce_value(value: Any) -> Any:
    """财务指标值：能转数值就转（int/float），否则原样保留（字符串类指标合法）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return value
    return value


def _empty_financial() -> pd.DataFrame:
    return pd.DataFrame(columns=FINANCIAL_COLUMNS)


def _walk_financial_record(rec: dict) -> list[tuple[Any, str, Any, str]]:
    """单条财务记录 → [(period, indicator, value, unit)]（抹平三张表的结构差异）。

    支持四种实测/文档形态（真实形态以在线核对为准，均落到同一长表）：
      A table 为 {指标: [各期值]}，time 为标量或等长数组；
      B tbl 为 [{'indicator': .., 'value': ..}, ...] 的列表；
      C tbl 为 [{指标: [值]}, ...]（每期一个 dict）；
      D 无 table/tbl：指标直接挂在记录上。
    """
    rows: list[tuple[Any, str, Any, str]] = []
    unit_map = rec.get("unit") if isinstance(rec.get("unit"), dict) else {}

    def emit(period: Any, indicator: str, raw: Any, unit: str = "") -> None:
        vals = raw if isinstance(raw, (list, tuple)) else [raw]
        for idx, v in enumerate(vals):
            value = _coerce_value(v)
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue                     # 空值行不进长表（不伪造数值）
            p = period
            if isinstance(period, (list, tuple)):
                p = period[idx] if idx < len(period) else None
            rows.append((p, indicator, value, unit or str(unit_map.get(indicator, ""))))

    for container in _fin_containers(rec):
        if isinstance(container, list):                 # 形态 B/C
            for item in container:
                if not isinstance(item, dict):
                    continue
                if "indicator" in item:                 # B
                    emit(item.get("period") or rec.get("time"), str(item["indicator"]),
                         item.get("value"), str(item.get("unit", "")))
                    continue
                period = item.get("time") or item.get("period") or rec.get("time")
                for key, raw in item.items():           # C
                    if key in _META_KEYS:
                        period = raw if key == "time" else period
                        continue
                    emit(period, key, raw)
            continue
        # 形态 A/D：{指标: 值或值数组}
        period = container.get("time") or rec.get("time")
        for key, raw in container.items():
            if key in _META_KEYS or key == "time":
                continue
            emit(period, key, raw)
    return rows


def _fin_containers(rec: dict) -> list[Any]:
    """记录内的数据容器：table(dict) / tbl(list) / 记录本身。"""
    for key in ("table", "tbl"):
        value = rec.get(key)
        if isinstance(value, dict) and value:
            return [value]
        if isinstance(value, list) and value:
            return [value]
    flat = {k: v for k, v in rec.items() if k not in _META_KEYS}
    return [flat] if flat else []


def _parse_financial_response(resp: Any, symbol: str, report_type: str) -> pd.DataFrame:
    """财务报表响应 → 统一长表（列固定 FINANCIAL_COLUMNS，纯函数）。

    这是 iFinD 作为财务降级链第一优先源的主要代价：三张表 + 指标集返回结构不同，
    必须在本适配器内抹平，上层只见统一列（口径仍需与 tushare/akshare 交叉对数）。
    """
    rows: list[dict[str, Any]] = []
    for rec in _records_of(resp):
        code = str(rec.get("thscode") or symbol)
        update_time = str(rec.get("updatetime") or rec.get("endtime") or "")
        for period, indicator, value, unit in _walk_financial_record(rec):
            rows.append({
                "symbol": code,
                "report_type": report_type,
                "period": None if period is None else str(period),
                "indicator": indicator,
                "value": value,
                "unit": unit,
                "update_time": update_time,
            })
    if not rows:
        return _empty_financial()
    out = pd.DataFrame(rows, columns=FINANCIAL_COLUMNS)
    out = out.drop_duplicates(subset=["symbol", "report_type", "period", "indicator"],
                              keep="last").reset_index(drop=True)
    return out.sort_values(["period", "indicator"], na_position="first").reset_index(drop=True)


def _snapshot_table(raw: dict) -> tuple[str, dict, Any]:
    """实时推送记录 → (thscode, 扁平字段 dict, time)。列表值取首元素（快照只有一条）。"""
    code = str(raw.get("thscode") or "")
    table: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("thscode", "table", "tbl"):
            continue
        table[key] = value
    for src in (raw.get("table"), raw.get("tbl")):
        items = []
        if isinstance(src, dict):
            items = [src]
        elif isinstance(src, list):
            items = [i for i in src if isinstance(i, dict)]
        for item in items:
            for key, value in item.items():
                table[key] = value[0] if isinstance(value, list) and value else value
    t = raw.get("time")
    if isinstance(t, (list, tuple)):
        t = t[0] if t else None
    if t is None:
        t = table.get("thstime") or table.get("time")
    return code, table, t


def _pick(table: dict, names: tuple[str, ...]) -> Any:
    v = _first_present(table, names)
    if isinstance(v, (list, tuple)):
        return v[0] if len(v) else None
    return v


def _levels(table: dict, side: str) -> list[tuple[float, float]]:
    """五档盘口：price_i / vol_i 成对取值，缺档就少一档（不补 0 造盘口）。"""
    price_key, vol_key = _RT_LEVELS[side]
    out: list[tuple[float, float]] = []
    for i in range(1, 6):
        price = _coerce_value(_pick(table, (f"{price_key}{i}",)))
        vol = _coerce_value(_pick(table, (f"{vol_key}{i}",)))
        if not isinstance(price, float):
            break
        out.append((price, vol if isinstance(vol, float) else 0.0))
    return out


def _parse_realtime_snapshot(raw: Any) -> Tick | None:
    """实时快照 → Tick（五档齐全）。无法解析返回 None（调用方计入丢弃计数）。"""
    if not isinstance(raw, dict):
        return None
    recs: list[dict] = []
    if isinstance(raw.get("data"), list):
        recs = [r for r in raw["data"] if isinstance(r, dict)]
    elif isinstance(raw.get("data"), dict):
        recs = [raw["data"]]
    else:
        recs = [raw]
    for item in recs:
        code, table, t = _snapshot_table(item)
        last = _coerce_value(_pick(table, _RT_FIELDS["last"]))
        if not code or not isinstance(last, float):
            continue
        dt = pd.to_datetime(t if t is not None else datetime.now(), errors="coerce")
        if pd.isna(dt):
            continue
        nums = {k: _coerce_value(_pick(table, names)) for k, names in _RT_FIELDS.items()}
        def num(key: str) -> float:
            v = nums.get(key)
            return v if isinstance(v, float) else (last if key == "last" else 0.0)
        return Tick(symbol=code, dt=dt.to_pydatetime(), last=num("last"),
                    open=num("open"), high=num("high"), low=num("low"),
                    volume=num("volume"), amount=num("amount"),
                    bids=_levels(table, "bid"), asks=_levels(table, "ask"))
    return None


def _mask(value: str) -> str:
    """日志脱敏：前 4 位 + ***（宪法日志脱敏条款，复用保险库的 mask 口径）。"""
    return secrets.mask(value)


# ================= 单点凭据解析 =================

def _secret_lookup(key: str, settings_attr: str) -> str:
    """凭据查找唯一出口：保险库 → Settings 字段（字段不存在 → 空串）。

    core/secrets.py 由主线程维护；本适配器只经这一个函数取用，替换来源不改别处。
    DPAPI 不可用（非 Windows / 保险库损坏）时按「未配置」处理，由调用方抛引导，
    绝不退化成明文兜底，也绝不在异常信息里带出凭据内容。
    """
    try:
        return secrets.resolve(key, settings_attr)
    except Exception:  # noqa: BLE001 — ConfigError 等一律按未配置处理
        return ""


def _settings_value(attr: str, default: Any) -> Any:
    """Settings 字段读取（本项目正在补 ifind_* 字段；字段不存在时用构造默认值）。"""
    return getattr(get_settings(), attr, default)


class ThsIFindFeed(DataFeedAdapter):
    """同花顺 iFinD 数据适配器（付费）。"""

    name = "ths_ifind"
    label = "同花顺 iFinD（付费）"
    prereq = ("开通同花顺 iFinD 账号 → 安装同花顺提供的行情组件 → 在设置页填写账号密码或授权 token；"
              "账号与设备绑定，换电脑需重新授权")
    requires = (SDK_MODULE,)
    official = True
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL,
                              FeedCapability.REALTIME_TICK, FeedCapability.TRANSACTION})

    def __init__(self, refresh_token: str = "", username: str = "", password: str = "",
                 *, token_provider: Callable[[], str] | None = None,
                 min_interval: float | None = None, daily_quota: int | None = None,
                 store_root: str | Path | None = None, cache_dir: str | Path | None = None,
                 health_cache_ttl: float = 60.0, fin_cache_ttl: float = _FIN_CACHE_TTL):
        """凭据全部可注入；缺省时走 _resolve_token()/_resolve_credentials() 的收口链路。

        min_interval / daily_quota：Settings 尚无 ifind_min_interval / ifind_daily_quota
        字段，字段缺失时回落到 _DEFAULT_* 常量（主线程补字段后自动生效，无需改本文件）。
        store_root / cache_dir 缺省走 data_dir()/cache/ifind（历史强缓存在其下 parquet/
        子目录，与主 parquet 分目录避免跨源覆盖）；单测传 tmp_path 即可完全离线。
        """
        self._refresh_token = refresh_token or ""
        self._username = username or ""
        self._password = password or ""
        self._token_provider = token_provider
        self._health_ttl = float(health_cache_ttl)
        self._fin_ttl = float(fin_cache_ttl)

        interval = min_interval if min_interval is not None else float(
            _settings_value("ifind_min_interval", _DEFAULT_MIN_INTERVAL))
        self._limiter = RateLimiter(interval)
        self._daily_quota = int(daily_quota if daily_quota is not None else
                                _settings_value("ifind_daily_quota", _DEFAULT_DAILY_QUOTA))

        self._store_root = Path(store_root) if store_root is not None else None
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._store: ParquetStore | None = None

        self._api_lock = threading.RLock()    # SDK 进程级单会话：串行化一切外呼与重登
        self._sdk_mod: Any = None
        self._logged_in = False
        self._login_date: date | None = None
        self._login_count = 0
        self._connected = False

        self._mem: dict[str, pd.DataFrame] = {}
        self._quota_date: str = date.today().isoformat()
        self._quota_used = 0
        self._quota_loaded = False
        self._quota_lock = threading.Lock()

        self._health_cache: tuple[float, bool] = (0.0, False)
        self._rt_queue: Queue = Queue()
        self._rt_callback: Callable[[Tick], Any] | None = None
        self._rt_symbols: set[str] = set()
        self._rt_worker: threading.Thread | None = None
        self._rt_stop = threading.Event()
        self._rt_dropped = 0

    # ---------- 生命周期 ----------
    def connect(self, **credentials: str) -> None:
        self._sdk()                                   # 惰性导入（缺失抛带引导的 AdapterError）
        if credentials.get("refresh_token"):
            self._refresh_token = str(credentials["refresh_token"])
        if credentials.get("username"):
            self._username = str(credentials["username"])
        if credentials.get("password"):
            self._password = str(credentials["password"])
        self._login(force=True)
        self._connected = True

    def disconnect(self) -> None:
        """幂等：停工作线程 + 尽力登出（SDK 异常不外抛，清理路径）。"""
        self._stop_realtime_worker()
        with self._api_lock:
            if self._sdk_mod is not None and self._logged_in:
                fn = getattr(self._sdk_mod, "THS_iFinDLogout", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001 登出失败只是会话残留，不阻塞清理
                        log.warning("%s 登出异常（忽略）: %s", CHANNEL, e)
            self._logged_in = False
            self._login_date = None
        self._connected = False

    def health_check(self) -> bool:
        """轻量探活：一次基本数据点调用，结果缓存 60s（UI 状态灯/看门狗高频调用）。"""
        now = time.monotonic()
        if now - self._health_cache[0] < self._health_ttl:
            return self._health_cache[1]
        try:
            sdk = self._sdk()
            fn = getattr(sdk, "THS_BasicData", None)
            if not callable(fn):
                raise FeedCapabilityError(f"{SDK_MODULE} 未提供 THS_BasicData，无法探活")
            resp = self._request(lambda _sdk: fn("600000.SH", "ths_stock_short_name_stock", {}),
                                 op="health_check")
            ok = _errorcode(resp) == _OK_CODE
        except Exception:  # noqa: BLE001 探活吞异常返回 False（可探测语义）
            log.debug("%s health_check 失败", CHANNEL, exc_info=True)
            ok = False
        self._health_cache = (now, ok)
        return ok

    # ---------- SDK / 会话 ----------
    def _sdk(self) -> Any:
        if self._sdk_mod is None:
            self._sdk_mod = import_optional(SDK_MODULE, channel=CHANNEL,
                                            install_hint=INSTALL_HINT)
        return self._sdk_mod

    def _resolve_token(self) -> str:
        """refresh token 取用收口：构造参数 → 注入 callable → 保险库/Settings。"""
        if self._refresh_token:
            return self._refresh_token
        if self._token_provider is not None:
            return str(self._token_provider() or "")
        return _secret_lookup("ifind.refresh_token", "ifind_refresh_token")

    def _resolve_credentials(self) -> tuple[str, str]:
        user = self._username or _secret_lookup("ifind.username", "ifind_username")
        pwd = self._password or _secret_lookup("ifind.password", "ifind_password")
        return user, pwd

    def _login(self, *, force: bool = False) -> None:
        """登录（refresh token 优先，二选一）。在 _api_lock 内执行：与请求队列互斥。

        force=False 时，同一自然日已登录直接复用会话——这就是「每日定时重登」的
        落地：跨天后的第一次调用会自动重登，APScheduler 也可显式调 refresh_session()。
        """
        today = date.today()
        with self._api_lock:
            if self._logged_in and not force and self._login_date == today:
                return
            sdk = self._sdk()
            token = self._resolve_token()
            user, pwd = self._resolve_credentials()
            if not token and not (user and pwd):
                raise ConfigError(CREDENTIAL_GUIDE)
            account, secret, mode = self._login_arguments(sdk, token, user, pwd)
            log.info("%s 登录（方式=%s，凭据=%s）", CHANNEL, mode, _mask(account or secret))
            fn = getattr(sdk, "THS_iFinDLogin", None)
            if not callable(fn):
                raise FeedCapabilityError(f"{SDK_MODULE} 未提供 THS_iFinDLogin（whl 版本不符）")
            try:
                resp = fn(account, secret)
            except Exception as e:  # noqa: BLE001 登录异常一律结构化上抛
                self._logged_in = False
                raise ConnectionLostError(f"{CHANNEL} 登录异常: {e.__class__.__name__}: {e}") from e
            code = _errorcode(resp)
            if code != _OK_CODE:
                self._logged_in = False
                raise ConnectionLostError(
                    f"{CHANNEL} 登录失败 errorcode={code} errmsg={_mask(_errmsg(resp))}")
            self._logged_in = True
            self._login_date = today
            self._login_count += 1
            log.info("%s 登录成功（累计登录 %d 次）", CHANNEL, self._login_count)

    def _login_arguments(self, sdk: Any, token: str, user: str,
                         pwd: str) -> tuple[str, str, str]:
        """两种登录方式收口成一对参数（真实接口签名待在线核对）。

        refresh token 方式：先用 iFinDGetAccessToken（若 whl 提供）换成 access_token，
        再以 access_token 作为账号位登录；whl 不提供该函数时直接把 refresh token 交给
        THS_iFinDLogin。账号密码方式：直接 THS_iFinDLogin(user, pwd)。
        """
        if token:
            getter = getattr(sdk, "iFinDGetAccessToken", None)
            if callable(getter):
                try:
                    resp = getter(token)
                except Exception as e:  # noqa: BLE001 换票失败退化为直连登录
                    log.warning("%s access token 换取异常（退化直登）: %s", CHANNEL, e)
                    return token, "", "refresh_token"
                if _errorcode(resp) == _OK_CODE:
                    data = resp.get("data")
                    at = ""
                    if isinstance(data, dict):
                        at = str(data.get("access_token") or "")
                    elif isinstance(data, str):
                        at = data
                    if at:
                        return at, "", "refresh_token(access_token)"
                raise ConnectionLostError(
                    f"{CHANNEL} refresh token 换 access_token 失败 "
                    f"errorcode={_errorcode(resp)}（token={_mask(token)}）")
            return token, "", "refresh_token"
        return user, pwd, "username_password"

    def _ensure_session(self) -> None:
        """请求前的会话保证：未登录就登，跨自然日自动重登（每日定时重登的兜底一半）。"""
        with self._api_lock:
            self._login()

    def refresh_session(self) -> None:
        """供 services 层 APScheduler 每日盘前调用（幂等；失败只记日志不打断调度）。"""
        try:
            self._login(force=True)
        except Exception as e:  # noqa: BLE001 调度侧不应因单次重登失败崩溃
            log.warning("%s 定时重登失败: %s", CHANNEL, e)

    # ---------- 外呼骨架（限速 + 配额 + 会话失效重试一次） ----------
    def _request(self, fn: Callable[[Any], Any], *, op: str = "") -> Any:
        """一次外呼：限速 → 发送 → 会话失效则重登一次并重试一次（宪法：最多重试 1 次）。

        只有真正走到这里才计配额（缓存命中不消耗）。持 _api_lock：重登不会与
        其他线程的在途请求竞争登录态。
        """
        with self._api_lock:
            self._ensure_session()
            sdk = self._sdk()
            self._limiter.acquire()
            self._count_request()
            resp = self._send(fn, sdk, op)
            if _is_session_invalid(resp):
                log.warning("%s 会话失效（op=%s, errorcode=%s），重登一次后重试",
                            CHANNEL, op, _errorcode(resp))
                self._login(force=True)
                self._limiter.acquire()
                self._count_request()
                resp = self._send(fn, sdk, op)
                if _is_session_invalid(resp):
                    self._logged_in = False
                    raise ConnectionLostError(
                        f"{CHANNEL} 重登后仍会话失效（op={op}, errorcode={_errorcode(resp)}），"
                        "已标记通道断线，交由上层降级/守护处理")
            code = _errorcode(resp)
            if code != _OK_CODE:
                if code in _CONNECTION_CODES:
                    self._logged_in = False
                    raise ConnectionLostError(
                        f"{CHANNEL} 调用失败（op={op}）errorcode={code} errmsg={_errmsg(resp)}")
                raise AdapterError(
                    f"{CHANNEL} 调用失败（op={op}）errorcode={code} errmsg={_errmsg(resp)}")
            return resp

    @staticmethod
    def _send(fn: Callable[[Any], Any], sdk: Any, op: str) -> Any:
        """SDK 调用异常归一为 ConnectionLostError（禁止裸异常冒泡；已结构化的原样上抛）。"""
        try:
            return fn(sdk)
        except QuantDeskError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ConnectionLostError(
                f"{CHANNEL} 外呼异常（op={op}）: {e.__class__.__name__}: {e}") from e

    # ---------- 配额计数（落盘，重启不丢） ----------
    def quota_status(self) -> dict:
        """当日配额快照（供适配器健康面板展示「iFinD 配额余量」）。"""
        with self._quota_lock:
            self._load_quota_once()
            self._roll_quota_day()
            return {"date": self._quota_date, "used": self._quota_used,
                    "limit": self._daily_quota,
                    "left": max(0, self._daily_quota - self._quota_used),
                    "exceeded": self._quota_used >= self._daily_quota}

    def _quota_file(self) -> Path:
        return self._cache_root() / "quota.json"

    def _load_quota_once(self) -> None:
        if self._quota_loaded:
            return
        self._quota_loaded = True
        p = self._quota_file()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 计数文件损坏 → 当日重新计数（保守，不猜已用量）
            log.warning("%s 配额文件不可读（按 0 重计）: %s", CHANNEL, e)
            return
        if str(raw.get("date", "")) == self._quota_date:
            self._quota_used = int(raw.get("used", 0) or 0)

    def _roll_quota_day(self) -> None:
        today = date.today().isoformat()
        if self._quota_date != today:
            self._quota_date, self._quota_used = today, 0
            self._persist_quota()

    def _count_request(self) -> None:
        with self._quota_lock:
            self._load_quota_once()
            self._roll_quota_day()
            self._quota_used += 1
            self._persist_quota()

    def _persist_quota(self) -> None:
        try:
            p = self._quota_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"date": self._quota_date, "used": self._quota_used,
                                     "limit": self._daily_quota}, ensure_ascii=False),
                         encoding="utf-8")
        except Exception as e:  # noqa: BLE001 计数落盘失败不影响主流程（内存计数仍有效）
            log.warning("%s 配额落盘失败: %s", CHANNEL, e)

    def _quota_exceeded(self) -> bool:
        with self._quota_lock:
            self._load_quota_once()
            self._roll_quota_day()
            return self._quota_used >= self._daily_quota

    # ---------- 缓存（进程内 + Parquet/长表磁盘强缓存） ----------
    def _cache_root(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = Path(_settings_value("data_dir", Path("data"))) / "cache" / "ifind"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _parquet(self) -> ParquetStore:
        if self._store is None:
            root = self._store_root if self._store_root is not None else \
                self._cache_root() / "parquet"      # 与主 parquet 分目录：不混淆源口径
            self._store = ParquetStore(root)
        return self._store

    def _mem_get(self, key: str) -> pd.DataFrame | None:
        df = self._mem.get(key)
        return df.copy() if df is not None else None

    def _mem_put(self, key: str, df: pd.DataFrame) -> None:
        self._mem[key] = df.copy()
        while len(self._mem) > _MAX_MEM_FRAMES:     # 先进先出淘汰，长跑不涨内存
            self._mem.pop(next(iter(self._mem)))

    # ---------- 核心接口：历史 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        """THS_HistoryQuotes 历史 K 线；严格 HISTORY_COLUMNS、dt datetime64[ns] 升序。"""
        if freq is Frequency.TICK:
            raise FeedCapabilityError(
                f"{CHANNEL} 不提供历史 tick 回放（逐笔走 THS_HF/DR，接口待在线核对后另开方法）")
        if freq not in _INTERVAL:
            raise FeedCapabilityError(f"{CHANNEL} 不支持周期 {freq}")
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        if e < s:
            raise AdapterError(f"{CHANNEL} 区间反向: {s} > {e}")
        key = f"hist|{symbol}|{freq.value}|{s:%Y%m%d}|{e:%Y%m%d}|{adjust.value}"

        cached = self._mem_get(key)                 # 命中缓存：配额计数不变
        if cached is not None:
            return cached
        cached = self._disk_history_hit(symbol, freq, s, e, adjust)
        if cached is not None:
            self._mem_put(key, cached)
            return cached
        if self._quota_exceeded():
            q = self.quota_status()
            raise AdapterError(
                f"{CHANNEL} 当日配额已用尽（{q['used']}/{q['limit']}）且本地缓存未覆盖请求区间，"
                f"按配额纪律不外呼；请明日重跑（已入库部分走本地命中）或上调 "
                f"daily_quota/ifind_daily_quota。请求: {symbol} {freq.value} {key.split('|')[3]}-"
                f"{key.split('|')[4]}")

        params = {"Interval": _INTERVAL[freq], "CTP": _CTP[adjust], "is_local": "0"}
        resp = self._request(
            lambda sdk: sdk.THS_HistoryQuotes(symbol, _HQ_INDICATORS, params,
                                              f"{s:%Y-%m-%d}", f"{e:%Y-%m-%d}"),
            op=f"history:{symbol}:{freq.value}")
        out = _parse_history_response(resp)
        if out.empty:
            return out                              # 问到了但无数据：不写缓存、不抛异常
        self._mem_put(key, out)
        try:
            self._parquet().write(symbol, freq, out, adjust)
        except Exception as ex:  # noqa: BLE001 回写失败不影响返回
            log.error("%s 历史强缓存回写失败 %s: %s", CHANNEL, symbol, ex)
        return out.copy()

    def _disk_history_hit(self, symbol: str, freq: Frequency, s: pd.Timestamp,
                          e: pd.Timestamp, adjust: AdjustType) -> pd.DataFrame | None:
        """Parquet 强缓存：本地已覆盖区间才返回（端点非交易日给容差，容差外算缺口）。"""
        try:
            df = self._parquet().read(symbol, freq, s, e, adjust)
        except Exception as ex:  # noqa: BLE001 缓存读取失败按 miss 处理
            log.warning("%s 强缓存读取失败（按 miss 处理）: %s", CHANNEL, ex)
            return None
        if df is None or df.empty:
            return None
        tol = pd.Timedelta(days=_COVERAGE_TOL_DAYS[freq in _DAILY_FREQS])
        if df["dt"].iloc[0] <= s + tol and df["dt"].iloc[-1] >= e - tol:
            return df.reset_index(drop=True)
        return None

    # ---------- 核心接口：财务（降级链第一优先源） ----------
    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        """财务报表/指标 → 统一长表 FINANCIAL_COLUMNS（结构差异在本适配器抹平）。"""
        if report_type not in _FIN_REPORT_TYPE:
            raise FeedCapabilityError(
                f"{CHANNEL} 未知 report_type: {report_type}（支持 {_FIN_REPORT_TYPE}）")
        key = f"fin|{symbol}|{report_type}"
        cached = self._mem_get(key)
        if cached is not None:
            return cached
        cached = self._disk_financial_hit(symbol, report_type)
        if cached is not None:
            self._mem_put(key, cached)
            return cached
        if self._quota_exceeded():
            q = self.quota_status()
            raise AdapterError(
                f"{CHANNEL} 当日配额已用尽（{q['used']}/{q['limit']}），财务请求走本地缓存；"
                f"{symbol}/{report_type} 无可用缓存，请明日重试或下调拉取批量")

        resp = self._request(lambda sdk: self._financial_call(sdk, symbol, report_type),
                             op=f"financial:{symbol}:{report_type}")
        out = _parse_financial_response(resp, symbol, report_type)
        if out.empty:                               # 空结果不进缓存：不把"暂无"固化成永久暂无
            return out
        self._mem_put(key, out)
        self._write_financial_cache(symbol, report_type, out)
        return out.copy()

    def _financial_call(self, sdk: Any, symbol: str, report_type: str) -> Any:
        """三张表用 THS_FeedBatch（多期批量），指标集用 THS_BasicData；缺函数互为兜底。"""
        indicator = _FIN_SHEET[report_type]
        params = dict(_FIN_FUNC_PARAMS[report_type])
        feed_batch = getattr(sdk, "THS_FeedBatch", None)
        basic = getattr(sdk, "THS_BasicData", None)
        if report_type in ("income", "balance", "cashflow"):
            if callable(feed_batch):
                return feed_batch(symbol, indicator, params)
            if callable(basic):
                return basic(symbol, indicator, params)
        else:
            if callable(basic):
                return basic(symbol, indicator, params)
            if callable(feed_batch):
                return feed_batch(symbol, indicator, params)
        raise FeedCapabilityError(
            f"{SDK_MODULE} 未提供 THS_FeedBatch/THS_BasicData，财务能力不可用（whl 版本待核对）")

    def _financial_file(self, symbol: str, report_type: str) -> Path:
        return self._cache_root() / f"fin_{symbol}_{report_type}.parquet"

    def _disk_financial_hit(self, symbol: str, report_type: str) -> pd.DataFrame | None:
        p = self._financial_file(symbol, report_type)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self._fin_ttl:
            return None
        try:
            df = pd.read_parquet(p)
        except Exception as e:  # noqa: BLE001 缓存损坏按 miss 处理（下次外呼覆盖）
            log.warning("%s 财务缓存不可读: %s", CHANNEL, e)
            return None
        if list(df.columns) != FINANCIAL_COLUMNS:
            log.warning("%s 财务缓存列不符统一长表契约（按 miss 处理）: %s", CHANNEL,
                        list(df.columns))
            return None
        return df.reset_index(drop=True)

    def _write_financial_cache(self, symbol: str, report_type: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return                                  # 空结果不写缓存：避免把"暂无"固化
        try:
            df.to_parquet(self._financial_file(symbol, report_type), index=False)
        except Exception as e:  # noqa: BLE001 缓存写失败不影响返回
            log.warning("%s 财务缓存写入失败: %s", CHANNEL, e)

    # ---------- 核心接口：实时（快照式，回调只入队） ----------
    def subscribe_realtime(self, symbols: list[str], callback: Callable[[Tick], None]) -> None:
        """增量追加订阅。SDK 回调线程只往队列投递，归一化与用户回调在工作线程做。"""
        if not symbols:
            return
        sdk = self._sdk()
        fn = self._realtime_func(sdk)
        new = [s for s in symbols if s not in self._rt_symbols]
        if not new:
            return
        self._rt_callback = callback
        self._start_realtime_worker()
        # 先登记再外呼：订阅受理与首条推送几乎同时到达，晚登记会把首批快照当"未订阅"丢掉
        pending = set(new)
        self._rt_symbols |= pending
        try:
            self._request(lambda _sdk: fn(",".join(new), _RT_INDICATORS, self._on_snapshot),
                          op=f"subscribe:{len(new)}")
        except Exception:
            self._rt_symbols -= pending          # 失败回滚（fail-closed，不留假订阅）
            if not self._rt_symbols:
                self._stop_realtime_worker()
            raise
        log.info("%s 实时订阅生效（当前 %d 只）", CHANNEL, len(self._rt_symbols))

    def unsubscribe_realtime(self, symbols: list[str]) -> None:
        removed = [s for s in symbols if s in self._rt_symbols]
        if not removed:
            return
        self._rt_symbols.difference_update(removed)
        unsub = None
        if self._sdk_mod is not None:
            for name in _RT_UNSUB_FUNC_NAMES:
                candidate = getattr(self._sdk_mod, name, None)
                if callable(candidate):
                    unsub = candidate
                    break
        if unsub is not None:
            try:
                self._request(lambda _sdk: unsub(",".join(removed)), op="unsubscribe")
            except Exception as e:  # noqa: BLE001 退订失败只记日志（本地已停止转发）
                log.warning("%s 退订异常: %s", CHANNEL, e)
        else:
            log.info("%s SDK 无退订接口，已本地停止转发 %d 只", CHANNEL, len(removed))
        if not self._rt_symbols:
            self._stop_realtime_worker()

    def _realtime_func(self, sdk: Any) -> Callable:
        for name in _RT_FUNC_NAMES:
            fn = getattr(sdk, name, None)
            if callable(fn):
                return fn
        raise FeedCapabilityError(
            f"{SDK_MODULE} 未提供实时订阅接口（候选 {_RT_FUNC_NAMES} 均缺失；"
            "真实接口名待在线核对，见 docs/integration/ths_ifind.md）")

    def _on_snapshot(self, raw: Any) -> None:
        """SDK 推送入口：只入队，禁止阻塞、禁止抛异常（回调线程归宪法约束）。"""
        try:
            self._rt_queue.put_nowait(raw)
        except Exception:  # noqa: BLE001 队列异常只丢数据，绝不冒泡回 SDK 线程
            self._rt_dropped += 1

    def _start_realtime_worker(self) -> None:
        if self._rt_worker is not None and self._rt_worker.is_alive():
            return
        self._rt_stop.clear()
        self._rt_worker = threading.Thread(target=self._realtime_loop,
                                          name="ifind-rt-normalizer", daemon=True)
        self._rt_worker.start()

    def _stop_realtime_worker(self) -> None:
        """幂等停线程。"""
        self._rt_stop.set()
        worker, self._rt_worker = self._rt_worker, None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    def _realtime_loop(self) -> None:
        while not self._rt_stop.is_set():
            try:
                raw = self._rt_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                tick = _parse_realtime_snapshot(raw)
            except Exception as e:  # noqa: BLE001 单条快照解析失败不拖垮线程
                log.warning("%s 快照解析异常: %s", CHANNEL, e)
                continue
            if tick is None:
                self._rt_dropped += 1
                continue
            if tick.symbol not in self._rt_symbols and self._rt_symbols:
                continue                             # 已退订的标的不再转发
            cb = self._rt_callback
            if cb is None:
                continue
            try:
                cb(tick)
            except Exception as e:  # noqa: BLE001 用户回调异常隔离
                self._rt_dropped += 1
                log.warning("%s 实时回调异常: %s", CHANNEL, e)


ADAPTER = ThsIFindFeed
