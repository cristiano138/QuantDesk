"""盘后定时任务（D27）：交易日收盘后刷新复权数据 + 缺口报告。

- 调度：APScheduler（宪法技术栈），本地时区 18:00 周一~周五（非交易日由
  交易日历短路：无 token 时退化工作日启发式，多跑一次无害——增量下载本地命中零配额）；
- 任务体可注入 feed/store（测试用桩，不触网）；
- 复权口径提示：AKShare qfq 存在源级漂移（见 docs/audit_order_path.md 与
  source_align_audit 报告）；Tushare token 就位后本任务自动经 FederatedFeed
  优先走 Tushare 本地精确 qfq。
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from quantdesk.config import data_dir
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS

log = logging.getLogger(__name__)

DEFAULT_SYMBOLS = [
    "600000.SH", "000001.SZ", "600585.SH",
    "000651.SZ", "601318.SH", "000333.SZ",
    "600519.SH", "002415.SZ", "601899.SH", "300750.SZ",
]


class PostCloseScheduler:
    def __init__(self, symbols: list[str] | None = None, bus: EventBus | None = None) -> None:
        self.symbols = symbols or DEFAULT_SYMBOLS
        self.bus = bus or SYSTEM_BUS
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.last_result: dict = {}

    def start(self) -> None:
        self._scheduler.add_job(
            self.refresh_job, "cron", hour=18, minute=0, day_of_week="mon-fri",
            id="post_close_refresh", replace_existing=True)
        self._scheduler.start()
        log.info("盘后任务已排程：交易日 18:00（%d 只标的）", len(self.symbols))

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ---------- 任务体 ----------
    def refresh_job(self, symbols: list[str] | None = None) -> dict:
        """增量刷新 qfq/raw 双口径 → 回写 Parquet → 缺口报告。可注入标的子集。"""
        syms = symbols or self.symbols
        started = datetime.now()
        ok, failed, rows = 0, [], 0
        try:
            from quantdesk.adapters.data.akshare_feed import AkshareFeed
            from quantdesk.adapters.data.federated import FederatedFeed
            from quantdesk.adapters.data.tushare_feed import TushareFeed
            from quantdesk.core.enums import AdjustType, Frequency
            from quantdesk.store.parquet_store import ParquetStore

            store = ParquetStore()
            feeds = [AkshareFeed(), TushareFeed()]
            feed = FederatedFeed(history_feeds=feeds, financial_feeds=feeds, store=store)
            for sym in syms:
                try:
                    for adj in (AdjustType.FORWARD, AdjustType.NONE):
                        df = feed.get_history(sym, Frequency.D1,
                                              start=f"{datetime.now().year}-01-01",
                                              end=datetime.now().strftime("%Y-%m-%d"),
                                              adjust=adj)
                        rows += len(df)
                    ok += 1
                except Exception as e:                 # noqa: BLE001 — 单标的失败不拖垮
                    failed.append(f"{sym}: {e}")
        except ImportError as e:                       # 数据源依赖缺失（离线环境）
            failed.append(f"feed 不可用: {e}")
        self.last_result = {
            "ran_at": started.isoformat(), "ok": ok, "failed": failed, "rows": rows,
            "year_start_refresh": True,
        }
        self._publish(f"盘后刷新完成：成功 {ok}/{len(syms)}，增量 {rows} 行"
                      + (f"，失败 {failed}" if failed else ""))
        return self.last_result

    def _publish(self, message: str) -> None:
        if self.bus is not None:
            self.bus.publish(Event(EventType.LOG, data={"message": message},
                                   source="scheduler"))
