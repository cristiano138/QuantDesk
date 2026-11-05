"""应用内使用手册：结构、API 契约与检索行为。

为什么要单测内容模块：手册是「数据」，写坏了不会抛异常，只会静默缺章节或缺字段。
import 期自检（services/manual.py 的 _validate）已钉住结构，这里钉的是它对外的行为：
每个章节指向的页面在前端真的存在、检索能照着报错原文找到处置、接口结构与前端契约一致。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantdesk.services import manual

REPO = Path(__file__).resolve().parents[1]

ALL_SLUGS = {s.slug for s in manual.SECTIONS}


@pytest.fixture(scope="module")
def client() -> TestClient:
    from quantdesk.app import app

    return TestClient(app)


# ---------------- 结构 ----------------

def test_every_nav_page_has_a_manual_section():
    """左侧导航里每个页面都必须有对应章节——「给每个板块写使用书」的验收口径。"""
    nav = re.findall(r"\{ to: '([^']+)', label: '([^']+)' \}",
                     (REPO / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8"))
    routes = {r for r, _ in nav if r != "/manual"}
    covered = {s.route for s in manual.SECTIONS}
    assert routes - covered == set(), f"这些导航页面没有手册章节: {sorted(routes - covered)}"


def test_section_routes_exist_in_frontend():
    """反向也要成立：手册不许把用户指向一个不存在的路由（深链 404 最难查）。"""
    app_src = (REPO / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    real = set(re.findall(r"<Route path=\"([^\"]+)\"", app_src))
    for sec in manual.SECTIONS:
        assert sec.route in real, f"{sec.slug} 指向未注册路由 {sec.route}"


def test_groups_are_declared_and_sections_complete():
    assert len(manual.SECTIONS) == 13
    assert {s.group for s in manual.SECTIONS} <= set(manual.GROUPS)
    for g in manual.GROUPS:
        assert any(s.group == g for s in manual.SECTIONS), f"分组 {g} 里一章都没有"


def test_each_section_has_actionable_content():
    """每章至少一条前置（带自查）、一条步骤、一条误解澄清——缺一项就退化成宣传文案。"""
    for s in manual.SECTIONS:
        assert s.prerequisites and s.steps and s.gotchas, s.slug
        for p in s.prerequisites:
            assert p.ready, f"{s.slug} 前置「{p.what}」没给自查方法"
        for e in s.errors:
            assert e.meaning and e.fix, f"{s.slug} 提示「{e.text[:16]}」只有原文没有处置"


# ---------------- API 契约 ----------------

def test_sections_endpoint_shape(client: TestClient):
    rows = client.get("/api/manual/sections").json()
    assert len(rows) == len(manual.SECTIONS)
    assert {"slug", "title", "route", "nav", "summary", "group"} <= set(rows[0])


def test_section_detail_endpoint_fields(client: TestClient):
    body = client.get("/api/manual/watch").json()
    for key in ("intro", "status", "prerequisites", "steps", "readouts",
                "gotchas", "errors", "faq", "links"):
        assert key in body, key
    assert body["prerequisites"][0].keys() >= {"what", "why", "where", "ready"}
    assert body["errors"][0].keys() >= {"text", "meaning", "fix"}


def test_unknown_slug_is_404_with_readable_detail(client: TestClient):
    r = client.get("/api/manual/no-such-chapter")
    assert r.status_code == 404
    assert "手册章节不存在" in r.json()["detail"]


def test_search_hits_error_text_verbatim(client: TestClient):
    """用户是照着界面上那句话来搜的，所以原文必须能命中并落到有处置的那一章。"""
    hits = client.get("/api/manual/search", params={"q": "二次确认超时"}).json()["hits"]
    assert hits and hits[0]["slug"] == "monitor"
    assert "处置" in hits[0]["snippet"] or hits[0]["field"].startswith("提示原文")


def test_search_empty_query_returns_nothing(client: TestClient):
    assert client.get("/api/manual/search", params={"q": "  "}).json()["hits"] == []


def test_internal_links_all_resolve(client: TestClient):
    """章节之间的跳转用 ?s=slug——slug 打错了页面会退回目录，属于静默劣化。"""
    for slug in ALL_SLUGS:
        doc = client.get(f"/api/manual/{slug}").json()
        for link in doc["links"]:
            m = re.search(r"\?s=([\w-]+)", link["to"])
            if m:
                assert m.group(1) in ALL_SLUGS, f"{slug} → {link['to']} 指向缺章节"


def test_status_labels_are_honest_not_marketing(client: TestClient):
    """每章的 status 必须写清做到什么程度（未实测的要说未实测），界面原样显示不美化。"""
    for s in manual.SECTIONS:
        assert s.status and ("已实测" in s.status or "待" in s.status or "未" in s.status), s.slug


def test_every_editable_setting_is_explained_in_manual():
    """设置页能改的每一项，手册里必须有一句解释——防止界面加了开关、手册还在旧版本。

    商业交付里「界面有个看不懂的字段的」成本是用户自己试错；手册是这一层的兜底。
    这条不测文案好坏，只测覆盖：漏一个就红，比漏了没人发现强。
    """
    from quantdesk.services.settings_svc import EDITABLE_FIELDS

    blob = "\n".join(s.haystack() for s in manual.SECTIONS)
    missing = [k for k in EDITABLE_FIELDS if k not in blob]
    assert not missing, f"这些设置项在手册里搜不到：{missing}（新增可改字段要同步写进对应章节）"
