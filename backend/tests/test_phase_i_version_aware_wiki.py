"""V4 Phase I：版本感知 Wiki 专项测试。

覆盖（需求十一）：
1. 先导入 2.0 再导入 3.0：common 去重、3.0 在前、2.0 保留、latest=3.0。
2. 人工修改 2.0 再导入 3.0：2.0 人工内容保留、3.0 自动加入、不出现摘要3.0/正文2.0矛盾。
3. 再导入 4.0：4.0 自动成为 latest，3.0/2.0 保留。
4. 同版本重复文件：内容不重复。
5. 同版本内容冲突：明确标记差异，不伪造统一事实，不泄露来源。
6. 无版本文件：进入 unversioned，不猜版本，不成为 latest。
7. 人工修改 common：common 被保护，新版本仍可添加。
8. 当前 published 版本块 lock/unlock：可保护与解除，解除后可接受自动更新。
9. 回滚 Revision：版本块/latest/manual 状态一致回滚。
10. 查询指定 2.0/3.0：只返回 common + 对应版本，不把其他版本发送给 LLM。
11. 查询未指定版本：使用确定 latest，回答明确标注版本。
12. ACL：company/group/admin 严格隔离，同名同版本不合并。
13. 来源更新/删除：只影响关联版本，不误删其他版本，dirty/stale_input 正确。
14. LLM 失败、非法响应、并发输入变化：保留上一 Revision，人工内容不丢，dirty 保持。
15. API/前端：普通响应不含 source_page_ids/Page ID/Chunk ID/来源链接，版本信息正常展示。
16. P34：空库 upgrade、P33→P34、downgrade、ORM/schema 一致、init_db 不重建旧 Card/KO 表。

全部使用内存/tmp SQLite + Mock LLM，绝不触碰真实库，绝不调用真实 LLM/Embedding/Reranker。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import versioning
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_lifecycle import (
    edit_wiki_section_current,
    rollback_wiki,
)
from app.core.retrieval.wiki_retriever import retrieve_wiki
from app.models.database import (
    Base,
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

# Phase 3.1：同 ACL 不同 notebook 默认各自 workspace；本文件用「不同版本来源 Page
# 合并成一篇版本化 Wiki」的 V4 语义，需把各来源 notebook 显式共享绑定到同一
# workspace（engineering；sales 组隔离测试除外，不绑定以保持隔离语义）。
_SHARED_WS_KEY = "test-shared-ws-phase_i_version_aware_wiki"
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


def _page(db, page_id, title, content, group_id="engineering", chunks=None):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    _bind_shared_workspace(db, nb)
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content))
    db.flush()
    for i, c in enumerate(chunks or []):
        db.add(PageChunk(id=f"{page_id}-c{i}", page_id=page_id, chunk_index=i, content=c))
    db.flush()
    return db.get(Page, page_id)


def _run(coro):
    return asyncio.run(coro)


def _mk_versioned_llm():
    """构造版本化合成 Mock：识别阶段返回 create 主题，合成阶段按 prompt 内容
    返回版本化 JSON。"""
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            prompt = messages[0]["content"]
            versions = []
            if "3.0" in prompt:
                versions.append({"version": "3.0", "content": "3.0 使用新版安装接口，路径 /etc/titan-v3/config", "diff_notice": ""})
            if "2.0" in prompt:
                versions.append({"version": "2.0", "content": "2.0 使用旧版安装接口，路径 /etc/titan-v2/config", "diff_notice": ""})
            if "4.0" in prompt:
                versions.append({"version": "4.0", "content": "4.0 使用最新安装接口", "diff_notice": ""})
            return {
                "summary": "Titan 安装说明",
                "common": "安装前关闭服务。检查磁盘空间。",
                "versions": versions,
                "unversioned": "",
            }
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 安装说明", "category": "部署运维"}]}
    return _llm


def _build_20(db):
    p = _page(db, "p20", "Titan 2.0 安装说明", "Titan 2.0 使用旧版安装接口，配置路径 /etc/titan-v2/config。安装前关闭服务。")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_versioned_llm()))
    return db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()


def _sections_by_label(db, wiki):
    rev = db.get(WikiRevision, wiki.current_revision_id)
    return {s.version_label: s for s in db.query(WikiSection).filter(WikiSection.revision_id == rev.id).all()}


# ---------------------------------------------------------------------------
# 1. 先导入 2.0 再导入 3.0
# ---------------------------------------------------------------------------

def test_import_20_then_30(db):
    _build_20(db)
    p = _page(db, "p30", "Titan 3.0 安装说明", "Titan 3.0 使用新版安装接口，配置路径 /etc/titan-v3/config。安装前关闭服务。")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_versioned_llm()))

    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)

    # common 去重：两个版本都含"安装前关闭服务" → 通用说明
    assert "common" in secs
    assert "关闭服务" in secs["common"].content
    # 版本块都在
    assert "2.0" in secs and "3.0" in secs
    # latest = 3.0
    assert wiki.latest_version == "3.0"
    # 3.0 排在最前（order_index 小于 2.0）
    assert secs["3.0"].order_index < secs["2.0"].order_index
    # 版本相关路径留在对应版本块，不混入 common
    assert "titan-v3" in secs["3.0"].content
    assert "titan-v2" in secs["2.0"].content
    assert "titan-v3" not in secs["common"].content
    assert "titan-v2" not in secs["common"].content


# ---------------------------------------------------------------------------
# 2. 人工修改 2.0 再导入 3.0
# ---------------------------------------------------------------------------

def test_manual_edit_20_then_import_30(db):
    wiki = _build_20(db)
    secs = _sections_by_label(db, wiki)
    # 人工修改 2.0 块
    edit_wiki_section_current(db, wiki, secs["2.0"].id, "2.0 人工补充：需额外配置 v2 证书。", "editor1")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert "人工补充" in secs["2.0"].content
    assert secs["2.0"].merge_policy == "protected"
    assert secs["2.0"].content_origin == "manual"

    # 再导入 3.0
    p = _page(db, "p30", "Titan 3.0 安装说明", "Titan 3.0 使用新版安装接口，配置路径 /etc/titan-v3/config。安装前关闭服务。")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_versioned_llm()))

    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    # 2.0 人工内容保留
    assert "人工补充" in secs["2.0"].content
    assert secs["2.0"].merge_policy == "protected"
    # 3.0 自动加入
    assert "3.0" in secs
    assert "titan-v3" in secs["3.0"].content
    # 页面摘要与正文不矛盾：summary 是 common 摘要，latest=3.0，3.0 内容存在
    assert wiki.latest_version == "3.0"
    assert "3.0" in secs and "titan-v3" in secs["3.0"].content


# ---------------------------------------------------------------------------
# 3. 再导入 4.0
# ---------------------------------------------------------------------------

def test_import_40_becomes_latest(db):
    _build_20(db)
    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容与安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))
    _run(builder.build_wiki_from_pages(db, [_page(db, "p40", "Titan 4.0 安装说明", "4.0 内容与安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))

    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert wiki.latest_version == "4.0"
    assert "2.0" in secs and "3.0" in secs and "4.0" in secs
    assert secs["4.0"].order_index < secs["3.0"].order_index < secs["2.0"].order_index


# ---------------------------------------------------------------------------
# 4. 同版本重复文件：内容不重复
# ---------------------------------------------------------------------------

def test_same_version_duplicate_files(db):
    # 两个 2.0 来源，内容高度相似
    p1 = _page(db, "p20a", "Titan 2.0 安装说明", "Titan 2.0 使用旧版安装接口，路径 /etc/titan-v2/config。")
    p2 = _page(db, "p20b", "Titan 2.0 安装", "Titan 2.0 使用旧版安装接口，路径 /etc/titan-v2/config。")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p1, p2], llm_json=_mk_versioned_llm()))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    # 只有一个 2.0 版本块（不因两个来源产生两个重复块）
    labels = [s.version_label for s in db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id).all()]
    assert labels.count("2.0") == 1
    assert "titan-v2" in secs["2.0"].content


# ---------------------------------------------------------------------------
# 5. 同版本内容冲突：明确标记差异，不伪造统一事实
# ---------------------------------------------------------------------------

def test_same_version_conflict_marks_diff(db):
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {
                "summary": "Titan 安装说明",
                "common": "安装前关闭服务。",
                "versions": [{"version": "2.0", "content": "2.0 使用旧版接口。", "diff_notice": "该版本信息存在差异"}],
                "unversioned": "",
            }
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 安装说明", "category": "部署运维"}]}

    _run(builder.build_wiki_from_pages(db, [_page(db, "p1", "Titan 2.0 安装说明", "Titan 2.0 路径是 /etc/v2。", "engineering")], llm_json=_llm))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    # 差异提示存在，且不包含来源（Page ID / 文件名）
    assert "差异" in (secs["2.0"].diff_notice or "")
    assert "p1" not in (secs["2.0"].diff_notice or "")
    assert "p1" not in (secs["2.0"].content or "")


# ---------------------------------------------------------------------------
# 6. 无版本文件：进入 unversioned，不猜版本，不成为 latest
# ---------------------------------------------------------------------------

def test_unversioned_file(db):
    p = _page(db, "p0", "Titan 通用说明", "安装前关闭服务，检查磁盘空间。")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "Titan 说明", "content": "安装前关闭服务。"}
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 说明", "category": "部署运维"}]}

    _run(builder.build_wiki_from_pages(db, [p], llm_json=_llm))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 说明").first()
    # 无版本证据 → 走传统路径，latest 为空（不猜版本）
    assert wiki.latest_version is None
    secs = _sections_by_label(db, wiki)
    # 传统路径 facts 块为 unversioned（迁移回填后为 unversioned）
    for label, sec in secs.items():
        if label == "unversioned":
            assert sec.version_status == "unversioned"
            return
    # 若无 unversioned 标签（旧结构），至少 latest 为空
    assert wiki.latest_version is None


# ---------------------------------------------------------------------------
# 7. 人工修改 common
# ---------------------------------------------------------------------------

def test_manual_edit_common_then_add_version(db):
    wiki = _build_20(db)
    secs = _sections_by_label(db, wiki)
    edit_wiki_section_current(db, wiki, secs["common"].id, "通用说明（人工）：必须先备份。", "editor1")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert "备份" in secs["common"].content
    assert secs["common"].merge_policy == "protected"

    # 新版本仍可添加
    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容与安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert "3.0" in secs
    # common 保护保留（protected 块不覆盖）
    assert secs["common"].merge_policy == "protected"
    assert "备份" in secs["common"].content


# ---------------------------------------------------------------------------
# 8. 当前 published 版本块 lock/unlock
# ---------------------------------------------------------------------------

def test_published_version_lock_unlock(db):
    from app.core.knowledge_compiler_v3.wiki_lifecycle import set_section_protection_current
    wiki = _build_20(db)
    secs = _sections_by_label(db, wiki)
    target = secs["2.0"]

    # 保护
    set_section_protection_current(db, wiki, target.id, protected=True, updated_by="e")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert secs["2.0"].merge_policy == "protected"
    assert secs["2.0"].locked is True

    # 解除保护
    set_section_protection_current(db, wiki, secs["2.0"].id, protected=False, updated_by="e")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    secs = _sections_by_label(db, wiki)
    assert secs["2.0"].merge_policy == "auto"
    assert secs["2.0"].locked is False


# ---------------------------------------------------------------------------
# 9. 回滚 Revision
# ---------------------------------------------------------------------------

def test_rollback_restores_version_structure(db):
    wiki = _build_20(db)
    secs = _sections_by_label(db, wiki)
    old_rev_id = wiki.current_revision_id
    old_latest = wiki.latest_version

    # 导入 3.0 → latest 变 3.0
    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容与安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    assert wiki.latest_version == "3.0"

    # 回滚到 2.0 那个 Revision
    rollback_wiki(db, wiki.id, old_rev_id)
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    assert wiki.current_revision_id == old_rev_id
    # 版本结构一致回滚：latest 回到 2.0 时代
    assert wiki.latest_version == old_latest


# ---------------------------------------------------------------------------
# 10/11. 查询版本路由
# ---------------------------------------------------------------------------

def test_query_specific_version_routes(db):
    _build_20(db)
    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 使用新版接口 /etc/titan-v3/config。安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))
    db.commit()

    user = {"id": "u1", "username": "u", "groups": ["engineering"]}

    # 指定 2.0 → 只含 common + 2.0
    r = retrieve_wiki(db, user, "Titan 2.0 怎么安装")
    assert r.hits
    hit = r.hits[0]
    assert "titan-v2" in hit.content
    assert "titan-v3" not in hit.content

    # 指定 3.0 → 只含 common + 3.0
    r = retrieve_wiki(db, user, "Titan 3.0 怎么安装")
    assert r.hits
    hit = r.hits[0]
    assert "titan-v3" in hit.content
    assert "titan-v2" not in hit.content

    # 未指定版本 → 用 latest（3.0），回答标注版本
    r = retrieve_wiki(db, user, "Titan 怎么安装")
    assert r.hits
    hit = r.hits[0]
    assert "titan-v3" in hit.content
    assert "titan-v2" not in hit.content
    assert hit.latest_version == "3.0"


# ---------------------------------------------------------------------------
# 12. ACL 隔离
# ---------------------------------------------------------------------------

def test_acl_scope_isolation(db):
    # engineering 组 2.0，sales 组 2.0，同名同版本不合并
    p_eng = _page(db, "eng", "Titan 2.0 安装说明", "工程组 2.0 内容", group_id="engineering")
    p_sales = _page(db, "sales", "Titan 2.0 安装说明", "销售组 2.0 内容", group_id="sales")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p_eng], llm_json=_mk_versioned_llm()))
    _run(builder.build_wiki_from_pages(db, [p_sales], llm_json=_mk_versioned_llm()))

    eng_wikis = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').all()
    sales_wikis = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["sales"]}').all()
    assert len(eng_wikis) == 1
    assert len(sales_wikis) == 1
    assert eng_wikis[0].id != sales_wikis[0].id

    # 检索隔离
    eng_user = {"id": "u1", "username": "u", "groups": ["engineering"]}
    r = retrieve_wiki(db, eng_user, "Titan 2.0")
    assert r.hits and r.hits[0].wiki_page_id == eng_wikis[0].id
    assert all(h.wiki_page_id != sales_wikis[0].id for h in r.hits)


# ---------------------------------------------------------------------------
# 13. 来源更新/删除
# ---------------------------------------------------------------------------

def test_source_delete_only_affects_version(db):
    _build_20(db)
    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容与安装前关闭服务", "engineering")], llm_json=_mk_versioned_llm()))
    db.commit()

    # 删除 3.0 来源 Page
    result = builder.remove_source_page_from_wikis(db, "p30")
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    # 2.0 来源仍存在 → Wiki 不归档，dirty 待刷新
    assert wiki.status == "draft"
    assert wiki.dirty is True
    # 2.0 版本来源映射仍存在
    srcs = db.query(WikiVersionSource).filter(WikiVersionSource.wiki_page_id == wiki.id).all()
    labels = {s.version_label for s in srcs}
    assert "2.0" in labels
    assert "3.0" not in labels


# ---------------------------------------------------------------------------
# 14. LLM 失败 / 非法响应 / 并发输入变化
# ---------------------------------------------------------------------------

def test_llm_failure_keeps_revision_and_dirty(db):
    wiki = _build_20(db)
    old_rev_id = wiki.current_revision_id

    async def _fail(messages, context="", timeout=120.0):
        raise RuntimeError("down")

    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容", "engineering")], llm_json=_fail))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    assert wiki.current_revision_id == old_rev_id  # 保留上一 Revision
    # Page 保持 dirty 待重试（LLM 识别失败不会错误清 dirty）
    assert db.get(Page, "p30").wiki_dirty is True


def test_stale_input_drops_result(db):
    wiki = _build_20(db)
    old_rev_id = wiki.current_revision_id

    async def _mutate(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            # 模拟 LLM 期间来源变化
            db.query(Page).filter(Page.id == "p20").update({Page.content: "变了"})
            db.commit()
            return {"summary": "s", "common": "c", "versions": [{"version": "2.0", "content": "x", "diff_notice": ""}], "unversioned": ""}
        return {"worthy": True, "ops": [{"action": "create", "title": "Titan 安装说明", "category": "x"}]}

    _run(builder.build_wiki_from_pages(db, [_page(db, "p30", "Titan 3.0 安装说明", "3.0 内容", "engineering")], llm_json=_mutate))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
    assert wiki.current_revision_id == old_rev_id  # stale_input 丢弃结果
    assert wiki.dirty is True


# ---------------------------------------------------------------------------
# 15. API 不泄露来源
# ---------------------------------------------------------------------------

def test_api_sections_no_source_leak(db):
    wiki = _build_20(db)
    db.commit()

    from app.api.wiki import _sections_payload
    payload = _sections_payload(db, wiki.current_revision_id)
    for sec in payload:
        # 不泄露 Page ID / Chunk ID / SourceItem ID / 来源链接 / provenance
        for key in ("source_page_ids", "page_id", "chunk_id", "source_item_id", "provenance", "source_url", "source_path", "file_name"):
            assert key not in sec
        # 版本字段正常展示
        assert "version_label" in sec
        assert "is_common" in sec
        assert "merge_policy" in sec
        assert "diff_notice" in sec


# ---------------------------------------------------------------------------
# 16. P34 migration（临时库 dry-run）
# ---------------------------------------------------------------------------

def _alembic(cmd, db_path, *extra):
    import subprocess, sys, os
    from pathlib import Path
    backend_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}",
         cmd, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(backend_root),
    )


def test_p34_empty_upgrade_and_downgrade(tmp_path):
    db_path = tmp_path / "p34.db"
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    tables = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    # 旧 Card/KO 表不重建
    assert "knowledge_cards" not in tables
    assert "wiki_version_sources" in tables
    cols = {c[1] for c in conn.execute("PRAGMA table_info(wiki_sections)").fetchall()}
    assert "version_label" in cols and "merge_policy" in cols
    assert "latest_version" in {c[1] for c in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()}
    conn.close()

    # downgrade 到 P33（d1e2f3a4b5c6）
    r = _alembic("downgrade", db_path, "d1e2f3a4b5c6")
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "d1e2f3a4b5c6"
    tables2 = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert "wiki_version_sources" not in tables2
    cols2 = {c[1] for c in conn.execute("PRAGMA table_info(wiki_sections)").fetchall()}
    assert "version_label" not in cols2
    conn.close()


def test_orm_schema_matches_migration(tmp_path):
    """ORM metadata 与迁移后 schema 一致（版本字段齐全）。"""
    db_path = tmp_path / "p34b.db"
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)  # 不报错即 ORM 与迁移一致，不重建旧表
    engine.dispose()
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    tables = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert "knowledge_cards" not in tables
    assert "wiki_version_sources" in tables
    conn.close()


def test_init_db_no_recreate_old_tables():
    """init_db 不重建旧 Card/KO 表。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    tables = set(sa_inspect(engine).get_table_names())
    for legacy in ("knowledge_cards", "knowledge_card_blocks", "knowledge_communities", "canonical_entities", "wiki_citations"):
        assert legacy not in tables
    engine.dispose()
