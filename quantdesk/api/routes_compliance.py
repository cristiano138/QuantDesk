"""合规报备 API（D28）：报备清单 CRUD + 文本导出。

清单模板按证监会《证券市场程序化交易管理规定（试行）》要点预置；
页面为常驻导航入口（宪法第 9 条，非弹窗）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Response

from quantdesk import __version__
from quantdesk.config import get_settings
from quantdesk.store import repo

router = APIRouter(prefix="/api/compliance", tags=["compliance"])

DEFAULT_ITEMS: list[tuple[str, str, str]] = [
    ("账户信息（名称/账号类型/资金账户）已填写并核对", "账户", ""),
    ("程序化交易策略类型已登记（趋势/套利/做市等）", "策略", ""),
    ("策略说明：标的范围/频率/委托方式 已填写", "策略", ""),
    ("技术系统：下单通道（QMT/掘金）与部署位置（本机）已登记", "技术", ""),
    ("系统测试报告（回测+模拟盘运行记录）已归档", "技术", ""),
    ("异常处置预案：断线/错单/超额委托处理流程已知悉", "风控", ""),
    ("已知晓：程序化交易报告内容发生变更需及时更新报备", "合规", ""),
    ("已知晓：未报备不得开展程序化交易", "合规", ""),
]


DB_PATH = None  # 测试可注入临时库路径


def _list() -> list[dict]:
    return [
        {"id": r.id, "item": r.item, "category": r.category,
         "checked": r.checked, "note": r.note, "updated_at": r.updated_at}
        for r in repo.compliance_list(db_path=DB_PATH)
    ]


@router.get("")
def list_items() -> list[dict]:
    items = _list()
    if not items:                               # 首次访问：预置模板
        for item, cat, note in DEFAULT_ITEMS:
            repo.compliance_upsert(item, category=cat, note=note, db_path=DB_PATH)
        items = _list()
    return items


@router.put("")
def upsert_item(body: dict) -> list[dict]:
    item = str(body.get("item", "")).strip()
    if not item:
        raise HTTPException(status_code=400, detail="item 必填")
    repo.compliance_upsert(item, category=str(body.get("category", "general")),
                           checked=bool(body.get("checked", False)),
                           note=str(body.get("note", "")), db_path=DB_PATH)
    return _list()


@router.get("/export")
def export() -> Response:
    """导出报备清单文本（交付/存档用）。"""
    s = get_settings()
    lines = [
        "程序化交易报备清单（QuantDesk 导出）",
        f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"系统版本：QuantDesk v{__version__}",
        f"运行模式：{'实盘' if not s.simulation_only else '模拟盘'}｜通道：{s.broker_channel}",
        "=" * 48,
    ]
    for it in _list():
        mark = "[x]" if it["checked"] else "[ ]"
        lines.append(f"{mark} ({it['category']}) {it['item']}")
        if it["note"]:
            lines.append(f"      备注：{it['note']}")
    text = "\n".join(lines)
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=compliance_{datetime.now():%Y%m%d_%H%M%S}.txt"},
    )
