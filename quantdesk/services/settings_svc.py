"""设置服务（D19）：读取/更新全局设置 + 变更 WS 广播。"""

from __future__ import annotations

import threading

from quantdesk.config import Settings, get_settings, update_settings
from quantdesk.core.events import Event, EventType, EventBus, SYSTEM_BUS

__all__ = ["get_current_settings", "apply_settings", "EDITABLE_FIELDS"]

# 设置页可编辑字段（编辑器级白名单；红线字段如绑定地址不在其中）
EDITABLE_FIELDS = {
    "simulation_only": bool,
    "broker_channel": str,
    "commission_rate": float,
    "min_commission": float,
    "stamp_tax_sell": float,
    "transfer_fee": float,
    "akshare_min_interval": float,
    "tushare_min_interval": float,
}

_lock = threading.RLock()


def get_current_settings() -> dict:
    s = get_settings()
    return {
        "simulation_only": s.simulation_only,
        "broker_channel": s.broker_channel,
        "commission_rate": s.commission_rate,
        "min_commission": s.min_commission,
        "stamp_tax_sell": s.stamp_tax_sell,
        "transfer_fee": s.transfer_fee,
        "akshare_min_interval": s.akshare_min_interval,
        "tushare_min_interval": s.tushare_min_interval,
    }


def apply_settings(overrides: dict, bus: EventBus | None = None) -> dict:
    """校验 → 持久化 → 失效缓存 → 广播 SETTINGS 事件（前端即时生效）。"""
    clean: dict = {}
    for k, v in overrides.items():
        if k not in EDITABLE_FIELDS:
            raise ValueError(f"不可修改的字段: {k}（红线字段拒绝经设置接口变更）")
        t = EDITABLE_FIELDS[k]
        if t is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} 必须为布尔值")
        elif t is float:
            v = float(v)
            if v < 0:
                raise ValueError(f"{k} 不能为负")
        else:
            v = str(v)
            if k == "broker_channel" and v not in ("paper", "qmt", "gm", "easytrader"):
                raise ValueError(f"未知通道: {v}")
        clean[k] = v
    with _lock:
        snap = update_settings(**clean)
    target = bus or SYSTEM_BUS
    target.publish(Event(EventType.SETTINGS, data={
        "changed": sorted(clean.keys()),
        "simulation_only": snap["simulation_only"],
        "broker_channel": snap["broker_channel"],
    }, source="settings"))
    return get_current_settings()
