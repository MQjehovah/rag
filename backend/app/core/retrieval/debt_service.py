"""V4 Phase F：知识债务服务（去 Card/KO/owner/review 化，封板）。

债务只代表「用户需要但知识库确实缺失的内容」，绝不代表模型故障、Card 状态、
审核任务或负责人工作。

核心设计：
- 零 Card/KO 依赖。
- scope 二态：visible_scope_ids（用户可见）+ validated_scope（本次 active scope）。
- 规范化：NFKC + 小写 + 去标点。
- cluster_key = sha256(scope_id:normalized_query)，数据库级唯一约束。
- reopen 语义：同 cluster_key 全表唯一；resolved 后再缺失 → 复用原记录 reopen。
- 并发幂等：occurrence_count 用数据库原子 UPDATE（不读改写）；affected_user_count
  经 knowledge_debt_users 唯一约束 + savepoint 去重；IntegrityError 时回滚重试。
- 相似合并：token Jaccard + char-bigram Jaccard 取 max，按 sim desc + cluster_key 稳定排序。
- 查询级自动重验证：携带 changed 文本，先有界筛选候选债务，再逐条精确 scope 过滤的
  Wiki/Raw 检索 + 充分性判断，只有充分证据才 resolved；无关债务保持 open。
- 精确 scope 过滤：company 债务只能 company 内容解决；group:A 只能 group:A；admin 只能 admin；
  跨域不解决。检索不模拟普通用户/admin 用户，直接按 scope 匹配 acl_scope / Notebook.group_id。
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import access_control
from app.core.retrieval.text_similarity import char_bigrams
from app.core.retrieval.wiki_retriever import (
    meaningful_tokenize,
    retrieve_wiki,
)
from app.core.retrieval.sufficiency_judge import judge_wiki_sufficiency
from app.core.retrieval.raw_retriever import RawDocumentRetriever
from app.core.retrieval.raw_sufficiency_judge import judge_raw_sufficiency
from app.models.database import (
    KnowledgeDebt,
    KnowledgeDebtUser,
    Notebook,
    Page,
)

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.6
RETRIEVAL_REASON_MISSING = "missing_knowledge"

# 有界处理：候选债务/相似扫描的每批大小（keyset 分页续处理，不永久漏检）
MAX_CANDIDATE_BATCH = 100
MAX_SIMILAR_SCAN = 100

# scope 常量
SCOPE_COMPANY = "company"
SCOPE_ADMIN = "admin"
SCOPE_GROUP_PREFIX = "group:"


def normalize_query(question: str) -> str:
    """确定性规范化：NFKC + 小写 + 去空白标点，保留中文/英文/数字。

    委托 debt_keying（冻结版本），保证与 Alembic reconciliation migration 一致。
    """
    from app.core.retrieval.debt_keying import normalize_query as _normalize_query
    return _normalize_query(question)


def build_cluster_key(scope_id: str, normalized_query: str) -> str:
    """稳定 cluster_key：基于 scope_id + 规范化查询字符串，不依赖 jieba 分词。

    委托 debt_keying（冻结版本 v1），保证跨环境、跨 hash seed 一致，
    并与 Alembic reconciliation migration 内联的算法完全一致。
    相似问题合并由 _similarity 独立处理，不进入本键。
    """
    from app.core.retrieval.debt_keying import build_cluster_key as _build_cluster_key
    return _build_cluster_key(scope_id, normalized_query)


# ---------------------------------------------------------------------------
# scope 设计
# ---------------------------------------------------------------------------

def visible_scope_ids(current_user: dict) -> list[str] | None:
    """用户可见的 scope 列表。None = 管理员可见全部。"""
    if access_control.is_admin(current_user):
        return None  # 全部
    result = {SCOPE_COMPANY}
    for g in sorted(access_control.business_groups(current_user)):
        result.add(SCOPE_GROUP_PREFIX + g)
    return sorted(result)


def validated_scope(current_user: dict, requested_scope_id: str | None) -> tuple[str, str]:
    """校验并确定本次 active scope。返回 (scope_id, error)。"""
    is_admin = access_control.is_admin(current_user)
    business = access_control.business_groups(current_user)

    if requested_scope_id:
        if requested_scope_id == SCOPE_COMPANY:
            return SCOPE_COMPANY, ""
        if requested_scope_id == SCOPE_ADMIN:
            return (SCOPE_ADMIN, "") if is_admin else ("", "forbidden")
        if requested_scope_id.startswith(SCOPE_GROUP_PREFIX):
            gname = requested_scope_id[len(SCOPE_GROUP_PREFIX):]
            if is_admin or gname in business:
                return requested_scope_id, ""
            return "", "forbidden"
        return "", "forbidden"

    # 无显式 scope：自动推断
    if is_admin:
        return "", "scope_required"
    if not business:
        return SCOPE_COMPANY, ""
    if len(business) == 1:
        return SCOPE_GROUP_PREFIX + next(iter(business)), ""
    return "", "scope_required"


def scope_id_from_group_id(group_id: str | None) -> str:
    scope = access_control.scope_from_group_id(group_id)
    if scope.kind == "group":
        return SCOPE_GROUP_PREFIX + ",".join(sorted(scope.groups))
    return scope.kind


def scope_id_from_acl(acl_json: str | None) -> str:
    """从 acl_scope JSON 解析规范 scope_id（委托 debt_keying 冻结版本）。

    与 Alembic reconciliation migration 内联算法一致；管理员组冻结为
    __local_admin__（本地试点，V4 计划 8.2）。
    """
    from app.core.retrieval.debt_keying import scope_id_from_acl as _scope_id_from_acl
    return _scope_id_from_acl(acl_json)


# ---------------------------------------------------------------------------
# 记录 / 累计 / reopen / 并发幂等
# ---------------------------------------------------------------------------

@dataclass
class DebtRecordResult:
    ok: bool
    debt_id: str | None = None
    created_new: bool = False
    reopened: bool = False
    error: str = ""


def _bigram_similarity(a: str, b: str) -> float:
    ba = char_bigrams(a)
    bb = char_bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def _similarity(a: str, b: str) -> float:
    ta = set(meaningful_tokenize(a))
    tb = set(meaningful_tokenize(b))
    token_sim = 0.0
    if ta and tb:
        token_sim = len(ta & tb) / len(ta | tb)
    return max(token_sim, _bigram_similarity(a, b))


def _find_similar_open_debt(db: Session, scope_id: str, norm: str) -> KnowledgeDebt | None:
    """相似候选：同 scope open 债务（有界），按 sim desc + cluster_key 稳定排序。"""
    candidates = (
        db.query(KnowledgeDebt)
        .filter(KnowledgeDebt.scope_id == scope_id, KnowledgeDebt.status == "open")
        .order_by(KnowledgeDebt.last_seen_at.desc(), KnowledgeDebt.id)
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


def _atomic_increment(db: Session, debt_id: str, *, occurrence: bool) -> None:
    """数据库原子更新 occurrence_count（不读改写，避免并发丢失）。"""
    if occurrence:
        db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt_id).update(
            {
                KnowledgeDebt.occurrence_count: KnowledgeDebt.occurrence_count + 1,
                KnowledgeDebt.last_seen_at: datetime.now(),
            },
            synchronize_session=False,
        )


def _add_user(db: Session, debt_id: str, user_id: str) -> None:
    """affected_user 去重：savepoint 内插入，唯一冲突不影响外层事务。

    使用 func.coalesce 兼容历史 NULL（NULL 视为 0 后 +1）。
    """
    if not user_id:
        return
    from sqlalchemy import func
    try:
        with db.begin_nested():
            db.add(KnowledgeDebtUser(id=str(uuid.uuid4()), debt_id=debt_id, user_id=user_id))
            db.flush()
            db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt_id).update(
                {KnowledgeDebt.affected_user_count: func.coalesce(KnowledgeDebt.affected_user_count, 0) + 1},
                synchronize_session=False,
            )
    except IntegrityError:
        pass  # 用户已存在，不重复计数


def _acquire_scope_lock(db: Session, scope_id: str) -> None:
    """数据库级 scope 锁：串行化同 scope 的债务写入，消除相似合并竞态。

    - PostgreSQL：pg_advisory_xact_lock(hashtext(scope_id))（事务级，commit/rollback 释放）。
    - SQLite：BEGIN IMMEDIATE 获取写锁（busy_timeout 由连接配置），测试降级但语义正确。
    不依赖进程内锁。
    """
    from sqlalchemy import text
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:s))"), {"s": scope_id})
    else:
        # SQLite：结束 autobegin 只读事务后立即获取写锁
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))


def record_missing_knowledge(
    db: Session,
    *,
    original_query: str,
    user_id: str,
    scope_id: str,
    retrieval_reason: str = RETRIEVAL_REASON_MISSING,
) -> DebtRecordResult:
    """记录一次真实知识缺失请求（幂等累计 + reopen + 并发安全）。"""
    if not scope_id:
        return DebtRecordResult(ok=False, error="scope_unknown")
    norm = normalize_query(original_query)
    if not norm:
        return DebtRecordResult(ok=False, error="empty_query")

    cluster_key = build_cluster_key(scope_id, norm)
    now = datetime.now()

    # 数据库级 scope 锁：确保相似合并/精确去重在同一 scope 内串行
    try:
        _acquire_scope_lock(db, scope_id)
    except Exception as exc:  # noqa: BLE001 —— 锁失败仍继续（退化为非串行，但唯一约束兜底精确去重）
        # V4 Phase G 硬化：锁获取失败不得静默无记录，输出结构化 warning/metric。
        logger.warning(
            "scope_advisory_lock_failed",
            extra={
                "scope_id": scope_id,
                "dialect": (db.bind.dialect.name if db.bind is not None else "unknown"),
                "error": str(exc),
            },
        )

    for _attempt in range(3):
        try:
            # 1. 精确 cluster_key 命中（含 resolved → reopen）
            debt = (
                db.query(KnowledgeDebt)
                .filter(KnowledgeDebt.cluster_key == cluster_key)
                .first()
            )

            if debt is not None and debt.status != "open":
                # reopen 原记录（同 cluster_key 全表唯一）
                db.query(KnowledgeDebt).filter(KnowledgeDebt.id == debt.id).update(
                    {"status": "open", "resolved_at": None, "last_seen_at": now},
                    synchronize_session=False,
                )
                _atomic_increment(db, debt.id, occurrence=True)
                _add_user(db, debt.id, user_id)
                db.commit()
                return DebtRecordResult(ok=True, debt_id=debt.id, reopened=True)

            if debt is not None:
                # open 累计（原子 occurrence + 原子 affected user）
                _atomic_increment(db, debt.id, occurrence=True)
                _add_user(db, debt.id, user_id)
                db.commit()
                return DebtRecordResult(ok=True, debt_id=debt.id)

            # 2. 相似合并
            similar = _find_similar_open_debt(db, scope_id, norm)
            if similar is not None:
                if not similar.cluster_key:
                    similar.cluster_key = cluster_key
                _atomic_increment(db, similar.id, occurrence=True)
                _add_user(db, similar.id, user_id)
                db.commit()
                return DebtRecordResult(ok=True, debt_id=similar.id)

            # 3. 新建
            debt = KnowledgeDebt(
                debt_type="missing_knowledge",
                description=f"知识缺失：{original_query}",
                related_question=original_query,
                score=1.0,
                status="open",
                title=original_query,
                root_cause=RETRIEVAL_REASON_MISSING,
                original_query=original_query,
                normalized_query=norm,
                cluster_key=cluster_key,
                scope_id=scope_id,
                retrieval_reason=retrieval_reason,
                occurrence_count=1,
                affected_user_count=0,
                first_seen_at=now,
                last_seen_at=now,
                acl_scope=_scope_to_acl_json(scope_id),
            )
            db.add(debt)
            db.flush()
            _add_user(db, debt.id, user_id)
            db.commit()
            return DebtRecordResult(ok=True, debt_id=debt.id, created_new=True)
        except IntegrityError:
            # 并发插入竞争（cluster_key 唯一冲突）：rollback → 重读 → 原子累计
            db.rollback()
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_missing_knowledge failed: %s", exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return DebtRecordResult(ok=False, error="persist_failed")

    return DebtRecordResult(ok=False, error="persist_failed")


def _scope_to_acl_json(scope_id: str) -> str:
    import json as _json
    if scope_id == SCOPE_COMPANY:
        return _json.dumps({"groups": ["__public__"]}, ensure_ascii=False)
    if scope_id == SCOPE_ADMIN:
        return _json.dumps({"groups": ["__local_admin__"]}, ensure_ascii=False)
    if scope_id.startswith(SCOPE_GROUP_PREFIX):
        names = scope_id[len(SCOPE_GROUP_PREFIX):].split(",")
        return _json.dumps({"groups": names}, ensure_ascii=False)
    return "{}"


# ---------------------------------------------------------------------------
# 查询级自动重验证（精确 scope 过滤）
# ---------------------------------------------------------------------------

def _candidate_open_debts(
    db: Session,
    scope_id: str,
    changed_text: str,
    *,
    with_bounds: bool = False,
):
    """有界筛选可能相关的 open 债务（keyset 分页，不永久漏检）。

    scope 过滤先于分页；每批 MAX_CANDIDATE_BATCH，按 id 递增续处理直到耗尽。
    with_bounds=True 时返回 (related, batch_count, truncated)。
    """
    if not scope_id or not (changed_text or "").strip():
        if with_bounds:
            return [], 0, False
        return []
    changed_tokens = set(meaningful_tokenize(changed_text))
    changed_bigrams = char_bigrams(changed_text)

    related: list[KnowledgeDebt] = []
    last_id = ""
    batch_count = 0
    truncated = False
    while True:
        q = db.query(KnowledgeDebt).filter(
            KnowledgeDebt.scope_id == scope_id,
            KnowledgeDebt.status == "open",
        )
        if last_id:
            q = q.filter(KnowledgeDebt.id > last_id)
        batch = q.order_by(KnowledgeDebt.id).limit(MAX_CANDIDATE_BATCH + 1).all()
        batch_count += 1
        if len(batch) > MAX_CANDIDATE_BATCH:
            truncated = True
            batch = batch[:MAX_CANDIDATE_BATCH]
        if not batch:
            break
        for d in batch:
            query = d.original_query or d.normalized_query or ""
            if not query:
                continue
            qt = set(meaningful_tokenize(query))
            qb = char_bigrams(query)
            if changed_tokens & qt or changed_bigrams & qb:
                related.append(d)
        last_id = batch[-1].id
        if len(batch) < MAX_CANDIDATE_BATCH:
            break
    if with_bounds:
        return related, batch_count, truncated
    return related


def _debt_resolved(db: Session, debt: KnowledgeDebt) -> bool:
    """复用 Phase D 封板语义判断债务是否已获充分证据（精确 scope 过滤）。

    - Wiki：retrieve_wiki + judge_wiki_sufficiency（current published Revision）。
    - Raw：RawDocumentRetriever（_eligible_pages + active SourceItem 同 Connector）+ judge_raw_sufficiency。
    - scope 过滤通过 _scope_override 注入 current_user，在 SQL 层先于 limit 发生。
    - 检索异常返回 False（保持 open）。
    """
    question = debt.original_query or debt.normalized_query or ""
    if not question:
        return False
    scope_id = debt.scope_id
    scoped_user = {"groups": [], "_scope_override": scope_id}

    # Wiki
    try:
        wiki_result = retrieve_wiki(db, scoped_user, question)
        if wiki_result.hits:
            verdict = judge_wiki_sufficiency(wiki_result, question)
            if verdict.sufficient:
                return True
    except Exception:  # noqa: BLE001
        return False

    # Raw
    try:
        raw = RawDocumentRetriever(db).retrieve(db, question, scoped_user)
        raw_verdict = judge_raw_sufficiency(raw.hits, question)
        if raw_verdict.sufficient:
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def revalidate_debts_for_change(
    db: Session,
    *,
    scope_id: str,
    changed_text: str,
) -> dict:
    """查询级重验证：只解决那些查询已获充分证据的 open 债务，无关债务保持 open。

    V4 Phase G 硬化：返回 batch_count（实际扫过的批次数）与 truncated（是否有
    分页截断未完整扫完），避免未来数据规模扩大后无法观察处理范围。
    """
    if not scope_id:
        return {"resolved": 0, "checked": 0, "batch_count": 0, "truncated": False}
    if not (changed_text or "").strip():
        # 空 changed_text 不伪装成已接线，直接不重验证
        return {"resolved": 0, "checked": 0, "batch_count": 0, "truncated": False}
    candidates, batch_count, truncated = _candidate_open_debts(
        db, scope_id, changed_text, with_bounds=True
    )
    if not candidates:
        return {"resolved": 0, "checked": 0, "batch_count": batch_count, "truncated": truncated}

    now = datetime.now()
    resolved = 0
    for debt in candidates:
        try:
            if _debt_resolved(db, debt):
                debt.status = "resolved"
                debt.resolved_at = now
                resolved += 1
        except Exception:  # noqa: BLE001 —— 检索异常保持 open
            continue
    if resolved:
        db.commit()
    return {
        "resolved": resolved,
        "checked": len(candidates),
        "batch_count": batch_count,
        "truncated": truncated,
    }


def notify_knowledge_changed_for_page(db: Session, page_id: str) -> dict:
    """Page 新建/更新/索引/同步后：查询级重验证（携带 Page 文本）。

    J-4：同时重验证全局知识缺口（scope_id IS NULL），只解决已获充分证据者。
    """
    try:
        page = db.get(Page, page_id)
        if page is None or not page.notebook_id:
            return {"resolved": 0, "checked": 0}
        nb = db.get(Notebook, page.notebook_id)
        if nb is None:
            return {"resolved": 0, "checked": 0}
        scope_id = scope_id_from_group_id(nb.group_id)
        text = f"{page.title or ''} {page.content or ''}"
        result = revalidate_debts_for_change(db, scope_id=scope_id, changed_text=text)
        from app.core.retrieval.feedback_service import revalidate_global_debts_for_change
        global_result = revalidate_global_debts_for_change(db, text)
        return {
            "resolved": result["resolved"] + global_result["resolved"],
            "checked": result["checked"] + global_result["checked"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify_knowledge_changed_for_page failed: %s", exc)
        return {"resolved": 0, "checked": 0}


def notify_knowledge_changed_for_wiki(db: Session, acl_scope: str | None, changed_text: str) -> dict:
    """Wiki 新建/编辑/刷新后：查询级重验证（必须携带真实变更内容）。

    J-4：同时重验证全局知识缺口（scope_id IS NULL），只解决已获充分证据者。
    """
    try:
        scope_id = scope_id_from_acl(acl_scope)
        result = revalidate_debts_for_change(db, scope_id=scope_id, changed_text=changed_text)
        from app.core.retrieval.feedback_service import revalidate_global_debts_for_change
        global_result = revalidate_global_debts_for_change(db, changed_text)
        return {
            "resolved": result["resolved"] + global_result["resolved"],
            "checked": result["checked"] + global_result["checked"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify_knowledge_changed_for_wiki failed: %s", exc)
        return {"resolved": 0, "checked": 0}


# ---------------------------------------------------------------------------
# 列表 / 序列化
# ---------------------------------------------------------------------------

def list_debts_for_scopes(
    db: Session,
    *,
    visible_scope_ids: list[str] | None,
    status: str = "open",
    scope_filter: str | None = None,
    limit: int = 100,
) -> list[KnowledgeDebt]:
    """返回允许展示的债务。visible_scope_ids=None 表示管理员全部。"""
    q = db.query(KnowledgeDebt)
    if visible_scope_ids is not None:
        q = q.filter(KnowledgeDebt.scope_id.in_(visible_scope_ids))
    if scope_filter:
        q = q.filter(KnowledgeDebt.scope_id == scope_filter)
    if status:
        q = q.filter(KnowledgeDebt.status == status)
    return q.order_by(KnowledgeDebt.occurrence_count.desc(), KnowledgeDebt.last_seen_at.desc()).limit(limit).all()


def serialize_debt(debt: KnowledgeDebt) -> dict:
    return {
        "id": debt.id,
        "original_query": debt.original_query or debt.related_question or "",
        "normalized_query": debt.normalized_query or "",
        "cluster_key": debt.cluster_key or "",
        "occurrence_count": debt.occurrence_count or 1,
        "affected_user_count": debt.affected_user_count or 1,
        "first_seen_at": debt.first_seen_at.isoformat() if debt.first_seen_at else None,
        "last_seen_at": debt.last_seen_at.isoformat() if debt.last_seen_at else None,
        "scope_id": debt.scope_id or "",
        "retrieval_reason": debt.retrieval_reason or "",
        "status": debt.status,
        "resolved_at": debt.resolved_at.isoformat() if debt.resolved_at else None,
    }
