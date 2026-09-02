"""V4 Phase H：数据库与旧代码清理封板测试。

覆盖：
- 删除对象清单准确（ORM 无旧表/模型/字段）；
- 保留对象清单准确；
- upgrade 后无 Card/KO/owner/review/risk 表；
- init_db 不重建旧表；
- V4 Debt 字段及 knowledge_debt_users 仍存在；
- Wiki/Page/Chunk/Evidence/Source/ACL 数据不丢；
- 默认 Chat/Search/Community/Graph/Debt 无旧模型 import；
- 旧 API 全部 404；
- KNOWN_FLAGS 不含旧 flag；
- app.main 导入不加载旧模块；
- SQLite upgrade/downgrade/备份恢复路径；
- 外键检查与 integrity_check 通过；
- 备份脚本拒绝危险路径和覆盖。

全程使用临时 SQLite，绝不触碰真实库。
"""
from __future__ import annotations

import importlib
import inspect
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import Base, init_db

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 删除对象（表）
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


def test_orm_metadata_drop_and_keep_lists():
    orm_tables = set(Base.metadata.tables.keys())
    assert orm_tables & _DROP_TABLES == set(), f"ORM 仍含旧表：{orm_tables & _DROP_TABLES}"
    assert _KEEP_TABLES <= orm_tables, f"ORM 缺保留表：{_KEEP_TABLES - orm_tables}"


def test_orm_no_old_columns():
    # knowledge_debts 不应再有 resolution_card_id / resolution_evidence_id
    debt_cols = {c.name for c in Base.metadata.tables["knowledge_debts"].columns}
    assert "resolution_card_id" not in debt_cols
    assert "resolution_evidence_id" not in debt_cols
    # pages 不应再有 compile_hash
    page_cols = {c.name for c in Base.metadata.tables["pages"].columns}
    assert "compile_hash" not in page_cols
    # V4 Debt 字段保留
    for col in ("original_query", "normalized_query", "cluster_key",
                "affected_user_count", "scope_id", "retrieval_reason"):
        assert col in debt_cols, f"knowledge_debts 缺 V4 字段 {col}"


def test_known_flags_no_legacy():
    from app.core.feature_flags import KNOWN_FLAGS
    for legacy in ("card_v3_enabled", "unified_retrieval_enabled", "debt_chat_enabled",
                   "card_graph_enabled", "layered_retrieval_enabled",
                   "source_card_compile_enabled", "legacy_debt_card_enabled", "legacy_readonly"):
        assert legacy not in KNOWN_FLAGS, legacy


def test_default_modules_no_legacy_model_import():
    import app.api.chat
    import app.api.search_v2
    import app.api.rag_chat
    import app.api.debts_v4
    import app.api.v4_graph
    import app.core.retrieval.wiki_retriever
    import app.core.retrieval.raw_retriever
    import app.core.retrieval.debt_service
    import app.core.retrieval.community_expansion
    import app.core.knowledge_compiler_v3.page_graph
    import app.core.knowledge_compiler_v3.wiki_page_builder

    forbidden = (
        "KnowledgeCard", "KnowledgeClaim", "KnowledgeCardSource",
        "KnowledgeCardRevision", "KnowledgeCardBlock", "CardEntityLink",
        "CardGraphRelation", "KnowledgeCommunity", "CanonicalEntity",
        "EntityAlias", "KnowledgeObject", "EvidenceLink", "WikiCitation",
    )
    mods = [app.api.chat, app.api.search_v2, app.api.rag_chat, app.api.debts_v4,
            app.api.v4_graph, app.core.retrieval.wiki_retriever,
            app.core.retrieval.raw_retriever, app.core.retrieval.debt_service,
            app.core.retrieval.community_expansion,
            app.core.knowledge_compiler_v3.page_graph,
            app.core.knowledge_compiler_v3.wiki_page_builder]
    for mod in mods:
        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for f in forbidden:
            assert f not in joined, f"{mod.__name__} 不应 import {f}"


