"""AKShare 行情适配器（免费兜底源：历史日线 + 财务；不做实时——路由层据 capabilities 排除）。

已知坑（K3）与对策：
- 接口偶发变动 → 列名映射层隔离（接口变动只改本文件顶部映射常量）；
- 反爬限速约 1 req/s → 全局 RateLimiter + 随机抖动；
- 单次调用内部最多重试 1 次（宪法 DataFeed 约定），重试风暴归 FederatedFeed。

注意：日线成交量单位为「手」（东财原始口径），使用层如需「股」自行换算。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

import pandas as pd

from quantdesk.adapters.base.datafeed import HISTORY_COLUMNS, DataFeedAdapter
from quantdesk.config import get_settings
from quantdesk.core.enums import AdjustType, FeedCapability, Frequency
from quantdesk.core.errors import ConnectionLostError, FeedCapabilityError
from quantdesk.core.ratelimit import RateLimiter
from quantdesk.core.symbols import from_internal

log = logging.getLogger(__name__)

# 列名映射层（AKShare 接口变动只改这里）
_DAILY_COLMAP = {
    "日期": "dt", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}
_MIN_COLMAP = {
    "时间": "dt", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}
_PERIOD_DAILY = {Frequency.D1: "daily", Frequency.W1: "weekly", Frequency.MO1: "monthly"}
_PERIOD_MIN = {Frequency.M1: "1", Frequency.M5: "5", Frequency.M15: "15",
               Frequency.M30: "30", Frequency.H1: "60"}
_ADJ = {AdjustType.NONE: "", AdjustType.FORWARD: "qfq", AdjustType.BACKWARD: "hfq"}

T = TypeVar("T")


def normalize_history(df: pd.DataFrame, colmap: dict[str, str]) -> pd.DataFrame:
    """源列 → HISTORY_COLUMNS 归一化（纯函数，便于离线单测）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    out = df.rename(columns=colmap)
    missing = [c for c in HISTORY_COLUMNS if c not in out.columns]
    if missing:
        raise FeedCapabilityError(
            f"AKShare 返回缺少列 {missing}（接口可能变动，检查本文件列名映射）")
    out = out[HISTORY_COLUMNS].copy()
    out["dt"] = pd.to_datetime(out["dt"])
    for c in HISTORY_COLUMNS[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)


class AkshareFeed(DataFeedAdapter):
    name = "akshare"
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL})

    def __init__(self, min_interval: float | None = None):
        s = get_settings()
        self._limiter = RateLimiter(
            min_interval if min_interval is not None else s.akshare_min_interval)
        self._connected = False
        self._health_cache: tuple[float, bool] = (0.0, False)

    # ---------- 生命周期 ----------
    def connect(self) -> None:
        self._ak()  # 惰性导入，失败即抛 ConnectionLostError
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health_check(self) -> bool:
        now = time.monotonic()
        if now - self._health_cache[0] < 60:
            return self._health_cache[1]
        try:
            df = self._call(lambda: self._ak().tool_trade_date_hist_sina())
            ok = df is not None and len(df) > 0
        except Exception:  # noqa: BLE001 探活吞异常返回 False（可探测语义）
            ok = False
        self._health_cache = (now, ok)
        return ok

    # ---------- 内部 ----------
    @staticmethod
    def _ak():
        try:
            import akshare as ak
        except Exception as e:  # noqa: BLE001
            raise ConnectionLostError(f"akshare 导入失败: {e}") from e
        return ak

    def _call(self, fn: Callable[[], T]) -> T:
        """统一限速 + 单次重试（宪法：单次调用内部最多重试 1 次）。"""
        self._limiter.acquire()
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.warning("AKShare 调用失败，重试一次: %s", e)
            self._limiter.acquire()
            return fn()

    # ---------- 核心接口 ----------
    def get_history(self, symbol: str, freq: Frequency, start: datetime, end: datetime,
                    adjust: AdjustType = AdjustType.FORWARD) -> pd.DataFrame:
        code = from_internal(symbol, "akshare")
        s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
        if freq in _PERIOD_DAILY:
            raw = self._call(lambda: self._ak().stock_zh_a_hist(
                symbol=code, period=_PERIOD_DAILY[freq],
                start_date=f"{s_ts:%Y%m%d}", end_date=f"{e_ts:%Y%m%d}",
                adjust=_ADJ[adjust]))
            return normalize_history(raw, _DAILY_COLMAP)
        if freq in _PERIOD_MIN:
            raw = self._call(lambda: self._ak().stock_zh_a_hist_min_em(
                symbol=code, period=_PERIOD_MIN[freq],
                start_date=f"{s_ts:%Y-%m-%d %H:%M:%S}",
                end_date=f"{e_ts:%Y-%m-%d %H:%M:%S}",
                adjust=_ADJ[adjust]))
            return normalize_history(raw, _MIN_COLMAP)
        raise FeedCapabilityError(f"AKShare 不支持周期 {freq}")

    def get_financial_data(self, symbol: str, report_type: str = "income") -> pd.DataFrame:
        """W1 简化：返回财务摘要长表（报告期 × 指标）。

        report_type 仅校验取值并打标；报表级拆分与降级链在 FederatedFeed 与
        W6 iFinD 接入时完善。
        """
        if report_type not in ("income", "balance", "cashflow", "indicator"):
            raise FeedCapabilityError(f"未知 report_type: {report_type}")
        code = from_internal(symbol, "akshare")
        raw = self._call(lambda: self._ak().stock_financial_abstract(symbol=code))
        if raw is None or raw.empty:
            return pd.DataFrame()
        out = raw.copy()
        out.insert(0, "report_type", report_type)
        out.insert(0, "symbol", symbol)
        return out

    def subscribe_realtime(self, symbols: list[str], callback) -> None:
        raise FeedCapabilityError("AKShare 不支持实时行情（capabilities 不含 REALTIME_*）")


ADAPTER = AkshareFeed
