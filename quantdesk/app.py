"""QuantDesk API 入口。

宪法第 7 条：uvicorn 只绑 127.0.0.1（写死，不提供配置项）。

生产形态：前端 `pnpm build` 产物由本服务静态托管（同源，无需 Node），
桌面壳直接加载 http://127.0.0.1:8787 即可——所以这里必须有 SPA 兜底路由，
否则双击 启动.bat 打开的是 {"detail":"Not Found"}。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from quantdesk import __version__
from quantdesk.api.routes_backtest import router as backtest_router
from quantdesk.api.routes_channels import router as channels_router
from quantdesk.api.routes_compliance import router as compliance_router
from quantdesk.api.routes_data import router as data_router
from quantdesk.api.routes_guide import router as guide_router
from quantdesk.api.routes_interop import router as interop_router
from quantdesk.api.routes_manual import router as manual_router
from quantdesk.api.routes_market import router as market_router
from quantdesk.api.routes_settings import router as settings_router
from quantdesk.api.routes_strategy import router as strategy_router
from quantdesk.api.routes_system import router as system_router
from quantdesk.api.routes_trading import router as trading_router
from quantdesk.api.routes_watch import router as watch_router
from quantdesk.api.ws import router as ws_router
from quantdesk.config import data_dir, get_settings
from quantdesk.store.db import init_db

import logging

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    d = data_dir()
    (d / "parquet").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    init_db()                              # D03：启动自动建表（幂等）

    from quantdesk.services.channels import get_hub
    from quantdesk.services.logsink import get_log_sink
    from quantdesk.services.realtime import get_pump
    from quantdesk.services.reconcile import get_reconcile_service
    from quantdesk.services.scheduler import PostCloseScheduler
    from quantdesk.services.trading import get_trading_service

    hub = get_hub()
    hub.start()                            # 预热已就绪的行情通道（失败的只记录不阻断）
    get_trading_service()                   # 建立交易编排（paper + 待确认单恢复）
    sink = get_log_sink()
    sink.start()                            # LOG/RISK 事件落库（可检索的运行痕迹）
    pump = get_pump()
    pump.start()                            # 实时泵：行情→快照/撮合/策略进程
    reconciler = get_reconcile_service()
    reconciler.start()                      # 实盘/模拟盘周期对账（差异即事故）
    sched = PostCloseScheduler()           # D27：盘后复权刷新（交易日 18:00）
    sched.start()
    yield
    sched.stop()
    reconciler.stop()
    pump.stop()
    sink.stop()
    hub.stop()


app = FastAPI(title="QuantDesk", version=__version__, lifespan=lifespan)
app.include_router(backtest_router)
app.include_router(trading_router)
app.include_router(strategy_router)
app.include_router(market_router)
app.include_router(settings_router)
app.include_router(compliance_router)
app.include_router(channels_router)
app.include_router(interop_router)
app.include_router(watch_router)
app.include_router(guide_router)
app.include_router(manual_router)
app.include_router(data_router)
app.include_router(system_router)
app.include_router(ws_router)


@app.get("/api/system/health")
def health() -> dict:
    """健康检查（每次调用读取最新配置，为 D19 运行时热切开关留口）。"""
    return {
        "status": "ok",
        "version": __version__,
        "simulation_only": get_settings().simulation_only,
    }


# ---------- 前端静态托管（SPA） ----------
if (STATIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """非 /api 路径一律回落 index.html（客户端路由）；未知 /api 路径仍是 404 JSON。"""
    if path.startswith("api/") or path.startswith("ws"):
        raise HTTPException(status_code=404, detail=f"No such endpoint: /{path}")
    if path in ("favicon.ico", "vite.svg"):
        raise HTTPException(status_code=404)
    file = (STATIC_DIR / path).resolve()
    if STATIC_DIR.resolve() in file.parents and file.is_file():
        return FileResponse(file)
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            status_code=503,
            detail="前端产物未构建：cd frontend && pnpm build（开发模式请用 vite dev 的 5173 端口）")
    return FileResponse(index)


def main() -> None:
    """本地启动入口。host 写死 127.0.0.1（宪法红线，无配置项）。"""
    settings = get_settings()
    uvicorn.run(
        "quantdesk.app:app",
        host="127.0.0.1",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
