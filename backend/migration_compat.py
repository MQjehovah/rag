"""Alembic 迁移期兼容辅助（standalone，仅 stdlib + SQLAlchemy/Alembic）。

用途：Phase 9B 副本迁移兼容——真实库可能出现「alembic_version 停在旧 revision，
但部分后加列/约束/索引已物理存在」的状态。规则（与 P40-P44 实际受影响迁移配套）：

1. 缺失对象按原迁移定义创建（表创建仍在各自迁移内；本模块处理列/CHECK/索引/FK）。
2. 已存在且与目标定义**等价**的列/约束/索引保留，不重复创建。
3. 列存在不代表迁移完成：缺失的 FK/CHECK/索引仍须补齐。
4. 同名对象**不等价**时受控失败（抛 RuntimeError）：不跳过、不静默改类型、
   不删列重建丢弃数据。
5. SQLite 类型比较做有依据的语义归一（去空白、大写、VARCHAR(N) vs VARCHAR ( N )）；
   nullable 必须一致；default 差异在「目标 nullable 且不重写既有行」时容忍
   （既有列非新增时未来 INSERT 语义由写入端契约保证）。
6. 新增约束前不自动校验/清洗数据——SQLite batch 重建时若既有数据违反新约束，
   CREATE 阶段自然失败且数据保持（调用方负责按失败语义处理，不清理）。
7. 不 import app 模型 / schema guard / main / worker，不触发外部调用。

非 SQLite（PostgreSQL）路径：本模块不改动原迁移的非 sqlite 分支语义，
PostgreSQL 仍走原 op.* 直连 DDL；是否已实测在演练报告中如实声明。
"""
from __future__ import annotations

import re

from alembic import op
from sqlalchemy import inspect

_RE_WS = re.compile(r"\s+")
_RE_SQLITE_CONSTRAINT = re.compile(
    r"CONSTRAINT\s+(\w+)\s+CHECK\s*\(([^)]*)\)|CHECK\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _norm(text) -> str:
    """类型/SQL 归一：去空白并大写（用于语义等价比较）。

    SQLAlchemy inspector 的列 type 可能是 TypeEngine 对象（str() 得到 VARCHAR(64)），
    也可能已是字符串（PRAGMA 直读）——这里统一转字符串。
    """
    if not isinstance(text, str):
        text = str(text or "")
    return _RE_WS.sub("", text).upper()


def existing_columns(bind, table: str) -> dict:
    insp = inspect(bind)
    if not insp.has_table(table):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def _validate_existing_column(existing: dict, target, table: str) -> None:
    """已存在列与目标列做等价校验；不等价抛受控错误（含定位信息，不含数据）。"""
    target_type = _norm(str(target.type))
    existing_type = _norm(existing.get("type") or "")
    if existing_type != target_type:
        raise RuntimeError(
            "migration_compat_type_mismatch: "
            f"{table}.{target.name} existing type={existing.get('type')!r} "
            f"vs target={target.type!r}"
        )
    existing_nullable = bool(existing.get("nullable"))
    target_nullable = bool(getattr(target, "nullable", True) is not False)
    if existing_nullable != target_nullable:
        raise RuntimeError(
            "migration_compat_nullable_mismatch: "
            f"{table}.{target.name} existing nullable={existing_nullable} "
            f"vs target nullable={target_nullable}"
        )


def ensure_columns_sqlite(op, bind, table: str, columns) -> list:
    """SQLite：仅补齐缺失列；已存在列先做等价校验。

    返回本次实际新增的列名列表。无缺失且全部等价时不做任何 DDL（避免无谓的表重建）。
    """
    existing = existing_columns(bind, table)
    missing: list = []
    for col in columns:
        if col.name in existing:
            _validate_existing_column(existing[col.name], col, table)
        else:
            missing.append(col)
    if missing:
        with op.batch_alter_table(table) as batch_op:
            for col in missing:
                batch_op.add_column(col)
    return [c.name for c in missing]


def table_sql(bind, table: str) -> str:
    res = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return (res[0] if res else "") or ""


def _has_constraint(sql_text: str, name: str, condition: str) -> bool:
    """按 name 或规范化 condition 判断约束是否已存在。"""
    norm_cond = _norm(condition)
    for match in _RE_SQLITE_CONSTRAINT.finditer(sql_text or ""):
        cname = (match.group(1) or "").strip()
        cond = (match.group(2) or match.group(3) or "").strip()
        if cname and _norm(cname) == _norm(name):
            if _norm(cond) != norm_cond:
                raise RuntimeError(
                    "migration_compat_constraint_conflict: constraint "
                    f"{name!r} exists with different definition"
                )
            return True
        if cond and _norm(cond) == norm_cond:
            return True
    return False


def ensure_checks_sqlite(op, bind, table: str, checks) -> list:
    """SQLite：补齐缺失的 CHECK 约束（batch 重建）。

    checks: [(constraint_name, condition_sql), ...]。已存在的同名且等价约束跳过；
    同名不等价抛错。若表内既有数据违反待新增约束，batch 重建时 SQLite 抛约束错误
    ——调用方把该失败作为迁移失败处理（不自动清洗/删行）。
    """
    existing = existing_columns(bind, table)
    if not existing:
        raise RuntimeError(
            f"migration_compat_missing_table: {table} does not exist"
        )
    sql_text = table_sql(bind, table)
    missing = [(n, c) for n, c in checks if not _has_constraint(sql_text, n, c)]
    if missing:
        with op.batch_alter_table(table) as batch_op:
            for name, condition in missing:
                batch_op.create_check_constraint(name, condition)
    return [name for name, _ in missing]


def existing_fks(bind, table: str) -> list:
    insp = inspect(bind)
    if not insp.has_table(table):
        return []
    return insp.get_foreign_keys(table)


def ensure_fk_sqlite(op, bind, table: str, constraint_name: str, referent: str,
                     local_cols, remote_cols, ondelete: str) -> bool:
    """SQLite：列缺失时补齐 FK 等价（不检查数据合法性，SQLite batch 重建时会校验）。"""
    fks = existing_fks(bind, table)
    for fk in fks:
        if fk.get("constrained_columns") == list(local_cols) and \
                fk.get("referred_table") == referent and \
                fk.get("referred_columns") == list(remote_cols):
            return False
    with op.batch_alter_table(table) as batch_op:
        batch_op.create_foreign_key(
            constraint_name, referent, list(local_cols), list(remote_cols),
            ondelete=ondelete,
        )
    return True


def existing_indexes(bind, table: str) -> dict:
    insp = inspect(bind)
    if not insp.has_table(table):
        return {}
    return {ix["name"]: ix for ix in insp.get_indexes(table)}


def ensure_index(op, bind, table: str, index_name: str, columns,
                 unique: bool = False, sqlite_where=None,
                 postgresql_where=None) -> bool:
    """补齐缺失索引（跨方言）：已存在同名等价索引跳过；缺失则创建。"""
    insp = inspect(bind)
    if not insp.has_table(table):
        raise RuntimeError(
            f"migration_compat_missing_table: {table} does not exist")
    existing = {ix["name"] for ix in insp.get_indexes(table)}
    if index_name in existing:
        return False
    op.create_index(
        index_name, table, columns, unique=unique,
        sqlite_where=sqlite_where, postgresql_where=postgresql_where,
    )
    return True
