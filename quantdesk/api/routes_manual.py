"""应用内使用手册 API（只读）：目录 / 正文 / 检索。

内容在 services/manual.py 的词表里（结构化，不是 markdown 大字符串），理由与术语库一样：
内容型数据坏了只会静默缺条，所以必须在 import 期自检（slug 唯一、分组合法、链接可达），
而自检只有在「真的被 import」时才会跑——这个路由存在本身就是它的检查入口。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quantdesk.services import manual

router = APIRouter(prefix="/api/manual", tags=["manual"])


@router.get("/sections")
def sections() -> list[dict]:
    """目录（前端左侧列表按 group 分组渲染）。"""
    return manual.sections()


@router.get("/groups")
def groups() -> list[str]:
    return list(manual.GROUPS)


@router.get("/search")
def search(q: str = "", limit: int = 12) -> dict:
    """全文检索：命中回 slug/title/field/snippet——用户多半是照着报错那句话来搜的。"""
    n = max(1, min(int(limit or 12), 50))
    return {"q": q, "hits": manual.search(q, limit=n)}


@router.get("/{slug}")
def section(slug: str) -> dict:
    doc = manual.get_section(slug)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"手册章节不存在: {slug}")
    return doc
