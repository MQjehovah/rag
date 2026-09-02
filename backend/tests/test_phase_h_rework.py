"""V4 Phase H-1 返工封板补强测试。

覆盖：P32 schema 漂移 reconciliation、旧债务回填/隔离、P33 不可逆 downgrade、
废弃 flag 清理、P33 保留表/索引/约束、备份恢复一致性/篡改/并发 WAL、
脚本 smoke test、PostgreSQL 删除顺序依赖图。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import init_db

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_DROP_TABLES = {
    "knowledge_cards", "knowledge_card_blocks", "knowledge_card_revisions",
    "knowledge_card_sources", "knowledge_claims", "card_compile_reports",
    "card_entity_links", "card_graph_relations", "canonical_entities",
    "entity_aliases", "knowledge_communities", "community_members",
    "community_rebuild_jobs", "wiki_citations", "conflict_tasks",
    "query_logs", "debt_candidates", "knowledge_debt_queries",
    "knowledge_debt_cards", "evidence_links", "graph_edges",
}
_KEEP_TABLES = {
    "notebooks", "pages", "page_chunks", "users", "user_groups",
    "vision_analysis_jobs", "wiki_pages", "wiki_revisions", "wiki_sections",
    "wiki_links", "knowledge_debts", "knowledge_debt_users",
    "evidence_items", "asset_observations", "runtime_feature_flags",
    "source_connections", "source_items", "source_sync_runs", "source_sync_errors",
}


def _alembic(cmd, db_path, *extra):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}",
         cmd, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BACKEND_ROOT),
    )


def _build_drift_db(tmp_path, insert_debts=False):
    """构造 P31 漂移库：P32 列和 users 表存在，但三个 debt 索引缺失。"""
    db_path = tmp_path / "drift.db"
    _alembic("upgrade", db_path, "b8e9f0a1b2c3")
    conn = sqlite3.connect(str(db_path))
    for ddl in [
        "ALTER TABLE knowledge_debts ADD COLUMN original_query TEXT",
        "ALTER TABLE knowledge_debts ADD COLUMN normalized_query VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN cluster_key VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN affected_user_count INTEGER",
        "ALTER TABLE knowledge_debts ADD COLUMN scope_id VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN retrieval_reason VARCHAR(50)",
    ]:
        conn.execute(ddl)
    conn.execute(
        "CREATE TABLE knowledge_debt_users ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
        "user_id VARCHAR(255) NOT NULL, "
        "FOREIGN KEY(debt_id) REFERENCES knowledge_debts (id) ON DELETE CASCADE)"
    )
    if insert_debts:
        debt = [
            ("d1", "no_answer", "如何给 Titan 810 换轮胎", None),
            ("d2", "no_answer", "Titan 810 标准电池是多少?", None),
            ("d3", "no_answer", "如何安装 Skywalker50 电池", None),
            ("d4", "low_score", "如何给 Titan 810 换轮胎", None),
            ("d5", "no_evidence", "", None),
            ("d6", "missing_knowledge", "nihao", '{"groups": ["__local_admin__"]}'),
        ]
        for did, dtype, q, acl in debt:
            conn.execute(
                "INSERT INTO knowledge_debts (id, debt_type, description, related_question, acl_scope, status) "
                "VALUES (?, ?, ?, ?, ?, 'open')",
                (did, dtype, dtype, q, acl),
            )
    conn.commit()
    conn.close()
    _alembic("stamp", db_path, "c5e6f7a8b9d0")
    return db_path


def test_reconciliation_repairs_drift_and_backfills(tmp_path):
    db_path = _build_drift_db(tmp_path, insert_debts=True)
    r = _alembic("upgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    debt_idx = {r0[1] for r0 in conn.execute("PRAGMA index_list(knowledge_debts)").fetchall()}
    for name in ("ix_knowledge_debts_normalized_query", "ux_knowledge_debts_cluster_key", "ix_knowledge_debts_scope_id"):
        assert name in debt_idx, f"缺 debt 索引 {name}"
    users_idx = {r0[1] for r0 in conn.execute("PRAGMA index_list(knowledge_debt_users)").fetchall()}
    for name in ("ix_knowledge_debt_users_debt_id", "ix_knowledge_debt_users_user_id", "ux_kdu_debt_user"):
        assert name in users_idx, f"缺 users 索引 {name}"
    d6 = conn.execute("SELECT normalized_query, scope_id, cluster_key, retrieval_reason FROM knowledge_debts WHERE id='d6'").fetchone()
    assert d6[0] == "nihao" and d6[1] == "admin" and d6[2] and d6[3] == "missing_knowledge"
    d1 = conn.execute("SELECT normalized_query, scope_id, retrieval_reason FROM knowledge_debts WHERE id='d1'").fetchone()
    assert d1[0] is None and d1[1] is None and d1[2] == "legacy_unmigratable_no_scope"
    d5 = conn.execute("SELECT retrieval_reason FROM knowledge_debts WHERE id='d5'").fetchone()
    assert d5[0] == "legacy_unmigratable_empty_query"
    conn.close()


def test_reconciliation_detects_duplicate_cluster_key(tmp_path):
    db_path = _build_drift_db(tmp_path, insert_debts=False)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO knowledge_debts (id, debt_type, description, cluster_key) VALUES ('x1','x','x','dup')")
    conn.execute("INSERT INTO knowledge_debts (id, debt_type, description, cluster_key) VALUES ('x2','x','x','dup')")
    conn.commit()
    conn.close()
    r = _alembic("upgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode != 0
    assert "重复 cluster_key" in (r.stderr + r.stdout)


def test_p33_removes_legacy_flags_and_keeps_valid(tmp_path):
    db_path = tmp_path / "flags.db"
    _alembic("upgrade", db_path, "b8e9f0a1b2c3")
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('card_v3_enabled', 1)")
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('dingtalk_connector_enabled', 1)")
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('source_hub_enabled', 1)")
    conn.commit()
    conn.close()
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    rows = {r0[0]: r0[1] for r0 in conn.execute("SELECT name, enabled FROM runtime_feature_flags").fetchall()}
    assert "card_v3_enabled" not in rows
    assert "dingtalk_connector_enabled" in rows
    assert "source_hub_enabled" in rows
    conn.close()


def test_p33_preserves_v4_tables_indexes_constraints(tmp_path):
    db_path = tmp_path / "full.db"
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(str(db_path))
    tables = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert tables & _DROP_TABLES == set(), f"旧表残留：{tables & _DROP_TABLES}"
    assert _KEEP_TABLES <= tables, f"保留表缺失：{_KEEP_TABLES - tables}"
    debt_idx = {r0[1]: r0[2] for r0 in conn.execute("PRAGMA index_list(knowledge_debts)").fetchall()}
    assert debt_idx.get("ux_knowledge_debts_cluster_key") == 1
    fk_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_debt_users'").fetchone()[0]
    assert "ON DELETE CASCADE" in fk_sql
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_p33_downgrade_rejected_and_version_unchanged(tmp_path):
    db_path = tmp_path / "downgrade.db"
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr
    r = _alembic("downgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode != 0
    assert "不可逆" in (r.stderr + r.stdout)
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "d1e2f3a4b5c6"
    conn.close()


def test_scripts_smoke_no_legacy_imports():
    scripts_dir = BACKEND_ROOT / "scripts"
    forbidden_fragments = (
        "knowledge_compiler_v3.card", "knowledge_compiler_v3.claims",
        "knowledge_compiler_v3.cluster", "knowledge_compiler_v3.persistence",
        "knowledge_compiler_v3.pipeline", "knowledge_compiler_v3.quality",
        "knowledge_compiler_v3.wiki_builder", "knowledge_compiler_v3.wiki_links",
        "knowledge_compiler_v3.worthiness", "knowledge_compiler_v3.consolidation",
        "knowledge_compiler_v3.derivatives", "knowledge_compiler_v3.diff",
        "knowledge_compiler_v3.matcher", "knowledge_compiler_v3.migration",
        "knowledge_compiler_v3.entity_resolution", "knowledge_compiler_v3.graph_retrieval",
        "knowledge_compiler_v3.community", "knowledge_compiler.conflict_center",
        "knowledge.debt", "knowledge.quality_metrics", "knowledge.root_cause",
        "legacy_ko_inventory", "retrieval.qa", "retrieval.pipeline", "retrieval.dense",
        "evidence_validator", "core.graph",
    )
    for p in sorted(scripts_dir.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
        bad = [m for m in mods if any(frag in m for frag in forbidden_fragments)]
        assert bad == [], f"{p.name} 不应 import 已删除模块：{bad}"


def test_backup_restore_manifest_tamper_rejected(tmp_path):
    db_file = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(engine)
    engine.dispose()
    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")
    r = subprocess.run(
        [sys.executable, script, "backup", str(db_file), "-o", str(tmp_path / "bk")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    bdir = next((tmp_path / "bk").iterdir())
    mf = bdir / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["files"][0]["sha256"] = "0" * 64
    mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    restore_target = tmp_path / "restored.db"
    r = subprocess.run(
        [sys.executable, script, "restore", str(bdir), str(restore_target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode != 0
    assert "SHA-256" in (r.stderr + r.stdout)
    assert not restore_target.exists()


def test_backup_restore_concurrent_wal_snapshot(tmp_path):
    db_file = tmp_path / "wal.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(engine)
    s = sessionmaker(bind=engine)()
    from app.models.database import KnowledgeDebt
    for i in range(100):
        s.add(KnowledgeDebt(id=f"d{i}", debt_type="missing_knowledge", description="d",
                            original_query=f"q{i}", normalized_query=f"q{i}",
                            scope_id="company", occurrence_count=1))
        s.commit()
    s.close()
    engine.dispose()
    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")
    r = subprocess.run(
        [sys.executable, script, "backup", str(db_file), "-o", str(tmp_path / "bk2")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    bdir = next((tmp_path / "bk2").iterdir())
    snapshot = bdir / "wal.db"
    conn = sqlite3.connect(str(snapshot))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_debts").fetchone()[0] == 100
    conn.close()


def test_postgres_drop_order_respects_fk():
    spec = importlib.util.spec_from_file_location(
        "p33", str(BACKEND_ROOT / "alembic" / "versions" / "d1e2f3a4b5c6_p33_drop_card_ko_tables.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    order = mod._DROP_TABLES
    deps = {
        "knowledge_debt_queries": ["knowledge_debts", "query_logs"],
        "knowledge_debt_cards": ["knowledge_debts"],
        "community_members": ["knowledge_communities", "canonical_entities"],
        "entity_aliases": ["canonical_entities"],
        "card_entity_links": ["canonical_entities"],
        "card_graph_relations": ["canonical_entities"],
        "knowledge_card_sources": ["knowledge_cards"],
        "knowledge_claims": ["knowledge_cards", "knowledge_card_revisions"],
        "knowledge_card_blocks": ["knowledge_cards", "knowledge_card_revisions"],
        "knowledge_card_revisions": ["knowledge_cards"],
    }
    pos = {name: i for i, name in enumerate(order)}
    for source, targets in deps.items():
        if source not in pos:
            continue
        for target in targets:
            if target in pos:
                assert pos[source] < pos[target], f"删除顺序错误：{source} 应在 {target} 之前"
    assert len(order) == 21
    assert len(set(order)) == 21
