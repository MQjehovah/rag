"""P32.5 V4 Phase H：知识债务 schema 漂移 reconciliation（P32 与 P33 之间）。

背景（真实库漂移）：
- 真实库 alembic_version 停留在 P31（b8e9f0a1b2c3），但 `init_db` 的
  `_migrate_schema` 曾静默补齐了 P32 的列与 knowledge_debt_users 表，却从未
  更新版本号、也从未创建 P32 声明的索引/唯一约束。
- 直接 `upgrade head` 会在 P32 重复 add_column 失败；即使 stamp P32，也会跳过
  P32 缺失的三个 debt 索引与 users 唯一约束，违反 Phase F 封板语义。

本 reconciliation 职责（幂等，可对全新库 / 漂移库重复执行）：
1. 完整校验 P32 的列、类型、nullable、长度（字段/表存在 ≠ P32 完整）。
2. keyset/batch 回填旧债务数据（不 fetchall 无界加载）：只迁移能无歧义重建
   query 与 scope 的记录；无法确定的记录明确标记隔离，绝不猜测、绝不伪造。
3. 检查重复 cluster_key，存在则 fail closed（不盲目建唯一索引、不删数据）。
4. 精确校验并补齐三个 debt 索引与 knowledge_debt_users 的列/索引/级联 FK；
   对同名但定义错误的索引/约束 fail closed（不静默重建、不静默通过）。

自包含与确定性：
- 不 import app.core.retrieval.debt_service / access_control / 任何运行时业务模块。
- 规范化、scope 解析、cluster_key 算法内联为版本化纯函数（与
  app/core/retrieval/debt_keying.py v1 完全一致），不依赖 jieba / settings /
  LDAP / hash seed / 未来业务代码变化。

downgrade：本 reconciliation 补齐的是 P32 本应具备的结构（三个 debt 索引 +
  users 索引/FK）；回填数据不可逆。downgrade 设为 no-op，绝不生成
  「版本为 P32 但缺 P32 索引」的结构。

Revision ID: a8b9c0d1e2f3
Revises: c5e6f7a8b9d0 (P32)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "c5e6f7a8b9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# 内联冻结纯函数（与 app/core/retrieval/debt_keying.py v1 一致，勿改）。
# ---------------------------------------------------------------------------

_SCOPE_COMPANY = "company"
_SCOPE_ADMIN = "admin"
_SCOPE_GROUP_PREFIX = "group:"
_SCOPE_UNKNOWN = "unknown"
_ADMIN_GROUPS = frozenset({"__local_admin__"})
_PUBLIC_MARKERS = frozenset({"__public__"})


def _normalize_query(question: str) -> str:
    q = unicodedata.normalize("NFKC", question or "")
    q = q.lower()
    return re.sub(r"[^\w一-鿿]", "", q)


def _build_cluster_key(scope_id: str, normalized_query: str) -> str:
    return hashlib.sha256(f"{scope_id}:{normalized_query}".encode("utf-8")).hexdigest()


def _scope_from_group_set(gs: set[str]) -> str:
    if not gs:
        return _SCOPE_UNKNOWN
    has_public = bool(gs & _PUBLIC_MARKERS)
    has_admin = bool(gs & _ADMIN_GROUPS)
    business = gs - _PUBLIC_MARKERS - _ADMIN_GROUPS
    if has_public and not has_admin and not business:
        return _SCOPE_COMPANY
    if has_admin and not has_public and not business:
        return _SCOPE_ADMIN
    if not has_public and not has_admin and len(business) == 1:
        return _SCOPE_GROUP_PREFIX + next(iter(business))
    return _SCOPE_UNKNOWN


def _scope_id_from_acl(acl_json: str | None) -> str:
    if not acl_json:
        return _SCOPE_UNKNOWN
    try:
        data = json.loads(acl_json)
    except (TypeError, ValueError):
        return _SCOPE_UNKNOWN
    if isinstance(data, list):
        gs = {str(x).strip() for x in data if x is not None and str(x).strip()}
        return _scope_from_group_set(gs)
    if not isinstance(data, dict):
        return _SCOPE_UNKNOWN
    explicit = data.get("scope")
    groups_raw = data.get("groups")
    gs = (
        {str(x).strip() for x in groups_raw if x is not None and str(x).strip()}
        if isinstance(groups_raw, list)
        else set()
    )
    if gs:
        resolved = _scope_from_group_set(gs)
        if explicit == _SCOPE_COMPANY and resolved != _SCOPE_COMPANY:
            return _SCOPE_UNKNOWN
        if explicit == _SCOPE_ADMIN and resolved != _SCOPE_ADMIN:
            return _SCOPE_UNKNOWN
        if explicit == "group" and not resolved.startswith(_SCOPE_GROUP_PREFIX):
            return _SCOPE_UNKNOWN
        return resolved
    if explicit == _SCOPE_COMPANY:
        return _SCOPE_COMPANY
    if explicit == _SCOPE_ADMIN:
        return _SCOPE_ADMIN
    return _SCOPE_UNKNOWN

# ---------------------------------------------------------------------------
# 迁移常量
# ---------------------------------------------------------------------------

_UNMIGRATABLE_NO_SCOPE = "legacy_unmigratable_no_scope"
_UNMIGRATABLE_EMPTY_QUERY = "legacy_unmigratable_empty_query"
_BACKFILL_RETRIEVAL_REASON = "missing_knowledge"

# P32 声明的三个 debt 索引：名称 -> (列及顺序, unique)。
_DEBT_INDEXES: dict[str, tuple[list[str], bool]] = {
    "ix_knowledge_debts_normalized_query": (["normalized_query"], False),
    "ux_knowledge_debts_cluster_key": (["cluster_key"], True),
    "ix_knowledge_debts_scope_id": (["scope_id"], False),
}

# knowledge_debt_users 的三个索引：名称 -> (列及顺序, unique)。
_DEBT_USERS_INDEXES: dict[str, tuple[list[str], bool]] = {
    "ix_knowledge_debt_users_debt_id": (["debt_id"], False),
    "ix_knowledge_debt_users_user_id": (["user_id"], False),
    "ux_kdu_debt_user": (["debt_id", "user_id"], True),
}

# P32 声明的新列：名称 -> (python_type, 期望长度或 None)。
_P32_COLUMNS: dict[str, tuple[type, int | None]] = {
    "original_query": (str, None),            # Text
    "normalized_query": (str, 255),           # String(255)
    "cluster_key": (str, 255),                # String(255)
    "affected_user_count": (int, None),       # Integer
    "scope_id": (str, 255),                   # String(255)
    "retrieval_reason": (str, 50),            # String(50)
}

# knowledge_debt_users 期望列：名称 -> (python_type, length, nullable, primary_key)。
_DEBT_USERS_COLUMNS: dict[str, tuple[type, int | None, bool, bool]] = {
    "id": (str, 36, False, True),
    "debt_id": (str, 36, False, False),
    "user_id": (str, 255, False, False),
}

_BACKFILL_BATCH = 200


def _col_pytype(col: dict) -> type | None:
    return getattr(col.get("type"), "python_type", None)


def _col_length(col: dict) -> int | None:
    return getattr(col.get("type"), "length", None)


def _assert_p32_columns(inspector) -> None:
    """校验 P32 六个列存在、类型、nullable、长度。缺失/不符即 fail closed。"""
    if not inspector.has_table("knowledge_debts"):
        raise RuntimeError("reconciliation 失败：knowledge_debts 表不存在")
    existing = {c["name"]: c for c in inspector.get_columns("knowledge_debts")}
    missing = [n for n in _P32_COLUMNS if n not in existing]
    if missing:
        raise RuntimeError(
            "reconciliation 失败：P32 债务字段缺失（P32 未完整执行）：" + ", ".join(missing)
        )
    for name, (pytype, length) in _P32_COLUMNS.items():
        col = existing[name]
        if _col_pytype(col) is not pytype:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debts.{name} 类型不匹配，"
                f"期望 {pytype.__name__}，实际 {_col_pytype(col)!r}（{col.get('type')!r}）"
            )
        if col.get("nullable") is not True:
            raise RuntimeError(f"reconciliation 失败：knowledge_debts.{name} 应为 nullable=True")
        if length is not None and _col_length(col) != length:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debts.{name} 长度不匹配，"
                f"期望 {length}，实际 {_col_length(col)!r}"
            )


def _backfill_debt_data(bind) -> dict:
    """keyset/batch 回填旧债务（不 fetchall 无界加载）。

    只迁移能无歧义重建 query 与 scope 的记录；其余标记隔离，不猜测、不伪造。
    """
    stats = {"migrated": 0, "unmigratable_no_scope": 0, "unmigratable_empty_query": 0, "skipped": 0}
    last_id = ""
    while True:
        q = (
            "SELECT id, original_query, related_question, acl_scope, "
            "normalized_query, scope_id, cluster_key, retrieval_reason "
            "FROM knowledge_debts "
        )
        params: dict = {"batch": _BACKFILL_BATCH}
        if last_id:
            q += " WHERE id > :last_id "
            params["last_id"] = last_id
        q += " ORDER BY id LIMIT :batch"
        rows = bind.execute(sa.text(q), params).fetchall()
        if not rows:
            break
        for row in rows:
            (did, original_query, related_question, acl_scope,
             normalized_query, scope_id, cluster_key, retrieval_reason) = row
            if normalized_query and scope_id and cluster_key:
                stats["skipped"] += 1
                continue
            if not normalized_query:
                source = (original_query or "").strip() or (related_question or "").strip()
                if not source:
                    bind.execute(sa.text(
                        "UPDATE knowledge_debts SET retrieval_reason = :r WHERE id = :i"
                    ), {"r": _UNMIGRATABLE_EMPTY_QUERY, "i": did})
                    stats["unmigratable_empty_query"] += 1
                    continue
                normalized_query = _normalize_query(source)
            if not scope_id:
                resolved = _scope_id_from_acl(acl_scope)
                if not resolved or resolved == _SCOPE_UNKNOWN:
                    bind.execute(sa.text(
                        "UPDATE knowledge_debts SET retrieval_reason = :r WHERE id = :i"
                    ), {"r": _UNMIGRATABLE_NO_SCOPE, "i": did})
                    stats["unmigratable_no_scope"] += 1
                    continue
                scope_id = resolved
            cluster_key = _build_cluster_key(scope_id, normalized_query)
            bind.execute(sa.text(
                "UPDATE knowledge_debts SET normalized_query = :nq, scope_id = :si, "
                "cluster_key = :ck, retrieval_reason = :rr WHERE id = :i"
            ), {
                "nq": normalized_query, "si": scope_id, "ck": cluster_key,
                "rr": _BACKFILL_RETRIEVAL_REASON, "i": did,
            })
            stats["migrated"] += 1
        if len(rows) < _BACKFILL_BATCH:
            break
        last_id = rows[-1][0]
    return stats


def _assert_no_duplicate_cluster_key(bind) -> None:
    """检查重复 cluster_key（NULL 除外）。存在则 fail closed。"""
    dup = bind.execute(sa.text(
        "SELECT cluster_key, COUNT(*) AS c FROM knowledge_debts "
        "WHERE cluster_key IS NOT NULL GROUP BY cluster_key HAVING c > 1"
    )).fetchall()
    if dup:
        raise RuntimeError(
            f"reconciliation 失败：存在 {len(dup)} 组重复 cluster_key，"
            "无法安全创建唯一索引（需人工审计，不得盲目删除数据）"
        )


def _exact_index_matches(existing: dict, name: str, cols: list[str], unique: bool) -> bool:
    idx = existing.get(name)
    if idx is None:
        return False
    return list(idx.get("column_names") or []) == cols and bool(idx.get("unique")) is unique


def _ensure_debt_indexes(inspector) -> None:
    """精确校验 + 补齐三个 debt 索引；同名但定义错误 → fail closed。"""
    existing = {idx["name"]: idx for idx in inspector.get_indexes("knowledge_debts")}
    for name, (cols, unique) in _DEBT_INDEXES.items():
        if name not in existing:
            op.create_index(name, "knowledge_debts", cols, unique=unique)
            logger.info("reconciliation: created index %s", name)
            continue
        # 同名索引必须列/顺序/unique 完全一致，否则 fail closed（不静默通过/重建）。
        idx = existing[name]
        actual_cols = list(idx.get("column_names") or [])
        actual_unique = bool(idx.get("unique"))
        if actual_cols != cols or actual_unique is not unique:
            raise RuntimeError(
                f"reconciliation 失败：索引 {name} 定义错误，"
                f"期望列={cols} unique={unique}，实际列={actual_cols} unique={actual_unique}"
            )


def _assert_debt_users_columns(inspector) -> None:
    """校验 knowledge_debt_users 全部列、类型/长度、nullable、主键。"""
    if not inspector.has_table("knowledge_debt_users"):
        op.create_table(
            "knowledge_debt_users",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("debt_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["debt_id"], ["knowledge_debts.id"], ondelete="CASCADE"),
        )
        return
    existing = {c["name"]: c for c in inspector.get_columns("knowledge_debt_users")}
    for name, (pytype, length, nullable, primary_key) in _DEBT_USERS_COLUMNS.items():
        if name not in existing:
            raise RuntimeError(f"reconciliation 失败：knowledge_debt_users 缺列 {name}")
        col = existing[name]
        if _col_pytype(col) is not pytype:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debt_users.{name} 类型不匹配，"
                f"期望 {pytype.__name__}，实际 {_col_pytype(col)!r}"
            )
        if _col_length(col) != length:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debt_users.{name} 长度不匹配，"
                f"期望 {length}，实际 {_col_length(col)!r}"
            )
        if col.get("nullable") is not nullable:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debt_users.{name} nullable 不匹配，"
                f"期望 {nullable}，实际 {col.get('nullable')!r}"
            )
        if bool(col.get("primary_key")) is not primary_key:
            raise RuntimeError(
                f"reconciliation 失败：knowledge_debt_users.{name} primary_key 不匹配，"
                f"期望 {primary_key}，实际 {col.get('primary_key')!r}"
            )


def _ensure_debt_users_indexes_and_fk(inspector) -> None:
    """精确校验 + 补齐 users 索引与 debt_id 级联 FK；定义错误 → fail closed。"""
    existing_idx = {idx["name"]: idx for idx in inspector.get_indexes("knowledge_debt_users")}
    for name, (cols, unique) in _DEBT_USERS_INDEXES.items():
        if name not in existing_idx:
            op.create_index(name, "knowledge_debt_users", cols, unique=unique)
            logger.info("reconciliation: created index %s", name)
            continue
        idx = existing_idx[name]
        if list(idx.get("column_names") or []) != cols or bool(idx.get("unique")) is not unique:
            raise RuntimeError(
                f"reconciliation 失败：索引 {name} 定义错误，"
                f"期望列={cols} unique={unique}，实际列={list(idx.get('column_names') or [])} "
                f"unique={bool(idx.get('unique'))}"
            )

    fks = inspector.get_foreign_keys("knowledge_debt_users")
    debt_fk_ok = any(
        fk.get("constrained_columns") == ["debt_id"]
        and fk.get("referred_table") == "knowledge_debts"
        and (fk.get("options", {}).get("ondelete") or "").upper() == "CASCADE"
        for fk in fks
    )
    if not debt_fk_ok:
        raise RuntimeError(
            "reconciliation 失败：knowledge_debt_users.debt_id 缺少 ON DELETE CASCADE 外键"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. 校验 P32 列（字段存在 ≠ 完整：类型/nullable/长度）。
    _assert_p32_columns(inspector)

    # 2. keyset/batch 回填旧债务。
    stats = _backfill_debt_data(bind)
    logger.info(
        "reconciliation: debt backfill migrated=%s unmigratable_no_scope=%s "
        "unmigratable_empty_query=%s skipped=%s",
        stats["migrated"], stats["unmigratable_no_scope"],
        stats["unmigratable_empty_query"], stats["skipped"],
    )

    # 3. 检查重复 cluster_key（fail closed，先于建唯一索引）。
    _assert_no_duplicate_cluster_key(bind)

    # 4. 精确校验 + 补齐 debt 索引。
    _ensure_debt_indexes(inspector)

    # 5. 精确校验 + 补齐 knowledge_debt_users 列/索引/FK。
    _assert_debt_users_columns(inspector)
    _ensure_debt_users_indexes_and_fk(inspector)


def downgrade() -> None:
    """no-op：本 reconciliation 补齐的是 P32 本应具备的结构。

    不回滚数据（回填不可逆）、不删 P32 索引（否则会生成「版本为 P32 但缺
    P32 索引」的错误结构）。恢复只能通过迁移前已验证备份。
    """
    pass
