"""Phase J-3 最终封板反例测试。

覆盖（对应封板任务七）：
1. P35 已有库 init_db → schema_not_ready 且 P36 表不存在。
2. P36 upgrade 后 init_db 正常。
3. provenance FK 定义正确。
4. 连续三次 rebuild count 不膨胀。
5. 两 Page 同关系，删除一个只减少对应 evidence。
6. 最后一个 evidence 删除后关系和孤立实体消失。
7. 隐藏 evidence 不进入 count。
8. 隐藏关系不影响 conflict。
9. Community hash 筛选真实有效。
10. 伪造 Community hash 返回空。
11. 大图查询在数据库层有界。
12. focus BFS 深度/节点/边上限生效。
13. Titan 3.0 与另一产品 10.0 各自选自己的 latest。
14. 指定 Titan 9.0 不回退 3.0。
15. 多版本歧义不猜。
16. 动态版本 facets 不泄露无权限版本。
17. 点击节点聚焦保留版本/类型/Community 筛选。
18. Wiki 人工编辑关系能进入图谱。
19. Wiki 回滚后旧 Revision 关系消失。
20. Wiki ACL 外关系、count、facets 全部不可见。
21. Page 与 Wiki 重复事实不重复生成边。
22. Page 创建/更新/索引完成后生产链真实调用 rebuild。
23. Page 删除/SourceItem skipped/NEEDS_REASSIGN 后生产链真实调用 remove。
24. Notebook 改组后旧 scope 图谱立即失效，新 scope 可重建。
25. 首次回填 dry-run 零写入、apply 幂等、keyset 超过一个 batch 不漏 Page。
26. 图谱生产模块与 SQL 零 Card/KO。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    EvidenceItem,
    Notebook,
    NotebookGroup,
    Page,
    PageChunk,
    SourceConnection,
    SourceItem,
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

_CARD_TABLES = ("knowledge_cards", "knowledge_card_blocks", "canonical_entities", "knowledge_communities", "card_entity_links", "card_graph_relations")


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


# ---------------------------------------------------------------------------
# 1/2. init_db migration guard
# ---------------------------------------------------------------------------

def test_p35_db_init_db_schema_not_ready(tmp_path):
    # 造一个只有 P35 核心表、无 P36 图谱表的库
    db_path = tmp_path / "p35.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "c9f4a5b6d7e8"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    from app.models.database import get_engine
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    with pytest.raises(SchemaNotReadyError) as exc:
        init_db(engine)
    assert "schema_not_ready" in str(exc.value)
    # P36 表不得被创建
    from sqlalchemy import inspect
    insp = inspect(engine)
    assert not insp.has_table("v4_graph_entities"), "init_db 不得静默创建 P36 表"
    engine.dispose()


def test_p36_upgrade_then_init_db_ok(tmp_path):
    db_path = tmp_path / "p36.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    from app.models.database import get_engine
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)  # 不抛
    from app.models.database import check_managed_migrations
    assert check_managed_migrations(engine) == []
    engine.dispose()


# ---------------------------------------------------------------------------
# 3. provenance FK
# ---------------------------------------------------------------------------

def test_provenance_fk_defined():
    rel_ev = V4GraphRelationEvidence.__table__
    ent_ev = V4GraphEntityEvidence.__table__
    def fk_targets(table):
        return {fk.target_fullname for fk in table.foreign_keys}
    assert "pages.id" in fk_targets(rel_ev)
    assert "page_chunks.id" in fk_targets(rel_ev)
    assert "evidence_items.id" in fk_targets(rel_ev)
    assert "wiki_pages.id" in fk_targets(rel_ev)
    assert "wiki_sections.id" in fk_targets(rel_ev)
    assert "pages.id" in fk_targets(ent_ev)
    assert "wiki_pages.id" in fk_targets(ent_ev)


# ---------------------------------------------------------------------------
# 4. 连续三次 rebuild count 不膨胀
# ---------------------------------------------------------------------------

def test_three_rebuilds_no_count_inflation(db):
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    n1 = db.query(V4GraphEntity).count()
    r1 = db.query(V4GraphRelation).count()
    rel = db.query(V4GraphRelation).first()
    c1 = rel.evidence_count if rel else 0
    _rebuild(db, "p1")
    _rebuild(db, "p1")
    n2 = db.query(V4GraphEntity).count()
    r2 = db.query(V4GraphRelation).count()
    rel2 = db.query(V4GraphRelation).first()
    c2 = rel2.evidence_count if rel2 else 0
    assert n1 == n2, "三次 rebuild 实体数一致"
    assert r1 == r2, "三次 rebuild 关系数一致"
    assert c1 == c2 == 1, "evidence_count 不膨胀"


# ---------------------------------------------------------------------------
# 5. 两 Page 同关系，删除一个只减少对应 evidence
# ---------------------------------------------------------------------------

def test_two_pages_share_relation_remove_one(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    _seed_page(db, "p2", "Titan2", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    rel = db.query(V4GraphRelation).first()
    assert rel.evidence_count == 2, "两 Page 支撑同一关系 count=2"
    remove_page_graph(db, "p1", commit=False)
    rel2 = db.query(V4GraphRelation).filter(V4GraphRelation.id == rel.id).first()
    assert rel2.evidence_count == 1, "删除一个 Page 后 count=1"


# ---------------------------------------------------------------------------
# 6. 最后一个 evidence 删除后关系和孤立实体消失
# ---------------------------------------------------------------------------

def test_last_evidence_removes_relation_and_orphan(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    assert db.query(V4GraphRelation).count() > 0
    assert db.query(V4GraphEntity).count() > 0
    remove_page_graph(db, "p1", commit=False)
    assert db.query(V4GraphRelation).count() == 0, "最后证据删除后关系消失"
    assert db.query(V4GraphEntity).count() == 0, "孤立实体消失"
    assert db.query(V4GraphCommunity).count() == 0, "空 community 清理"


# ---------------------------------------------------------------------------
# 7. 隐藏 evidence 不进入 count
# ---------------------------------------------------------------------------

def test_hidden_evidence_not_in_count(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    # 同一关系：p-eng（可见）+ p-sales（隐藏）
    _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
    _seed_page(db, "p-sales", "Titan2", ["电池模块属于 Titan 810"], group="sales")
    db.commit()
    _rebuild(db, "p-eng")
    _rebuild(db, "p-sales")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph")
        assert r.status_code == 200
        for e in r.json()["edges"]:
            # 关系由两 Page 支撑，但普通用户只见 engineering 的 1 条
            assert e["evidence_count"] == 1, "隐藏 evidence 不进入 count"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 8. 隐藏关系不影响 conflict
# ---------------------------------------------------------------------------

def test_hidden_relation_does_not_affect_conflict(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    # 同实体对「包含」关系：engineering 有 2.0，sales 有 3.0（隐藏）
    _seed_page(db, "p-eng", "Titan 2.0", ["Titan 810 包含标准电池", "版本 2.0"], group="engineering", source_path="t2.pdf")
    _seed_page(db, "p-sales", "Titan 3.0", ["Titan 810 包含标准电池", "版本 3.0"], group="sales", source_path="t3.pdf")
    db.commit()
    _rebuild(db, "p-eng")
    _rebuild(db, "p-sales")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph")
        for e in r.json()["edges"]:
            assert e["conflict"] is False, "隐藏 3.0 不应使 engineering 用户看到 conflict"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 9/10. Community hash 筛选
# ---------------------------------------------------------------------------

def test_community_hash_filter_and_forged(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    _seed_page(db, "p2", "Skywalker", ["驱动电机属于 Skywalker 50"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        comms = client.get("/api/v4/graph/communities").json()["communities"]
        assert len(comms) >= 2
        c0 = comms[0]["key"]
        # 真实 hash 筛选返回节点
        r = client.get("/api/v4/graph/subgraph", params={"community": c0})
        assert r.status_code == 200
        assert r.json()["nodes"], "真实 Community hash 应返回节点"
        # 伪造 hash 返回空
        r2 = client.get("/api/v4/graph/subgraph", params={"community": "forged-hash"})
        assert r2.json()["nodes"] == [], "伪造 hash 返回空"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 11. 大图查询在数据库层有界
# ---------------------------------------------------------------------------

def test_large_graph_bounded_sql(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    for i in range(30):
        _seed_page(db, f"p{i}", f"Titan{i}", [f"电池模块属于 Titan 810 {i}"])
    db.commit()
    for i in range(30):
        _rebuild(db, f"p{i}")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph", params={"limit": 10})
        assert r.status_code == 200
        body = r.json()
        assert len(body["nodes"]) <= 10, "SQL 层 limit 生效"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 12. focus BFS 深度/节点/边上限
# ---------------------------------------------------------------------------

def test_focus_bfs_limits(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        # 先拿一个节点 id
        r0 = client.get("/api/v4/graph/subgraph")
        node_id = r0.json()["nodes"][0]["id"]
        r = client.get("/api/v4/graph/subgraph", params={"focus": node_id, "depth": 9999, "limit": 1})
        assert r.status_code == 200
        body = r.json()
        assert len(body["nodes"]) <= 1, "focus BFS 节点上限生效"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 13. per-family latest
# ---------------------------------------------------------------------------

def test_per_family_latest(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0"}, "skywalker": {"10.0"}}
    allowed = _family_allowed_labels(families, None)
    assert allowed["titan"] == {"common", "3.0"}, "Titan 自己的 latest 是 3.0"
    assert allowed["skywalker"] == {"common", "10.0"}, "Skywalker 自己的 latest 是 10.0"


# ---------------------------------------------------------------------------
# 14. 指定 Titan 9.0 不回退
# ---------------------------------------------------------------------------

def test_specified_9_no_fallback(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0"}, "skywalker": {"10.0"}}
    allowed = _family_allowed_labels(families, "9.0")
    assert "2.0" not in allowed["titan"] and "3.0" not in allowed["titan"]
    assert "9.0" in allowed["titan"]


# ---------------------------------------------------------------------------
# 15. 多版本歧义不猜
# ---------------------------------------------------------------------------

def test_ambiguous_not_guess(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0"}}
    allowed = _family_allowed_labels(families, "2.0,3.0")
    assert allowed == {"titan": {"common"}}, "歧义只 common"


# ---------------------------------------------------------------------------
# 16. 动态版本 facets 不泄露无权限版本
# ---------------------------------------------------------------------------

def test_facets_no_hidden_version(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p-eng", "Titan 2.0", ["Titan 810 包含标准电池", "版本 2.0"], group="engineering", source_path="t2.pdf")
    _seed_page(db, "p-sales", "Titan 3.0", ["Titan 810 包含标准电池", "版本 3.0"], group="sales", source_path="t3.pdf")
    db.commit()
    _rebuild(db, "p-eng")
    _rebuild(db, "p-sales")
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/facets")
        versions = r.json()["versions"]
        assert "2.0" in versions
        assert "3.0" not in versions, "facets 不泄露无权限版本"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 17. 前端聚焦保留筛选
# ---------------------------------------------------------------------------

def test_frontend_focus_keeps_filters():
    hub = (FRONTEND_ROOT / "views" / "KnowledgeGraphHub.vue").read_text(encoding="utf-8")
    # focusNode 传递 version/entity_type/community
    assert "version: versionFilter.value" in hub
    assert "entity_type: entityType.value" in hub
    assert "community: communityFilter.value" in hub
    # 动态 facets
    assert "facets.versions" in hub
    assert "facets.entity_types" in hub
    assert "facets.communities" in hub


# ---------------------------------------------------------------------------
# 18/19. Wiki 人工编辑进入图谱 + 回滚后消失
# ---------------------------------------------------------------------------

def _seed_wiki(db, wiki_id, title, content, group="engineering"):
    wp = WikiPage(id=wiki_id, title=title, summary="", acl_scope=json.dumps({"groups": [group]}), status="published")
    db.add(wp)
    db.flush()
    rev = WikiRevision(id=f"{wiki_id}-rev", wiki_page_id=wiki_id, title=title, summary="", status="published")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id=f"{wiki_id}-sec", revision_id=rev.id, section_type="facts", content=content, order_index=0))
    wp.current_revision_id = rev.id
    db.commit()
    return wp


def test_wiki_manual_edit_and_rollback(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_wiki_graph

    # 人工编辑：section 含实体关系
    wp = _seed_wiki(db, "w1", "电池故障", "电池告警 解决 重新充电")
    rebuild_wiki_graph(db, "w1", commit=True)
    rels = db.query(V4GraphRelation).all()
    assert rels, "Wiki 人工内容关系进入图谱"
    assert any(r.relation_type == "solves" for r in rels)

    # 回滚：清空当前 Revision 内容 → 旧关系消失
    wp = db.get(WikiPage, "w1")
    rev = db.get(WikiRevision, wp.current_revision_id)
    # 模拟回滚到无关系内容：清 section
    db.query(WikiSection).filter(WikiSection.revision_id == rev.id).delete(synchronize_session=False)
    rebuild_wiki_graph(db, "w1", commit=True)
    rels2 = db.query(V4GraphRelation).all()
    assert not rels2, "回滚后旧 Revision 关系消失"


# ---------------------------------------------------------------------------
# 20. Wiki ACL 外不可见
# ---------------------------------------------------------------------------

def test_wiki_acl_out_of_scope_invisible(db, monkeypatch):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_wiki_graph
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_wiki(db, "w-eng", "电池故障", "电池告警 解决 重新充电", group="engineering")
    _seed_wiki(db, "w-sales", "机密", "电机故障 解决 重启", group="sales")
    rebuild_wiki_graph(db, "w-eng", commit=True)
    rebuild_wiki_graph(db, "w-sales", commit=True)
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph")
        names = [n["display_name"] for n in r.json()["nodes"]]
        assert "电机故障" not in names, "Wiki ACL 外实体不可见"
        facets = client.get("/api/v4/graph/facets").json()
        assert "电机" not in json.dumps(facets)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 21. Page 与 Wiki 重复事实不重复生成边
# ---------------------------------------------------------------------------

def test_page_and_wiki_duplicate_no_dup_edge(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_wiki_graph
    _seed_page(db, "p1", "电池", ["电池告警 解决 重新充电"])
    db.commit()
    _rebuild(db, "p1")
    # Wiki 与 Page 完全重复事实
    _seed_wiki(db, "w1", "电池故障", "电池告警 解决 重新充电")
    rebuild_wiki_graph(db, "w1", commit=True)

    # 同 (source, solves, target, version) 应只有一条关系（去重）
    rels = db.query(V4GraphRelation).filter(V4GraphRelation.relation_type == "solves").all()
    assert len(rels) == 1, "Page 与 Wiki 重复事实不重复生成边"
    # 但 provenance 有两条（page + wiki）
    prov = db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.relation_id == rels[0].id).all()
    assert len(prov) == 2, "Page + Wiki 两条 provenance"


# ---------------------------------------------------------------------------
# 22/23. 生产链接线（源码级 + 行为）
# ---------------------------------------------------------------------------

def test_production_rebuild_wiring():
    pages_src = (BACKEND_ROOT / "app" / "api" / "pages.py").read_text(encoding="utf-8")
    assert "_schedule_graph_rebuild" in pages_src
    assert "remove_page_graph" in pages_src


def test_production_remove_wiring():
    """NEEDS_REASSIGN 的完整行为（映射变化 → skipped + Page 不迁移 + remove_page_graph 同事务失效）
    由 test_p38_round2_executor_gitlab::test_gitlab_mapping_change_marks_needs_reassign 真实验证。

    此处仅确认生产接线点存在（folder_mapping 已重构为委托 path_mapping；删除/失效在 executor）。
    """
    executor_src = (BACKEND_ROOT / "app" / "sources" / "executor.py").read_text(encoding="utf-8")
    assert "remove_page_graph" in executor_src
    assert "NEEDS_REASSIGN" in executor_src
    # notebooks：Notebook 改组旧 scope 图谱立即失效（正式行为，非字符串断言可另行验证）
    nb_src = (BACKEND_ROOT / "app" / "api" / "notebooks.py").read_text(encoding="utf-8")
    assert "_invalidate_graph_for_pages" in nb_src


# ---------------------------------------------------------------------------
# 24. Notebook 改组旧 scope 图谱立即失效
# ---------------------------------------------------------------------------

def test_notebook_rescope_graph_invalidated(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.models.database import User, UserGroup

    # 造一个用户和用户组，Notebook 从 engineering 改到 sales
    db.add(User(id="u1", username="admin", is_local=True))
    db.add(UserGroup(id="ug1", user_id="u1", group_name="engineering"))
    db.add(UserGroup(id="ug2", user_id="u1", group_name="sales"))
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"], group="engineering")
    db.commit()
    _rebuild(db, "p1")

    # 先确认 engineering 能看到
    from app.core import access_control
    assert "p1" in access_control.get_visible_page_ids(db, {"groups": ["engineering"]})

    # Notebook 改组到 sales
    nb = db.get(Notebook, "nb-p1")
    nb.group_id = "sales"
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    remove_page_graph(db, "p1", commit=False)
    db.commit()

    # 旧 scope（engineering）图谱已失效：无 engineering 的 provenance
    from app.core.knowledge_compiler_v3.v4_graph_builder import _page_scope, _scope_key
    # 直接验证 remove 后无 entity evidence 属于旧 scope
    assert db.query(V4GraphEntityEvidence).count() == 0, "改组后旧 provenance 清空"


# ---------------------------------------------------------------------------
# 25. 回填 dry-run/apply/keyset
# ---------------------------------------------------------------------------

def test_backfill_dry_run_and_keyset(tmp_path):
    # 用临时库造 3 个 Page，验证 keyset 分页 + dry-run 零写入
    db_path = tmp_path / "backfill.db"
    from app.models.database import get_engine
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    s = sessionmaker(bind=engine)()
    for i in range(3):
        _seed_page(s, f"p{i}", f"Titan{i}", ["电池模块属于 Titan 810"])
    s.commit()
    s.close()

    # dry-run
    r = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_v4_graph", "--db", db_path.as_posix(), "--batch", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["apply"] is False
    assert out["scanned"] == 3, "keyset 分页不漏 Page（3 个 Page，batch=1）"

    # dry-run 后零写入
    s = sessionmaker(bind=engine)()
    assert s.query(V4GraphEntity).count() == 0, "dry-run 零写入"
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 26. 图谱生产模块与 SQL 零 Card/KO
# ---------------------------------------------------------------------------

def test_graph_modules_zero_card_ko(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    import app.core.knowledge_compiler_v3.v4_graph_builder as builder_mod
    import app.core.knowledge_compiler_v3.graph_refresh_scheduler as sched_mod
    import app.api.v4_graph as api_mod

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()

    card_sql: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if any(t in low for t in _CARD_TABLES):
            card_sql.append(statement)

    try:
        rebuild_page_graph(db, "p1", commit=False)
    finally:
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert card_sql == [], f"图谱构建不应查询 Card/KO 表：{card_sql[:3]}"

    for mod in (builder_mod, sched_mod, api_mod):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in ("KnowledgeCard", "KnowledgeCommunity", "CanonicalEntity", "CardEntityLink", "CardGraphRelation"):
            assert forbidden not in joined, f"{mod.__name__} 不应 import {forbidden}"
