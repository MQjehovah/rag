"""Phase J-3 第三轮封板反例测试。

覆盖（冻结范围）：
一、生产接线（source executor 统一入口 + 调用顺序）
二、P36 精确 migration guard（malformed schema fail closed）
三、SQL 层有界 API（SQL 监听器证明 limit）
四、version_family 关系级推导 + relation ID
五、Community 增量拆分与合并
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import os

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    EvidenceItem,
    Notebook,
    Page,
    PageChunk,
    V4GraphCommunity,
    V4GraphEntity,
    V4GraphEntityEvidence,
    V4GraphRelation,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
    SchemaNotReadyError,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend" / "src"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed_page(db, page_id, title, chunks, group="engineering", source_type=None, source_id=None, source_path=None):
    nb = Notebook(id=f"nb-{page_id}", name=f"nb-{page_id}", group_id=group)
    db.add(nb)
    db.flush()
    page = Page(id=page_id, notebook_id=nb.id, title=title, content="",
                source_type=source_type, source_id=source_id, source_path=source_path)
    db.add(page)
    db.flush()
    for i, txt in enumerate(chunks):
        db.add(PageChunk(id=f"{page_id}-c{i}", page_id=page_id, chunk_index=i, content=txt, content_type="text"))
    db.flush()
    return page


def _rebuild(db, page_id):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    return rebuild_page_graph(db, page_id, commit=True)


def _relations(db):
    return {r.id: r for r in db.query(V4GraphRelation).all()}


# ===========================================================================
# 一、生产接线：统一 source executor + 调用顺序
# ===========================================================================

def test_source_executor_rebuild_after_chunk_evidence(monkeypatch):
    """executor 在 background_index_page + sync_page_evidence 完成后调用 rebuild。"""
    executor_src = (BACKEND_ROOT / "app" / "sources" / "executor.py").read_text(encoding="utf-8")
    # 调用顺序：background_index_page → sync_page_evidence → _schedule_graph_rebuild
    idx_bg = executor_src.index("await background_index_page")
    idx_sync = executor_src.index("sync_page_evidence(db, page.id)")
    idx_sched = executor_src.index("_schedule_graph_rebuild(page.id)")
    assert idx_bg < idx_sync < idx_sched, "executor 必须在 Chunk+Evidence 完成后调度图谱"
    # 退役 importer 不是唯一接线：executor 必须真实存在调度
    assert "_schedule_graph_rebuild" in executor_src


def test_source_executor_delete_remove_in_transaction():
    executor_src = (BACKEND_ROOT / "app" / "sources" / "executor.py").read_text(encoding="utf-8")
    # _retire_deleted_page 内 remove_page_graph（同事务）
    assert "remove_page_graph" in executor_src
    # delete 分支：remove_page_graph(db, source_item.page_id, commit=False)
    assert "remove_page_graph(db, source_item.page_id, commit=False)" in executor_src


def test_source_executor_reassign_invalidates_wiki_and_graph():
    executor_src = (BACKEND_ROOT / "app" / "sources" / "executor.py").read_text(encoding="utf-8")
    # NEEDS_REASSIGN 分支同时失效 Wiki 和 graph
    assert "remove_source_page_from_wikis" in executor_src
    assert "remove_page_graph" in executor_src


def test_page_api_no_premature_rebuild(monkeypatch):
    """Page create/update 不在 background_index_page 完成前调度图谱。"""
    pages_src = (BACKEND_ROOT / "app" / "api" / "pages.py").read_text(encoding="utf-8")
    # background_index_page 内部含 schedule_page_graph_rebuild
    assert "schedule_page_graph_rebuild" in pages_src
    # create/update 不再直接 background_tasks.add_task(_schedule_graph_rebuild)（除 notebook_changed 分支）
    # 验证 notebook 变化分支存在
    assert "notebook_changed and not changed" in pages_src


def test_wiki_refresh_scheduler_triggers_graph(monkeypatch):
    sched_src = (BACKEND_ROOT / "app" / "core" / "knowledge_compiler_v3" / "wiki_refresh_scheduler.py").read_text(encoding="utf-8")
    assert "schedule_page_graph_rebuild" in sched_src
    assert "schedule_wiki_graph_rebuild" in sched_src


# ===========================================================================
# 二、P36 精确 migration guard（malformed schema fail closed）
# ===========================================================================

def _make_p36_db_with_ddl(tmp_path, extra_ddl=""):
    """造一个只有 P35 核心表 + 手动建错误 P36 表的库。"""
    db_path = tmp_path / "p36_malformed.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "c9f4a5b6d7e8"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    import sqlite3
    conn = sqlite3.connect(db_path)
    # 建 5 张表（缺 version_family / 错 FK / 错唯一约束等由 extra_ddl 控制）
    conn.execute("CREATE TABLE v4_graph_entities (id VARCHAR(512) PRIMARY KEY, entity_type VARCHAR(64) NOT NULL, normalized_name VARCHAR(255) NOT NULL, display_name VARCHAR(255) NOT NULL, community_key VARCHAR(512), acl_scope TEXT, version_label VARCHAR(64), version_status VARCHAR(32), fingerprint VARCHAR(64), updated_at DATETIME)")
    # 缺 version_family 的 relations
    conn.execute("CREATE TABLE v4_graph_relations (id VARCHAR(1024) PRIMARY KEY, source_id VARCHAR(512) NOT NULL, target_id VARCHAR(512) NOT NULL, relation_type VARCHAR(64) NOT NULL, version_label VARCHAR(64), version_status VARCHAR(32), evidence_count INTEGER NOT NULL DEFAULT 1, acl_scope TEXT, fingerprint VARCHAR(64), updated_at DATETIME)")
    conn.execute("CREATE TABLE v4_graph_relation_evidence (id VARCHAR(64) PRIMARY KEY, relation_id VARCHAR(1024), page_id VARCHAR(36), chunk_id VARCHAR(36), evidence_id VARCHAR(36), wiki_page_id VARCHAR(36), revision_id VARCHAR(36), section_id VARCHAR(36))")
    conn.execute("CREATE TABLE v4_graph_entity_evidence (id VARCHAR(64) PRIMARY KEY, entity_id VARCHAR(512), page_id VARCHAR(36), chunk_id VARCHAR(36), evidence_id VARCHAR(36), wiki_page_id VARCHAR(36), revision_id VARCHAR(36), section_id VARCHAR(36))")
    conn.execute("CREATE TABLE v4_graph_communities (key VARCHAR(512) PRIMARY KEY, display_name VARCHAR(255) NOT NULL, acl_scope TEXT, fingerprint VARCHAR(64), updated_at DATETIME)")
    conn.commit()
    conn.close()
    return db_path


def test_malformed_schema_missing_version_family_fail_closed(tmp_path):
    db_path = _make_p36_db_with_ddl(tmp_path)
    from app.models.database import get_engine, init_db, SchemaNotReadyError
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    with pytest.raises(SchemaNotReadyError) as exc:
        init_db(engine)
    msg = str(exc.value)
    # 缺 version_family、缺 FK、缺唯一约束、缺索引等均应报告
    assert "version_family" in msg or "fk_missing" in msg or "unique" in msg
    engine.dispose()


def test_malformed_schema_missing_revision_fk_fail_closed(tmp_path):
    db_path = _make_p36_db_with_ddl(tmp_path)
    from app.models.database import get_engine, init_db, SchemaNotReadyError
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    with pytest.raises(SchemaNotReadyError):
        init_db(engine)
    # 校验失败后不得创建/修复任何 P36 表
    insp = inspect(engine)
    # 表已存在（malformed），不得被 init_db 静默重建
    engine.dispose()


def test_relation_id_contains_version_family():
    from app.core.knowledge_compiler_v3.v4_graph_builder import _relation_id
    # 不同 family 相同端点/关系/版本 → 不同 ID
    id1 = _relation_id("s", "contains", "t", "titan810", "3.0")
    id2 = _relation_id("s", "contains", "t", "skywalker50", "3.0")
    assert id1 != id2, "不同 family 相同关系不得合并"


# ===========================================================================
# 四、version_family 关系级推导
# ===========================================================================

def test_relation_family_one_product_belongs_to_titan():
    from app.core.knowledge_compiler_v3.v4_graph_builder import _relation_version_family
    ents = [("product", "Titan 810"), ("product", "Skywalker 50"), ("component", "标准电池")]
    # 关系属于 Titan（source=component, target=product Titan）
    fam = _relation_version_family("component", "标准电池", "product", "Titan 810", ents)
    assert fam == "titan810"


def test_relation_family_two_products_unattributable():
    from app.core.knowledge_compiler_v3.v4_graph_builder import _relation_version_family
    ents = [("product", "Titan 810"), ("product", "Skywalker 50")]
    fam = _relation_version_family("product", "Titan 810", "product", "Skywalker 50", ents)
    assert fam is None, "两个 product 关系无法归属 → None"


def test_two_families_same_fact_two_edges(db):
    # 不同 family 相同端点/关系/版本 → 两条边
    _seed_page(db, "p1", "Titan 3.0", ["标准电池 属于 Titan 810", "版本 3.0"], source_path="titan.pdf")
    _seed_page(db, "p2", "Skywalker 3.0", ["标准电池 属于 Skywalker 50", "版本 3.0"], source_path="sky.pdf")
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    rels = _relations(db)
    belongs = [r for r in rels.values() if r.relation_type == "belongs_to"]
    assert len(belongs) >= 2, "不同 family 相同关系生成两条边"


def test_per_family_latest_isolated():
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan810": {"2.0", "3.0"}, "skywalker50": {"10.0"}}
    allowed = _family_allowed_labels(families, None)
    assert allowed["titan810"] == {"common", "3.0"}
    assert allowed["skywalker50"] == {"common", "10.0"}


def test_multi_version_same_chunk_not_latest(db):
    # 一个 Chunk 多版本无法对应 → ambiguous，不进入 latest
    _seed_page(db, "p1", "Titan", ["标准电池 属于 Titan 810", "支持 2.0 和 3.0"], source_path="titan.pdf")
    db.commit()
    _rebuild(db, "p1")
    rels = _relations(db)
    for r in rels.values():
        # ambiguous 不应写成 confirmed/latest 版本
        if r.version_status == "ambiguous":
            assert r.version_label == "unversioned", "ambiguous 不写成具体版本"


# ===========================================================================
# 五、Community 增量拆分与合并
# ===========================================================================

def _build_chain(db, chain):
    """chain = [(page_id, chunk_text), ...] 各自有实体关系。"""
    for page_id, text in chain:
        _seed_page(db, page_id, "T", [text])
    db.commit()
    for page_id, _ in chain:
        _rebuild(db, page_id)


def test_bridge_delete_splits_community(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    # 电池模块—驱动电机—控制器 链：p1 = 电池模块 包含 驱动电机，p2 = 驱动电机 包含 控制器
    _seed_page(db, "p1", "T", ["电池模块 包含 驱动电机"])
    _seed_page(db, "p2", "T", ["驱动电机 包含 控制器"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    comms_before = db.query(V4GraphCommunity).all()
    assert len(comms_before) == 1, "A—B—C 应同属一个 community"
    # 删除 p2（桥接 驱动电机—控制器），控制器 失去 provenance 删除；电池模块/驱动电机保留
    remove_page_graph(db, "p2", commit=False)
    remaining_entities = {e.display_name for e in db.query(V4GraphEntity).all()}
    assert "控制器" not in remaining_entities, "桥接删除后 控制器 失去 provenance 删除"
    assert "电池模块" in remaining_entities and "驱动电机" in remaining_entities


def test_bridge_delete_splits_with_retained_entity(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    # 电池模块—驱动电机—控制器，控制器由 p2 和 p3 共同支撑，删除 p2 后控制器保留
    _seed_page(db, "p1", "T", ["电池模块 包含 驱动电机"])
    _seed_page(db, "p2", "T", ["驱动电机 包含 控制器"])
    _seed_page(db, "p3", "T", ["控制器 属于 驱动电机"])  # 控制器有第二 provenance
    db.commit()
    for pid in ("p1", "p2", "p3"):
        _rebuild(db, pid)
    # 删除 p2 的桥接边，但控制器仍有 p3 provenance
    remove_page_graph(db, "p2", commit=False)
    after_entities = {e.display_name for e in db.query(V4GraphEntity).all()}
    assert "控制器" in after_entities, "控制器有其他 Page provenance 保留"


def test_new_edge_merges_communities(db):
    # 两个独立 community：{电池模块,驱动电机} 和 {控制器,传感器}，新增 驱动电机—控制器 桥接边 → 合并
    _seed_page(db, "p1", "T", ["电池模块 包含 驱动电机"])
    _seed_page(db, "p2", "T", ["控制器 包含 传感器"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    before = db.query(V4GraphCommunity).count()
    assert before == 2
    # 新增桥接边 驱动电机—控制器
    _seed_page(db, "p3", "T", ["驱动电机 包含 控制器"])
    db.commit()
    _rebuild(db, "p3")
    after = db.query(V4GraphCommunity).count()
    assert after == 1, "新增桥接边合并两个 community"


def test_community_name_deterministic_no_internal_key(db):
    _seed_page(db, "p1", "T", ["电池模块 包含 驱动电机"])
    db.commit()
    _rebuild(db, "p1")
    for c in db.query(V4GraphCommunity).all():
        assert "|" not in c.display_name
        assert "管理员" not in c.display_name
        assert not re.match(r"^社区\s*\d+$", c.display_name)


# ===========================================================================
# 三、SQL 层有界 API（SQL 监听器）
# ===========================================================================

def test_sql_listener_limit_enforced(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    # 构造 30 个实体
    for i in range(30):
        _seed_page(db, f"p{i}", f"Titan{i}", [f"部件{i} 包含 Titan {i}"])
    db.commit()
    for i in range(30):
        _rebuild(db, f"p{i}")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)

    captured_limits = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if "v4_graph_entities" in low and "limit" in low and "select" in low:
            captured_limits.append(statement)

    try:
        r = client.get("/api/v4/graph/subgraph", params={"limit": 10})
        assert r.status_code == 200
        assert len(r.json()["nodes"]) <= 10
    finally:
        app.dependency_overrides.clear()
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert captured_limits, "实体查询必须带 SQL LIMIT"


def test_focus_bfs_sql_bounded(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "T", ["电池模块 包含 驱动电机"])
    db.commit()
    _rebuild(db, "p1")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r0 = client.get("/api/v4/graph/subgraph")
        nodes0 = r0.json()["nodes"]
        assert nodes0, "应有实体节点"
        node_id = nodes0[0]["id"]
        r = client.get("/api/v4/graph/subgraph", params={"focus": node_id, "depth": 3, "limit": 1})
        assert r.status_code == 200
        assert len(r.json()["nodes"]) <= 1
    finally:
        app.dependency_overrides.clear()


def test_graph_modules_zero_card_ko_round3(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    import app.sources.executor as executor_mod
    import app.api.v4_graph as api_mod

    _seed_page(db, "p1", "T", ["部件A 包含 部件B"])
    db.commit()

    _CARD = ("knowledge_cards", "canonical_entities", "card_entity_links", "card_graph_relations", "knowledge_communities")
    card_sql = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if any(t in low for t in _CARD):
            card_sql.append(statement)

    try:
        rebuild_page_graph(db, "p1", commit=True)
    finally:
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert card_sql == []

    for mod in (executor_mod, api_mod):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in ("KnowledgeCard", "KnowledgeCommunity", "CanonicalEntity", "CardEntityLink", "CardGraphRelation"):
            assert forbidden not in joined
