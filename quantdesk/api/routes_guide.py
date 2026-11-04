"""新手指引 API（只读术语库）。

术语内容在 services/guide.py 的词表里，这里只做浏览 / 搜索 / 详情三个只读入口。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from quantdesk.services import guide

router = APIRouter(prefix="/api/guide", tags=["guide"])


@router.get("/categories")
def categories() -> list[dict]:
    """分类清单（含每类术语数量）。"""
    return guide.categories()


@router.get("/terms")
def term_list(category: str | None = None, q: str | None = None,
              limit: int = 200) -> list[dict]:
    """术语列表：可按分类筛选、按关键词搜（命中数多的排前）。"""
    if category and not any(c["id"] == category for c in guide.CATEGORIES):
        raise HTTPException(status_code=404, detail=f"分类不存在: {category}")
    rows = guide.terms(category=category, q=q)
    return rows[: max(1, min(int(limit), len(rows)))]


@router.get("/terms/{slug}")
def term_detail(slug: str) -> dict:
    """单个术语详情（related 已附上名称，前端可直接跳转）。"""
    t = guide.get_term(slug)
    if t is None:
        raise HTTPException(status_code=404, detail=f"术语不存在: {slug}")
    return t
