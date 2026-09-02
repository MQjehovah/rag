"""模型健康检查（P13-MODEL-06，V3 计划 8.5）。

轻量探测公司 Embedding/Reranker 服务的模型状态：
- 只探测 /rag/v1/models 是否返回配置的 UID
- 结果短缓存，禁止每个请求都调 /v1/models
- 后端启动时只做轻量探测，不因模型不可用阻止启动
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings
from app.core.embedding.results import classify_http_error

logger = logging.getLogger(__name__)

# 短缓存：秒
_CACHE_TTL_SECONDS = 60

_health_cache: dict[str, tuple[float, dict]] = {}


def _models_url(api_url: str) -> str:
    """从 embedding/rerank 的 API URL 推导 models 端点。"""
    base = (api_url or "").rstrip("/")
    if base.endswith("/embeddings"):
        base = base[: -len("/embeddings")]
    if base.endswith("/rerank"):
        base = base[: -len("/rerank")]
    return f"{base}/models"


async def _probe_model(api_url: str, model_uid: str, auth_token: str = "") -> dict:
    """探测单个模型服务是否加载了指定 UID。"""
    result = {
        "configured": bool(api_url and model_uid),
        "model_uid": model_uid,
        "available": False,
        "dimensions": None,
        "last_success_at": None,
        "last_error_code": None,
    }
    if not api_url or not model_uid:
        result["last_error_code"] = "BAD_REQUEST"
        return result

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_models_url(api_url), headers=headers)
        if resp.status_code != 200:
            result["last_error_code"] = classify_http_error(resp.status_code, resp.text)
            return result
        data = resp.json()
        model_list = data.get("data", [])
        uids = {m.get("id") for m in model_list if isinstance(m, dict)}
        if model_uid in uids:
            result["available"] = True
            result["last_success_at"] = _now_iso()
        else:
            result["last_error_code"] = "MODEL_NOT_LOADED"
    except httpx.ConnectError:
        result["last_error_code"] = "NETWORK_UNREACHABLE"
    except httpx.TimeoutException:
        result["last_error_code"] = "TIMEOUT"
    except Exception as exc:
        result["last_error_code"] = "SERVER_ERROR"
    return result


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def get_model_health(force: bool = False) -> dict:
    """返回 embedding + reranker 健康状态（短缓存）。"""
    cache_key = "health"
    now = time.monotonic()
    if not force and cache_key in _health_cache:
        cached_at, cached = _health_cache[cache_key]
        if now - cached_at < _CACHE_TTL_SECONDS:
            return cached

    embedding = await _probe_model(
        settings.embedding_api_url, settings.embedding_model, settings.llm_api_key,
    )
    reranker = await _probe_model(
        settings.reranker_api_url, settings.reranker_model, settings.llm_api_key,
    )
    result = {"embedding": embedding, "reranker": reranker}
    _health_cache[cache_key] = (now, result)
    return result


def clear_health_cache() -> None:
    _health_cache.clear()
