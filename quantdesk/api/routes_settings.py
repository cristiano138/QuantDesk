"""设置 API（D19）：读取/更新全局设置（模拟盘开关热切 + WS 广播）。"""

from fastapi import APIRouter, HTTPException

from quantdesk.services.settings_svc import apply_settings, get_current_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings() -> dict:
    return get_current_settings()


@router.put("")
def write_settings(body: dict) -> dict:
    try:
        return apply_settings(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
