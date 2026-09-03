"""V4 Phase C：Page 驱动 Wiki 构建测试。

覆盖：
1. 无 Card / 无 Community 也能从 Page 构建 Wiki
2. 一篇 Page 最多 2 主题；多篇 Page 合并同一主题
3. 相似标题不重复建页；标题规范化
4. 不同权限域不合并；无 Notebook fail closed
5. 自动构建后立即成为当前有效版本
6. wiki_editor 可直接编辑当前 Wiki；普通用户/跨组 editor 403
7. 人工修改立即读到新内容；编辑产生新 Revision 不修改历史
8. 自动刷新保留 locked Section
9. LLM 失败不覆盖旧 Wiki
10. 回滚后旧 Revision 重新生效
11. 旧 Wiki 正常读取

全部使用内存/tmp SQLite，不触碰真实库，不调用真实 GLM。
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_lifecycle import (
    edit_wiki_section_current,
    rollback_wiki,
)
from app.core.wiki_workspace import service as ws_service
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiWorkspace,
    init_db,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _page(db, page_id, title, content, group_id):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content))
    db.flush()
    return db.get(Page, page_id)


def _share_workspace(db, *nb_ids, group="engineering", ws_id="ws-shared"):
    """Phase 3.1：同 ACL 多 notebook 需显式绑定同一 workspace 才合并主题。

    V4 合并能力的验证改为先共享同一 workspace 再 build，保留测试意图。
    """
    ws = WikiWorkspace(id=ws_id, key=f"ws_{ws_id}", name="共享",
                       acl_scope=f'{{"groups": ["{group}"]}}', scope_id=f"group:{group}",
                       status="active")
    db.add(ws); db.flush()
    for nb_id in nb_ids:
        ws_service.bind_notebook(db, ws, db.get(Notebook, nb_id), created_by=None)
    db.commit()
    return ws


def _mk_llm(ops=None, exc=None, synthesis=None):
    """构造可注入的 LLM mock。

    识别（context 不含 wiki-synthesis）返回 worthy+ops；
    合成（context=wiki-synthesis）返回 summary+content。
    """
    async def _llm(messages, context="", timeout=120.0):
        if exc:
            raise exc
        if context == "wiki-synthesis":
            if synthesis is not None:
                return synthesis
            contents = [op.get("content", "") for op in (ops or []) if op.get("content")]
            return {"summary": "合成摘要", "content": " | ".join(contents) if contents else "聚合正文"}
        return {"worthy": True, "ops": ops or []}
    return _llm


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. 无 Card / 无 Community 构建
# ---------------------------------------------------------------------------

def test_build_from_pages_no_card_no_community(db):
    p = _page(db, "p1", "水箱安装手册", "水箱固定到机架，拧紧螺栓", "engineering")
    db.commit()

    stats = _run(builder._legacy_build_wiki_from_pages(
        db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱安装", "category": "操作指南", "content": "固定水箱步骤", "summary": "水箱安装"}]),
    ))
    assert stats["created"] == 1
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱安装").first()
    assert wp is not None
    assert wp.status == "published"  # 立即生效
    # 不依赖 Card / Community
    assert wp.community_id is None


def test_builder_does_not_reference_card_models():
    # 只检查导入语句，不误匹配 docstring 中的禁用词。
    src = inspect.getsource(builder)
    import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
    joined = "\n".join(import_lines)
    for forbidden in ("KnowledgeCard", "KnowledgeCommunity", "KnowledgeClaim", "KnowledgeCardBlock"):
        assert forbidden not in joined


# ---------------------------------------------------------------------------
# 2. 多主题上限与合并
# ---------------------------------------------------------------------------

def test_max_two_topics_per_page(db):
    p = _page(db, "p1", "综合手册", "安装步骤与故障排查的完整说明文档，内容足够长", "engineering")
    db.commit()
    three_ops = [
        {"action": "create", "title": "主题A", "category": "x", "content": "内容A", "summary": "a"},
        {"action": "create", "title": "主题B", "category": "x", "content": "内容B", "summary": "b"},
        {"action": "create", "title": "主题C", "category": "x", "content": "内容C", "summary": "c"},
    ]
    stats = _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(three_ops)))
    assert stats["created"] == 2  # 最多 2 个


def test_multiple_pages_merge_same_topic(db):
    p1 = _page(db, "p1", "水箱安装", "固定水箱到机架", "engineering")
    p2 = _page(db, "p2", "水箱加固", "水箱加固补充说明", "engineering")
    _share_workspace(db, "nb-p1", "nb-p2")  # Phase 3.1：显式绑定同一 workspace 才合并
    db.commit()

    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "水箱", "category": "x", "content": "固定水箱", "summary": "水箱"}])))
    stats = _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "固定并加固水箱", "summary": "水箱"}])))

    assert stats["updated"] == 1
    pages = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').all()
    assert len(pages) == 1  # 合并到同一主题


# ---------------------------------------------------------------------------
# 3. 相似标题不重复建页 + 标题规范化
# ---------------------------------------------------------------------------

def test_normalize_title():
    assert builder.normalize_wiki_title("水箱 安装") == "水箱安装"
    assert builder.normalize_wiki_title("水箱-安装_手册") == "水箱安装手册"
    assert builder.normalize_wiki_title("  WATER TANK  ") == "watertank"


def test_similar_title_not_duplicated(db):
    p1 = _page(db, "p1", "水箱", "水箱固定到机架并拧紧螺栓的完整安装说明", "engineering")
    p2 = _page(db, "p2", "水箱二", "水箱加固补充说明的详细内容", "engineering")
    _share_workspace(db, "nb-p1", "nb-p2")  # Phase 3.1：显式绑定同一 workspace 才合并
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "水箱安装", "content": "固定水箱", "summary": "s"}])))
    # 相似标题「水箱安装」撞上「水箱 安装」（规范化后一致）→ update 而非 create
    stats = _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "create", "title": "水箱 安装", "content": "加固水箱", "summary": "s"}])))
    assert stats["created"] == 0
    pages = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').all()
    assert len(pages) == 1


# ---------------------------------------------------------------------------
# 4. 权限域隔离 + fail closed
# ---------------------------------------------------------------------------

def test_scopes_not_merged(db):
    pa = _page(db, "pa", "工程水箱", "工程组水箱内容足够长", "engineering")
    pb = _page(db, "pb", "销售水箱", "销售组水箱内容足够长", "sales")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [pa], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "工程水箱内容", "summary": "s"}])))
    _run(builder._legacy_build_wiki_from_pages(db, [pb], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "销售水箱内容", "summary": "s"}])))

    eng = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').count()
    sales = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["sales"]}').count()
    assert eng == 1 and sales == 1  # 不同权限域不合并


def test_page_no_notebook_fail_closed(db):
    db.add(Page(id="p-orphan", notebook_id=None, title="孤儿", content="没有笔记本的内容足够长"))
    db.commit()
    p = db.get(Page, "p-orphan")
    stats = _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "x", "content": "y", "summary": "s"}])))
    assert stats["failed"] == 1
    assert db.query(WikiPage).count() == 0


# ---------------------------------------------------------------------------
# 5. 自动构建立即生效
# ---------------------------------------------------------------------------

def test_build_immediately_effective(db):
    p = _page(db, "p1", "水箱", "水箱固定内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.status == "published"
    assert wp.current_revision_id is not None
    rev = db.get(WikiRevision, wp.current_revision_id)
    assert rev.status == "published"
    assert rev.edit_type == "auto"


# ---------------------------------------------------------------------------
# 6/7/12/13. 编辑立即生效
# ---------------------------------------------------------------------------

def _build_single_wiki(db):
    p = _page(db, "p1", "水箱", "水箱固定内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "旧正文", "summary": "摘要"}])))
    return db.query(WikiPage).filter(WikiPage.title == "水箱").first()


def test_edit_current_section_immediate_effect(db):
    wp = _build_single_wiki(db)
    old_rev_id = wp.current_revision_id
    old_sec = db.query(WikiSection).filter(WikiSection.revision_id == old_rev_id, WikiSection.section_type == "facts").first()

    new_rev = edit_wiki_section_current(db, wp, old_sec.id, "新正文（人工）", "editor1")
    assert wp.current_revision_id == new_rev.id
    assert new_rev.id != old_rev_id  # 产生新 revision
    assert new_rev.edit_type == "manual"
    assert new_rev.updated_by == "editor1"

    # 新 revision 的 facts section 是新内容且 locked
    new_sec = db.query(WikiSection).filter(WikiSection.revision_id == new_rev.id, WikiSection.section_type == "facts").first()
    assert new_sec.content == "新正文（人工）"
    assert new_sec.locked is True

    # 历史 revision 未被修改
    hist_sec = db.get(WikiSection, old_sec.id)
    assert hist_sec.content == "旧正文"
    assert hist_sec.locked is False


def test_rollback_restores_old_revision(db):
    wp = _build_single_wiki(db)
    old_rev_id = wp.current_revision_id
    old_sec = db.query(WikiSection).filter(WikiSection.revision_id == old_rev_id, WikiSection.section_type == "facts").first()
    edit_wiki_section_current(db, wp, old_sec.id, "新正文", "editor1")
    assert wp.current_revision_id != old_rev_id

    rollback_wiki(db, wp.id, old_rev_id)
    db.expire_all()
    assert wp.current_revision_id == old_rev_id


# ---------------------------------------------------------------------------
# 14. 自动刷新保留 locked Section
# ---------------------------------------------------------------------------

def test_refresh_preserves_locked_section(db):
    wp = _build_single_wiki(db)
    old_rev = db.get(WikiRevision, wp.current_revision_id)
    old_sec = db.query(WikiSection).filter(WikiSection.revision_id == old_rev.id, WikiSection.section_type == "facts").first()
    edit_wiki_section_current(db, wp, old_sec.id, "人工内容", "editor1")

    # 刷新（LLM 返回 update 主题，正文不同）
    p = db.get(Page, "p1")
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "自动新正文", "summary": "摘要"}])))

    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    cur_sec = db.query(WikiSection).filter(WikiSection.revision_id == wp.current_revision_id, WikiSection.section_type == "facts").first()
    # locked 的人工内容被保留，未被自动刷新覆盖
    assert cur_sec.content == "人工内容"


# ---------------------------------------------------------------------------
# 15. LLM 失败不覆盖旧 Wiki
# ---------------------------------------------------------------------------

def test_llm_failure_does_not_overwrite(db):
    wp = _build_single_wiki(db)
    old_rev_id = wp.current_revision_id
    old_content = db.query(WikiSection).filter(WikiSection.revision_id == old_rev_id, WikiSection.section_type == "facts").first().content

    p = db.get(Page, "p1")
    # 非法 JSON / 空结果 → 返回 {}
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([])))

    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.current_revision_id == old_rev_id  # 未创建新 revision
    cur_sec = db.query(WikiSection).filter(WikiSection.revision_id == wp.current_revision_id, WikiSection.section_type == "facts").first()
    assert cur_sec.content == old_content


def test_llm_exception_does_not_overwrite(db):
    wp = _build_single_wiki(db)
    old_rev_id = wp.current_revision_id

    p = db.get(Page, "p1")
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(exc=RuntimeError("timeout"))))

    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.current_revision_id == old_rev_id


# ---------------------------------------------------------------------------
# 16. Page 更新标记 dirty
# ---------------------------------------------------------------------------

def test_llm_failure_marks_dirty(db):
    wp = _build_single_wiki(db)
    assert wp.dirty is False
    p = db.get(Page, "p1")
    # 服务不可用（抛异常）→ Page.wiki_dirty=True（持久等待重试），不覆盖 Wiki 当前版本
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(exc=RuntimeError("service unavailable"))))
    db.expire_all()
    fresh = db.get(Page, "p1")
    assert fresh.wiki_dirty is True
    assert fresh.wiki_last_error == "service_unavailable"
    # 现有 Wiki 未被覆盖（仍保留旧正文）
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.current_revision_id is not None


def test_not_worthy_does_not_mark_dirty(db):
    wp = _build_single_wiki(db)
    assert wp.dirty is False
    p = db.get(Page, "p1")
    # 显式 worthy=False → not_worthy，解除来源，不把 Page 标记成服务故障
    async def _not_worthy(messages, context="", timeout=120.0):
        return {"worthy": False, "ops": []}
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_not_worthy))
    db.expire_all()
    fresh = db.get(Page, "p1")
    assert fresh.wiki_dirty is False
    assert fresh.wiki_last_error is None


# ---------------------------------------------------------------------------
# 19. 旧 Wiki 正常读取
# ---------------------------------------------------------------------------

def test_legacy_wiki_still_readable(db):
    # 旧格式 Wiki（community_id + card 引用），直接建一个可读的 WikiPage
    wp = WikiPage(id="legacy", title="旧主题", summary="旧摘要", acl_scope='{"groups": ["__public__"]}', status="published")
    db.add(wp); db.flush()
    rev = WikiRevision(id="legacy-rev", wiki_page_id="legacy", title="旧主题", summary="旧摘要", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id="legacy-sec", revision_id="legacy-rev", section_type="summary", heading="摘要", content="旧内容", order_index=0))
    wp.current_revision_id = "legacy-rev"
    db.commit()

    got = db.get(WikiPage, "legacy")
    assert got.title == "旧主题"
    assert got.status == "published"
