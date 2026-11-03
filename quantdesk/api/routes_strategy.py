"""策略管理 API（D12）：CRUD / 启停 / 状态 / 门禁。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.services.strategy_mgr import (
    StrategyGateError,
    get_strategy_manager,
)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class SaveStrategyRequest(BaseModel):
    path: str = Field(description="相对 strategies/ 的路径，如 examples/dual_ma.py 或 my_ma.py")
    code: str = Field(min_length=1)


class StartStrategyRequest(BaseModel):
    mode: str = Field(default="paper", description="paper / live")
    params: dict = Field(default_factory=dict)


@router.get("/files")
def list_files() -> list[str]:
    return get_strategy_manager().list_files()


@router.get("/file")
def read_file(path: str) -> dict:
    try:
        return {"path": path, "code": get_strategy_manager().read_file(path)}
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/file")
def save_file(req: SaveStrategyRequest) -> dict:
    try:
        saved = get_strategy_manager().write_file(req.path, req.code)
        return {"saved": saved}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{strategy_id}/start")
def start(strategy_id: str, req: StartStrategyRequest) -> dict:
    """启动策略（live 需通过生命周期门禁：有 completed 模拟/回测记录）。"""
    mgr = get_strategy_manager()
    path = f"{strategy_id}.py" if not strategy_id.endswith(".py") else strategy_id
    try:
        return mgr.start(path, mode=req.mode, params=req.params)
    except StrategyGateError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{strategy_id}/stop")
def stop(strategy_id: str) -> dict:
    ok = get_strategy_manager().stop(strategy_id)
    return {"stopped": ok, "strategy_id": strategy_id}


@router.get("/{strategy_id}/status")
def status(strategy_id: str) -> dict:
    st = get_strategy_manager().status(strategy_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"策略 {strategy_id} 未运行")
    return st
