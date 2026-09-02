"""V4 Phase C 最后一次修复的行为测试。

覆盖：同步调度、queue 上限、recover_dirty_pages、输入哈希、聚合、dirty 恢复。
全部使用临时 SQLite/内存库 + Mock LLM。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3 import wiki_refresh_scheduler as scheduler
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiWorkspace,
    init_db,
)

# Phase 3.1：同 ACL 不同 notebook 默认各自 workspace；本文件用「多来源合并成一篇
# Wiki」的 V4 语义，需把各来源 notebook 显式共享绑定到同一 workspace（engineering）。
_SHARED_WS_KEY = "test-shared-ws-wiki_v4_final"
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


def _page(db, page_id, title, content, group_id):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    _bind_shared_workspace(db, nb)
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content, wiki_dirty=True))
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


# 1. pages._schedule_wiki_refresh 是同步函数，不 await 同步函数
def test_schedule_wiki_refresh_is_sync():
    import app.api.pages as pages
    assert not inspect.iscoroutinefunction(pages._schedule_wiki_refresh)


# 2. executor 不再导入 fire_and_forget
def test_executor_does_not_import_fire_and_forget():
    import app.sources.executor as ex
    src = inspect.getsource(ex._schedule_wiki_refresh_for_page)
    assert "fire_and_forget" not in src


# 6. {} 不得判为 not_worthy
def test_empty_dict_not_worthy():
    assert builder._llm_outcome({}).status == "invalid_response"


# 7. 缺 worthy 不得 not_worthy
def test_missing_worthy_not_worthy():
    assert builder._llm_outcome({"ops": [{"action": "create", "title": "x", "category": "y"}]}).status == "invalid_response"


# 8. 只有 worthy=false 才 not_worthy；worthy=true + 空 ops = invalid_response
def test_only_explicit_false_is_not_worthy():
    assert builder._llm_outcome({"worthy": False, "ops": []}).status == "not_worthy"
    assert builder._llm_outcome({"worthy": True, "ops": []}).status == "invalid_response"
    assert builder._llm_outcome({"worthy": True, "ops": [{"action": "create", "title": "x", "category": "y"}]}).status == "success"


# 12 + 13. queue_size 是真实上限；队列满时返回 False（Page 保持 dirty）
def test_queue_size_real_limit(monkeypatch):
    import threading
    monkeypatch.setattr(settings, "wiki_refresh_queue_size", 1)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_worker(page_id):
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(scheduler, "_run_page_refresh", _blocking_worker)
    scheduler.shutdown()
    scheduler._semaphore = None
    ok = scheduler.schedule_page_refresh("p1", changed=True)
    assert ok is True
    entered.wait(timeout=5)
    ok2 = scheduler.schedule_page_refresh("p2", changed=True)
    assert ok2 is False  # 信号量=1 已被占用 → 队列满，Page 保持 dirty
    release.set()
    scheduler.shutdown()


# 14. PageChunk 参与 input_hash
def test_input_hash_includes_chunks(db):
    p = _page(db, "p1", "水箱", "正文", "engineering")
    db.add(PageChunk(id="c1", page_id="p1", chunk_index=0, content="第一段独特内容ABC"))
    db.commit()
    s1 = builder._snapshot_page(db, p)
    h1 = s1.input_hash
    # 修改 chunk 内容
    db.query(PageChunk).filter(PageChunk.id == "c1").update({PageChunk.content: "第一段独特内容XYZ"})
    db.commit()
    db.expire_all()
    p2 = db.get(Page, "p1")
    s2 = builder._snapshot_page(db, p2)
    assert s1.input_hash != s2.input_hash


# 15. ACL scope 在 LLM 期间变化 → 旧域 Wiki archived
def test_acl_scope_change_archives_old_domain(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))

    # 在 LLM 调用期间修改 notebook group_id（scope 变化）
    async def _scope_llm(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            nb = db.get(Notebook, "nb-p1")
            nb.group_id = "sales"
            db.commit()
            return {"worthy": True, "ops": [{"action": "update", "title": "水箱", "content": "正文", "summary": "摘要"}]}
        return {"summary": "s", "content": "聚合"}

    out = _run(builder.process_page_wiki(db, "p1", _scope_llm, commit=True))
    assert out["status"] == "scope_changed"
    db.expire_all()
    old = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').first()
    assert old is None or old.status == "archived"


# 17. 两个来源聚合后正文包含两个来源
def test_two_sources_aggregated(db):
    p1 = _page(db, "p1", "来源一", "第一个来源的知识A", "engineering")
    p2 = _page(db, "p2", "来源二", "第二个来源的知识B", "engineering")
    db.commit()

    captured = {}

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            prompt = messages[0]["content"]
            captured["prompt"] = prompt
            return {"summary": "s", "content": "知识A+知识B 聚合"}
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "content": "内容", "summary": "s"}]}

    _run(builder.build_wiki_from_pages(db, [p1], llm_json=_llm))
    _run(builder.build_wiki_from_pages(db, [p2], llm_json=_llm))
    # 最终合成 prompt 应包含两个来源标题
    assert "来源一" in captured["prompt"]
    assert "来源二" in captured["prompt"]
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    rev = db.get(WikiRevision, wp.current_revision_id)
    facts = db.query(WikiSection).filter(WikiSection.revision_id == rev.id, WikiSection.section_type == "facts").first()
    assert "知识A+知识B" in facts.content


# 18. 删除一个来源后使用剩余来源重新合成
def test_delete_one_source_resynthesize(db):
    p1 = _page(db, "p1", "来源一", "第一个来源的知识A完整描述", "engineering")
    p2 = _page(db, "p2", "来源二", "第二个来源的知识B完整描述", "engineering")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            # 只返回剩余来源的知识（模拟删除 p1 后只合成 p2）
            return {"summary": "s", "content": "只剩知识B"}
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "content": "内容", "summary": "s"}]}

    _run(builder.build_wiki_from_pages(db, [p1], llm_json=_llm))
    _run(builder.build_wiki_from_pages(db, [p2], llm_json=_llm))
    builder.remove_source_page_from_wikis(db, "p1")
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert wp.dirty is True
    _run(builder.refresh_dirty_wikis(db, llm_json=_llm))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    rev = db.get(WikiRevision, wp.current_revision_id)
    facts = db.query(WikiSection).filter(WikiSection.revision_id == rev.id, WikiSection.section_type == "facts").first()
    assert "只剩知识B" in facts.content


# 19. 多 Page 一页失败不回滚其他 Page
def test_one_page_failure_does_not_rollback_others(db):
    p1 = _page(db, "p1", "水箱一", "知识A足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "知识B足够长", "engineering")
    db.commit()

    async def _selective_llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "s", "content": "聚合"}
        # 识别阶段：p1 返回 ops，p2 抛异常
        if "水箱二" in (messages[0].get("content") if messages else ""):
            raise RuntimeError("fail p2")
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "content": "知识A", "summary": "s"}]}

    stats = _run(builder.build_wiki_from_pages(db, [p1, p2], llm_json=_selective_llm))
    # p1 成功，p2 失败（服务不可用），p1 的 Wiki 已提交
    assert db.query(WikiPage).count() == 1


# 23. shutdown 自然结束 worker
def test_shutdown_natural_exit(monkeypatch):
    monkeypatch.setattr(settings, "wiki_refresh_queue_size", 10)
    monkeypatch.setattr(scheduler, "_run_page_refresh", lambda page_id: None)
    scheduler.shutdown()
    scheduler._semaphore = None
    scheduler.schedule_page_refresh("p1")
    scheduler.shutdown()  # 不应抛异常
    assert scheduler._executor is None
