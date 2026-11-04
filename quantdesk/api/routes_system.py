"""系统运维 API（D28）：适配器健康面板 + 历史日志清单。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from quantdesk import __version__
from quantdesk.adapters import describe_adapters
from quantdesk.config import data_dir, get_settings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/adapters")
def adapters() -> dict:
    """通道面板：注册状态 + 就绪状态 + 前置条件（不做外呼探活，避免刷新消耗配额）。"""
    s = get_settings()
    rows = describe_adapters(include_disabled=True)
    return {
        "version": __version__,
        "simulation_only": s.simulation_only,
        "broker_channel": s.broker_channel,
        "channels": rows,
        "priority": {
            "history": s.feed_priority_history,
            "realtime": s.feed_priority_realtime,
            "financial": s.feed_priority_financial,
        },
        "limits": {
            "akshare_min_interval": s.akshare_min_interval,
            "tushare_min_interval": s.tushare_min_interval,
        },
    }


@router.get("/logs")
def logs() -> list[dict]:
    """data/logs/ 历史日志文件清单（崩溃日志/缺口报告/审计报告）。"""
    d: Path = data_dir() / "logs"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.log")) + sorted(d.glob("*.csv")):
        out.append({
            "file": f.name,
            "kind": ("crash" if f.name.startswith("crash_")
                     else "gap" if f.name.startswith("gap_report")
                     else "audit"),
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:100]


@router.get("/logs/query")
def logs_query(source: str | None = None, level: str | None = None,
               contains: str = "", strategy_id: str | None = None,
               since_ms: int | None = None, until_ms: int | None = None,
               limit: int = 200, offset: int = 0) -> dict:
    """运行日志检索（LOG/RISK 落库后按条件分页查）。"""
    from quantdesk.services.logsink import get_log_sink

    sink = get_log_sink()
    sink.flush_now()                                 # 查前先冲队列，避免「刚发生的查不到」
    return sink.query(source=source, level=level, contains=contains,
                      strategy_id=strategy_id, since_ms=since_ms, until_ms=until_ms,
                      limit=limit, offset=offset)


@router.get("/logs/stats")
def logs_stats() -> dict:
    from quantdesk.services.logsink import get_log_sink

    return get_log_sink().stats()


@router.get("/logs/sources")
def logs_sources() -> list[dict]:
    """已有日志的来源与条数（历史日志页「来源」下拉的事实源）。"""
    from quantdesk.services.logsink import get_log_sink

    return get_log_sink().sources()
