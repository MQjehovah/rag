"""Phase E/F 降级问答（新端点，Phase G 起成为默认 Chat 的共享服务实现）。

POST /api/rag-chat：scope 校验 → Wiki 优先检索 → 有生成资格才调 LLM → 失败降级 retrieval_only。
POST /api/chat    ：复用 run_chat_query，与 /api/rag-chat 状态语义完全一致（Phase G 默认切换）。

scope（V4 Phase F 封板）：
- 检索前先校验 requested scope；非法/越权立即 403（orchestrator/LLM/债务调用 0 次）。
- 多组用户/admin 未指定 scope → 明确 scope_required（不静默猜测，不检索）。
- 选定 scope 约束本次 Wiki/Raw/Community/LLM 上下文与债务归属，不返回 scope 外结果。

J-4：普通提问、insufficient 响应本身不创建/累计 KnowledgeDebt。知识缺口只能来自
「两名不同用户有效差评达到阈值」或「用户主动点击“我仍需要这个答案”」。旧 Phase F
的 `_maybe_record_debt`/`_auto_scope_id` 写入路径已删除。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.retrieval.debt_service import validated_scope
from app.core.retrieval.degradation import (
    LLMAnswerResult,
    build_rag_response,
    call_llm_answer,
    should_generate_answer,
)
from app.core.retrieval.feedback_service import create_answer_snapshot
from app.core.retrieval.orchestrator import build_default_retrieval_orchestrator

router = APIRouter(prefix="/api/rag-chat", tags=["V4 问答（降级）"])

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是企业知识库助手。只依据给定参考资料回答，不编造。"
    "如果资料不足以回答，请诚实说明。使用中文回答。"
)


class RagChatRequest(BaseModel):
    query: str
    scope_id: str | None = None


def resolve_scope(current_user: dict, requested_scope_id: str | None) -> str | None:
    """检索前解析并校验 scope（J-1 自动权限）。

    - 未提供 scope_id → None（自动全可见范围：company + 用户所属全部业务组）。
    - 提供 scope_id → 校验越权（伪造旧 scope_id 不能扩大权限），非法 403。
    - 不再要求多组/admin 显式选择权限域（scope_required 已废弃）。
    """
    if not requested_scope_id:
        return None
    scope_id, err = validated_scope(current_user, requested_scope_id)
    if not scope_id:
        raise HTTPException(status_code=403, detail="无权访问该权限域")
    return scope_id


# 兼容别名：历史代码/测试引用 _resolve_scope。
_resolve_scope = resolve_scope


def _scoped_user(current_user: dict, scope_id: str | None) -> dict:
    """构造检索用 current_user。

    - scope_id 为 None → 自动全范围（不注入 _scope_override，检索全部可见内容）。
    - scope_id 非空 → 注入 _scope_override，约束为单权限域（兼容旧调用）。
    """
    if scope_id is None:
        return current_user
    return {**current_user, "_scope_override": scope_id}


def _finalize_chat_response(
    db: Session,
    current_user: dict,
    query: str,
    response: dict,
) -> dict:
    """J-4 最终响应：持久化 answer_id 快照，并移除普通响应中的债务字段。

    - answer_id 为服务端 UUID（不可预测、稳定）；
    - 只有 AnswerSnapshot 成功持久化后才返回 answer_id（快照失败不返回虚假 ID，
      前端因此不展示反馈入口）；
    - 回答本身仍正常返回（反馈附属功能失败不丢失回答）；
    - 普通响应不再暴露 debt_created / debt_recording_status / debt_id / debt_reopened。
    """
    for key in ("debt_created", "debt_recording_status", "debt_id", "debt_reopened"):
        response.pop(key, None)

    answer_id = str(uuid.uuid4())
    try:
        create_answer_snapshot(
            db,
            answer_id=answer_id,
            user_id=current_user.get("id", ""),
            original_query=query,
            response=response,
            current_user=current_user,
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— 快照失败不阻断回答，但不返回虚假 answer_id
        logger.warning("answer snapshot persist failed: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return response

    response["answer_id"] = answer_id
    return response


async def run_chat_query(
    db: Session,
    current_user: dict,
    query: str,
    scope_id: str | None,
) -> dict:
    """共享聊天服务（J-1 自动权限）：scope 校验 → 检索 → 门禁 → LLM → 降级。

    /api/chat 与 /api/rag-chat 共用本实现，保证状态语义完全一致。
    - scope_id 缺省 → 自动全可见范围（company + 用户全部业务组）。
    - scope_id 提供 → 校验越权，伪造不扩大权限。
    """
    # 1. 检索前校验 scope（越权在 orchestrator 构建前终止；缺省走自动全范围）
    scope_id = resolve_scope(current_user, scope_id)
    scoped_user = _scoped_user(current_user, scope_id)

    # 2. 检索（Wiki → Raw → Community，受 scope 约束，复用 Phase D 封板语义）
    orchestrator = build_default_retrieval_orchestrator(db)
    try:
        outcome = await orchestrator.retrieve_async(query, scoped_user)
    except Exception as exc:  # noqa: BLE001 —— 检索异常也不能 500
        logger.exception("retrieval failed during chat")
        response = build_rag_response(
            db,
            None,
            llm_answer=None,
            llm_called=False,
            llm_ok=False,
            retrieval_failed=True,
        )
        return _finalize_chat_response(db, current_user, query, response)

    # 3. LLM 生成门禁：只有 answer_eligible 才调用
    if not should_generate_answer(outcome):
        response = build_rag_response(
            db,
            outcome,
            llm_answer=None,
            llm_called=False,
            llm_ok=False,
        )
        return _finalize_chat_response(db, current_user, query, response)

    # 4. 组装回答上下文（仅 scope 内命中）
    wiki_hits = outcome.wiki_results.hits if outcome.wiki_results else []
    raw_hits = outcome.raw_results.hits if outcome.raw_results else []
    context_parts = []
    for h in wiki_hits:
        # V4 Phase I：仅当命中具体版本块时才标注版本；ambiguous/不存在版本
        # 时 version_label 为空，不标任何版本（避免误标 latest 或猜测）。
        version_note = ""
        if getattr(h, "version_label", None):
            version_note = f"（版本：{h.version_label}）"
        context_parts.append(f"【Wiki·{h.title}{version_note}】\n{h.content}")
    for h in raw_hits:
        context_parts.append(f"【{h.page_title}】\n{h.content}")
    context = "\n\n".join(context_parts) if context_parts else "未找到相关授权知识内容。"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"参考资料：\n{context}\n\n用户问题：{query}"},
    ]

    llm_result: LLMAnswerResult = await call_llm_answer(messages)

    response = build_rag_response(
        db,
        outcome,
        llm_answer=llm_result.answer if llm_result.ok else None,
        llm_called=True,
        llm_ok=llm_result.ok,
        llm_degraded_reason=llm_result.degraded_reason if not llm_result.ok else "",
    )
    return _finalize_chat_response(db, current_user, query, response)


@router.post("")
async def rag_chat(
    request: RagChatRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """降级问答（兼容别名）：与默认 Chat 共用同一服务实现。"""
    return await run_chat_query(db, current_user, request.query, request.scope_id)
