"""QuantDesk API 入口。

宪法第 7 条：uvicorn 只绑 127.0.0.1（写死，不提供配置项）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from quantdesk import __version__
from quantdesk.api.routes_backtest import router as backtest_router
from quantdesk.config import data_dir, get_settings
from quantdesk.store.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    d = data_dir()
    (d / "parquet").mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    init_db()                              # D03：启动自动建表（幂等）
    yield


app = FastAPI(title="QuantDesk", version=__version__, lifespan=lifespan)
app.include_router(backtest_router)


@app.get("/api/system/health")
def health() -> dict:
    """健康检查（每次调用读取最新配置，为 D19 运行时热切开关留口）。"""
    return {
        "status": "ok",
        "version": __version__,
        "simulation_only": get_settings().simulation_only,
    }


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