def test_app_main_does_not_load_legacy_modules():
    code = (
        "import sys\n"
        "import app.main\n"
        "forbidden = [\n"
        " 'app.api.cards','app.api.conflicts','app.api.governance','app.api.p5_graph',\n"
        " 'app.api.debts','app.api.legacy',\n"
        " 'app.core.knowledge_compiler.conflict_center',\n"
        " 'app.core.knowledge_compiler_v3.pipeline',\n"
        " 'app.core.knowledge_compiler_v3.persistence',\n"
        " 'app.core.knowledge_compiler_v3.card_builder',\n"
        " 'app.core.knowledge_compiler_v3.card_graph',\n"
        " 'app.core.knowledge_compiler_v3.community',\n"
        " 'app.core.knowledge_compiler_v3.wiki_builder',\n"
        " 'app.core.knowledge_compiler_v3.wiki_links',\n"
        " 'app.core.retrieval.qa','app.core.retrieval.pipeline','app.core.retrieval.dense',\n"
        " 'app.core.graph','app.core.evidence_validator','app.core.legacy_ko_inventory',\n"
        "]\n"
        "loaded = [m for m in forbidden if m in sys.modules]\n"
        "assert loaded == [], loaded\n"
        "print('NO_LEGACY_MODULE')\n"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, cwd=str(BACKEND_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "NO_LEGACY_MODULE" in proc.stdout


def test_old_apis_return_404(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(engine)
    from app.api import deps
    # 用 monkeypatch 还原，避免污染 deps._engine 全局状态（影响后续测试）。
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    try:
        for path in ["/api/cards", "/api/cards/c1", "/api/conflicts", "/api/conflicts/scan",
                     "/api/governance/overview", "/api/governance/quality",
                     "/api/p5/graph/graph", "/api/p5/graph/communities",
                     "/api/p5/graph/rebuild", "/api/knowledge/debts",
                     "/api/knowledge/debts/scan"]:
            assert client.get(path).status_code in (404, 405), path
            assert client.post(path, json={}).status_code in (404, 405), path
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _alembic(cmd: str, db_path: Path, *extra: str) -> subprocess.CompletedProcess:
    """用 subprocess 调 alembic，强制 -x database_url 指向临时库，绝不触碰真实库。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}",
         cmd, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BACKEND_ROOT),
    )


def test_alembic_upgrade_downgrade_and_parity(tmp_path):
    db_path = tmp_path / "h.db"

    # 升级到 head（临时库，-x 覆盖 settings.database_url）
    r = _alembic("upgrade", db_path, "head")
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(str(db_path))
    tables = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert tables & _DROP_TABLES == set(), f"upgrade 后仍残留旧表：{tables & _DROP_TABLES}"
    assert _KEEP_TABLES <= tables, f"upgrade 后缺保留表：{_KEEP_TABLES - tables}"
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fk == [], f"外键违规：{fk}"
    conn.close()

    # 用 init_db 跑一次，验证 ORM 与迁移 schema 一致（不重建旧表）
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    engine.dispose()
    conn = sqlite3.connect(str(db_path))
    tables2 = {r0[0] for r0 in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert tables2 & _DROP_TABLES == set()
    conn.close()

    # P33 downgrade 必须安全失败（不可逆迁移），且版本号不得改变。
    r = _alembic("downgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode != 0
    assert "不可逆" in r.stderr or "不可逆" in r.stdout or "cannot" in r.stderr.lower() or "RuntimeError" in r.stderr
    # 版本号仍为 head（d1e2f3a4b5c6），未回退。
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "d1e2f3a4b5c6"
    conn.close()


def test_backup_restore_roundtrip(tmp_path):
    # 造一个含 V4 数据的临时 SQLite
    db_file = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(engine)
    s = sessionmaker(bind=engine)()
    from app.models.database import Notebook, Page, KnowledgeDebt
    nb = Notebook(id="nb1", name="n", group_id="engineering")
    s.add(nb)
    s.flush()
    s.add(Page(id="p1", notebook_id="nb1", title="t", content="c"))
    s.add(KnowledgeDebt(id="d1", debt_type="missing_knowledge", description="d",
                        original_query="水箱容量", normalized_query="水箱容量",
                        scope_id="group:engineering", occurrence_count=1))
    s.commit()
    s.close()
    engine.dispose()

    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")

    # 备份
    r = subprocess.run(
        [sys.executable, script, "backup", str(db_file)], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    # 找到生成的备份目录
    created = [p for p in (db_file.parent / "backups").iterdir() if p.is_dir()]
    assert created, "未生成备份目录"
    bdir = created[0]

    # 恢复到新路径
    restore_target = tmp_path / "restored.db"
    r = subprocess.run(
        [sys.executable, script, "restore", str(bdir), str(restore_target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    assert restore_target.exists()

    # 校验恢复数据
    conn = sqlite3.connect(str(restore_target))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_debts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
    conn.close()


def test_backup_rejects_dangerous_paths(tmp_path):
    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")
    # 拒绝空路径 / 目录 / 不存在文件
    r = subprocess.run([sys.executable, script, "backup", ""], capture_output=True, text=True)
    assert r.returncode != 0
    r = subprocess.run([sys.executable, script, "backup", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode != 0
    r = subprocess.run([sys.executable, script, "backup", str(tmp_path / "nope.db")], capture_output=True, text=True)
    assert r.returncode != 0
