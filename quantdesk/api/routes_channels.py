"""通道 API：主流股票软件接入状态、凭据保险库、探活、对账。

安全边界（宪法第 7 条）：凭据**只写不读**——写入后任何接口只回掩码形态，
明文只在进程内流向真正需要它的适配器。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantdesk.core.secrets import delete_secret, describe, set_secret
from quantdesk.services.channels import credential_spec, get_hub

router = APIRouter(prefix="/api/channels", tags=["channels"])


class CredentialRequest(BaseModel):
    key: str = Field(description='凭据键名，如 "qmt.userdata_path"')
    value: str = Field(description="凭据值；空串表示删除该项")


@router.get("")
def channels() -> dict:
    """通道全景：注册/就绪/连接状态 + 当前路由 + 对账概览。"""
    from quantdesk.services.reconcile import get_reconcile_service

    hub = get_hub()
    body = hub.status()
    body["reconcile"] = get_reconcile_service().status()
    body["credentials_spec"] = credential_spec()
    return body


@router.post("/probe/{name}")
def probe(name: str) -> dict:
    """显式探活（用户在通道页点「测试连接」才走这里，面板刷新不外呼）。"""
    hub = get_hub()
    started = time.perf_counter()
    inst = hub.feed(name, force=True)
    if inst is None and name == hub._cfg().broker_channel:    # noqa: SLF001 — 面板需要如实原因
        broker = hub.live_broker(force=True)
        if broker is None:
            raise HTTPException(status_code=503, detail=hub.status()["live_error"])
        inst = broker
    if inst is None:
        reason = hub.feed_errors().get(name, "通道未就绪")
        raise HTTPException(status_code=503, detail=f"{name} 不可用：{reason}")
    ok = bool(inst.health_check())
    return {"name": name, "ok": ok,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


# ---------- 凭据保险库 ----------
@router.get("/credentials")
def credentials() -> list[dict]:
    """已录入凭据的掩码清单（永不回显明文）。"""
    return describe()


@router.put("/credentials")
def write_credential(req: CredentialRequest) -> dict:
    allowed = {c["key"] for c in credential_spec()}
    if req.key not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"未知凭据键: {req.key}（可用: {sorted(allowed)}）")
    try:
        set_secret(req.key, req.value)
    except Exception as e:  # noqa: BLE001 DPAPI 不可用/保险库损坏 → 如实报错
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"key": req.key, "set": bool(req.value)}


@router.delete("/credentials/{key}")
def erase_credential(key: str) -> dict:
    return {"key": key, "deleted": delete_secret(key)}


# ---------- 对账 ----------
@router.get("/reconcile")
def reconcile_state(limit: int = 100, unresolved_only: bool = False,
                    diffs_only: bool = True) -> dict:
    """对账差异。

    `diffs_only=True`（默认）：只给真正的差异，排除每轮无差异时落的 `kind="clean"`
    审计行——不排除的话「待处置」按 30s 一轮心跳涨，一天两千多条，真差异看不见。
    要看完整审计流水（含每轮无差异的记录）传 `diffs_only=false`。
    """
    from quantdesk.services.reconcile import get_reconcile_service

    svc = get_reconcile_service()
    diffs = (svc.open_diffs(limit=limit, diffs_only=diffs_only) if unresolved_only
             else svc.recent_diffs(limit=limit, diffs_only=diffs_only))
    return {"status": svc.status(), "diffs": diffs}


class ResolveRequest(BaseModel):
    note: str = Field(default="", max_length=200, description="一句话结论：核实后是什么原因/怎么处置的")
    resolve: bool = Field(default=True, description="true=标为已处置，false=退回未处置")


@router.post("/reconcile/{diff_id}/resolve")
def reconcile_resolve(diff_id: int, req: ResolveRequest) -> dict:
    """对账差异处置闭环：监控页的红条要能认领并留下结论，不是列着一排"待处理"变噪音。"""
    from quantdesk.services.reconcile import get_reconcile_service

    row = get_reconcile_service().resolve(diff_id, note=req.note, resolve=req.resolve)
    if row is None:
        raise HTTPException(status_code=404, detail=f"差异记录 {diff_id} 不存在")
    return row


@router.post("/reconcile/run")
def reconcile_now() -> dict:
    from quantdesk.services.reconcile import get_reconcile_service

    return get_reconcile_service().run_once()
