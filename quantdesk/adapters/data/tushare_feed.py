"""Tushare Pro 适配器（补充源：日线 + 财务；按积分档限速）。

- 代码格式与内部规范一致（"000001.SZ"），零转换；
- 复权本地计算（K3：adj_factor 是复权校验源）：
  hfq = 原始价 × factor；qfq = 原始价 × factor ÷ 最新 factor（与 tushare 口径一致）；
- token 缺失 → ConfigError（含注册引导），不静默；
- 按 trade_date 全量批量（get_history 直接按 ts_code+区间，省积分的批量模式
  供 scripts/download_history.py 扩展）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.config import get_settings
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import ConfigError, ConnectionLostError, FeedCapabilityError
from quantdesk.core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

_DAILY_RENAME = {"trade_date": "dt", "vol": "volume"}
_FIN_APIS = {"income": "income", "balance": "balancesheet",
             "cashflow": "cashflow", "indicator": "fina_indicator"}
_TOKEN_GUIDE = (
    "Tushare token 未配置：到 https://tushare.pro 注册获取 token，"
    "然后设置环境变量 QUANTDESK_TUSHARE_TOKEN 或写入项目根目录 .env 文件。"
)


class TushareFeed(DataFeedAdapter):
    name = "tushare"
    label = "Tushare Pro"
    prereq = "注册 tushare.pro 取 token → 设置页或 .env 写入 QUANTDESK_TUSHARE_TOKEN（积分档决定限速）"
    requires = ("tushare",)
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL})

    def __init__(self, min_interval: float | None = None, token: str = ""):
        s = get_settings()
        self._limiter = RateLimiter(
            min_interval if min_interval is not None else s.tushare_min_interval)
        self._token = token
        self._pro = None
        self._factor_cache: dict[str, pd.DataFrame] = {}
        self._health_cache: tuple[float, bool] = (0.0, False)

    # ---------- 生命周期 ----------
    def connect(self, **credentials: str) -> None:
        token = credentials.get("token") or self._token or get_settings().tushare_token
        if not token:
            raise ConfigError(_TOKEN_GUIDE)
        try:
            import tushare as ts
        except Exception as e:  # noqa: BLE001
            raise ConnectionLostError(f"tushare 导入失败: {e}") from e
        self._pro = ts.pro_api(token)
        self._token = token
        # 探活：极小范围调用
        self._call(lambda: self._pro.trade_cal(
            exchange="SSE", start_date="20240101", end_date="20240110"))

    def disconnect(self) -> None:
        self._pro = None

    def health_check(self) -> bool:
        if self._pro is None:
            return False
        now = time.monotonic()
        if now - self._health_cache[0] < 60:
            return self._health_cache[1]
        try:
            df = self._call(lambda: self._pro.trade_cal(
                exchange="SSE", start_date="20240101", end_date="20240110"))
            ok = df is not None and len(df) > 0
        except Exception:  # noqa: BLE001
            ok = False
        self._health_cache = (now, ok)
        return ok

    # ---------- 内部 ----------
    def _call(self, fn):
        if self._pro is None:
            raise ConnectionLostError("tushare 未连接：先 connect(token=...)")
        self._limiter.acquire()
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 单次重试（宪法 DataFeed 约定）
            log.warning("tushare 调用失败，重试一次: %s", e)
            self._limiter.acquire()
            return fn()

    def _factors(self, symbol: str) -> pd.DataFrame:
        """全量复权因子（trade_date 升序，内存缓存）。"""
        if symbol not in self._factor_cache:
            f = self._call(lambda: self._pro.adj_factor(
                ts_code=symbol, start_date="19900101", end_date="20991231"))
            if f is None or f.empty:
                raise FeedCapabilityError(f"无复权因子: {symbol}")
            f = f[["trade_date", "adj_factor"]].sort_values("trade_date").reset_index(drop=True)
            self._factor_cache[symbol] = f
        return self._factor_cache[symbol]

    def _apply_adjust(self, df: pd.DataFrame, symbol: str, adjust: AdjustType) -> pd.DataFrame:
        if adjust is AdjustType.NONE:
            return df
        f = self._factors(symbol)
        df = df.merge(f, on="trade_date", how="left")
        if df["adj_factor"].isna().any():
            raise FeedCapabilityError(f"复权因子存在缺失区间: {symbol}")
        latest = float(f["adj_factor"].iloc[-1])
        cols = ("open", "high", "low", "close")
        if adjust is AdjustType.FORWARD:    # qfq = 原始价 × factor / 最新 factor
            for c in cols:
                df[c] = df[c] * df["adj_factor"] / latest
        else:                               # hfq = 原始价 × factor
            for c in cols:
                df[c] = df[c] * df["adj_factor"]
        return df.drop(columns=["adj_factor"])

    # ---------- 核心接口 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        if freq is not Frequency.D1:
            raise FeedCapabilityError(
                f"tushare W1 仅支持日线（收到 {freq}）；分钟线走 AKShare/QMT")
        if self._pro is None:
            raise ConnectionLostError("tushare 未连接：先 connect(token=...)")
        raw = self._call(lambda: self._pro.daily(
            ts_code=symbol, start_date=f"{pd.Timestamp(start):%Y%m%d}",
            end_date=f"{pd.Timestamp(end):%Y%m%d}"))
        if raw is None or raw.empty:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        raw = self._apply_adjust(raw, symbol, adjust)
        out = raw.rename(columns=_DAILY_RENAME)
        missing = [c for c in HISTORY_COLUMNS if c not in out.columns]
        if missing:
            raise FeedCapabilityError(f"tushare daily 返回缺少列 {missing}")
        out = out[HISTORY_COLUMNS].copy()
        out["dt"] = pd.to_datetime(out["dt"])
        return out.sort_values("dt").reset_index(drop=True)

    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        if report_type not in _FIN_APIS:
            raise FeedCapabilityError(f"未知 report_type: {report_type}")
        if self._pro is None:
            raise ConnectionLostError("tushare 未连接：先 connect(token=...)")
        api = getattr(self._pro, _FIN_APIS[report_type])
        df = self._call(lambda: api(ts_code=symbol))
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        out.insert(0, "report_type", report_type)
        out.insert(0, "symbol", symbol)
        return out

    def subscribe_realtime(self, symbols: list[str], callback) -> None:
        raise FeedCapabilityError("tushare 不支持实时行情（capabilities 不含 REALTIME_*）")


ADAPTER = TushareFeed
