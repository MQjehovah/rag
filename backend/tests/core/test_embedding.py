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
