"""回测 API（D08 → 异步队列化）：

POST /api/backtest              运行回测（async_run 默认取 settings.backtest_async）
POST /api/backtest/async        强制入队，立即返回 run_id
GET  /api/backtest/{id}         查询运行（含 status/progress，前端轮询用）
GET  /api/backtest              最近运行清单
GET  /api/backtest/strategies   可用策略清单（含参数表）
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.config import get_settings
from quantdesk.services.backtest_svc import (
    BacktestService,
    available_strategies,
    get_backtest_service,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    strategy: str = Field(description="策略名，如 dual_ma")
    symbols: list[str] = Field(min_length=1, description="统一代码，如 [\"600000.SH\"]")
    params: dict = Field(default_factory=dict, description="策略参数（覆盖默认）")
    freq: str = Field(default="1d", description="K线周期")
    start: str | None = Field(default=None, description="开始日期 YYYY-MM-DD")
    end: str | None = Field(default=None, description="结束日期 YYYY-MM-DD")
    initial_cash: float = Field(default=1_000_000.0, gt=0)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    rf: float = Field(default=0.0, ge=0, description="无风险利率（年化，Sharpe 用）")
    async_run: bool | None = Field(default=None,
                                   description="true=入队后台执行；默认按设置 backtest_async")

    def kwargs(self) -> dict:
        return dict(strategy=self.strategy, symbols=self.symbols, params=self.params,
                    freq=self.freq, start=self.start, end=self.end,
                    initial_cash=self.initial_cash, slippage=self.slippage, rf=self.rf)


def _is_async(req: BacktestRequest) -> bool:
    return req.async_run if req.async_run is not None else bool(get_settings().backtest_async)


@router.get("/strategies")
def strategies() -> list[dict]:
    """可用策略清单（含默认参数表，前端参数表单据此渲染）。"""
    return available_strategies()


@router.get("")
def runs(limit: int = 50) -> list[dict]:
    return get_backtest_service().list_runs(limit=limit)


@router.post("")
def run_backtest(req: BacktestRequest) -> dict:
    svc: BacktestService = get_backtest_service()
    try:
        if _is_async(req):
            run_id = svc.submit(**req.kwargs())
            return {"run_id": run_id, "status": "queued", "async": True,
                    "metrics": {}, "equity": [], "trades": [], "orders": [],
                    "logs": [], "bars": 0, "config": {},
                    "progress": svc.progress(run_id)}
        return svc.run(**req.kwargs())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/async")
def run_backtest_async(req: BacktestRequest) -> dict:
    """强制入队（大任务：多标的/分钟线）。"""
    svc: BacktestService = get_backtest_service()
    try:
        run_id = svc.submit(**req.kwargs())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"run_id": run_id, "status": "queued", "async": True, "metrics": {},
            "equity": [], "progress": svc.progress(run_id)}


@router.get("/{run_id}/progress")
def backtest_progress(run_id: int) -> dict:
    return get_backtest_service().progress(run_id)


@router.get("/{run_id}")
def get_backtest(run_id: int) -> dict:
    svc: BacktestService = get_backtest_service()
    row = svc.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"回测 {run_id} 不存在")
    return row
