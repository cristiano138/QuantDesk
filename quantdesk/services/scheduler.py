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
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

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
        self.watch_last: dict = {}

    def start(self) -> None:
        from quantdesk.config import get_settings

        self._scheduler.add_job(
            self.refresh_job, "cron", hour=18, minute=0, day_of_week="mon-fri",
            id="post_close_refresh", replace_existing=True)
        s = get_settings()
        watch_jobs = []
        if getattr(s, "watch_enabled", True):
            # 盘中快照：间隔任务常驻，时段外由任务体自己短路（不外呼、不打扰限流）
            self._scheduler.add_job(
                self.watch_poll_job, "interval",
                seconds=max(2.0, float(s.watch_poll_interval_s)),
                id="watch_poll", replace_existing=True, max_instances=1,
                coalesce=True)
            # 收盘定格：A股 15:05、港股 16:10（各自只定格本市场，互不拖累）
            self._scheduler.add_job(self.watch_freeze_job, "cron", hour=15, minute=5,
                                    day_of_week="mon-fri", id="watch_freeze_cn",
                                    replace_existing=True, kwargs={"market": "CN"})
            self._scheduler.add_job(self.watch_freeze_job, "cron", hour=16, minute=10,
                                    day_of_week="mon-fri", id="watch_freeze_hk",
                                    replace_existing=True, kwargs={"market": "HK"})
            # 每日自动 K 线：把盯盘池的日线补进本地库（A股 15:25 / 港股 16:25，各市场自己跑）
            self._scheduler.add_job(self.watch_kline_job, "cron", hour=15, minute=25,
                                    day_of_week="mon-fri", id="watch_kline_cn",
                                    replace_existing=True, kwargs={"market": "CN"})
            self._scheduler.add_job(self.watch_kline_job, "cron", hour=16, minute=25,
                                    day_of_week="mon-fri", id="watch_kline_hk",
                                    replace_existing=True, kwargs={"market": "HK"})
            watch_jobs = ["watch_poll", "watch_freeze_cn", "watch_freeze_hk",
                          "watch_kline_cn", "watch_kline_hk"]
        self._scheduler.start()
        log.info("盘后任务已排程：交易日 18:00（%d 只标的）；盯盘任务：%s",
                 len(self.symbols), watch_jobs or "未启用")

    # ---------- 盯盘（只读看盘） ----------
    def watch_poll_job(self) -> dict:
        """盘中刷一整池快照。非交易时段直接返回，不发请求（公开接口有反爬）。"""
        from quantdesk.services.watch import get_watch_service

        svc = get_watch_service()
        try:
            out = svc.refresh_auto()
            if out["polled"]:
                self.watch_last = {"count": len(out["rows"]), "at": datetime.now().isoformat()}
                return self.watch_last
            return out
        except Exception as e:  # noqa: BLE001 定时任务绝不抛出（否则被 APScheduler 摘掉）
            log.warning("盯盘快照失败: %s", e)
            self._publish(f"盯盘快照失败：{type(e).__name__}: {e}")
            return {"error": str(e)}

    def watch_freeze_job(self, market: str = "CN") -> dict:
        """收盘定格当日买卖量（A股 15:05 / 港股 16:10 各跑一次）。"""
        from quantdesk.services.watch import get_watch_service

        svc = get_watch_service()
        syms = [s for s in svc.pool() if (s.endswith(".HK") == (market == "HK"))]
        if not syms:
            return {"saved": 0, "market": market, "reason": "该市场盯盘池为空"}
        try:
            out = svc.freeze(syms)
            out["market"] = market
            self._publish(f"{market} 收盘定格：写入 {out['saved']} 条买卖量"
                          + (f"，失败 {len(out['failed'])} 条" if out["failed"] else ""))
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("盯盘定格失败 %s: %s", market, e)
            return {"error": str(e), "market": market}

    def watch_kline_job(self, market: str = "CN") -> dict:
        """每日自动 K 线：把盯盘池该市场的日线补进本地库（只补缺，不覆盖已有行）。

        为什么单开一个任务而不是等 18:00 那个：盘后刷新跑的是固定样本列表（AKShare/Tushare
        口径），港股根本不在里面；盯盘池里的港股要靠腾讯日线才能每天攒下来。
        """
        from quantdesk.core.enums import AdjustType, Frequency
        from quantdesk.services.data_store import DataStoreService
        from quantdesk.services.watch import get_watch_service
        from quantdesk.store.parquet_store import ParquetStore

        svc = get_watch_service()
        tx = svc.history_feed()
        if tx is None:
            return {"market": market, "error": "K 线通道未就绪（tx_quote 不可用）"}
        syms = [s for s in svc.pool() if (s.endswith(".HK") == (market == "HK"))]
        if not syms:
            return {"market": market, "rows": 0, "reason": "该市场盯盘池为空"}
        end = datetime.now()
        start = end - timedelta(days=400)
        store = ParquetStore()
        out: dict = {"market": market, "symbols": len(syms)}
        try:
            for adj in (AdjustType.FORWARD, AdjustType.NONE):
                rep = DataStoreService(feed=tx, store=store).backfill(
                    syms, Frequency.D1, start, end, adj)
                out[f"rows_{adj.value}"] = rep.total_rows
                if rep.failed:
                    out["failed"] = rep.failed
            self._publish(
                f"{market} 盯盘 K 线补齐：前复权 {out.get('rows_forward', 0)} 行、"
                f"原始 {out.get('rows_none', 0)} 行"
                + (f"，失败 {len(out['failed'])} 个标的" if out.get("failed") else ""))
            return out
        except Exception as e:  # noqa: BLE001 定时任务绝不抛出
            log.warning("盯盘 K 线补齐失败 %s: %s", market, e)
            return {"market": market, "error": str(e)}

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
