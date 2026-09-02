"""服务降级层（V4 Phase E 最终封板）。

三个正交概念：
- retrieval_completed：检索是否成功完成（未抛异常）；
- answer_eligible：证据是否达到可生成答案条件（mode ∈ {wiki_hit, raw_hit}）；
- service_degraded：是否真实发生 LLM/Embedding/Reranker/Retrieval 降级（由 degraded
  reasons 决定，绝不因「是否有结果」推导）。

LLM 生成门禁：只有 answer_eligible 才允许调用 LLM；need_community_expansion、
Wiki/Raw 均空、检索异常均不调用 LLM（call_llm_answer 次数为 0）。

服务状态字段支持 used / degraded / not_used：
- model_status：used（LLM 成功）/ degraded（LLM 失败）/ not_used（未调用 LLM）；
- embedding_status / reranker_status：used / degraded（仅明确失败枚举）/ not_used
  （Wiki 命中导致 Raw 服务未调用，或 Raw 未真正用到该服务）。

债务边界：knowledge_missing 仅在「检索成功完成 且 确实无 Wiki/Raw」时为 true；
服务异常/超时/未执行绝不标记 knowledge_missing；Phase E 不写债务，debt_created 恒 false。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.retrieval.orchestrator import OrchestrationOutcome
from app.core.retrieval.raw_retriever import RawChunkHit
from app.core.retrieval.wiki_retriever import WikiHit
from app.models.database import Page, WikiPage, WikiRevision

logger = logging.getLogger(__name__)

# 安全 reason 枚举（不含响应正文/URL/密钥）
LLM_UNAUTHORIZED = "llm_unauthorized"
LLM_RATE_LIMITED = "llm_rate_limited"
LLM_TIMEOUT = "llm_timeout"
LLM_SERVER_ERROR = "llm_server_error"
LLM_NETWORK_ERROR = "llm_network_error"
LLM_INVALID_RESPONSE = "llm_invalid_response"
LLM_NOT_CONFIGURED = "llm_not_configured"
RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"

# 降级原因稳定排序（前端展示）
_DEGRADED_ORDER = [
    RETRIEVAL_UNAVAILABLE,
    LLM_NOT_CONFIGURED,
    LLM_UNAUTHORIZED,
    LLM_RATE_LIMITED,
    LLM_TIMEOUT,
    LLM_SERVER_ERROR,
    LLM_NETWORK_ERROR,
    LLM_INVALID_RESPONSE,
    "embedding_unavailable",
    "embedding_timeout",
    "embedding_unauthorized",
    "embedding_empty",
    "no_indexed_vectors",
    "embedding_dimension_mismatch",
    "reranker_unavailable",
    "reranker_timeout",
    "reranker_unauthorized",
    "reranker_invalid_response",
]

_EMBEDDING_REASONS = {
    "embedding_unavailable", "embedding_timeout", "embedding_unauthorized",
    "embedding_empty", "no_indexed_vectors", "embedding_dimension_mismatch",
}
_RERANKER_REASONS = {
    "reranker_unavailable", "reranker_timeout", "reranker_unauthorized",
    "reranker_invalid_response",
}


def llm_configured() -> bool:
    return bool(settings.llm_api_url and settings.llm_api_key)


def classify_llm_failure(exc: Exception) -> str:
    """把 LLM 异常映射为安全 reason。不返回响应正文/URL/密钥。"""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return LLM_UNAUTHORIZED
        if status == 429:
            return LLM_RATE_LIMITED
        if status >= 500:
            return LLM_SERVER_ERROR
        return LLM_INVALID_RESPONSE
    if isinstance(exc, httpx.TimeoutException):
        return LLM_TIMEOUT
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.ConnectTimeout)):
        return LLM_NETWORK_ERROR
    return LLM_INVALID_RESPONSE


@dataclass
class LLMAnswerResult:
    ok: bool
    answer: str = ""
    degraded_reason: str = ""


async def call_llm_answer(messages: list[dict], *, timeout: float = 120.0) -> LLMAnswerResult:
    """调用 LLM 生成答案。任何失败都降级为 ok=False + 安全 reason，不抛异常。"""
    if not llm_configured():
        return LLMAnswerResult(ok=False, degraded_reason=LLM_NOT_CONFIGURED)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                settings.llm_api_url,
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return LLMAnswerResult(ok=False, degraded_reason=classify_llm_failure(exc))
    except Exception as exc:  # noqa: BLE001
        return LLMAnswerResult(ok=False, degraded_reason=classify_llm_failure(exc))

    try:
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except (TypeError, AttributeError, IndexError, KeyError):
        return LLMAnswerResult(ok=False, degraded_reason=LLM_INVALID_RESPONSE)

    if not content or not content.strip():
        return LLMAnswerResult(ok=False, degraded_reason=LLM_INVALID_RESPONSE)
    return LLMAnswerResult(ok=True, answer=content.strip())


def should_generate_answer(outcome: OrchestrationOutcome) -> bool:
    """LLM 生成门禁：只有 Wiki/Raw 充分命中才允许生成答案。"""
    if outcome is None:
        return False
    if outcome.mode not in ("wiki_hit", "raw_hit"):
        return False
    has_wiki = bool(outcome.wiki_results and outcome.wiki_results.hits)
    has_raw = bool(outcome.raw_results and outcome.raw_results.hits)
    return has_wiki or has_raw


def _wiki_metadata(db: Session, hits: list[WikiHit]) -> dict[str, dict]:
    ids = [h.wiki_page_id for h in hits]
    if not ids:
        return {}
    meta: dict[str, dict] = {}
    for wp in db.query(WikiPage).filter(WikiPage.id.in_(ids)).all():
        meta[wp.id] = {"updated_at": wp.updated_at, "locked": bool(wp.locked), "edit_type": "auto"}
    for wp in db.query(WikiPage).filter(WikiPage.id.in_(ids)).all():
        if wp.current_revision_id:
            rev = db.get(WikiRevision, wp.current_revision_id)
            if rev is not None:
                meta[wp.id]["edit_type"] = rev.edit_type or "auto"
    return meta


def _page_metadata(db: Session, hits: list[RawChunkHit]) -> dict[str, dict]:
    ids = {h.page_id for h in hits}
    if not ids:
        return {}
    return {p.id: {"updated_at": p.updated_at} for p in db.query(Page).filter(Page.id.in_(ids)).all()}


def build_wiki_view(db: Session, hits: list[WikiHit]) -> list[dict]:
    meta = _wiki_metadata(db, hits)
    out = []
    for h in hits:
        m = meta.get(h.wiki_page_id, {})
        out.append({
            "wiki_page_id": h.wiki_page_id,
            "title": h.title,
            "content": h.content,
            "summary": h.summary,
            "score": h.score,
            "updated_at": m.get("updated_at").isoformat() if m.get("updated_at") else None,
            "manually_edited": m.get("edit_type") == "manual",
            "locked": m.get("locked", False),
            # V4 Phase I：版本字段（用户可见，不含来源）。
            "version_label": h.version_label,
            "is_common": h.is_common,
            "latest_version": h.latest_version,
            "diff_notice": h.diff_notice,
            "link": f"/knowledge/wiki/{h.wiki_page_id}",
        })
    return out


def build_raw_view(db: Session, hits: list[RawChunkHit]) -> list[dict]:
    meta = _page_metadata(db, hits)
    out = []
    for h in hits:
        m = meta.get(h.page_id, {})
        out.append({
            "chunk_id": h.chunk_id,
            "page_id": h.page_id,
            "title": h.page_title,
            "source_type": h.source_type,
            "chunk_index": h.chunk_index,
            "content": h.content,
            "updated_at": m.get("updated_at").isoformat() if m.get("updated_at") else None,
            "link": h.source_url or "/knowledge/documents",
        })
    return out


def _stable_reasons(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    order = {name: i for i, name in enumerate(_DEGRADED_ORDER)}
    out.sort(key=lambda x: order.get(x, len(order)))
    return out


def _service_status(
    *,
    used: bool,
    reasons: list[str],
    reason_set: set[str],
) -> str:
    """服务状态：used / degraded / not_used。只有明确失败枚举才是 degraded。"""
    if used:
        return "used"
    if any(r in reason_set for r in reasons):
        return "degraded"
    return "not_used"


def build_rag_response(
    db: Session,
    outcome: OrchestrationOutcome | None,
    *,
    llm_answer: str | None,
    llm_called: bool,
    llm_ok: bool,
    llm_degraded_reason: str = "",
    retrieval_failed: bool = False,
) -> dict:
    """把编排结果 + LLM 结果组装成 Phase E 完整响应。"""
    # ---- 正交概念 1：检索是否完成 ----
    retrieval_completed = not retrieval_failed

    if retrieval_failed or outcome is None:
        wiki_hits: list[WikiHit] = []
        raw_hits: list[RawChunkHit] = []
        mode = "none"
        raw_degraded: list[str] = []
        dense_used = False
        reranker_used = False
    else:
        wiki_hits = outcome.wiki_results.hits if outcome.wiki_results else []
        raw_hits = outcome.raw_results.hits if outcome.raw_results else []
        mode = outcome.mode
        raw_degraded = list(outcome.raw_results.degraded) if outcome.raw_results else []
        dense_used = bool(outcome.raw_results and outcome.raw_results.dense_used)
        reranker_used = bool(outcome.raw_results and outcome.raw_results.reranker_used)

    has_wiki = bool(wiki_hits)
    has_raw = bool(raw_hits)

    # ---- 正交概念 2：是否有生成资格 ----
    answer_eligible = (not retrieval_failed) and mode in ("wiki_hit", "raw_hit") and (has_wiki or has_raw)

    # ---- 降级原因汇总（真实发生才计入） ----
    reasons: list[str] = []
    if retrieval_failed:
        reasons.append(RETRIEVAL_UNAVAILABLE)
    reasons.extend(raw_degraded)
    if llm_called and not llm_ok and llm_degraded_reason:
        reasons.append(llm_degraded_reason)

    # model_status
    if not llm_called:
        model_status = "not_used"
    elif llm_ok:
        model_status = "used"
    else:
        model_status = "degraded"

    # embedding / reranker status（Wiki 命中 → Raw 未调用 → not_used）
    if retrieval_failed:
        embedding_status = "not_used"
        reranker_status = "not_used"
    elif mode == "wiki_hit":
        embedding_status = "not_used"
        reranker_status = "not_used"
    else:
        embedding_status = _service_status(used=dense_used, reasons=raw_degraded, reason_set=_EMBEDDING_REASONS)
        reranker_status = _service_status(used=reranker_used, reasons=raw_degraded, reason_set=_RERANKER_REASONS)

    # ---- 正交概念 3：service_degraded（真实降级，由 reasons 推导） ----
    service_degraded = bool(reasons)

    # ---- knowledge_missing（仅检索成功且确实无结果） ----
    knowledge_missing = retrieval_completed and not has_wiki and not has_raw

    # ---- response_mode ----
    # retrieval_only 只表示「本可生成答案，但 LLM 失败后切换资料检索」；
    # 证据不足（answer_eligible=false）一律 insufficient，绝不伪装成 retrieval_only。
    if retrieval_failed:
        response_mode = "insufficient"
    elif answer_eligible and llm_called and llm_ok:
        response_mode = "answer"
    elif answer_eligible and llm_called and not llm_ok:
        response_mode = "retrieval_only"
    else:
        response_mode = "insufficient"

    # ---- answer_source_mode ----
    if mode == "wiki_hit":
        answer_source_mode = "wiki"
    elif mode == "raw_hit":
        answer_source_mode = "raw"
    elif has_wiki:
        answer_source_mode = "wiki"
    elif has_raw:
        answer_source_mode = "raw"
    else:
        answer_source_mode = "none"

    return {
        "retrieval_completed": retrieval_completed,
        "answer_eligible": answer_eligible,
        "service_degraded": service_degraded,
        "response_mode": response_mode,
        "answer_source_mode": answer_source_mode,
        "model_status": model_status,
        "embedding_status": embedding_status,
        "reranker_status": reranker_status,
        "degraded_reasons": _stable_reasons(reasons),
        "answer": llm_answer if (llm_called and llm_ok) else None,
        "wiki_results": build_wiki_view(db, wiki_hits),
        "raw_results": build_raw_view(db, raw_hits),
        "knowledge_missing": knowledge_missing,
        "debt_created": False,
    }
