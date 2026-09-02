"""V4 Phase J-4：回答反馈、知识缺口与访问申请服务（最终数据一致性封板）。

职责：
- answer 快照（answer_id 持久化、不可猜、只能原用户使用；保存产生时的权限快照）。
- 回答反馈保存（同用户同 answer 仅一条；reason 仅 incorrect/incomplete/null；
  反馈代表「当前反馈」，撤销/换原因用精确 feedback_cluster_key 撤销）。
- 「我仍需要这个答案」（幂等、仅满足条件时进入后台分类、degraded 可重试）。
- 后台全库分类门禁（Wiki 优先；Raw 真实 degraded 才返回 degraded；异常安全降级）。
- 知识缺口 / 访问申请创建与原子累计、相似合并、并发幂等、自动解决。

事务边界（本次封板核心）：
- 反馈达到阈值后，在覆盖相似 cluster 的锁内：重新统计 distinct → 安全分类 →
  同一事务内写 KnowledgeDebt/AccessRequest + 更新 classification/classified_at →
  一次 commit。任一步失败全部 rollback，classified_at 保持 NULL。
- record_j4_debt / record_access_request 支持 commit=False，禁止内部提前 commit。

权限上下文：分类使用 answer_id 快照中的 business_groups + is_admin（回答产生时），
而非提交反馈时的 current_user。全库充分时按 cluster 内每个用户的权限组集合分别
判断，无权限的不同组集合分别创建访问申请。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import access_control
from app.core.retrieval.debt_service import (
    SIMILARITY_THRESHOLD,
    _similarity,
    normalize_query,
)
from app.core.retrieval.raw_retriever import RawDocumentRetriever
from app.core.retrieval.raw_sufficiency_judge import judge_raw_sufficiency
from app.core.retrieval.sufficiency_judge import judge_wiki_sufficiency
from app.core.retrieval.wiki_retriever import meaningful_tokenize, retrieve_wiki
from app.models.database import (
    AccessRequest,
    AccessRequestUser,
    AnswerFeedback,
    AnswerNeeded,
    AnswerSnapshot,
    FeedbackCluster,
    FeedbackClusterUser,
    KnowledgeDebt,
    KnowledgeDebtUser,
    Page,
    WikiPage,
    WikiVersionSource,
)

logger = logging.getLogger(__name__)

# 阈值集中定义
FEEDBACK_DEBT_DISTINCT_USER_THRESHOLD = 2

NOTE_MAX_LENGTH = 2000

# 有界处理：相似扫描 / 重验证每批上限（keyset 续处理，不永久漏检）
MAX_SIMILAR_SCAN = 100
MAX_REVALIDATE_BATCH = 100

# 反馈原因（前端/后端只接受 incorrect/incomplete；None = 未选原因 → 聚类计为 unspecified）
REASON_INCORRECT = "incorrect"
REASON_INCOMPLETE = "incomplete"
REASON_UNSPECIFIED = "unspecified"

# 债务来源（区分 query_missing 与三种反馈原因）
DEBT_REASON_QUERY_MISSING = "query_missing"
DEBT_REASON_FEEDBACK_INCORRECT = "feedback_incorrect"
DEBT_REASON_FEEDBACK_INCOMPLETE = "feedback_incomplete"
DEBT_REASON_FEEDBACK_UNSPECIFIED = "feedback_unspecified"

# 检索缺失原因（区分「全库无答案」与「全库只有不完整内容」）
RETRIEVAL_REASON_MISSING = "missing_knowledge"
RETRIEVAL_REASON_INCOMPLETE = "incomplete_knowledge"

# 分类门禁安全枚举
CLASS_VISIBLE_SUFFICIENT = "visible_sufficient"
CLASS_HIDDEN_SUFFICIENT = "hidden_sufficient"
CLASS_GLOBALLY_INCOMPLETE = "globally_incomplete"
CLASS_GLOBALLY_MISSING = "globally_missing"
CLASS_DEGRADED = "degraded"

# 全库检查用的管理员视图（管理员可见全部且排除失效来源/archived Wiki）。
_ADMIN_USER = {"groups": ["__local_admin__"]}

# 分类门禁的 Raw 检索使用纯 BM25 确定性基线，不主动依赖 Embedding/Reranker；
# 这两个 reason 是「未使用」而非「故障」，不构成分类降级。其余 reason（如
# embedding_dimension_mismatch / no_indexed_vectors / reranker_timeout /
# reranker_invalid_response）才是真实故障，必须返回 degraded。
_NON_FAILURE_DEGRADED = frozenset({"embedding_unavailable", "reranker_unavailable"})

_REASON_TO_DEBT_REASON = {
    REASON_INCORRECT: DEBT_REASON_FEEDBACK_INCORRECT,
    REASON_INCOMPLETE: DEBT_REASON_FEEDBACK_INCOMPLETE,
    REASON_UNSPECIFIED: DEBT_REASON_FEEDBACK_UNSPECIFIED,
}


class _ClassificationDegraded(Exception):
    """分类过程中任一用户权限视图 degraded，整组 fail closed（不写记录、不标记）。"""


def build_feedback_cluster_key(normalized_query: str, version_label: str | None, reason: str) -> str:
    """负面反馈聚类键（精确规范化查询；相似问题合并由相似匹配完成）。"""
    return hashlib.sha256(
        f"j4fb:{version_label or ''}:{normalized_query}:{reason}".encode("utf-8")
    ).hexdigest()


def build_debt_cluster_key(debt_reason: str, version_label: str | None, normalized_query: str) -> str:
    """J-4 知识缺口全局合并键（不按 scope 拆分；相似合并由相似匹配完成）。"""
    return hashlib.sha256(
        f"j4debt:{debt_reason}:{version_label or ''}:{normalized_query}".encode("utf-8")
    ).hexdigest()


def build_access_request_cluster_key(
    groups: list[str],
    normalized_query: str,
    version_label: str | None,
    target_notebook_id: str | None,
    target_wiki_page_id: str | None,
) -> str:
    """访问申请合并键：组集合 + normalized_query + version + 目标（两个目标都参与，严格隔离）。"""
    target = f"{target_notebook_id or ''}|{target_wiki_page_id or ''}"
    return hashlib.sha256(
        f"j4ar:{','.join(groups)}:{version_label or ''}:{normalized_query}:{target}".encode("utf-8")
    ).hexdigest()


def version_from_query(query: str) -> tuple[str | None, str | None]:
    """从查询确定性识别版本三态。返回 (version_label, version_status)。"""
    from app.core.retrieval.wiki_retriever import detect_query_version
    qv = detect_query_version(query)
    return qv.version_label, qv.state


# ---------------------------------------------------------------------------
# 回答快照（含权限上下文）
# ---------------------------------------------------------------------------

def create_answer_snapshot(
    db: Session,
    *,
    answer_id: str,
    user_id: str,
    original_query: str,
    response: dict,
    current_user: dict | None = None,
) -> AnswerSnapshot:
    """持久化最小回答快照（分类所需字段 + 产生时的权限快照，不复制完整 context）。"""
    norm = normalize_query(original_query)
    version_label, version_status = version_from_query(original_query)

    business_groups: list[str] = []
    is_admin = False
    if current_user:
        business_groups = sorted(access_control.business_groups(current_user))
        is_admin = access_control.is_admin(current_user)

    snapshot = AnswerSnapshot(
        id=answer_id,
        user_id=user_id,
        original_query=original_query,
        normalized_query=norm,
        version_label=version_label,
        version_status=version_status,
        response_mode=response.get("response_mode"),
        retrieval_completed=bool(response.get("retrieval_completed")),
        answer_eligible=bool(response.get("answer_eligible")),
        service_degraded=bool(response.get("service_degraded")),
        has_visible_sufficient_evidence=bool(response.get("answer_eligible")),
        business_groups=json.dumps(business_groups, ensure_ascii=False),
        is_admin=is_admin,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _get_snapshot(db: Session, answer_id: str, user_id: str) -> AnswerSnapshot | None:
    """按 answer_id 取快照，且必须属于当前用户（其他用户不可见/不可用）。"""
    snap = db.get(AnswerSnapshot, answer_id)
    if snap is None or snap.user_id != user_id:
        return None
    return snap


def _snapshot_permission_view(snapshot: AnswerSnapshot) -> dict:
    """从快照还原权限视图（按回答产生时上下文），供分类使用。"""
    groups: list[str] = []
    try:
        groups = sorted(set(json.loads(snapshot.business_groups or "[]")))
    except (TypeError, ValueError):
        groups = []
    if snapshot.is_admin:
        # 还原管理员身份：注入本地管理员组使 access_control.is_admin 判定正确。
        groups = list(groups) + ["__local_admin__"]
    return {"id": snapshot.user_id, "groups": groups}


def _snapshot_business_groups(snapshot: AnswerSnapshot) -> list[str]:
    try:
        return sorted(set(json.loads(snapshot.business_groups or "[]")))
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# 全库分类门禁（异常安全降级）
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    outcome: str
    target_notebook_id: str | None = None
    target_wiki_page_id: str | None = None


@dataclass
class ScopeAssessment:
    sufficient: bool
    has_hits: bool
    degraded: bool
    target_notebook_id: str | None = None
    target_wiki_page_id: str | None = None
    source_layer: str | None = None  # "wiki" / "raw" / None


def _notebook_for_wiki(db: Session, wiki_id: str) -> str | None:
    """通过 Wiki 当前有效来源（wiki_version_sources → Page）定位 Notebook。"""
    row = (
        db.query(WikiVersionSource.page_id)
        .filter(WikiVersionSource.wiki_page_id == wiki_id)
        .first()
    )
    if row and row[0]:
        page = db.get(Page, row[0])
        if page is not None and page.notebook_id:
            return page.notebook_id
    wiki = db.get(WikiPage, wiki_id)
    if wiki is not None and wiki.source_page_ids:
        try:
            page_ids = json.loads(wiki.source_page_ids)
            for pid in page_ids:
                page = db.get(Page, pid)
                if page is not None and page.notebook_id:
                    return page.notebook_id
        except (TypeError, ValueError):
            pass
    return None


def _wiki_assess(db: Session, current_user: dict, question: str) -> ScopeAssessment:
    """评估 Wiki 层。检索/充分性判断/来源定位任一异常 → degraded。"""
    try:
        result = retrieve_wiki(db, current_user, question)
    except Exception:  # noqa: BLE001
        return ScopeAssessment(sufficient=False, has_hits=False, degraded=True)

    if not result.hits:
        return ScopeAssessment(sufficient=False, has_hits=False, degraded=False)

    try:
        verdict = judge_wiki_sufficiency(result, question)
    except Exception:  # noqa: BLE001
        return ScopeAssessment(sufficient=False, has_hits=True, degraded=True)

    if verdict.sufficient:
        wiki_id = result.hits[0].wiki_page_id
        try:
            nb_id = _notebook_for_wiki(db, wiki_id)
        except Exception:  # noqa: BLE001
            return ScopeAssessment(sufficient=False, has_hits=True, degraded=True)
        return ScopeAssessment(
            sufficient=True,
            has_hits=True,
            degraded=False,
            target_notebook_id=nb_id,
            target_wiki_page_id=wiki_id,
            source_layer="wiki",
        )
    return ScopeAssessment(sufficient=False, has_hits=True, degraded=False, source_layer="wiki")


def _raw_assess(db: Session, current_user: dict, question: str) -> ScopeAssessment:
    """评估 Raw 层（纯 BM25 基线）。检索/充分性判断异常或真实 degraded → degraded。"""
    try:
        raw = RawDocumentRetriever(db).retrieve(db, question, current_user)
    except Exception:  # noqa: BLE001
        return ScopeAssessment(sufficient=False, has_hits=False, degraded=True)

    real_degraded = [d for d in raw.degraded if d not in _NON_FAILURE_DEGRADED]
    if real_degraded:
        return ScopeAssessment(sufficient=False, has_hits=bool(raw.hits), degraded=True)

    if not raw.hits:
        return ScopeAssessment(sufficient=False, has_hits=False, degraded=False)

    try:
        verdict = judge_raw_sufficiency(raw.hits, question)
    except Exception:  # noqa: BLE001
        return ScopeAssessment(sufficient=False, has_hits=True, degraded=True)

    if verdict.sufficient:
        supporting = {h.chunk_id for h in raw.hits if h.chunk_id in verdict.supporting_chunk_ids}
        target = next((h for h in raw.hits if h.chunk_id in supporting), raw.hits[0])
        return ScopeAssessment(
            sufficient=True,
            has_hits=True,
            degraded=False,
            target_notebook_id=target.notebook_id,
            source_layer="raw",
        )
    return ScopeAssessment(sufficient=False, has_hits=True, degraded=False, source_layer="raw")


def _assess_scope(db: Session, current_user: dict, question: str) -> ScopeAssessment:
    """综合评估一个权限视图：Wiki 优先，Wiki 充分即结束，不调 Raw；否则再查 Raw。"""
    wiki = _wiki_assess(db, current_user, question)
    if wiki.degraded or wiki.sufficient:
        return wiki

    raw = _raw_assess(db, current_user, question)
    if raw.degraded or raw.sufficient:
        return raw

    return ScopeAssessment(
        sufficient=False,
        has_hits=wiki.has_hits or raw.has_hits,
        degraded=False,
        source_layer=None,
    )


def classify_full_library(db: Session, question: str, current_user: dict) -> ClassificationResult:
    """后台全库分类门禁（只返回安全枚举，不向普通用户返回命中内容）。"""
    visible = _assess_scope(db, current_user, question)
    if visible.degraded:
        return ClassificationResult(CLASS_DEGRADED)
    if visible.sufficient:
        return ClassificationResult(CLASS_VISIBLE_SUFFICIENT)

    full = _assess_scope(db, _ADMIN_USER, question)
    if full.degraded:
        return ClassificationResult(CLASS_DEGRADED)

    if full.sufficient:
        return ClassificationResult(
            CLASS_HIDDEN_SUFFICIENT,
            target_notebook_id=full.target_notebook_id,
            target_wiki_page_id=full.target_wiki_page_id,
        )
    if full.has_hits:
        return ClassificationResult(CLASS_GLOBALLY_INCOMPLETE)
    return ClassificationResult(CLASS_GLOBALLY_MISSING)


# ---------------------------------------------------------------------------
# 锁与原子累计
# ---------------------------------------------------------------------------

def _acquire_cluster_lock(db: Session, lock_key: str) -> None:
    from sqlalchemy import text
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:s))"), {"s": lock_key})
    else:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))


def _add_debt_user(db: Session, debt_id: str, user_id: str) -> None:
    if not user_id:
        return
    try:
        with db.begin_nested():
            db.add(KnowledgeDebtUser(id=str(uuid.uuid4()), debt_id=debt_id, user_id=user_id))
            db.flush()
            db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt_id).update(
                {KnowledgeDebt.affected_user_count: func.coalesce(KnowledgeDebt.affected_user_count, 0) + 1},
                synchronize_session=False,
            )
    except IntegrityError:
        pass


def _add_request_user(db: Session, request_id: str, user_id: str) -> None:
    if not user_id:
        return
    try:
        with db.begin_nested():
            db.add(AccessRequestUser(id=str(uuid.uuid4()), request_id=request_id, user_id=user_id))
            db.flush()
            db.query(AccessRequest).filter(AccessRequest.id == request_id).update(
                {AccessRequest.affected_user_count: func.coalesce(AccessRequest.affected_user_count, 0) + 1},
                synchronize_session=False,
            )
    except IntegrityError:
        pass


# ---------------------------------------------------------------------------
# 知识缺口 / 访问申请创建（commit=False 支持；禁止内部提前 commit）
# ---------------------------------------------------------------------------

def _find_similar_global_debt(
    db: Session,
    debt_reason: str,
    version_label: str | None,
    norm: str,
) -> KnowledgeDebt | None:
    """同 debt_reason + 同 version 下，相似 open 全局债务（有界 + 稳定排序）。"""
    q = db.query(KnowledgeDebt).filter(
        KnowledgeDebt.scope_id.is_(None),
        KnowledgeDebt.status == "open",
        KnowledgeDebt.debt_reason == debt_reason,
    )
    if version_label is None:
        q = q.filter(KnowledgeDebt.version_label.is_(None))
    else:
        q = q.filter(KnowledgeDebt.version_label == version_label)
    candidates = (
        q.order_by(KnowledgeDebt.last_seen_at.desc(), KnowledgeDebt.id)
        .limit(MAX_SIMILAR_SCAN)
        .all()
    )
    scored = []
    for c in candidates:
        if not c.normalized_query:
            continue
        sim = _similarity(c.normalized_query, norm)
        if sim >= SIMILARITY_THRESHOLD:
            scored.append((sim, c.cluster_key or c.id, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2] if scored else None


def record_j4_debt(
    db: Session,
    *,
    debt_reason: str,
    version_label: str | None,
    version_status: str | None,
    original_query: str,
    user_ids: list[str],
    retrieval_reason: str,
    commit: bool = True,
) -> str | None:
    """创建/累计 J-4 知识缺口（全局合并 + 相似合并，不按 scope 拆分）。返回 debt_id。

    - occurrence_count 每次调用原子 +1（coalesce 兼容 NULL）；
    - affected_user_count 按 user_ids 去重累加；
    - commit=False 时由调用方管理事务，禁止内部 commit。
    """
    norm = normalize_query(original_query)
    if not norm:
        return None
    cluster_key = build_debt_cluster_key(debt_reason, version_label, norm)
    now = datetime.now()

    def _write() -> str | None:
        debt = (
            db.query(KnowledgeDebt)
            .filter(KnowledgeDebt.cluster_key == cluster_key)
            .first()
        )
        if debt is None:
            debt = _find_similar_global_debt(db, debt_reason, version_label, norm)

        if debt is not None:
            if debt.status != "open":
                db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt.id).update(
                    {"status": "open", "resolved_at": None, "last_seen_at": now},
                    synchronize_session=False,
                )
            db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt.id).update(
                {
                    KnowledgeDebt.occurrence_count: func.coalesce(KnowledgeDebt.occurrence_count, 0) + 1,
                    KnowledgeDebt.last_seen_at: now,
                },
                synchronize_session=False,
            )
            for uid in dict.fromkeys(user_ids):
                _add_debt_user(db, debt.id, uid)
            return debt.id

        debt = KnowledgeDebt(
            debt_type="missing_knowledge",
            description=f"知识缺失：{original_query}",
            related_question=original_query,
            score=1.0,
            status="open",
            title=original_query,
            root_cause=retrieval_reason,
            original_query=original_query,
            normalized_query=norm,
            cluster_key=cluster_key,
            scope_id=None,  # 全局合并，不按 scope 拆分
            retrieval_reason=retrieval_reason,
            debt_reason=debt_reason,
            version_label=version_label,
            version_status=version_status,
            occurrence_count=1,
            affected_user_count=0,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(debt)
        db.flush()
        for uid in dict.fromkeys(user_ids):
            _add_debt_user(db, debt.id, uid)
        return debt.id

    if commit:
        _acquire_cluster_lock(db, f"j4debt:{debt_reason}:{version_label or ''}")
        for _attempt in range(3):
            try:
                debt_id = _write()
                db.commit()
                return debt_id
            except IntegrityError:
                db.rollback()
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_j4_debt failed: %s", exc)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                return None
        return None
    # commit=False：调用方已持有锁并管理事务，异常向上抛。
    return _write()


def _find_similar_access_request(
    db: Session,
    groups: list[str],
    normalized_query: str,
    version_label: str | None,
    target_notebook_id: str | None,
    target_wiki_page_id: str | None,
) -> AccessRequest | None:
    """同组集合 + 同 version + 同目标（严格）下，相似 open 访问申请（有界 + 稳定排序）。"""
    groups_json = json.dumps(sorted(set(groups)), ensure_ascii=False)
    q = db.query(AccessRequest).filter(
        AccessRequest.status == "open",
        AccessRequest.requesting_groups == groups_json,
        AccessRequest.target_notebook_id == target_notebook_id,
        AccessRequest.target_wiki_page_id == target_wiki_page_id,
    )
    if version_label is None:
        q = q.filter(AccessRequest.version_label.is_(None))
    else:
        q = q.filter(AccessRequest.version_label == version_label)
    candidates = (
        q.order_by(AccessRequest.last_seen_at.desc(), AccessRequest.id)
        .limit(MAX_SIMILAR_SCAN)
        .all()
    )
    scored = []
    for c in candidates:
        if not c.normalized_query:
            continue
        sim = _similarity(c.normalized_query, normalized_query)
        if sim >= SIMILARITY_THRESHOLD:
            scored.append((sim, c.cluster_key or c.id, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2] if scored else None


def record_access_request(
    db: Session,
    *,
    groups: list[str],
    normalized_query: str,
    original_query: str,
    version_label: str | None,
    version_status: str | None,
    target_notebook_id: str | None,
    target_wiki_page_id: str | None,
    user_ids: list[str],
    commit: bool = True,
) -> str | None:
    """创建/累计访问申请（按组集合 + query + version + 目标严格合并，相似合并）。"""
    groups_sorted = sorted(set(groups))
    cluster_key = build_access_request_cluster_key(
        groups_sorted, normalized_query, version_label, target_notebook_id, target_wiki_page_id
    )
    now = datetime.now()

    def _write() -> str | None:
        req = (
            db.query(AccessRequest)
            .filter(AccessRequest.cluster_key == cluster_key)
            .first()
        )
        if req is None:
            req = _find_similar_access_request(
                db, groups_sorted, normalized_query, version_label, target_notebook_id, target_wiki_page_id
            )

        if req is not None:
            if req.status != "open":
                db.query(AccessRequest).filter(AccessRequest.id == req.id).update(
                    {"status": "open", "resolved_at": None, "last_seen_at": now},
                    synchronize_session=False,
                )
            db.query(AccessRequest).filter(AccessRequest.id == req.id).update(
                {
                    AccessRequest.occurrence_count: func.coalesce(AccessRequest.occurrence_count, 0) + 1,
                    AccessRequest.last_seen_at: now,
                },
                synchronize_session=False,
            )
            for uid in dict.fromkeys(user_ids):
                _add_request_user(db, req.id, uid)
            return req.id

        req = AccessRequest(
            cluster_key=cluster_key,
            normalized_query=normalized_query,
            original_query=original_query,
            version_label=version_label,
            version_status=version_status,
            requesting_groups=json.dumps(groups_sorted, ensure_ascii=False),
            target_notebook_id=target_notebook_id,
            target_wiki_page_id=target_wiki_page_id,
            occurrence_count=1,
            affected_user_count=0,
            status="open",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(req)
        db.flush()
        for uid in dict.fromkeys(user_ids):
            _add_request_user(db, req.id, uid)
        return req.id

    if commit:
        _acquire_cluster_lock(db, f"j4ar:{target_notebook_id or ''}:{version_label or ''}")
        for _attempt in range(3):
            try:
                req_id = _write()
                db.commit()
                return req_id
            except IntegrityError:
                db.rollback()
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_access_request failed: %s", exc)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                return None
        return None
    # commit=False：调用方已持有锁并管理事务，异常向上抛。
    return _write()


# ---------------------------------------------------------------------------
# 反馈保存（当前反馈语义：精确 cluster 关联 + 撤销/换原因）
# ---------------------------------------------------------------------------

def _find_similar_feedback_cluster(
    db: Session,
    version_label: str | None,
    normalized_query: str,
    reason: str,
) -> FeedbackCluster | None:
    """同 version + 同 reason 下，相似 cluster（有界 + 稳定排序）。"""
    q = db.query(FeedbackCluster).filter(FeedbackCluster.reason == reason)
    if version_label is None:
        q = q.filter(FeedbackCluster.version_label.is_(None))
    else:
        q = q.filter(FeedbackCluster.version_label == version_label)
    candidates = (
        q.order_by(FeedbackCluster.last_seen_at.desc(), FeedbackCluster.cluster_key)
        .limit(MAX_SIMILAR_SCAN)
        .all()
    )
    scored = []
    for c in candidates:
        if not c.normalized_query:
            continue
        sim = _similarity(c.normalized_query, normalized_query)
        if sim >= SIMILARITY_THRESHOLD:
            scored.append((sim, c.cluster_key, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2] if scored else None


def _resolve_or_create_cluster(db: Session, snapshot: AnswerSnapshot, user_id: str, reason: str) -> str:
    """在当前事务内把用户加入负面 cluster（相似合并），返回实际 cluster_key。

    不锁、不 commit（调用方已持有版本级锁并管理事务）。
    """
    cluster = _find_similar_feedback_cluster(db, snapshot.version_label, snapshot.normalized_query, reason)
    if cluster is None:
        exact_key = build_feedback_cluster_key(snapshot.normalized_query, snapshot.version_label, reason)
        cluster = db.get(FeedbackCluster, exact_key)
    if cluster is None:
        now = datetime.now()
        cluster = FeedbackCluster(
            cluster_key=build_feedback_cluster_key(snapshot.normalized_query, snapshot.version_label, reason),
            normalized_query=snapshot.normalized_query,
            version_label=snapshot.version_label,
            version_status=snapshot.version_status,
            reason=reason,
            created_at=now,
            last_seen_at=now,
        )
        db.add(cluster)
        db.flush()
    else:
        cluster.last_seen_at = datetime.now()

    try:
        with db.begin_nested():
            db.add(FeedbackClusterUser(id=str(uuid.uuid4()), cluster_key=cluster.cluster_key, user_id=user_id))
            db.flush()
    except IntegrityError:
        pass
    return cluster.cluster_key


def _user_has_other_negative_in_cluster(db: Session, cluster_key: str, user_id: str, exclude_answer_id: str) -> bool:
    """该用户是否还有另一条当前负面反馈指向同一 cluster（排除当前这条）。"""
    if not exclude_answer_id:
        return False
    count = (
        db.query(AnswerFeedback)
        .filter(
            AnswerFeedback.feedback_cluster_key == cluster_key,
            AnswerFeedback.user_id == user_id,
            AnswerFeedback.helpful.is_(False),
            AnswerFeedback.id != exclude_answer_id,
        )
        .count()
    )
    return count > 0


def _cluster_current_negative_feedbacks(db: Session, cluster_key: str) -> list[tuple[str, AnswerSnapshot]]:
    """返回该 cluster 下当前负面反馈的 (user_id, snapshot)，按 user 去重。

    同一 user 多条负面反馈时，选择最近更新的一条（AnswerFeedback.updated_at DESC，
    id 稳定 tie-break），保证分类使用该用户最新权限快照。
    """
    rows = (
        db.query(AnswerFeedback, AnswerSnapshot)
        .join(AnswerSnapshot, AnswerSnapshot.id == AnswerFeedback.answer_id)
        .filter(
            AnswerFeedback.feedback_cluster_key == cluster_key,
            AnswerFeedback.helpful.is_(False),
        )
        .order_by(AnswerFeedback.updated_at.desc(), AnswerFeedback.id.desc())
        .all()
    )
    seen: dict[str, AnswerSnapshot] = {}
    for fb, snap in rows:
        if fb.user_id not in seen:
            seen[fb.user_id] = snap
    return [(uid, snap) for uid, snap in seen.items()]


def _apply_classification_for_cluster(
    db: Session,
    *,
    cluster: FeedbackCluster,
    representative: str,
    full: ScopeAssessment,
    debt_reason: str,
    feedbacks: list[tuple[str, AnswerSnapshot]],
) -> str:
    """据全库充分性在同事务内写知识缺口或访问申请（commit=False），返回 classification。

    - 全库 missing/incomplete → 全局建一次知识缺口；
    - 全库充分 → 对每个负面用户按快照权限判断 visible，无权限的组集合分别建访问申请。
    """
    user_ids = [uid for uid, _ in feedbacks]

    if not full.sufficient:
        retrieval_reason = RETRIEVAL_REASON_INCOMPLETE if full.has_hits else RETRIEVAL_REASON_MISSING
        record_j4_debt(
            db,
            debt_reason=debt_reason,
            version_label=cluster.version_label,
            version_status=cluster.version_status,
            original_query=representative,
            user_ids=user_ids,
            retrieval_reason=retrieval_reason,
            commit=False,
        )
        return CLASS_GLOBALLY_INCOMPLETE if full.has_hits else CLASS_GLOBALLY_MISSING

    # 全库充分：两阶段处理。
    # 第一阶段：先评估所有需要检查的用户权限视图，任一 degraded → 整组 fail closed
    # （抛异常让调用方 rollback，不写任何 AccessRequest、不标记 classified_at）。
    assessments: list[tuple[str, AnswerSnapshot, ScopeAssessment]] = []
    for uid, snapshot in feedbacks:
        if snapshot.is_admin:
            continue  # 管理员不形成访问申请
        view = _snapshot_permission_view(snapshot)
        visible = _assess_scope(db, view, representative)
        if visible.degraded:
            raise _ClassificationDegraded()
        assessments.append((uid, snapshot, visible))

    # 第二阶段：仅 visible insufficient 的用户按权限快照创建访问申请。
    created_any = False
    for uid, snapshot, visible in assessments:
        if visible.sufficient:
            continue  # 已可见只保留反馈
        groups = _snapshot_business_groups(snapshot)
        record_access_request(
            db,
            groups=groups,
            normalized_query=snapshot.normalized_query,
            original_query=snapshot.original_query,
            version_label=snapshot.version_label,
            version_status=snapshot.version_status,
            target_notebook_id=full.target_notebook_id,
            target_wiki_page_id=full.target_wiki_page_id,
            user_ids=[uid],
            commit=False,
        )
        created_any = True
    return CLASS_HIDDEN_SUFFICIENT if created_any else CLASS_VISIBLE_SUFFICIENT


def _classify_feedback_cluster(db: Session, cluster_key: str) -> None:
    """达到阈值后只触发一次分类（锁内统计 → 分类 → 同事务写记录 + 标记）。

    与 save_answer_feedback 使用同一 version 级锁命名空间（j4fb:{version}），
    保证反馈新增/撤销/换原因与分类互斥。任一步失败 rollback，classified_at
    保持 NULL，后续有效反馈可重试。
    """
    cluster = db.get(FeedbackCluster, cluster_key)
    if cluster is None or cluster.classified_at is not None:
        return

    lock_key = f"j4fb:{cluster.version_label or ''}"
    _acquire_cluster_lock(db, lock_key)
    try:
        # 锁内重读当前 cluster 状态与反馈（点赞撤销/原因切换可能先于本锁完成）。
        cluster = db.get(FeedbackCluster, cluster_key)
        if cluster is None or cluster.classified_at is not None:
            db.rollback()
            return

        feedbacks = _cluster_current_negative_feedbacks(db, cluster_key)
        if len(feedbacks) < FEEDBACK_DEBT_DISTINCT_USER_THRESHOLD:
            db.rollback()
            return

        representative = feedbacks[0][1].original_query or cluster.normalized_query or ""
        if not representative:
            db.rollback()
            return

        full = _assess_scope(db, _ADMIN_USER, representative)
        if full.degraded:
            db.rollback()
            return  # 不标记，可重试

        debt_reason = _REASON_TO_DEBT_REASON.get(cluster.reason, DEBT_REASON_FEEDBACK_UNSPECIFIED)
        classification = _apply_classification_for_cluster(
            db,
            cluster=cluster,
            representative=representative,
            full=full,
            debt_reason=debt_reason,
            feedbacks=feedbacks,
        )

        # 同事务：标记 classified_at + classification，一次 commit。
        cluster.classified_at = datetime.now()
        cluster.classification = classification
        db.commit()
    except Exception:  # noqa: BLE001 —— 任一步失败全部 rollback
        logger.warning("classify feedback cluster failed: %s", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def save_answer_feedback(
    db: Session,
    answer_id: str,
    current_user: dict,
    helpful: bool,
    reason: str | None,
    note: str | None,
) -> dict:
    """保存一条回答反馈（upsert，代表当前反馈）。返回安全结果（不泄露后台分类）。"""
    user_id = current_user.get("id", "")
    snapshot = _get_snapshot(db, answer_id, user_id)
    if snapshot is None:
        return {"ok": False, "error": "not_found"}

    if snapshot.response_mode != "answer":
        return {"ok": False, "error": "not_feedback_eligible"}

    if reason not in (None, REASON_INCORRECT, REASON_INCOMPLETE):
        return {"ok": False, "error": "invalid_reason"}

    if note and len(note) > NOTE_MAX_LENGTH:
        return {"ok": False, "error": "note_too_long"}

    if helpful:
        reason = None
        note = None

    # 版本级锁：覆盖撤销/加入的相似合并竞态。
    _acquire_cluster_lock(db, f"j4fb:{snapshot.version_label or ''}")
    try:
        snapshot = _get_snapshot(db, answer_id, user_id)
        existing = (
            db.query(AnswerFeedback)
            .filter(AnswerFeedback.answer_id == answer_id, AnswerFeedback.user_id == user_id)
            .first()
        )
        old_helpful = existing.helpful if existing is not None else None
        old_cluster_key = existing.feedback_cluster_key if existing is not None else None
        old_id = existing.id if existing is not None else None

        # 撤销旧负面（精确 key；若用户还有其他负面反馈指向同一 cluster 则保留成员）。
        if old_helpful is False and old_cluster_key:
            if not _user_has_other_negative_in_cluster(db, old_cluster_key, user_id, old_id or ""):
                db.query(FeedbackClusterUser).filter(
                    FeedbackClusterUser.cluster_key == old_cluster_key,
                    FeedbackClusterUser.user_id == user_id,
                ).delete(synchronize_session=False)

        # 加入新负面。
        new_cluster_key: str | None = None
        if not helpful:
            new_cluster_key = _resolve_or_create_cluster(db, snapshot, user_id, reason or REASON_UNSPECIFIED)

        # upsert（记录精确 cluster 关联）。
        if existing is not None:
            existing.helpful = helpful
            existing.reason = reason
            existing.note = note
            existing.feedback_cluster_key = new_cluster_key
            existing.updated_at = datetime.now()
        else:
            db.add(AnswerFeedback(
                id=str(uuid.uuid4()),
                answer_id=answer_id,
                user_id=user_id,
                helpful=helpful,
                reason=reason,
                note=note,
                feedback_cluster_key=new_cluster_key,
            ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise

    # 触发分类（独立事务，锁内 CAS）。
    if new_cluster_key:
        _classify_feedback_cluster(db, new_cluster_key)

    return {"ok": True}


# ---------------------------------------------------------------------------
# 「我仍需要这个答案」
# ---------------------------------------------------------------------------

def _apply_answer_needed_result(
    db: Session,
    snapshot: AnswerSnapshot,
    result: ClassificationResult,
    commit: bool = True,
) -> bool:
    """answer-needed 单用户分类：据快照权限上下文写记录。返回是否成功（degraded 不进入）。"""
    if result.outcome == CLASS_VISIBLE_SUFFICIENT:
        return True
    if result.outcome == CLASS_HIDDEN_SUFFICIENT:
        if snapshot.is_admin:
            return True  # 管理员不形成访问申请
        groups = _snapshot_business_groups(snapshot)
        return record_access_request(
            db,
            groups=groups,
            normalized_query=snapshot.normalized_query,
            original_query=snapshot.original_query,
            version_label=snapshot.version_label,
            version_status=snapshot.version_status,
            target_notebook_id=result.target_notebook_id,
            target_wiki_page_id=result.target_wiki_page_id,
            user_ids=[snapshot.user_id],
            commit=commit,
        ) is not None
    if result.outcome == CLASS_GLOBALLY_MISSING:
        return record_j4_debt(
            db,
            debt_reason=DEBT_REASON_QUERY_MISSING,
            version_label=snapshot.version_label,
            version_status=snapshot.version_status,
            original_query=snapshot.original_query,
            user_ids=[snapshot.user_id],
            retrieval_reason=RETRIEVAL_REASON_MISSING,
            commit=commit,
        ) is not None
    if result.outcome == CLASS_GLOBALLY_INCOMPLETE:
        return record_j4_debt(
            db,
            debt_reason=DEBT_REASON_QUERY_MISSING,
            version_label=snapshot.version_label,
            version_status=snapshot.version_status,
            original_query=snapshot.original_query,
            user_ids=[snapshot.user_id],
            retrieval_reason=RETRIEVAL_REASON_INCOMPLETE,
            commit=commit,
        ) is not None
    return True


def record_answer_needed(db: Session, answer_id: str, current_user: dict) -> dict:
    """处理「我仍需要这个答案」。返回统一安全提示，不泄露后台分类。

    分类使用快照权限上下文；分类 + 占位 + 写记录在同一事务；degraded/持久化失败
    不留占位，后续可重试。
    """
    user_id = current_user.get("id", "")
    snapshot = _get_snapshot(db, answer_id, user_id)
    if snapshot is None:
        return {"ok": False, "error": "not_found"}

    if not snapshot.retrieval_completed or snapshot.service_degraded or snapshot.answer_eligible:
        return {"ok": True}

    if db.query(AnswerNeeded).filter(AnswerNeeded.answer_id == answer_id).first() is not None:
        return {"ok": True}

    # 用快照权限上下文分类。
    view = _snapshot_permission_view(snapshot)
    result = classify_full_library(db, snapshot.original_query, view)
    if result.outcome == CLASS_DEGRADED:
        return {"ok": True}  # 不占位，可重试

    # 同事务：插入占位（幂等闸）+ 写记录。
    try:
        db.add(AnswerNeeded(id=str(uuid.uuid4()), answer_id=answer_id, user_id=user_id))
        db.flush()  # 唯一约束作并发幂等闸
        ok = _apply_answer_needed_result(db, snapshot, result, commit=False)
        if not ok:
            db.rollback()
            return {"ok": True}
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"ok": True}  # 并发已处理
    return {"ok": True}


# ---------------------------------------------------------------------------
# 访问申请列表 / 重验证
# ---------------------------------------------------------------------------

def list_access_requests(db: Session, status: str = "open", limit: int = 200) -> list[AccessRequest]:
    q = db.query(AccessRequest)
    if status:
        q = q.filter(AccessRequest.status == status)
    return q.order_by(AccessRequest.last_seen_at.desc()).limit(limit).all()


def serialize_access_request(req: AccessRequest) -> dict:
    try:
        groups = json.loads(req.requesting_groups or "[]")
    except (TypeError, ValueError):
        groups = []
    return {
        "id": req.id,
        "original_query": req.original_query or req.normalized_query or "",
        "groups": groups,
        "status": req.status,
        "target_notebook_id": req.target_notebook_id,
    }


def _request_scope_sufficient(db: Session, req: AccessRequest) -> bool:
    """申请业务组集合现在是否已能访问充分知识（并集判断，不精确 scope 集合匹配）。"""
    groups = []
    try:
        groups = sorted(set(json.loads(req.requesting_groups or "[]")))
    except (TypeError, ValueError):
        groups = []
    if not groups:
        # company 范围：无业务组用户可见 company + public 内容。
        user = {"groups": []}
    else:
        user = {"groups": groups}
    try:
        assessment = _assess_scope(db, user, req.original_query or req.normalized_query or "")
        return assessment.sufficient
    except Exception:  # noqa: BLE001
        return False


def revalidate_access_requests_for_notebook_change(db: Session, notebook_id: str | None = None) -> dict:
    """Notebook 权限变化后：只重验证「相关」open 访问申请（keyset 分批，不永久漏检）。

    - 相关过滤在 SQL 层（order_by/limit 之前）：target_notebook_id 等于变化 Notebook，
      或 target_wiki_page_id 的 WikiVersionSource 来源 Page 属于该 Notebook；
    - 不物化 Page/Wiki ID 列表（EXISTS/子查询，有界）；
    - 多组按可见范围并集判断；
    - keyset 完整遍历 → truncated=false；失败保持 open。
    """
    resolved = 0
    checked = 0
    batch_count = 0
    last_id = ""
    now = datetime.now()

    def _related_query():
        q = db.query(AccessRequest).filter(AccessRequest.status == "open")
        if notebook_id:
            wiki_target_subq = (
                db.query(WikiVersionSource.wiki_page_id)
                .join(Page, Page.id == WikiVersionSource.page_id)
                .filter(Page.notebook_id == notebook_id)
                .subquery()
            )
            q = q.filter(or_(
                AccessRequest.target_notebook_id == notebook_id,
                AccessRequest.target_wiki_page_id.in_(wiki_target_subq.select()),
            ))
        return q

    while True:
        q = _related_query()
        if last_id:
            q = q.filter(AccessRequest.id > last_id)
        batch = q.order_by(AccessRequest.id).limit(MAX_REVALIDATE_BATCH).all()
        batch_count += 1
        if not batch:
            break
        for req in batch:
            checked += 1
            if _request_scope_sufficient(db, req):
                req.status = "resolved"
                req.resolved_at = now
                resolved += 1
        last_id = batch[-1].id
        if len(batch) < MAX_REVALIDATE_BATCH:
            break
    if resolved:
        db.commit()
    return {
        "resolved": resolved,
        "checked": checked,
        "batch_count": batch_count,
        "truncated": False,  # keyset 完整遍历
    }


def revalidate_access_request(db: Session, request_id: str) -> bool:
    """管理员手动重验证单条访问申请。返回是否 resolved。"""
    req = db.get(AccessRequest, request_id)
    if req is None or req.status != "open":
        return False
    if _request_scope_sufficient(db, req):
        req.status = "resolved"
        req.resolved_at = datetime.now()
        db.commit()
        return True
    return False


# ---------------------------------------------------------------------------
# 全局知识缺口自动解决（Page/Wiki 内容变化后，keyset 分批）
# ---------------------------------------------------------------------------

def _global_debt_resolved(db: Session, debt: KnowledgeDebt) -> bool:
    """全库是否已有充分证据解决该全局知识缺口（复用 Wiki/Raw 充分性判断）。"""
    question = debt.original_query or debt.normalized_query or ""
    if not question:
        return False
    try:
        assessment = _assess_scope(db, _ADMIN_USER, question)
        return assessment.sufficient
    except Exception:  # noqa: BLE001 —— 检索失败保持 open
        return False


def revalidate_global_debts_for_change(db: Session, changed_text: str) -> dict:
    """Page/Wiki 内容变化后：只重验证相关 open 全局知识缺口（keyset 分批，不永久漏检）。"""
    changed_tokens = set(meaningful_tokenize(changed_text))
    if not changed_tokens:
        return {"resolved": 0, "checked": 0, "batch_count": 0, "truncated": False}

    resolved = 0
    checked = 0
    batch_count = 0
    last_id = ""
    now = datetime.now()
    while True:
        q = db.query(KnowledgeDebt).filter(
            KnowledgeDebt.scope_id.is_(None),
            KnowledgeDebt.status == "open",
            KnowledgeDebt.debt_reason.isnot(None),
        )
        if last_id:
            q = q.filter(KnowledgeDebt.id > last_id)
        batch = q.order_by(KnowledgeDebt.id).limit(MAX_REVALIDATE_BATCH).all()
        batch_count += 1
        if not batch:
            break
        for debt in batch:
            question = debt.original_query or debt.normalized_query or ""
            q_tokens = set(meaningful_tokenize(question))
            if not (changed_tokens & q_tokens):
                continue
            checked += 1
            if _global_debt_resolved(db, debt):
                debt.status = "resolved"
                debt.resolved_at = now
                resolved += 1
        last_id = batch[-1].id
        if len(batch) < MAX_REVALIDATE_BATCH:
            break
    if resolved:
        db.commit()
    return {
        "resolved": resolved,
        "checked": checked,
        "batch_count": batch_count,
        "truncated": False,  # keyset 完整遍历
    }
