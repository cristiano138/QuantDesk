"""回测 API（D08，同步小任务版）。

POST /api/backtest        运行回测（同步返回完整结果）
GET  /api/backtest/{id}   查询历史回测（backtest_runs 表）
GET  /api/backtest/strategies  可用策略清单（含参数表）
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


@router.get("/strategies")
def strategies() -> list[dict]:
    """可用策略清单（含默认参数表，前端参数表单据此渲染）。"""
    return available_strategies()


@router.post("")
def run_backtest(req: BacktestRequest) -> dict:
    svc: BacktestService = get_backtest_service()
    try:
        return svc.run(
            strategy=req.strategy, symbols=req.symbols, params=req.params,
            freq=req.freq, start=req.start, end=req.end,
            initial_cash=req.initial_cash, slippage=req.slippage, rf=req.rf,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/{run_id}")
def get_backtest(run_id: int) -> dict:
    svc: BacktestService = get_backtest_service()
    row = svc.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"回测 {run_id} 不存在")
    return row
