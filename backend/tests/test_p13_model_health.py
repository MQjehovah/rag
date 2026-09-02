"""P13-MODEL-06：模型健康检查测试。"""
from __future__ import annotations

import asyncio

import pytest

from app.core.embedding.health import _models_url, _probe_model, clear_health_cache


def _run(coro):
    return asyncio.run(coro)


def test_models_url_derivation():
    assert _models_url("https://bms/rag/v1/embeddings") == "https://bms/rag/v1/models"
    assert _models_url("https://bms/rag/v1/rerank") == "https://bms/rag/v1/models"


def test_probe_model_not_loaded(monkeypatch):
    """models 列表为空 → MODEL_NOT_LOADED。"""
    import httpx as h

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": []}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr("app.core.embedding.health.httpx.AsyncClient", lambda **kw: _Client())

    result = _run(_probe_model("https://bms/rag/v1/embeddings", "bge-large"))
    assert result["available"] is False
    assert result["last_error_code"] == "MODEL_NOT_LOADED"


def test_probe_model_available(monkeypatch):
    import httpx as h

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"id": "bge-large"}]}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr("app.core.embedding.health.httpx.AsyncClient", lambda **kw: _Client())

    result = _run(_probe_model("https://bms/rag/v1/embeddings", "bge-large"))
    assert result["available"] is True
    assert result["last_success_at"] is not None


def test_probe_model_unconfigured():
    result = _run(_probe_model("", ""))
    assert result["configured"] is False
    assert result["last_error_code"] == "BAD_REQUEST"


def test_probe_model_uid_not_in_list(monkeypatch):
    import httpx as h

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"id": "other-model"}]}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr("app.core.embedding.health.httpx.AsyncClient", lambda **kw: _Client())

    result = _run(_probe_model("https://bms/rag/v1/embeddings", "bge-large"))
    assert result["available"] is False
    assert result["last_error_code"] == "MODEL_NOT_LOADED"
