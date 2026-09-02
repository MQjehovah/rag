"""P13-MODEL-01/05：结果契约 + 错误分类测试。"""
from __future__ import annotations

from app.core.embedding.results import (
    EmbeddingBatchResult,
    RerankResult,
    classify_http_error,
    is_retryable,
)


def test_embedding_result_ok_semantics():
    ok = EmbeddingBatchResult(embeddings=[[1.0, 2.0]], used=True, dimensions=2)
    assert ok.ok is True

    degraded = EmbeddingBatchResult(embeddings=[], used=False, degraded=True, error_code="MODEL_NOT_LOADED")
    assert degraded.ok is False
    assert degraded.used is False  # 未真正调用


def test_rerank_result_ok_semantics():
    ok = RerankResult(results=[{"index": 0, "relevance_score": 0.9}], used=True)
    assert ok.ok is True

    degraded = RerankResult(results=[], used=False, degraded=True, error_code="MODEL_NOT_LOADED")
    assert degraded.ok is False


def test_classify_http_error_model_not_loaded():
    # 400 + "not in the model list" → MODEL_NOT_LOADED
    assert classify_http_error(400, "Model not found in the model list, uid: bge") == "MODEL_NOT_LOADED"


def test_classify_http_error_bad_request():
    assert classify_http_error(400, "invalid request") == "BAD_REQUEST"


def test_classify_http_error_auth_and_rate():
    assert classify_http_error(401, "") == "AUTH_FAILED"
    assert classify_http_error(403, "") == "AUTH_FAILED"
    assert classify_http_error(429, "") == "RATE_LIMITED"


def test_classify_http_error_server():
    assert classify_http_error(500, "") == "SERVER_ERROR"


def test_is_retryable():
    assert is_retryable("RATE_LIMITED") is True
    assert is_retryable("NETWORK_UNREACHABLE") is True
    assert is_retryable("MODEL_NOT_LOADED") is False
    assert is_retryable("DIMENSION_MISMATCH") is False
    assert is_retryable(None) is False
    assert is_retryable("UNKNOWN") is False
