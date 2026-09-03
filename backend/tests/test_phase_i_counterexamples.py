"""V4 Phase I 返工：反例测试。

覆盖需求七（补充必要反例）：
1. 2.10 不变成 2.1；
2. 10.0 不变成 1.0；
3. 2.10.0 与 2.10 排序一致；
4. 2.10.1 高于 2.10；
5. latest_version 正确；
6. 查询没有版本时使用 latest；
7. 查询指定一个版本时只返回该版本；
8. "比较 2.0 和 3.0"不选择 latest；
9. 指定不存在的 9.0 不返回 2.0/3.0；
10. common 为空时也不回退全部版本；
11. diff_notice 真实进入 API 响应；
12. version source 如实为 Page 级映射；
13. 人工保护 2.0 后仍可新增 3.0。

全部使用内存/tmp SQLite + Mock LLM，绝不触碰真实库。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import versioning
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_lifecycle import edit_wiki_section_current
from app.core.retrieval.wiki_retriever import (
    QUERY_VERSION_AMBIGUOUS,
    QUERY_VERSION_SPECIFIED,
    QUERY_VERSION_UNSPECIFIED,
    detect_query_version,
    retrieve_wiki,
)
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
    WikiWorkspace,
    init_db,
)

# Phase 3.1：同 ACL 不同 notebook 默认各自 workspace；本文件用「多版本来源 Page
# 合并成一篇版本化 Wiki」的 V4 语义，需把各来源 notebook 显式共享绑定到同一
# workspace（engineering）。
_SHARED_WS_KEY = "test-shared-ws-phase_i_counterexamples"
_SHARED_WS_ACL = '{"groups": ["engineering"]}'
_SHARED_WS_SCOPE_ID = "group:engineering"


def _shared_workspace(db):
    """返回本文件共享 workspace（active，engineering），无则创建。"""
    ws = db.query(WikiWorkspace).filter(WikiWorkspace.key == _SHARED_WS_KEY).first()
    if ws is None:
        ws = WikiWorkspace(
            key=_SHARED_WS_KEY, name="shared-engineering",
            acl_scope=_SHARED_WS_ACL, scope_id=_SHARED_WS_SCOPE_ID, status="active",
        )
        db.add(ws)
        db.flush()
    return ws


def _bind_shared_workspace(db, notebook):
    """把 notebook 显式绑定到共享 workspace（仅 ACL 等价时绑定，不破坏隔离/scope 测试）。"""
    ws = _shared_workspace(db)
    scope = access_control.scope_from_notebook(db, notebook)
    if not access_control.acl_scope_equivalent(access_control.acl_json_for_scope(scope), ws.acl_scope):
        return
    db.add(NotebookWorkspaceBinding(
        notebook_id=notebook.id, workspace_id=ws.id, status="active",
    ))
    db.flush()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _page(db, page_id, title, content, group_id="engineering"):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    _bind_shared_workspace(db, nb)
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content))
    db.flush()
    return db.get(Page, page_id)


def _run(coro):
    return asyncio.run(coro)


def _mk_llm(versions, common="安装前关闭服务。", summary="Titan 安装说明"):
    """构造版本化合成 Mock，按 prompt 中出现的版本返回对应内容。"""
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            prompt = messages[0]["content"]
            out_versions = [{"version": v, "content": f"版本 {v} 内容 /etc/titan-{v}/config", "diff_notice": ""} for v in versions if v in prompt]
            return {"summary": summary, "common": common, "versions": out_versions, "unversioned": ""}
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 安装说明", "category": "部署运维"}]}
    return _llm


def _build_wiki(db, versions, common="安装前关闭服务。"):
    """按 versions 列表逐个来源构建（每个版本一个 Page）。"""
    for v in versions:
        p = _page(db, f"p{v}", f"Titan {v} 安装说明", f"Titan {v} 内容，安装前关闭服务。")
        db.commit()
        _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(versions, common)))
    db.expire_all()
    return db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()


def _sections(db, wiki):
    rev = db.get(WikiRevision, wiki.current_revision_id)
    return {s.version_label: s for s in db.query(WikiSection).filter(WikiSection.revision_id == rev.id).all()}


_USER = {"id": "u1", "username": "u", "groups": ["engineering"]}


# ---------------------------------------------------------------------------
# 1-5. 版本号规范化与排序
# ---------------------------------------------------------------------------

def test_2_10_not_1():
    assert versioning.parse_semver("2.10").label == "2.10"
    assert versioning.parse_semver("2.10.0").label == "2.10"


def test_10_0_not_1_0():
    assert versioning.parse_semver("10.0").label == "10.0"


def test_2_10_0_and_2_10_same_sort_key():
    assert versioning.version_sort_key("2.10.0") == versioning.version_sort_key("2.10")


def test_2_10_1_higher_than_2_10():
    assert versioning.version_sort_key("2.10.1") > versioning.version_sort_key("2.10")


def test_latest_version_correct():
    assert versioning.latest_version(["2.10", "2.9", "3.0", "10.0"]) == "10.0"
    assert versioning.latest_version(["2.10.1", "2.10"]) == "2.10.1"
    # 2.10 比 2.9 大
    assert versioning.latest_version(["2.9", "2.10"]) == "2.10"


# ---------------------------------------------------------------------------
# 6-10. 三态版本路由
# ---------------------------------------------------------------------------

def test_detect_query_version_three_states():
    assert detect_query_version("Titan 怎么安装").state == QUERY_VERSION_UNSPECIFIED
    assert detect_query_version("Titan 2.0 怎么安装").state == QUERY_VERSION_SPECIFIED
    assert detect_query_version("Titan 2.0 怎么安装").version_label == "2.0"
    assert detect_query_version("比较 Titan 2.0 和 3.0").state == QUERY_VERSION_AMBIGUOUS


def test_query_no_version_uses_latest(db):
    wiki = _build_wiki(db, ["2.0", "3.0"])
    r = retrieve_wiki(db, _USER, "Titan 怎么安装")
    assert r.hits
    hit = r.hits[0]
    # 未指定 → 命中 latest 3.0，不返回 2.0 专属
    assert "3.0" in hit.content or hit.latest_version == "3.0"
    assert "2.0" not in hit.content


def test_query_specified_version_only(db):
    wiki = _build_wiki(db, ["2.0", "3.0"])
    r = retrieve_wiki(db, _USER, "Titan 2.0 怎么安装")
    assert r.hits
    hit = r.hits[0]
    assert "2.0" in hit.content
    assert "3.0" not in hit.content


def test_compare_two_versions_no_latest(db):
    wiki = _build_wiki(db, ["2.0", "3.0"])
    r = retrieve_wiki(db, _USER, "比较 Titan 2.0 和 3.0")
    # ambiguous：只返回 common，不选 latest，不返回任何版本专属正文
    if r.hits:
        hit = r.hits[0]
        # 不返回 2.0/3.0 版本专属内容
        assert "titan-2.0" not in hit.content
        assert "titan-3.0" not in hit.content
        # 不标 latest
        assert hit.version_label is None


def test_nonexistent_version_not_return_other_versions(db):
    wiki = _build_wiki(db, ["2.0", "3.0"])
    r = retrieve_wiki(db, _USER, "Titan 9.0 怎么安装")
    # 指定 9.0：不返回 2.0/3.0 版本专属正文
    for hit in r.hits:
        assert "titan-2.0" not in hit.content
        assert "titan-3.0" not in hit.content
        # 绝不能把 9.0 误命中成其他版本
        assert hit.version_label != "2.0"
        assert hit.version_label != "3.0"


def test_common_empty_no_fallback_to_full_body(db):
    wiki = _build_wiki(db, ["2.0", "3.0"], common="")
    # 指定不存在版本 9.0，且 common 为空
    r = retrieve_wiki(db, _USER, "Titan 9.0 怎么安装")
    # 不得回退到全部版本正文
    for hit in r.hits:
        assert "titan-2.0" not in hit.content
        assert "titan-3.0" not in hit.content


# ---------------------------------------------------------------------------
# 11. diff_notice 真实进入 API 响应
# ---------------------------------------------------------------------------

def test_diff_notice_into_api_response(db):
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {
                "summary": "Titan 安装说明",
                "common": "安装前关闭服务。",
                "versions": [{"version": "2.0", "content": "2.0 使用旧接口。", "diff_notice": "该版本信息存在差异"}],
                "unversioned": "",
            }
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 安装说明", "category": "部署运维"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [_page(db, "p1", "Titan 2.0 安装说明", "Titan 2.0 路径 /etc/v2。")], llm_json=_llm))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()

    # 通过 retrieve_wiki 验证 diff_notice 进入 WikiHit
    r = retrieve_wiki(db, _USER, "Titan 2.0 怎么安装")
    assert r.hits
    assert r.hits[0].diff_notice == "该版本信息存在差异"

    # 通过 build_wiki_view 验证进入 API 响应
    from app.core.retrieval.degradation import build_wiki_view
    view = build_wiki_view(db, r.hits)
    assert view and view[0]["diff_notice"] == "该版本信息存在差异"


# ---------------------------------------------------------------------------
# 12. version source 如实为 Page 级映射
# ---------------------------------------------------------------------------

def test_version_source_page_level_mapping(db):
    _build_wiki(db, ["2.0", "3.0"])
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    srcs = db.query(WikiVersionSource).filter(WikiVersionSource.wiki_page_id == wiki.id).all()
    assert srcs
    for s in srcs:
        # 如实声明：page_id 有值，chunk_id/source_item_id 为 None
        assert s.page_id is not None
        assert s.chunk_id is None
        assert s.source_item_id is None
    # 映射了 2 个来源 Page（2.0 和 3.0）
    page_ids = {s.page_id for s in srcs}
    assert page_ids == {"p2.0", "p3.0"}


# ---------------------------------------------------------------------------
# 13. 人工保护 2.0 后仍可新增 3.0
# ---------------------------------------------------------------------------

def test_manual_protect_20_still_add_30(db):
    wiki = _build_wiki(db, ["2.0"])
    secs = _sections(db, wiki)
    # 人工修改并保护 2.0
    edit_wiki_section_current(db, wiki, secs["2.0"].id, "2.0 人工补充：需 v2 证书。", "editor1")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections(db, wiki)
    assert secs["2.0"].merge_policy == "protected"
    assert "人工补充" in secs["2.0"].content

    # 新增 3.0
    p = _page(db, "p3.0", "Titan 3.0 安装说明", "Titan 3.0 内容，安装前关闭服务。")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(["2.0", "3.0"])))

    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections(db, wiki)
    # 2.0 人工内容保留，3.0 新增
    assert "人工补充" in secs["2.0"].content
    assert secs["2.0"].merge_policy == "protected"
    assert "3.0" in secs
    assert "titan-3.0" in secs["3.0"].content


# ---------------------------------------------------------------------------
# 14. unspecified 版本选择：不混入 unversioned / 仅 unversioned 时不标版本
# ---------------------------------------------------------------------------

def _seed_wiki_with_unversioned(db, latest_version):
    """直接构造一个含 common + unversioned + (可选)版本块的 Wiki，返回 WikiPage。"""
    wiki = WikiPage(
        id="w-mixed", title="Titan 安装说明", summary="s",
        acl_scope='{"groups": ["engineering"]}', status="published",
        latest_version=latest_version,
    )
    db.add(wiki); db.flush()
    rev = WikiRevision(id="w-mixed-rev", wiki_page_id="w-mixed", title="Titan 安装说明", summary="s", status="published")
    db.add(rev); db.flush()
    order = 0
    db.add(WikiSection(id="w-common", revision_id=rev.id, section_type="facts", content="通用说明：安装前关闭服务", order_index=order, is_common=True, version_label="common", version_status="confirmed", merge_policy="auto"))
    order += 1
    if latest_version:
        db.add(WikiSection(id="w-ver", revision_id=rev.id, section_type="facts", content="3.0 内容 /etc/titan-3.0/config", order_index=order, is_common=False, version_label=latest_version, version_status="confirmed", merge_policy="auto"))
        order += 1
    db.add(WikiSection(id="w-unv", revision_id=rev.id, section_type="facts", content="旧版未标明内容 /legacy/config", order_index=order, is_common=False, version_label="unversioned", version_status="unversioned", merge_policy="auto"))
    wiki.current_revision_id = rev.id
    db.commit()
    db.expire_all()
    return db.get(WikiPage, "w-mixed")


def test_unspecified_with_latest_excludes_unversioned(db):
    _seed_wiki_with_unversioned(db, latest_version="3.0")
    r = retrieve_wiki(db, _USER, "Titan 怎么安装")
    assert r.hits
    hit = r.hits[0]
    # 只包含 common + 3.0，不包含 unversioned
    assert "通用说明" in hit.content
    assert "titan-3.0" in hit.content
    assert "legacy" not in hit.content
    # 命中 latest 3.0
    assert hit.version_label == "3.0"


def test_unspecified_no_latest_returns_unversioned_no_version_label(db):
    _seed_wiki_with_unversioned(db, latest_version=None)
    r = retrieve_wiki(db, _USER, "Titan 怎么安装")
    assert r.hits
    hit = r.hits[0]
    # 返回 common + unversioned
    assert "通用说明" in hit.content
    assert "legacy" in hit.content
    # 不标具体版本
    assert hit.version_label is None
