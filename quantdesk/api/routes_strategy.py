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
    freq: str = Field(default="1d", description="实时下行聚合周期：1d / 1m / 5m / 15m / 30m / 60m")


@router.get("/files")
def list_files() -> list[str]:
    return get_strategy_manager().list_files()


@router.get("/file")
def read_file(path: str = "", module: str = "") -> dict:
    """读策略源码。两种写法都收：path=相对 strategies/ 的路径，
    module=回测页那种点分模块名（examples.dual_ma）——同一个文件不该要求调用方两套标识。
    报错只说清"哪个名字没找到"，不把本机绝对路径带进响应。
    """
    target = (path or "").strip() or _module_to_path(module)
    if not target:
        raise HTTPException(status_code=400, detail="path 或 module 至少给一个")
    try:
        return {"path": target, "code": get_strategy_manager().read_file(target)}
    except FileNotFoundError:
        raise HTTPException(status_code=404,
                            detail=f"策略文件不存在: {target}（在「策略工作台」左侧列表里点选，或先新建）") from None
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=404, detail=f"读取失败: {e}") from e


def _module_to_path(module: str) -> str:
    """examples.dual_ma → examples/dual_ma.py（空串原样返回）。"""
    text = (module or "").strip()
    if not text:
        return ""
    parts = [p for p in text.replace("/", ".").split(".") if p]
    if not parts:
        return ""
    file = parts[-1] if parts[-1].endswith(".py") else f"{parts[-1]}.py"
    return "/".join([*parts[:-1], file])


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
        return mgr.start(path, mode=req.mode, params=req.params, freq=req.freq)
    except StrategyGateError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/runtime/realtime")
def realtime_state() -> dict:
    """实时泵状态：谁订了什么、源是什么、扇出了多少（面板与排障用）。"""
    from quantdesk.services.realtime import get_pump

    return get_pump().status()


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
