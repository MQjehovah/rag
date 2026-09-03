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
    _run(b._legacy_build_wiki_from_pages(db, [p]))
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

    _run(b._legacy_build_wiki_from_pages(db, [p]))
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
    result = _run(b._legacy_build_wiki_from_pages(db, [p]))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_last_error == "invalid_response"


# backlog 续泵（legacy）在 Phase 5.1 不再生产触发：旧 worker/pump 仅保留定义。
# 本测试更新为新语义：schedule_* 单轨 —— kill switch on → enqueue run；off →
# 返回未调度且保持 dirty；两者都不再 submit 旧 worker。
def test_schedule_entries_single_track_no_legacy_worker(monkeypatch):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    sch.shutdown()
    sch._semaphore = None

    submitted = {"pages": [], "wikis": []}

    def _fake_page_worker(page_id):
        submitted["pages"].append(page_id)

    def _fake_wiki_worker(wiki_id):
        submitted["wikis"].append(wiki_id)

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_page_worker)
    monkeypatch.setattr(sch, "_run_wiki_rebuild", _fake_wiki_worker)
    # kill switch 门控打桩（DB-first 细节由 test_phase51 覆盖）。
    monkeypatch.setattr(sch, "_pipeline_kill_switch_enabled", lambda: True)

    # kill switch on：调度入口走 enqueue（捕获入队调用，不再触达旧 worker）。
    enqueued = {"page_changed": [], "manual_rebuild": []}

    def _fake_enqueue_page(page_id):
        enqueued["page_changed"].append(page_id)
        return True

    def _fake_enqueue_wiki(wiki_id):
        enqueued["manual_rebuild"].append(wiki_id)
        return True

    monkeypatch.setattr(sch, "_enqueue_page_changed", _fake_enqueue_page)
    monkeypatch.setattr(sch, "_enqueue_manual_rebuild", _fake_enqueue_wiki)

    ok_page = sch.schedule_page_refresh("p1", changed=True)
    ok_wiki = sch.schedule_wiki_rebuild("w1")
    assert ok_page is True
    assert ok_wiki is True
    assert enqueued["page_changed"] == ["p1"]
    assert enqueued["manual_rebuild"] == ["w1"]
    assert submitted == {"pages": [], "wikis": []}, "不再 submit 旧 worker"

    # kill switch off：不建 run、不 submit worker、未调度（dirty 由 recover 兜底）。
    monkeypatch.setattr(sch, "_pipeline_kill_switch_enabled", lambda: False)
    assert sch.schedule_page_refresh("p2", changed=True) is False
    assert sch.schedule_wiki_rebuild("w2") is False
    assert enqueued["page_changed"] == ["p1"], "kill off 不得 enqueue"
    assert enqueued["manual_rebuild"] == ["w1"]
    assert submitted == {"pages": [], "wikis": []}
    sch.shutdown()
