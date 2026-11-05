"""设置服务（D19）：读取/更新全局设置 + 变更 WS 广播。

服务端强制（宪法第 2 条「前端弹窗只是表现层，直连 API 也必须过闸」）：
- 打开实盘（simulation_only=False）与启用非官方通道（enable_easytrader=True）
  都必须带**服务端校验**的确认文本——此前只在 SettingsPage 里判，curl 直连可绕过；
- broker_channel 只能选「注册表里实际可见」的通道：默认禁用/未安装的通道
  在这里就被拒，不给前端留「选了个连不上的通道」的机会（fail-closed 前置）。
"""

from __future__ import annotations

import threading

from quantdesk.config import get_settings, update_settings

__all__ = ["get_current_settings", "apply_settings", "EDITABLE_FIELDS", "RISK_CONFIRM_PHRASE"]

# 设置页可编辑字段（白名单；绑定地址等红线字段永不进这里）
EDITABLE_FIELDS: dict[str, type] = {
    "simulation_only": bool,
    "broker_channel": str,
    "commission_rate": float,
    "min_commission": float,
    "stamp_tax_sell": float,
    "transfer_fee": float,
    "akshare_min_interval": float,
    "tushare_min_interval": float,
    # —— 风控热参（RiskGate 每单动态读取）——
    "max_position_value": float,
    "max_daily_orders": int,
    # —— 通道韧性/对账 ——
    "watchdog_interval_s": float,
    "reconcile_interval_s": float,
    "backtest_async": bool,
    "log_retention_days": int,
    # —— 软件互操作目录 ——
    "tdx_install_dir": str,
    "ths_install_dir": str,
    # —— 非官方通道启用位（需确认文本）——
    "enable_easytrader": bool,
    # —— 通道路由优先级（逗号分隔注册名；用户选主源/降级链的地方，不写死在代码里）——
    "feed_priority_history": str,
    "feed_priority_realtime": str,
    "feed_priority_financial": str,
}

# 需要确认文本才能打开的危险开关 → 语义说明（供 UI 展示影响清单）
GATED_FIELDS: dict[str, tuple[object, str]] = {
    # 危险方向 → 需要确认文本。注意 simulation_only 的危险值是 False（关模拟=开实盘），
    # enable_easytrader 的危险值是 True——两者方向相反，不能一律按「改成 True 才拦」。
    "simulation_only": (False, "关闭模拟盘 → 委托将真实发往券商通道"),
    "enable_easytrader": (True, "启用非官方界面自动下单 → 稳定性低、可能违反客户端使用条款"),
}

RISK_CONFIRM_PHRASE = "我知晓并承担实盘交易风险"

_lock = threading.RLock()

_INT_FIELDS = {"max_daily_orders"}


def get_current_settings() -> dict:
    s = get_settings()
    return {k: getattr(s, k) for k in EDITABLE_FIELDS}


def _validate(k: str, v):
    t = EDITABLE_FIELDS[k]
    if t is bool:
        if not isinstance(v, bool):
            raise ValueError(f"{k} 必须为布尔值")
        return v
    if t is int:
        iv = int(v)
        if iv <= 0:
            raise ValueError(f"{k} 必须为正整数")
        return iv
    if t is float:
        fv = float(v)
        if fv < 0:
            raise ValueError(f"{k} 不能为负")
        return fv
    sv = str(v)
    if k == "broker_channel":
        _validate_channel(sv)
    if k.startswith("feed_priority_"):
        _validate_feed_chain(k, sv)
    return sv


def _validate_feed_chain(key: str, value: str) -> None:
    """路由优先级串：每个名字都得是注册表里真实存在的**行情**通道。

    允许尚未就绪（SDK 还没装）——用户经常是先在界面里配好主源，再去装客户端；
    但不允许写错名或把交易通道名塞进来，那会让降级链静默少一环、排障时找不到原因。
    """
    from quantdesk.adapters import describe_adapters

    names = [x.strip() for x in (value or "").split(",") if x.strip()]
    if not names:
        raise ValueError(f"{key} 不能为空（至少保留一个可用源，否则整条链无行情）")
    data_names = {r["name"] for r in describe_adapters(include_disabled=False)
                  if r["kind"] == "data"}
    unknown = [n for n in names if n not in data_names]
    if unknown:
        raise ValueError(
            f"{key} 含未知或不可用的行情通道: {unknown}（可选: {sorted(data_names)}；"
            "默认禁用/未安装的通道需先在「通道」页启用与接入）")


def _validate_channel(value: str) -> None:
    from quantdesk.adapters import describe_adapters

    # BrokerAdapter 才算交易通道：行情通道名（akshare…）不能当交易通道选
    broker_names = {r["name"] for r in describe_adapters(include_disabled=False)
                    if r["kind"] == "broker"}
    allowed = {"paper", *broker_names}
    if value not in allowed:
        raise ValueError(
            f"未知或不可用的交易通道: {value!r}（可选: {sorted(allowed)}；"
            "未安装/默认禁用的通道需先在「通道」页完成接入与启用）")


def apply_settings(overrides: dict, bus=None, confirm: str | None = None) -> dict:
    """校验 → 持久化 → 失效缓存 → 广播 SETTINGS 事件（前端即时生效）。"""
    clean: dict = {}
    for k, v in overrides.items():
        if k not in EDITABLE_FIELDS:
            raise ValueError(f"不可修改的字段: {k}（红线字段拒绝经设置接口变更）")
        val = _validate(k, v)
        if k in GATED_FIELDS:
            danger_value, impact = GATED_FIELDS[k]
            if val == danger_value and (confirm or "").strip() != RISK_CONFIRM_PHRASE:
                raise ValueError(
                    f"修改「{k}」到危险值需输入确认文本：{impact}"
                    f"（请在请求中携带 confirm='{RISK_CONFIRM_PHRASE}'）")
        if k == "broker_channel" and val != "paper" and (confirm or "").strip() != RISK_CONFIRM_PHRASE:
            raise ValueError(
                "选择实盘交易通道需输入确认文本：委托将经该通道真实发往券商"
                f"（请在请求中携带 confirm='{RISK_CONFIRM_PHRASE}'）")
        clean[k] = val
    with _lock:
        update_settings(**clean)
    from quantdesk.core.events import Event, EventType, SYSTEM_BUS

    snap = get_current_settings()
    target = bus or SYSTEM_BUS
    target.publish(Event(EventType.SETTINGS, data={
        "changed": sorted(clean.keys()),
        "simulation_only": snap["simulation_only"],
        "broker_channel": snap["broker_channel"],
    }, source="settings"))
    return snap
