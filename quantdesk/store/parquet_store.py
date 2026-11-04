"""Parquet 历史行情存储。

路径规约（宪法存储层 + W1 扩展点）：
  {root}/{market}/{symbol}/{freq}/{adjust}/{part}.parquet
  日线族（1d/1w/1M）按年分文件 part=YYYY；分钟族按月分文件 part=YYYYMM。
  扩展说明：在宪法路径上新增 adjust 子目录，使原始价/前复权/后复权三口径可并存
  （宪法价格口径：下单用原始价、回测用前复权；D27 复权审计依赖三口径对照）。

列固定 HISTORY_COLUMNS，dt 为 datetime64[ns] 升序；写入幂等（按 dt 去重，断点重跑无重复行）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from quantdesk.core.enums import AdjustType, Frequency

log = logging.getLogger(__name__)

HISTORY_COLUMNS = ["dt", "open", "high", "low", "close", "volume", "amount"]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]

_YEARLY_FREQS = {Frequency.D1, Frequency.W1, Frequency.MO1}


class SchemaError(ValueError):
    """HISTORY_COLUMNS schema 校验失败。"""


class ParquetStore:
    def __init__(self, root: str | Path | None = None):
        # 调用时才取 data_dir：模块级绑定会把「当时的」函数对象焊死，
        # 测试夹具改 quantdesk.config.data_dir 就失效——曾因此把合成行情写进真实开发库。
        from quantdesk.config import data_dir

        self.root = Path(root) if root is not None else data_dir() / "parquet"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------
    @staticmethod
    def _market(symbol: str) -> str:
        return symbol.split(".")[1] if "." in symbol else "UNKNOWN"

    @staticmethod
    def _part(freq: Frequency, dt: pd.Timestamp) -> str:
        return f"{dt:%Y}" if freq in _YEARLY_FREQS else f"{dt:%Y%m}"

    def _dir(self, symbol: str, freq: Frequency, adjust: AdjustType) -> Path:
        return self.root / self._market(symbol) / symbol / freq.value / adjust.value

    def file_for(self, symbol: str, freq: Frequency, adjust: AdjustType, dt) -> Path:
        part = self._part(freq, pd.Timestamp(dt))
        return self._dir(symbol, freq, adjust) / f"{part}.parquet"

    # ---------- 校验 ----------
    @staticmethod
    def validate(df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in HISTORY_COLUMNS if c not in df.columns]
        if missing:
            raise SchemaError(f"缺少列: {missing}（要求 {HISTORY_COLUMNS}）")
        out = df[HISTORY_COLUMNS].copy()
        dropped = [c for c in df.columns if c not in HISTORY_COLUMNS]
        if dropped:
            log.debug("忽略额外列: %s", dropped)
        out["dt"] = pd.to_datetime(out["dt"], errors="raise")
        if out["dt"].isna().any():
            raise SchemaError("dt 列存在无法解析的值")
        for c in NUMERIC_COLUMNS:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out

    # ---------- 读写 ----------
    def write(self, symbol: str, freq: Frequency, df: pd.DataFrame,
              adjust: AdjustType = AdjustType.NONE) -> int:
        """写入（append + 按 dt 去重）。返回本次参与合并的行数；空 DataFrame 直接返回 0。"""
        if df is None or df.empty:
            return 0
        out = self.validate(df).dropna(subset=["dt"])
        out = out.drop_duplicates(subset=["dt"], keep="last").sort_values("dt")
        n = 0
        for _, grp in out.groupby(out["dt"].map(lambda t: self._part(freq, t)), sort=True):
            f = self.file_for(symbol, freq, adjust, grp["dt"].iloc[0])
            f.parent.mkdir(parents=True, exist_ok=True)
            merged = grp
            if f.exists():
                old = pd.read_parquet(f)
                merged = pd.concat([old, grp], ignore_index=True)
                merged = merged.drop_duplicates(subset=["dt"], keep="last").sort_values("dt")
            merged.to_parquet(f, index=False)
            n += len(grp)
        return n

    def read(self, symbol: str, freq: Frequency,
             start: str | pd.Timestamp | None = None,
             end: str | pd.Timestamp | None = None,
             adjust: AdjustType = AdjustType.NONE) -> pd.DataFrame:
        """读取 [start, end]（含端点）。无数据返回带列空 DataFrame（不抛异常）。"""
        files = sorted(self._dir(symbol, freq, adjust).glob("*.parquet")) \
            if self._dir(symbol, freq, adjust).exists() else []
        if not files:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.sort_values("dt").reset_index(drop=True)
        if start is not None:
            df = df[df["dt"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["dt"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    def range_of(self, symbol: str, freq: Frequency,
                 adjust: AdjustType = AdjustType.NONE) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        df = self.read(symbol, freq, adjust=adjust)
        if df.empty:
            return None
        return df["dt"].iloc[0], df["dt"].iloc[-1]
