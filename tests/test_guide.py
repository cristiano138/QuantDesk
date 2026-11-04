"""新手指引术语库：内容完整性自检 + 只读 API 行为。

术语库是手写数据表，最怕复制粘贴留下悬空引用与重复条目——这些用例就是那把尺子，
不校验文笔，只校验结构：slug 唯一、分类闭合、related 指得回去、搜索命中可用。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quantdesk.app import app
from quantdesk.services import guide


def test_every_term_has_required_shape():
    for t in guide.TERMS:
        assert t["slug"] and t["term"] and t["category"]
        assert t["level"] in ("入门", "进阶"), t["slug"]
        assert len(t["one_liner"]) >= 8, f"{t['slug']} 缺一句话解释"
        assert len(t["explain"]) >= 30, f"{t['slug']} 解释过短"
        assert t["pitfalls"], f"{t['slug']} 缺新手易踩坑"
        assert t["where_to_see"], f"{t['slug']} 没说明在本工作台哪里能看到"
        for k in ("one_liner", "explain", "example", "where_to_see"):
            assert '"' not in t[k], f"{t['slug']}.{k} 里混进了未闭合的引号"
        for p in t["pitfalls"]:
            assert '"' not in p, f"{t['slug']} 的坑说明里有未闭合引号"


def test_categories_cover_every_term_and_counts_add_up():
    ids = [c["id"] for c in guide.CATEGORIES]
    assert len(ids) == len(set(ids))
    assert set(t["category"] for t in guide.TERMS) <= set(ids)
    cats = guide.categories()
    assert sum(c["count"] for c in cats) == len(guide.TERMS)
    assert all(c["summary"] for c in cats)


def test_related_slugs_all_resolve():
    slugs = {t["slug"] for t in guide.TERMS}
    for t in guide.TERMS:
        assert set(t["related"]) <= slugs, t["slug"]


def test_get_term_resolves_related_names():
    t = guide.get_term("outer-inner")
    assert t["related_terms"]
    assert all(r["term"] for r in t["related_terms"])       # 每条都翻译成名字，不给悬空 slug
    assert guide.get_term("不存在") is None


def test_search_ranks_term_name_matches_first():
    rows = guide.terms(q="外盘")
    assert rows and rows[0]["slug"] == "outer-inner"
    assert guide.terms(q="不存在的那个词") == []
    assert len(guide.terms(category="hk")) == guide.categories()[8]["count"]


def test_api_endpoints_and_404_semantics():
    with TestClient(app) as client:
        cats = client.get("/api/guide/categories").json()
        assert len(cats) == len(guide.CATEGORIES)
        allrows = client.get("/api/guide/terms").json()
        assert len(allrows) == len(guide.TERMS)
        one = client.get("/api/guide/terms/adjust-forward").json()
        assert one["related_terms"] and "复权" in one["term"]
        assert client.get("/api/guide/terms/没有这个").status_code == 404
        assert client.get("/api/guide/terms?category=没有这个分类").status_code == 404
        limited = client.get("/api/guide/terms", params={"limit": 5}).json()
        assert len(limited) == 5


def test_guide_router_exposes_only_reads():
    """术语模块也不许有写入口。"""
    paths = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith("/api/guide")}
    for p, ops in paths.items():
        assert set(ops) <= {"get"}, f"{p} 上有非只读方法: {set(ops)}"


def test_flow_terms_exist_for_everything_the_watch_page_shows():
    """盯盘页展示的每个概念都得有对应术语——小白看到就得能查。"""
    required = ["outer-inner", "side-code", "tick-data", "big-order", "capital-flow",
                "five-level", "bid-ratio", "intraday", "amplitude", "turnover-rate",
                "unit-shares", "post-close", "data-source", "realtime-lag", "hk-share",
                "board-lot", "hk-connect", "limit-up-down", "t1-rule", "auction"]
    slugs = {t["slug"] for t in guide.TERMS}
    missing = [s for s in required if s not in slugs]
    assert not missing, f"盯盘页概念缺术语: {missing}"


@pytest.mark.parametrize("cat", [c["id"] for c in guide.CATEGORIES])
def test_each_category_is_navigable(cat):
    rows = guide.terms(category=cat)
    assert rows, f"分类 {cat} 是空的"
    assert all(r["category"] == cat for r in rows)
