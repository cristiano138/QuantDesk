"""系统运维 API（D28）：适配器健康面板 + 历史日志清单。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

from quantdesk import __version__
from quantdesk.adapters import list_adapters
from quantdesk.config import data_dir, get_settings

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/adapters")
def adapters() -> dict:
    """适配器健康面板：注册状态 + 通道配置（不做外呼探活——避免面板刷新消耗配额）。"""
    s = get_settings()
    registered = set(list_adapters())
    data_feeds = ["qmt_xtdata", "tdx_aidata", "ths_ifind", "akshare_feed", "tushare_feed"]
    brokers = ["paper_broker", "qmt_broker", "gm_broker", "easytrader_broker"]

    def entry(name: str) -> dict:
        return {
            "name": name,
            "registered": name in registered,
            "status": "ok" if name in registered else "unavailable",
            # unavailable = 可选依赖缺失/未接入（注册表扫描跳过），接入后自动转绿
        }

    return {
        "version": __version__,
        "simulation_only": s.simulation_only,
        "broker_channel": s.broker_channel,
        "feeds": [entry(n) for n in data_feeds],
        "brokers": [entry(n) for n in brokers],
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
