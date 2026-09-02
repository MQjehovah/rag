"""V4 Phase C 调用链接线修复的行为测试。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable
from app.models.database import (
    Notebook,
    Page,
    WikiPage,
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


# 401 → service_unavailable（真实 call_wiki_llm_json 链路）
def test_http_401_maps_service_unavailable(db, monkeypatch):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    class _Resp:
        status_code = 401
        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=httpx.Request("POST", "http://x"), response=httpx.Response(401))
    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(settings, "llm_api_url", "http://mock")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")
    import app.core.knowledge_compiler_v3.wiki_page_builder as b
    monkeypatch.setattr(b.httpx, "AsyncClient", _Client)

    with pytest.raises(LLMServiceUnavailable):
        _run(b.call_wiki_llm_json([{"role": "user", "content": "x"}], context="wiki-synthesis"))

    # 通过正式 build 链路 → Page.wiki_last_error = service_unavailable
    _run(b.build_wiki_from_pages(db, [p]))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_last_error == "service_unavailable"


# timeout → service_unavailable
def test_http_timeout_maps_service_unavailable(db, monkeypatch):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(settings, "llm_api_url", "http://mock")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")
    import app.core.knowledge_compiler_v3.wiki_page_builder as b
    monkeypatch.setattr(b.httpx, "AsyncClient", _Client)

    with pytest.raises(LLMServiceUnavailable):
        _run(b.call_wiki_llm_json([{"role": "user", "content": "x"}], context="wiki-synthesis"))

    _run(b.build_wiki_from_pages(db, [p]))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_last_error == "service_unavailable"


# HTTP 200 但非法 JSON → invalid_response（通过 build 链路）
def test_http_200_illegal_json_maps_invalid_response(db, monkeypatch):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    class _Resp:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "这不是 JSON"}}]}
    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(settings, "llm_api_url", "http://mock")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")
    import app.core.knowledge_compiler_v3.wiki_page_builder as b
    monkeypatch.setattr(b.httpx, "AsyncClient", _Client)

    # 识别阶段返回非法 JSON → 空 dict → invalid_response
    result = _run(b.build_wiki_from_pages(db, [p]))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_last_error == "invalid_response"


# backlog 续泵：queue_size=1，3 dirty page + 2 dirty wiki，一次 recover，最终全部执行
def test_backlog_pump_executes_all(monkeypatch):
    import threading
    import time
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    monkeypatch.setattr(settings, "wiki_refresh_queue_size", 1)
    monkeypatch.setattr(settings, "wiki_refresh_workers", 1)
    sch.shutdown()
    sch._semaphore = None

    executed = {"pages": set(), "wikis": set()}

    def _fake_page_worker(page_id):
        executed["pages"].add(page_id)
        # 模拟真实 worker 的 finally：release 信号量 + 续泵
        sch._get_semaphore().release()
        sch._pump_recovery_backlog()

    def _fake_wiki_worker(wiki_id):
        executed["wikis"].add(wiki_id)
        sch._get_semaphore().release()
        sch._pump_recovery_backlog()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_page_worker)
    monkeypatch.setattr(sch, "_run_wiki_rebuild", _fake_wiki_worker)

    sch._recovery_page_backlog = ["p1", "p2", "p3"]
    sch._recovery_wiki_backlog = ["w1", "w2"]
    sch._attempted_page_ids.clear()
    sch._attempted_wiki_ids.clear()

    sch._pump_recovery_backlog()
    # 等待异步 worker 完成
    time.sleep(0.5)

    assert executed["pages"] == {"p1", "p2", "p3"}
    assert executed["wikis"] == {"w1", "w2"}
    sch.shutdown()
