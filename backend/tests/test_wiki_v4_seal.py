"""V4 Phase C 封板补丁测试。"""
from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.models.database import (
    Notebook,
    Page,
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


def _page(db, page_id, title, content, group_id):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content, wiki_dirty=True))
    db.flush()
    return db.get(Page, page_id)


def _run(coro):
    return asyncio.run(coro)


# 一、并发时序：_pump_lock 保证 pump 串行，失败项放回后由后续 pump 继续
def test_pump_lock_concurrent_timing(monkeypatch):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    monkeypatch.setattr(settings, "wiki_refresh_queue_size", 1)
    monkeypatch.setattr(settings, "wiki_refresh_workers", 1)
    sch.shutdown()
    sch._semaphore = None
    sch._shutdown_requested = False

    executed = set()

    def _fake_worker(page_id):
        executed.add(page_id)
        # 模拟真实 worker finally：清 pending → release → pump
        with sch._lock:
            sch._pending_pages.discard(page_id)
        sch._get_semaphore().release()
        sch._pump_recovery_backlog()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_worker)

    # 预置 3 个 page 任务
    with sch._lock:
        sch._recovery_page_backlog = ["p1", "p2", "p3"]
        sch._recovery_wiki_backlog = []

    sch._pump_recovery_backlog()
    import time
    deadline = time.time() + 3
    while time.time() < deadline:
        with sch._lock:
            remaining = len(sch._recovery_page_backlog) + len(sch._recovery_wiki_backlog)
            pending = len(sch._pending_pages) + len(sch._pending_wikis)
        if remaining == 0 and pending == 0:
            break
        time.sleep(0.01)

    assert executed == {"p1", "p2", "p3"}
    with sch._lock:
        assert len(sch._recovery_page_backlog) == 0
        assert len(sch._pending_pages) == 0
    sch.shutdown()


# 二、Map-Reduce reduce_limit_exceeded：不调用最终合成、不覆盖 Revision、保持 dirty
def test_mapreduce_limit_exceeded_no_final_synthesis(db):
    p1 = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p1], llm_json=lambda messages, context="", timeout=120.0: _identify_llm()))
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    # 重新构造一个多来源 wiki 触发 mapreduce，且每层返回超长摘要
    # 直接调 _mapreduce_synthesize 验证 limit exceeded
    from app.core.knowledge_compiler_v3.wiki_page_builder import _mapreduce_synthesize

    pages = []
    for i in range(200):
        pages.append(_page(db, f"m{i}", f"来源{i}", f"来源{i}的超长内容" * 200, "engineering"))
    db.commit()

    final_called = {"n": 0}

    async def _long_llm(messages, context="", timeout=120.0):
        if context == "wiki-mapreduce":
            final_called["n"] += 1
            return {"summary": "s", "content": "最终"}
        # 每层都返回远超预算的摘要，使 joined 持续超预算，最终达到 max_layers
        return {"summary": "X" * 20000}

    wiki = WikiPage(id="w1", title="主题", acl_scope='{"groups": ["engineering"]}', status="published", dirty=True, source_page_ids="[]")
    db.add(wiki); db.flush()
    rev = WikiRevision(id="rev1", wiki_page_id="w1", title="主题", summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id="sec1", revision_id="rev1", section_type="facts", heading="正文", content="旧", order_index=1))
    wiki.current_revision_id = "rev1"
    db.commit()

    old_rev_id = wiki.current_revision_id
    old_rev_count = db.query(WikiRevision).count()

    content, summary = _run(_mapreduce_synthesize(db, wiki, pages, "旧", _long_llm))

    # 达到 max_layers 后仍超预算 → 返回 None（invalid_response / reduce_limit_exceeded）
    assert content is None
    # 最终合成没有被调用
    assert final_called["n"] == 0
    # current_revision_id 不变、Revision 不增加、dirty 保持 True
    db.expire_all()
    wiki = db.get(WikiPage, "w1")
    assert wiki.current_revision_id == old_rev_id
    assert db.query(WikiRevision).count() == old_rev_count
    assert wiki.dirty is True


async def _identify_llm():
    return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}
