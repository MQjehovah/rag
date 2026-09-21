"""wiki 语义搜索接口与向量回填端点的 API 测试。"""
import json
import uuid

import pytest

from app.models.database import WikiPage, get_session

DIM = 1024


def _vec(*idxs):
    v = [0.0] * DIM
    for i in idxs:
        v[i] = 1.0
    return v


class FakeEmbeddingService:
    """假嵌入服务,避免接口测试触发真实网络调用。"""

    def __init__(self, *args, **kwargs):
        pass

    async def encode(self, text):
        return _vec(0)

    async def encode_batch(self, texts, batch_size=32):
        return [_vec(0) for _ in texts]

    async def close(self):
        pass


@pytest.fixture
def fake_embedding(monkeypatch):
    monkeypatch.setattr("app.api.wiki.EmbeddingService", FakeEmbeddingService)
    monkeypatch.setattr("app.core.wiki_embedding.EmbeddingService", FakeEmbeddingService)


def _seed_wiki(engine, rows):
    """rows: (id, group_id, with_embedding) 列表。"""
    session = get_session(engine)
    try:
        for page_id, group_id, with_embedding in rows:
            session.add(WikiPage(
                id=page_id,
                title=f"页面-{uuid.uuid4()}",
                summary="摘要",
                content="正文",
                category="测试",
                group_id=group_id,
                embedding=json.dumps(_vec(0)) if with_embedding else None,
            ))
        session.commit()
    finally:
        session.close()


def test_search_route_declared_before_page_id():
    """/api/wiki/search 必须排在 /api/wiki/{page_id} 之前,否则会被单段路由吞掉。"""
    from app.main import app

    paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/wiki")]
    assert "/api/wiki/search" in paths
    assert paths.index("/api/wiki/search") < paths.index("/api/wiki/{page_id}")


def test_search_endpoint_reachable(api_engine, api_client, as_user, fake_embedding):
    """真实请求 /api/wiki/search:命中搜索端点(返回 results/total)而不是被 /{page_id} 接管。"""
    _seed_wiki(api_engine, [("w-public", None, True), ("w-fin", "财务部", True)])
    as_user(["研发部"])

    res = api_client.post("/api/wiki/search", json={"query": "查询内容", "top_k": 5})

    assert res.status_code == 200
    body = res.json()
    assert "results" in body and "total" in body
    assert {r["id"] for r in body["results"]} == {"w-public"}


def test_search_empty_query_400(api_client, as_user, fake_embedding):
    as_user(["研发部"])
    assert api_client.post("/api/wiki/search", json={"query": "   "}).status_code == 400


def test_reindex_forbidden_for_non_admin(api_client, as_user, fake_embedding):
    as_user(["研发部"])
    assert api_client.post("/api/wiki/reindex-embeddings").status_code == 403


def test_reindex_backfills_and_is_idempotent(api_engine, api_client, as_user, fake_embedding):
    _seed_wiki(api_engine, [("w-1", None, False), ("w-2", None, False)])
    as_user(["__local_admin__"])

    first = api_client.post("/api/wiki/reindex-embeddings")
    assert first.status_code == 200
    assert first.json() == {"embedded": 2, "errors": 0, "total": 2}

    second = api_client.post("/api/wiki/reindex-embeddings")
    assert second.status_code == 200
    assert second.json()["embedded"] == 0
