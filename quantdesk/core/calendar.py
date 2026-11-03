"""交易日历：tushare trade_cal 拉取并缓存本地 SQLite（trading_days 表）。

- token 缺失 → 抛 ConfigError（含注册引导），不静默（宪法：外部调用结构化异常）。
- 无 token 且区间未缓存 → 退化工作日启发式，结果带 used_fallback 标记，不冒充真日历。
- trading_days 表与 store/models_db.TradingDayRow 同构（init_db 建表亦兼容）。
"""

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from quantdesk.core.errors import ConfigError

log = logging.getLogger(__name__)

DDL = (
    "CREATE TABLE IF NOT EXISTS trading_days ("
    " date TEXT PRIMARY KEY,"
    " is_open INTEGER NOT NULL)"
)

TUSHARE_GUIDE = (
    "Tushare token 未配置：到 https://tushare.pro 注册获取 token，"
    "然后设置环境变量 QUANTDESK_TUSHARE_TOKEN 或写入项目根目录 .env 文件。"
)


def _fmt(d: str | date | datetime) -> str:
    """统一成 YYYYMMDD。"""
    if isinstance(d, str):
        return d.replace("-", "")
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%Y%m%d")


def _parse(d: str) -> date:
    return datetime.strptime(_fmt(d), "%Y%m%d").date()


def fetch_trade_cal(start: str | date, end: str | date, token: str) -> list[tuple[str, int]]:
    """调用 tushare pro.trade_cal。独立函数便于测试替换。"""
    import tushare as ts  # 惰性导入

    pro = ts.pro_api(token)
    df = pro.trade_cal(exchange="SSE", start_date=_fmt(start), end_date=_fmt(end))
    if df is None or df.empty:
        raise ConfigError("tushare trade_cal 返回空数据")
    rows = [(str(r.cal_date), int(r.is_open)) for r in df.itertuples(index=False)]
    return rows


class TradingCalendar:
    """线程安全的交易日历（sqlite3 连接内部串行使用，check_same_thread=False）。"""

    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 缓存 ----------
    def upsert(self, rows: list[tuple[str, int]]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO trading_days(date, is_open) VALUES(?, ?) "
                "ON CONFLICT(date) DO UPDATE SET is_open=excluded.is_open",
                rows,
            )

    def min_max(self) -> tuple[str, str] | None:
        cur = self._conn.execute("SELECT MIN(date), MAX(date) FROM trading_days")
        row = cur.fetchone()
        return (row[0], row[1]) if row and row[0] else None

    def covers(self, start: str | date, end: str | date) -> bool:
        rng = self.min_max()
        return bool(rng and rng[0] <= _fmt(start) and rng[1] >= _fmt(end))

    def ensure_range(self, start: str | date, end: str | date, token: str = "") -> bool:
        """确保 [start, end] 已缓存；缺失则用 tushare 拉取。返回是否发生网络拉取。"""
        if self.covers(start, end):
            return False
        if not token:
            raise ConfigError(TUSHARE_GUIDE)
        rows = fetch_trade_cal(start, end, token)
        self.upsert(rows)
        return True

    # ---------- 查询 ----------
    def is_open(self, d: str | date) -> bool | None:
        """None 表示未知（未缓存该日期）。"""
        cur = self._conn.execute(
            "SELECT is_open FROM trading_days WHERE date=?", (_fmt(d),)
        )
        row = cur.fetchone()
        return bool(row[0]) if row else None

    def open_days(self, start: str | date, end: str | date) -> list[str]:
        cur = self._conn.execute(
            "SELECT date FROM trading_days WHERE date BETWEEN ? AND ? AND is_open=1 ORDER BY date",
            (_fmt(start), _fmt(end)),
        )
        return [r[0] for r in cur.fetchall()]

    def prev_day(self, d: str | date) -> str | None:
        cur = self._conn.execute(
            "SELECT date FROM trading_days WHERE date < ? AND is_open=1 ORDER BY date DESC LIMIT 1",
            (_fmt(d),),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def next_day(self, d: str | date) -> str | None:
        cur = self._conn.execute(
            "SELECT date FROM trading_days WHERE date > ? AND is_open=1 ORDER BY date LIMIT 1",
            (_fmt(d),),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def open_days_with_fallback(self, start: str | date, end: str | date) -> tuple[list[str], bool]:
        """优先真日历；区间未覆盖时退化为工作日启发式。

        返回 (交易日列表, used_fallback)。
        """
        s, e = _fmt(start), _fmt(end)
        if self.covers(s, e):
            return self.open_days(s, e), False
        fallback: list[str] = []
        d = _parse(s)
        last = _parse(e)
        while d <= last:
            if d.weekday() < 5:
                fallback.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        log.warning("交易日历未覆盖 %s~%s，退化为工作日启发式（缺口报告仅供参考）", s, e)
        return fallback, True
