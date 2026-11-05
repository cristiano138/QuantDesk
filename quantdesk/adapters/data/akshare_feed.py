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
# 财务三表（新浪）：表种 → 新浪接口的 symbol 参数；摘要（东财）另走一条路
_FIN_SINA = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}
_FIN_PERIODS = 8            # 长表只留最近 8 期（新浪单张表有 100+ 期，全给会把响应撑爆）
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
    label = "免费公开行情（东方财富 / 新浪等）"
    prereq = "无需开户与账号，联网即可使用；免费来源有取数频率限制，批量拉取会自动放慢节奏"
    requires = ("akshare",)
    capabilities = frozenset({FeedCapability.HISTORY_BAR, FeedCapability.FINANCIAL})

    def __init__(self, min_interval: float | None = None):
        s = get_settings()
        self._limiter = RateLimiter(
            min_interval if min_interval is not None else s.akshare_min_interval)
        self._connected = False
        self._health_cache: tuple[float, bool] = (0.0, False)
        # 限流熔断：冷却期内不外呼（K3 已知坑——封禁期内继续调用会延长封禁）
        self._blocked_until = 0.0    # monotonic 秒
        self._cooldown = 300.0       # 5min → 15min → 30min（cap），成功后复位

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
        """统一限速 + 单次重试（宪法：单次调用内部最多重试 1 次）。

        限流类异常例外：立即熔断（冷却期翻倍递增），不再立即重试——
        封禁期内重试只会延长封禁（实测教训，D04 批量拉取触发 push2 限流）。
        """
        if time.monotonic() < self._blocked_until:
            remain = self._blocked_until - time.monotonic()
            raise ConnectionLostError(
                f"akshare 限流冷却中（剩余 {remain:.0f}s），已熔断避免加重封禁；"
                "稍后重跑同一命令即可增量续拉（已入库标的走本地命中）")
        self._limiter.acquire()
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            if self._is_rate_limit(e):
                self._trip()
                raise ConnectionLostError(
                    f"akshare 限流触发熔断（冷却 {self._cooldown:.0f}s）: {e}") from e
            log.warning("AKShare 调用失败，重试一次: %s", e)
            self._limiter.acquire()
            return fn()
        self._reset_cooldown()
        return out

    @staticmethod
    def _is_rate_limit(e: Exception) -> bool:
        name = type(e).__name__
        return "RateLimit" in name or "限流" in str(e)

    def _trip(self) -> None:
        self._blocked_until = time.monotonic() + self._cooldown
        log.warning("akshare 熔断 %.0fs（下次冷却翻倍）", self._cooldown)
        self._cooldown = min(self._cooldown * 3, 1800.0)

    def _reset_cooldown(self) -> None:
        self._cooldown = 300.0
        self._blocked_until = 0.0

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
        """财务数据：三表走新浪（真按表种取），摘要走东财（指标合表）。

        以前 income/balance/cashflow 三种都返回同一张东财摘要，只把 report_type 打了个标——
        界面上挂"利润表/资产负债表/现金流量表"三个页签就成了假拆分（数字一模一样）。
        现在表种真的不同；要"比率/每股"这类衍生指标看 indicator（东财摘要）。

        统一长表形状：symbol / report_type / 选项 / 指标 / <YYYYMMDD 各期>。
        三表在新浪是"行=报告期、列=科目"，这里转置成同样的形状，服务层只认一种结构。
        """
        if report_type not in ("income", "balance", "cashflow", "indicator"):
            raise FeedCapabilityError(f"未知 report_type: {report_type}")
        code = from_internal(symbol, "akshare")

        if report_type == "indicator":
            raw = self._call(lambda: self._ak().stock_financial_abstract(symbol=code))
            if raw is None or raw.empty:
                return pd.DataFrame()
            out = raw.copy()
            out.insert(0, "report_type", report_type)
            out.insert(0, "symbol", symbol)
            return out

        table = _FIN_SINA[report_type]
        sina_code = self._sina_code(symbol)
        raw = self._call(lambda: self._ak().stock_financial_report_sina(
            stock=sina_code, symbol=table))
        return self._sina_to_long(raw, symbol, report_type, table)

    @staticmethod
    def _sina_code(symbol: str) -> str:
        """600000.SH → sh600000（新浪口径要市场前缀）。新浪这条路不覆盖港股，如实拒绝。"""
        from quantdesk.core.symbols import normalize

        internal = normalize(symbol)
        suffix = internal.rsplit(".", 1)[-1].lower()
        if suffix == "hk":
            raise FeedCapabilityError(
                f"新浪财务三表只覆盖 A股（{internal}）；港股财务请用 iFinD（付费）")
        return f"{suffix}{internal.split('.')[0]}"

    @staticmethod
    def _sina_to_long(raw: pd.DataFrame | None, symbol: str,
                      report_type: str, table: str) -> pd.DataFrame:
        """行=报告期、列=科目 → 长表（每个科目一行，各期一列），值统一转数值。"""
        if raw is None or raw.empty or "报告日" not in raw.columns:
            return pd.DataFrame()
        periods = [str(p) for p in raw["报告日"].tolist()][:_FIN_PERIODS]
        idx = raw["报告日"].astype(str)
        rows = []
        for col in raw.columns:
            if col == "报告日":
                continue
            values: dict[str, float] = {}
            for p in periods:
                cell = pd.to_numeric(raw.loc[idx == p, col], errors="coerce")
                values[p] = None if cell.empty or pd.isna(cell.iloc[0]) else float(cell.iloc[0])
            if all(v is None for v in values.values()):
                continue      # 最近几期全未披露的科目不占屏（缺期在摘要里也看不到）
            rows.append({"选项": f"{table}科目", "指标": str(col), **values})
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows)
        out.insert(0, "report_type", report_type)
        out.insert(0, "symbol", symbol)
        return out

    def subscribe_realtime(self, symbols: list[str], callback) -> None:
        raise FeedCapabilityError("AKShare 不支持实时行情（capabilities 不含 REALTIME_*）")


ADAPTER = AkshareFeed
