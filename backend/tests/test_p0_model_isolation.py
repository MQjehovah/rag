"""P0-BE-10：外部模型隔离规格测试。

锁定 tests/conftest.py autouse fixture 的行为契约：
- Fake 向量确定性（同文本同向量，不同文本不同向量，维度正确）
- 隔离实际生效（EmbeddingService 实例 encode 不外呼；LLM/Reranker/视觉配置被清空）
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.core.rag import EmbeddingService
from tests.conftest import fake_embedding_vector


def test_fake_vector_is_deterministic():
    assert fake_embedding_vector("电池安装") == fake_embedding_vector("电池安装")


def test_fake_vector_differs_by_text():
    assert fake_embedding_vector("电池安装") != fake_embedding_vector("电池拆卸")


def test_fake_vector_dimension_matches_config():
    assert len(fake_embedding_vector("x")) == settings.embedding_dimensions


@pytest.mark.asyncio
async def test_embedding_service_instance_uses_fake_encode():
    """autouse fixture patch 的是类方法——任何新实例化点都自动覆盖。"""
    svc = EmbeddingService()
    try:
        vec = await svc.encode("测试文本")
        assert vec == fake_embedding_vector("测试文本")
    finally:
        await svc.close()


def test_external_model_configs_cleared():
    """LLM/Reranker/视觉配置被清空，走各自生产降级路径（不外呼）。"""
    assert settings.llm_api_url == ""
    assert settings.reranker_api_url == ""
    assert settings.pdf_vision_enabled is False
