"""V4 Phase E：服务降级测试（最终封板状态机）。

覆盖：三正交概念、LLM 生成门禁、正确状态矩阵、服务状态字段、债务边界、
LLM 各类失败、retrieval_only Wiki/Raw、ACL 不泄露、degraded reason 不泄露。
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.degradation import (
    build_rag_response,
    call_llm_answer,
    classify_llm_failure,
    llm_configured,
    should_generate_answer,
)
from app.core.retrieval.orchestrator import OrchestrationOutcome
from app.core.retrieval.raw_retriever import RawChunkHit, RawRetrievalResult
from app.core.retrieval.wiki_retriever import WikiHit, WikiRetrievalResult
from app.models.database import (
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


def _wiki_hit(page_id, title, content):
    return WikiHit(
        wiki_page_id=page_id, title=title, summary="", content=content,
        score=0.9, acl_scope='{"groups": ["engineering"]}',
    )


def _raw_hit(chunk_id, page_id, content, source_type="dingtalk", source_url="https://doc/x"):
    return RawChunkHit(
        chunk_id=chunk_id, page_id=page_id, notebook_id="nb", page_title="原始文档",
        content=content, chunk_index=0, final_score=0.8, bm25_score=0.8,
        dense_score=None, rerank_score=None, source_type=source_type,
        source_url=source_url, retrieval_round=1,
    )


def _wiki_outcome(hits):
    return OrchestrationOutcome(
        mode="wiki_hit",
        wiki_results=WikiRetrievalResult(hits=hits, visible_wiki_count=len(hits), published_wiki_count=len(hits), query="q"),
    )


def _raw_outcome(hits, degraded=None, dense=False, reranker=False, mode="raw_hit"):
    return OrchestrationOutcome(
        mode=mode,
        wiki_results=WikiRetrievalResult(),
        raw_results=RawRetrievalResult(
            hits=hits, degraded=list(degraded or []), dense_used=dense, reranker_used=reranker,
        ),
    )


def _seed_wiki(db, page_id, title, content):
    page = WikiPage(id=page_id, title=title, summary="", acl_scope='{"groups": ["engineering"]}', status="published")
    db.add(page); db.flush()
    rev = WikiRevision(id=f"{page_id}-rev", wiki_page_id=page_id, title=title, summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.commit()


# ---------------------------------------------------------------------------
# LLM 失败分类
# ---------------------------------------------------------------------------

def test_classify_llm_failure_unauthorized():
    exc = httpx.HTTPStatusError("401", request=httpx.Request("POST", "http://x"), response=httpx.Response(401, request=httpx.Request("POST", "http://x")))
    assert classify_llm_failure(exc) == "llm_unauthorized"


def test_classify_llm_failure_timeout():
    assert classify_llm_failure(httpx.ConnectTimeout("timeout")) == "llm_timeout"


def test_classify_llm_failure_server_error():
    exc = httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500, request=httpx.Request("POST", "http://x")))
    assert classify_llm_failure(exc) == "llm_server_error"


def test_classify_llm_failure_network():
    assert classify_llm_failure(httpx.ConnectError("conn")) == "llm_network_error"


def test_classify_llm_failure_rate_limited():
    exc = httpx.HTTPStatusError("429", request=httpx.Request("POST", "http://x"), response=httpx.Response(429, request=httpx.Request("POST", "http://x")))
    assert classify_llm_failure(exc) == "llm_rate_limited"


@pytest.mark.asyncio
async def test_call_llm_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_url", "")
    r = await call_llm_answer([{"role": "user", "content": "hi"}])
    assert r.ok is False
    assert r.degraded_reason == "llm_not_configured"


@pytest.mark.asyncio
async def test_call_llm_http_error_safe(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_url", "http://llm.local")
    monkeypatch.setattr(settings, "llm_api_key", "secret-key")
    monkeypatch.setattr(settings, "llm_model", "gpt")

    async def _fake_post(*a, **k):
        raise httpx.HTTPStatusError(
            "401", request=httpx.Request("POST", "http://llm.local"),
            response=httpx.Response(401, request=httpx.Request("POST", "http://llm.local"), text="secret response body"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    r = await call_llm_answer([{"role": "user", "content": "hi"}])
    assert r.ok is False
    assert r.degraded_reason == "llm_unauthorized"
    assert "secret" not in r.degraded_reason


@pytest.mark.asyncio
async def test_call_llm_success(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_url", "http://llm.local")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "答案是 X"}}]}

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **k): return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    r = await call_llm_answer([{"role": "user", "content": "hi"}])
    assert r.ok is True
    assert r.answer == "答案是 X"


# ---------------------------------------------------------------------------
# LLM 生成门禁
# ---------------------------------------------------------------------------

def test_should_generate_answer_wiki_hit():
    assert should_generate_answer(_wiki_outcome([_wiki_hit("w1", "T", "C")])) is True


def test_should_generate_answer_raw_hit():
    assert should_generate_answer(_raw_outcome([_raw_hit("c1", "p1", "内容")])) is True


def test_should_generate_answer_need_community():
    # need_community_expansion + partial hits → 不生成
    outcome = _raw_outcome([_raw_hit("c1", "p1", "部分片段")], mode="need_community_expansion")
    assert should_generate_answer(outcome) is False


def test_should_generate_answer_empty():
    outcome = OrchestrationOutcome(mode="need_community_expansion", wiki_results=WikiRetrievalResult(), raw_results=RawRetrievalResult())
    assert should_generate_answer(outcome) is False


# ---------------------------------------------------------------------------
# 状态矩阵
# ---------------------------------------------------------------------------

def test_normal_wiki_answer(db):
    outcome = _wiki_outcome([_wiki_hit("w1", "T", "C")])
    resp = build_rag_response(db, outcome, llm_answer="回答", llm_called=True, llm_ok=True)
    assert resp["response_mode"] == "answer"
    assert resp["service_degraded"] is False
    assert resp["embedding_status"] == "not_used"
    assert resp["reranker_status"] == "not_used"
    assert resp["model_status"] == "used"
    assert resp["answer_eligible"] is True


def test_normal_raw_answer(db):
    outcome = _raw_outcome([_raw_hit("c1", "p1", "内容")], dense=True, reranker=True)
    resp = build_rag_response(db, outcome, llm_answer="回答", llm_called=True, llm_ok=True)
    assert resp["response_mode"] == "answer"
    assert resp["embedding_status"] == "used"
    assert resp["reranker_status"] == "used"
    # 无 degraded reasons → service_degraded=false
    assert resp["service_degraded"] is False


def test_llm_fail_with_data_retrieval_only(db):
    outcome = _wiki_outcome([_wiki_hit("w1", "T", "C")])
    resp = build_rag_response(
        db, outcome, llm_answer=None, llm_called=True, llm_ok=False,
        llm_degraded_reason="llm_timeout",
    )
    assert resp["response_mode"] == "retrieval_only"
    assert resp["service_degraded"] is True
    assert resp["knowledge_missing"] is False
    assert resp["model_status"] == "degraded"
    assert resp["answer"] is None
    assert "llm_timeout" in resp["degraded_reasons"]


def test_no_result_knowledge_missing(db):
    outcome = OrchestrationOutcome(mode="need_community_expansion", wiki_results=WikiRetrievalResult(), raw_results=RawRetrievalResult())
    resp = build_rag_response(db, outcome, llm_answer=None, llm_called=False, llm_ok=False)
    assert resp["response_mode"] == "insufficient"
    assert resp["knowledge_missing"] is True
    assert resp["service_degraded"] is False
    assert resp["debt_created"] is False
    assert resp["model_status"] == "not_used"


def test_retrieval_failed(db):
    resp = build_rag_response(
        db, None, llm_answer=None, llm_called=False, llm_ok=False, retrieval_failed=True,
    )
    assert resp["response_mode"] == "insufficient"
    assert resp["service_degraded"] is True
    assert resp["knowledge_missing"] is False  # 异常不标记 knowledge_missing
    assert resp["debt_created"] is False
    assert "retrieval_unavailable" in resp["degraded_reasons"]
    assert resp["retrieval_completed"] is False


def test_partial_hits_no_answer(db):
    # need_community_expansion + partial hits：不生成答案，允许展示片段
    outcome = _raw_outcome([_raw_hit("c1", "p1", "部分片段")], mode="need_community_expansion")
    resp = build_rag_response(db, outcome, llm_answer=None, llm_called=False, llm_ok=False)
    assert resp["response_mode"] == "insufficient"
    assert resp["answer"] is None
    assert resp["answer_eligible"] is False
    assert resp["model_status"] == "not_used"
    assert resp["knowledge_missing"] is False
    assert resp["debt_created"] is False
    assert len(resp["raw_results"]) == 1
    # service_degraded 仅由真实 degraded reasons 决定（此处无 reasons → false）
    assert resp["service_degraded"] is False
    # 无 llm_* degraded reason（LLM 未调用）
    assert not any(r.startswith("llm_") for r in resp["degraded_reasons"])


# ---------------------------------------------------------------------------
# 服务状态字段
# ---------------------------------------------------------------------------

def test_embedding_reranker_degraded(db):
    outcome = _raw_outcome(
        [_raw_hit("c1", "p1", "内容")],
        degraded=["embedding_unavailable", "reranker_unavailable"],
    )
    resp = build_rag_response(db, outcome, llm_answer=None, llm_called=True, llm_ok=False, llm_degraded_reason="llm_timeout")
    assert resp["embedding_status"] == "degraded"
    assert resp["reranker_status"] == "degraded"
    assert "embedding_unavailable" in resp["degraded_reasons"]
    assert "reranker_unavailable" in resp["degraded_reasons"]


def test_embedding_reranker_not_used_when_no_raw(db):
    outcome = _wiki_outcome([_wiki_hit("w1", "T", "C")])
    resp = build_rag_response(db, outcome, llm_answer="回答", llm_called=True, llm_ok=True)
    assert resp["embedding_status"] == "not_used"
    assert resp["reranker_status"] == "not_used"


def test_no_sensitive_leak(db):
    outcome = _raw_outcome([_raw_hit("c1", "p1", "内容")], degraded=["reranker_unauthorized"])
    resp = build_rag_response(db, outcome, llm_answer=None, llm_called=True, llm_ok=False, llm_degraded_reason="llm_unauthorized")
    joined = json.dumps(resp, ensure_ascii=False)
    assert "Bearer" not in joined
    assert "secret" not in joined
    assert "http://llm" not in joined


# ---------------------------------------------------------------------------
# 端点级：LLM 未配置 → retrieval_only（不 500）
# ---------------------------------------------------------------------------

def test_rag_chat_endpoint_retrieval_only(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "llm_api_url", "")

    def _user():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.post("/api/rag-chat", json={"query": "水箱", "scope_id": "admin"})
        assert r.status_code == 200
        data = r.json()
        assert data["response_mode"] in ("retrieval_only", "insufficient")
        assert data["model_status"] == "not_used"  # 门禁：零结果或未生成 → 未调用 LLM
        assert "debt_created" not in data  # J-4：普通响应不再暴露债务字段
        assert "answer_id" in data
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# 端点级：partial hits（need_community_expansion）→ insufficient，LLM 调用 0 次
# ---------------------------------------------------------------------------

def test_rag_chat_endpoint_partial_hits_insufficient(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps, rag_chat
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    def _user():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    # fake orchestrator：need_community_expansion + partial raw hits
    partial_outcome = OrchestrationOutcome(
        mode="need_community_expansion",
        wiki_results=WikiRetrievalResult(),
        raw_results=RawRetrievalResult(hits=[_raw_hit("c1", "p1", "部分片段内容")]),
    )

    class _FakeOrchestrator:
        def __init__(self, *a, **k):
            pass
        async def retrieve_async(self, question, current_user):
            return partial_outcome

    llm_calls = []

    async def _fake_call_llm(messages, **k):
        llm_calls.append(messages)
        from app.core.retrieval.degradation import LLMAnswerResult
        return LLMAnswerResult(ok=True, answer="不应被调用")

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(rag_chat, "call_llm_answer", _fake_call_llm)

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.post("/api/rag-chat", json={"query": "水箱故障", "scope_id": "admin"})
        assert r.status_code == 200
        data = r.json()
        assert data["response_mode"] == "insufficient"
        assert data["answer_eligible"] is False
        assert data["answer"] is None
        assert data["model_status"] == "not_used"
        assert data["knowledge_missing"] is False
        assert "debt_created" not in data  # J-4：普通响应不再暴露债务字段
        assert "answer_id" in data
        assert len(data["raw_results"]) == 1  # partial 资料仍返回
        assert not any(x.startswith("llm_") for x in data["degraded_reasons"])
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert len(llm_calls) == 0  # 门禁：未调用 LLM
