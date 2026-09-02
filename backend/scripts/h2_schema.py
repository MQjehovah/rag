"""Phase H-2 共享 schema 基线（只读，供 preflight/postflight 复用）。

冻结的批准基线（来自真实库只读诊断 + P33 migration 定义）：
- P31 版本、P32 漂移形态（六字段已补、users 表/索引已建、三 debt 索引缺失）；
- 21 张待删表 + 3 个待删字段；
- 19 张保留表；
- 关键保留表行数基线；
- 6 条旧债务分类基线（migrated=1 / no_scope=4 / empty_query=1）；
- card_v3_enabled=1 必须存在（批准基线）。

本模块只包含常量与只读 fingerprint 采集函数，不 import 任何业务模块。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# 批准基线：Alembic 版本（P31）。
EXPECTED_VERSION = "b8e9f0a1b2c3"

# reconciliation 后版本（postflight reconciled stage）。
RECONCILED_VERSION = "a8b9c0d1e2f3"

# P33 后版本（postflight p33 stage）。
P33_VERSION = "d1e2f3a4b5c6"

# P32 声明的新列：名称 -> 精确 SQLite 声明类型（PRAGMA table_info.type）。
P32_COLUMNS = {
    "original_query": "TEXT",
    "normalized_query": "VARCHAR(255)",
    "cluster_key": "VARCHAR(255)",
    "affected_user_count": "INTEGER",
    "scope_id": "VARCHAR(255)",
    "retrieval_reason": "VARCHAR(50)",
}

# knowledge_debt_users 精确列：名称 -> (类型, nullable, primary_key)。
DEBT_USERS_COLUMNS = {
    "id": ("VARCHAR(36)", False, True),
    "debt_id": ("VARCHAR(36)", False, False),
    "user_id": ("VARCHAR(255)", False, False),
}

# knowledge_debt_users.debt_id 结构化外键基线（PRAGMA foreign_key_list 判定）。
# 键名与 foreign_keys() 返回的 dict 字段一致。
DEBT_USERS_FK = {
    "from": "debt_id",
    "table": "knowledge_debts",
    "to": "id",
    "on_delete": "CASCADE",
}

# users 精确列：名称 -> (类型, nullable, primary_key)。来自真实库只读诊断。
USERS_COLUMNS = {
    "id": ("VARCHAR(36)", False, True),
    "username": ("VARCHAR(255)", False, False),
    "email": ("VARCHAR(255)", True, False),
    "display_name": ("VARCHAR(255)", True, False),
    "is_local": ("BOOLEAN", True, False),
    "password_hash": ("VARCHAR(255)", True, False),
    "is_active": ("BOOLEAN", True, False),
    "created_at": ("DATETIME", True, False),
    "updated_at": ("DATETIME", True, False),
}

# knowledge_debt_users 三个索引：名称 -> (列及顺序, unique)。
DEBT_USERS_INDEXES = {
    "ix_knowledge_debt_users_debt_id": (["debt_id"], False),
    "ix_knowledge_debt_users_user_id": (["user_id"], False),
    "ux_kdu_debt_user": (["debt_id", "user_id"], True),
}

# 三个 debt 索引（漂移形态应恰好缺失；reconciliation 后应精确存在）。
DEBT_INDEXES = {
    "ix_knowledge_debts_normalized_query": (["normalized_query"], False),
    "ux_knowledge_debts_cluster_key": (["cluster_key"], True),
    "ix_knowledge_debts_scope_id": (["scope_id"], False),
}

# 21 张待删表（P33 _DROP_TABLES 顺序）。
DROP_TABLES = [
    "knowledge_debt_cards", "knowledge_debt_queries", "debt_candidates",
    "community_members", "community_rebuild_jobs", "wiki_citations",
    "card_compile_reports", "evidence_links", "card_graph_relations",
    "card_entity_links", "entity_aliases", "conflict_tasks", "query_logs",
    "knowledge_card_sources", "knowledge_claims", "knowledge_card_blocks",
    "knowledge_card_revisions", "knowledge_cards", "canonical_entities",
    "knowledge_communities", "graph_edges",
]

# 3 个待删字段（表 -> [列名]）。
DROP_COLUMNS = {
    "knowledge_debts": ["resolution_card_id", "resolution_evidence_id"],
    "pages": ["compile_hash"],
}

# 19 张保留表。
KEEP_TABLES = {
    "notebooks", "pages", "page_chunks", "users", "user_groups",
    "vision_analysis_jobs", "wiki_pages", "wiki_revisions", "wiki_sections",
    "wiki_links", "knowledge_debts", "knowledge_debt_users",
    "evidence_items", "asset_observations", "runtime_feature_flags",
    "source_connections", "source_items", "source_sync_runs", "source_sync_errors",
}

# 关键保留表行数基线（批准值）。
ROW_BASELINE = {
    "pages": 144,
    "page_chunks": 5403,
    "wiki_pages": 1,
    "wiki_revisions": 3,
    "knowledge_debts": 6,
    "evidence_items": 5403,
    "source_connections": 1,
    "notebooks": 1,
}

# 债务总数基线（必须等于 6）。
DEBT_COUNT_BASELINE = 6

# 债务分类基线（reconciliation 后）：migrated / no_scope / empty_query。
DEBT_CLASSIFICATION_BASELINE = {
    "migrated": 1,
    "no_scope": 4,
    "empty_query": 1,
}

# 已移除的 8 个 legacy flag（P33 后应消失）。
LEGACY_FLAGS = (
    "card_v3_enabled", "unified_retrieval_enabled", "debt_chat_enabled",
    "card_graph_enabled", "layered_retrieval_enabled",
    "source_card_compile_enabled", "legacy_debt_card_enabled", "legacy_readonly",
)

# 保留的有效 flag（P33 后应保留）。
KEEP_FLAGS = (
    "wiki_topic_enabled", "source_hub_enabled",
    "dingtalk_connector_enabled", "gitlab_connector_enabled",
)


def _readonly_conn(path: str) -> sqlite3.Connection:
    p = Path(path).expanduser().resolve()
    if not p.exists() or p.is_dir():
        raise SystemExit(f"错误：数据库文件不存在或为目录：{p}")
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def table_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}


def index_names(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def index_defs(conn, table: str) -> dict[str, dict]:
    """返回 table 的非 sqlite_autoindex 索引：名称 -> {cols, unique}。"""
    out = {}
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name = row[1]
        if name.startswith("sqlite_autoindex"):
            continue
        unique = bool(row[2])
        cols = [c[2] for c in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        out[name] = {"cols": cols, "unique": unique}
    return out


def column_defs(conn, table: str) -> dict[str, dict]:
    """返回 table 的列定义：名称 -> {type, notnull, pk}。"""
    out = {}
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        out[row[1]] = {"type": row[2], "notnull": row[3], "pk": row[5]}
    return out


def foreign_keys(conn, table: str) -> list[dict]:
    """结构化读取 table 的外键（PRAGMA foreign_key_list）。

    绝不依赖建表 SQL 字符串匹配 ON DELETE CASCADE。
    返回 [{"table", "from", "to", "on_update", "on_delete"}, ...]。
    """
    out = []
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        # foreign_key_list 列：id, seq, table, from, to, on_update, on_delete, match
        out.append({
            "table": row[2],
            "from": row[3],
            "to": row[4],
            "on_update": row[5],
            "on_delete": row[6],
        })
    return out


def has_cascade_fk(conn, table: str, from_col: str, to_table: str, to_col: str) -> bool:
    """结构化判定 table.from_col 是否存在 to_table.to_col 的 ON DELETE CASCADE 外键。"""
    for fk in foreign_keys(conn, table):
        if (fk["from"] == from_col and fk["table"] == to_table
                and fk["to"] == to_col
                and (fk["on_delete"] or "").upper() == "CASCADE"):
            return True
    return False


def flag_baseline(conn) -> dict[str, bool]:
    """采集 runtime_feature_flags 完整 name→enabled 状态（不含 updated_by 等身份信息）。"""
    if "runtime_feature_flags" not in table_names(conn):
        return {}
    return {
        r[0]: bool(r[1])
        for r in conn.execute("SELECT name, enabled FROM runtime_feature_flags").fetchall()
    }


def alembic_version(conn) -> str | None:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def row_counts(conn, tables) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def debt_classification(conn) -> dict[str, int]:
    """按 retrieval_reason 分类债务（不含敏感正文）。"""
    counts = {"migrated": 0, "no_scope": 0, "empty_query": 0}
    for row in conn.execute(
        "SELECT retrieval_reason, COUNT(*) FROM knowledge_debts GROUP BY retrieval_reason"
    ).fetchall():
        reason, cnt = row
        if reason == "missing_knowledge":
            counts["migrated"] += cnt
        elif reason == "legacy_unmigratable_no_scope":
            counts["no_scope"] += cnt
        elif reason == "legacy_unmigratable_empty_query":
            counts["empty_query"] += cnt
    return counts


def fingerprint(conn) -> dict:
    """采集不含敏感正文的 schema fingerprint + 行数基线，供 compare。"""
    return {
        "version": alembic_version(conn),
        "p32_columns": column_defs(conn, "knowledge_debts"),
        "debt_users_columns": column_defs(conn, "knowledge_debt_users"),
        "debt_users_indexes": index_defs(conn, "knowledge_debt_users"),
        "debt_indexes": index_defs(conn, "knowledge_debts"),
        "tables": sorted(table_names(conn)),
        "row_counts": row_counts(conn, sorted(ROW_BASELINE.keys())),
        "debt_classification": debt_classification(conn),
        "flags": flag_baseline(conn),
    }


def dump_baseline(conn, db_path: str) -> dict:
    """生成 baseline JSON（不含敏感正文）。"""
    return {"db": db_path, "fingerprint": fingerprint(conn)}
