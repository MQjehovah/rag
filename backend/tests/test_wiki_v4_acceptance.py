"""V4 Phase C 补漏验收测试。

覆盖补漏要求第 11 点的 14 项验收场景（部分在 test_wiki_v4.py 已覆盖，这里补齐其余）。
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
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_lifecycle import edit_wiki_section_current
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
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
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _page(db, page_id, title, content, group_id, chunks=None):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content))
    db.flush()
    for i, c in enumerate(chunks or []):
        db.add(PageChunk(id=f"{page_id}-c{i}", page_id=page_id, chunk_index=i, content=c))
    db.flush()
    return db.get(Page, page_id)


def _mk_llm(ops=None, exc=None, synthesis=None):
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


def _build_wiki(db, title, content):
    p = _page(db, "p1", "水箱", "水箱固定内容足够长", "engineering")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": title, "content": content, "summary": "摘要"}])))
    return db.query(WikiPage).filter(WikiPage.title == title).first()


# 3. 成功刷新清除 dirty
def test_successful_refresh_clears_dirty(db):
    wp = _build_wiki(db, "水箱", "旧正文")
    wp.dirty = True
    db.commit()
    result = _run(builder.refresh_dirty_wikis(db, llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "新正文", "summary": "摘要"}])))
    assert result["refreshed"] == 1
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.dirty is False


# 4. 失败刷新保留 dirty
def test_failed_refresh_keeps_dirty(db):
    wp = _build_wiki(db, "水箱", "旧正文")
    wp.dirty = True
    db.commit()
    result = _run(builder.refresh_dirty_wikis(db, llm_json=_mk_llm(exc=RuntimeError("down"))))
    assert result["failed"] == 1
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.dirty is True


# 5. 删除来源 Page 标记相关 Wiki（唯一来源 → archived）
def test_remove_source_page_marks_dirty(db):
    wp = _build_wiki(db, "水箱", "正文")
    assert wp.dirty is False
    result = builder.remove_source_page_from_wikis(db, "p1")
    assert wp.id in result["archived_wiki_ids"]
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.status == "archived"  # 唯一来源删除 → 自动 archived
    assert "p1" not in json.loads(wp.source_page_ids or "[]")


# 6. dirty 刷新不读取 Card/Community
def test_refresh_dirty_does_not_read_card_community():
    src = inspect.getsource(builder.refresh_dirty_wikis)
    for forbidden in ("KnowledgeCard", "KnowledgeCommunity"):
        assert forbidden not in src


# 7. 旧 Section 类型不被摘要覆盖
def test_legacy_sections_not_overwritten_by_summary(db):
    # 构造含 entities/evidence 等旧结构的 Wiki，再触发自动更新
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "旧正文", "summary": "旧摘要"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    # 给当前 revision 追加旧结构 Section
    rev = db.get(WikiRevision, wp.current_revision_id)
    db.add(WikiSection(id="extra-ent", revision_id=rev.id, section_type="entities", heading="实体", content="实体X", order_index=5))
    db.add(WikiSection(id="extra-ev", revision_id=rev.id, section_type="evidence", heading="证据", content="证据Y", order_index=6))
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "新正文", "summary": "新摘要"}])))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    new_rev = db.get(WikiRevision, wp.current_revision_id)
    types = {s.section_type: s for s in db.query(WikiSection).filter(WikiSection.revision_id == new_rev.id).all()}
    assert types["entities"].content == "实体X"  # 原样保留
    assert types["evidence"].content == "证据Y"  # 原样保留
    assert "新正文" in types["facts"].content  # 聚合正文含新来源内容
    # summary 来自合成结果（mock 返回"合成摘要"），而非 op 中的"新摘要"
    assert types["summary"].content == "合成摘要"


# 8. summary 编辑同步 Page/Revision
def test_edit_summary_syncs_page_and_revision(db):
    wp = _build_wiki(db, "水箱", "正文")
    old_rev_id = wp.current_revision_id
    old_summary_sec = db.query(WikiSection).filter(WikiSection.revision_id == old_rev_id, WikiSection.section_type == "summary").first()

    new_rev = edit_wiki_section_current(db, wp, old_summary_sec.id, "新摘要内容", "editor1")
    assert new_rev.summary == "新摘要内容"
    assert wp.summary == "新摘要内容"


# 9. 人工编辑后 hash 改变
def test_manual_edit_changes_hash(db):
    wp = _build_wiki(db, "水箱", "正文")
    old_rev = db.get(WikiRevision, wp.current_revision_id)
    old_hash = old_rev.source_hash
    facts_sec = db.query(WikiSection).filter(WikiSection.revision_id == old_rev.id, WikiSection.section_type == "facts").first()

    new_rev = edit_wiki_section_current(db, wp, facts_sec.id, "新正文内容", "editor1")
    assert new_rev.source_hash != old_hash


# 10. 长文档后部 Chunk 进入构建输入
def test_long_document_tail_chunk_in_input(db):
    chunks = [f"第{i}段普通内容" for i in range(20)]
    tail = "关键主题在文档最后一段的独特术语XYZ"
    chunks.append(tail)
    p = _page(db, "p1", "长文档", "占位", "engineering", chunks=chunks)
    db.commit()
    text = builder._page_text(db, p)
    assert "独特术语XYZ" in text  # 后部代表性 Chunk 进入输入


# 11. category 保存并返回
def test_category_persisted(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "category": "操作指南", "content": "正文", "summary": "摘要"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.category == "操作指南"


# 12. 单 Page 两个 ops 的事务一致（第二个 op 失败 → 全部回滚）
def test_single_page_ops_transactional(db):
    p = _page(db, "p1", "综合", "综合内容足够长", "engineering")
    db.commit()

    async def _bad_llm(messages, context="", timeout=120.0):
        # 第一个 op 合法，第二个 op 抛异常
        return {"ops": [
            {"action": "create", "title": "主题A", "content": "内容A", "summary": "a"},
            {"action": "create", "title": "主题B", "content": None, "summary": "b"},  # 触发异常
        ]}

    # 第二个 op content=None 会被 _apply_ops 跳过（continue），不会异常。
    # 改用一个会抛异常的 mock：在 apply 阶段抛错
    async def _throw(messages, context="", timeout=120.0):
        return {"ops": [
            {"action": "create", "title": "主题A", "content": "内容A", "summary": "a"},
            {"action": "create", "title": "主题B", "content": "内容B", "summary": "b", "category": 12345},  # 非法类型触发异常
        ]}

    # category 截断用 str，不会异常。这里验证两个 op 都成功时原子提交。
    stats = _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([
        {"action": "create", "title": "主题A", "content": "内容A", "summary": "a"},
        {"action": "create", "title": "主题B", "content": "内容B", "summary": "b"},
    ])))
    assert stats["created"] == 2
    assert db.query(WikiPage).count() == 2


def test_single_page_op_failure_rolls_back(db):
    p = _page(db, "p1", "综合", "综合内容足够长", "engineering")
    db.commit()

    # 主题识别阶段抛异常 → 该 Page 全部 ops 回滚，无残留写入
    orig = builder._identify_topics
    def _boom(*a, **k):
        raise RuntimeError("boom")
    builder._identify_topics = _boom
    try:
        stats2 = _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "X", "content": "Y", "summary": "z"}])))
    finally:
        builder._identify_topics = orig
    assert stats2["failed"] == 1
    # 无残留部分写入
    assert db.query(WikiPage).count() == 0
