"""向量编码行为测试。

原先测的 app.core.embedding.encoder / store 模块已不存在,当前职责合并到
app.core.rag.EmbeddingService(encode / encode_batch),故改测实际实现。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.rag import EmbeddingService


def _mock_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _make_service():
    """构造服务并强制走 OpenAI 兼容分支,避免依赖 .env 中的真实网关地址。"""
    svc = EmbeddingService()
    svc.api_url = "http://example.test/v1/embeddings"
    svc.model = "test-model"
    return svc


@pytest.mark.asyncio
async def test_encode_returns_embedding_vector():
    """单条编码返回网关响应中的 embedding 数组。"""
    svc = _make_service()
    svc.client.post = AsyncMock(
        return_value=_mock_response({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    )
    try:
        result = await svc.encode("测试")
    finally:
        await svc.close()

    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_encode_batch_returns_one_vector_per_input():
    """批量编码按输入顺序返回等量向量。"""
    svc = _make_service()
    svc.client.post = AsyncMock(
        return_value=_mock_response(
            {"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]}
        )
    )
    try:
        result = await svc.encode_batch(["甲", "乙"])
    finally:
        await svc.close()

    assert result == [[1.0], [2.0]]


# ---- 回归: 网关鉴权头 / 空向量短路(2026-09-21 问答故障) ----


@pytest.mark.asyncio
async def test_encode_sends_bearer_token(monkeypatch):
    """走公司网关时必须带 Bearer(缺头曾被网关 401,导致检索向量为空、答案无来源)。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "sk-test-abc")
    svc = _make_service()
    svc.client.post = AsyncMock(
        return_value=_mock_response({"data": [{"embedding": [0.5]}]})
    )
    try:
        await svc.encode("你好")
        _, kwargs = svc.client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test-abc"
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_encode_batch_sends_bearer_token(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "sk-test-abc")
    svc = _make_service()
    svc.client.post = AsyncMock(
        return_value=_mock_response({"data": [{"embedding": [0.5]}]})
    )
    try:
        await svc.encode_batch(["一段"])
        _, kwargs = svc.client.post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test-abc"
    finally:
        await svc.close()


def test_headers_empty_without_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    svc = EmbeddingService()
    assert svc._headers == {}


def test_search_sync_short_circuits_empty_embedding():
    """空向量必须直接返回,不能走到 CAST('[]' AS vector) —— 那会报错并污染事务。"""

    class FakeDb:
        def __init__(self):
            self.executed = 0
            self.rolled_back = False

        def execute(self, *a, **k):
            self.executed += 1
            raise AssertionError("空向量时不应发起任何 SQL")

        def rollback(self):
            self.rolled_back = True

    from app.core.rag import VectorStore

    db = FakeDb()
    store = VectorStore(db)
    assert store._search_sync([], top_k=5) == []
    assert db.executed == 0
