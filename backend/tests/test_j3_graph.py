"""Phase J-3 反例测试：真正的实体关系知识图谱。

覆盖 30 条反例（见模块 docstring 尾部编号注释）。
全程临时 SQLite，不触碰真实库，不调用真实模型。
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    EvidenceItem,
    Notebook,
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
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend" / "src"

# Card/KO 相关表名片段（SQL 监听用）。
_CARD_TABLES = (
    "knowledge_cards", "knowledge_card_blocks", "knowledge_card_revisions",
    "knowledge_card_sources", "knowledge_claims", "knowledge_objects",
    "ko_chunks", "card_entity_links", "card_graph_relations", "canonical_entities",
    "knowledge_communities",
)


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
    page = Page(
        id=page_id, notebook_id=nb.id, title=title, content="",
        source_type=source_type, source_id=source_id, source_path=source_path,
    )
    db.add(page)
    db.flush()
    for i, txt in enumerate(chunks):
        db.add(PageChunk(id=f"{page_id}-c{i}", page_id=page_id, chunk_index=i, content=txt, content_type="text"))
    db.flush()
    return page


def _rebuild(db, page_id):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    return rebuild_page_graph(db, page_id, commit=False)


def _entities(db):
    return {e.id: e for e in db.query(V4GraphEntity).all()}


def _relations(db):
    return {r.id: r for r in db.query(V4GraphRelation).all()}


# ---------------------------------------------------------------------------
# 1. 同一 Chunk 的实体能形成正确关系
# ---------------------------------------------------------------------------

def test_same_chunk_forms_relation(db):
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    rels = _relations(db)
    assert rels, "同 Chunk 内应形成关系"
    assert any(r.relation_type == "belongs_to" for r in rels.values())


# ---------------------------------------------------------------------------
# 2. 不同无关 Chunk 的实体不能形成关系
# ---------------------------------------------------------------------------

def test_different_chunks_no_cross_relation(db):
    # 两个 chunk 各含一个实体，无关系词，不应跨 chunk 建关系
    _seed_page(db, "p1", "Titan", ["电池模块", "Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    rels = _relations(db)
    assert not rels, "不同 Chunk 实体不得笛卡尔组合"


# ---------------------------------------------------------------------------
# 3. Evidence 支撑的关系可以建立
# ---------------------------------------------------------------------------

def test_evidence_supported_relation(db):
    _seed_page(db, "p1", "Titan", ["无实体正文"])
    db.commit()
    db.add(EvidenceItem(
        id="ev1", source_page_id="p1", content="电池模块属于 Titan 810",
        evidence_type="text", status="active",
    ))
    db.commit()
    _rebuild(db, "p1")
    rels = _relations(db)
    assert any(r.relation_type == "belongs_to" for r in rels.values()), "Evidence 支撑的关系应建立"


# ---------------------------------------------------------------------------
# 4. 无证据的关系不能建立
# ---------------------------------------------------------------------------

def test_no_evidence_no_relation(db):
    _seed_page(db, "p1", "Titan", ["只有单个实体，无关系词"])
    db.commit()
    _rebuild(db, "p1")
    assert not _relations(db), "无关系词/无证据不得建立关系"


# ---------------------------------------------------------------------------
# 5. 重复 Page 构建不产生重复实体/边
# ---------------------------------------------------------------------------

def test_rebuild_idempotent(db):
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    n1 = db.query(V4GraphEntity).count()
    r1 = db.query(V4GraphRelation).count()
    _rebuild(db, "p1")
    n2 = db.query(V4GraphEntity).count()
    r2 = db.query(V4GraphRelation).count()
    assert n1 == n2, "重复构建不产生重复实体"
    assert r1 == r2, "重复构建不产生重复关系"


# ---------------------------------------------------------------------------
# 6. 更新一个 Page 只替换该 Page 的证据和关系，不影响其他 Page
# ---------------------------------------------------------------------------

def test_update_one_page_only(db):
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    _seed_page(db, "p2", "Skywalker", ["驱动电机属于 Skywalker 50"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p2")
    # 修改 p1，只影响 p1
    db.query(PageChunk).filter(PageChunk.page_id == "p1").delete(synchronize_session=False)
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="充电桩连接到 Titan 810"))
    db.commit()
    _rebuild(db, "p1")
    p2_ev = db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.page_id == "p2").all()
    assert p2_ev, "更新 p1 不得影响 p2 的证据"
    p2_rel = db.query(V4GraphRelation).filter(
        V4GraphRelation.id.in_([e.relation_id for e in p2_ev])
    ).all()
    assert any("skywalker" in r.source_id or "skywalker" in r.target_id for r in p2_rel)


# ---------------------------------------------------------------------------
# 7. Page 删除或 SourceItem skipped 后旧关系不再可见
# ---------------------------------------------------------------------------

def test_remove_page_hides_relations(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    assert _relations(db)
    remove_page_graph(db, "p1", commit=False)
    assert not _relations(db), "删除 Page 后旧关系应失效"
    assert not db.query(V4GraphRelationEvidence).all()


# ---------------------------------------------------------------------------
# 8. 图谱模块和 SQL 零 Card/KO
# ---------------------------------------------------------------------------

def test_graph_modules_zero_card_ko(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    import app.core.knowledge_compiler_v3.v4_graph_builder as mod

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
    # 源码级：import 语句不引入旧图谱模型
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
    joined = "\n".join(import_lines)
    for forbidden in ("KnowledgeCard", "KnowledgeCommunity", "CanonicalEntity", "CardEntityLink", "CardGraphRelation", "EntityAlias"):
        assert forbidden not in joined, f"构建器不应 import {forbidden}"


# ---------------------------------------------------------------------------
# 9. 2.0 和 3.0 关系不混合
# ---------------------------------------------------------------------------

def test_versions_not_mixed(db):
    _seed_page(db, "p2", "Titan 2.0", ["Titan 810 包含标准电池", "版本 2.0"], source_path="Titan-2.0.pdf")
    _seed_page(db, "p3", "Titan 3.0", ["Titan 810 包含标准电池", "版本 3.0"], source_path="Titan-3.0.pdf")
    db.commit()
    _rebuild(db, "p2")
    _rebuild(db, "p3")
    rels = _relations(db)
    # 同 (source, contains, target) 应有两行：2.0 和 3.0
    contains = [r for r in rels.values() if r.relation_type == "contains"]
    versions = {r.version_label for r in contains}
    assert "2.0" in versions and "3.0" in versions, "2.0/3.0 关系不合并"


# ---------------------------------------------------------------------------
# 10. unspecified + latest 不混入 unversioned
# ---------------------------------------------------------------------------

def test_unspecified_latest_excludes_unversioned(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0", "unversioned"}}
    allowed = _family_allowed_labels(families, None)
    assert allowed == {"titan": {"common", "3.0"}}, f"latest 3.0 不混入 unversioned，got {allowed}"


# ---------------------------------------------------------------------------
# 11. 只有 unversioned 时不标 latest
# ---------------------------------------------------------------------------

def test_only_unversioned_no_latest(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"unversioned"}}
    allowed = _family_allowed_labels(families, None)
    assert "3.0" not in allowed["titan"] and "2.0" not in allowed["titan"]
    assert "unversioned" in allowed["titan"]


# ---------------------------------------------------------------------------
# 12. ambiguous 只返回 common
# ---------------------------------------------------------------------------

def test_ambiguous_only_common(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0"}}
    allowed = _family_allowed_labels(families, "2.0,3.0")
    assert allowed == {"titan": {"common"}}


# ---------------------------------------------------------------------------
# 13. 指定不存在 9.0 不回退其他版本
# ---------------------------------------------------------------------------

def test_specified_missing_version_no_fallback(db):
    from app.api.v4_graph import _family_allowed_labels
    families = {"titan": {"2.0", "3.0"}}
    allowed = _family_allowed_labels(families, "9.0")
    assert "2.0" not in allowed["titan"] and "3.0" not in allowed["titan"]
    assert "9.0" in allowed["titan"] and "common" in allowed["titan"]


# ---------------------------------------------------------------------------
# 14. 冲突版本关系分别保留
# ---------------------------------------------------------------------------

def test_conflict_versions_kept_separate(db):
    _seed_page(db, "p2", "Titan 2.0", ["标准电池 容量 60Ah", "版本 2.0"], source_path="titan-2.0.pdf")
    _seed_page(db, "p3", "Titan 3.0", ["标准电池 容量 60Ah", "版本 3.0"], source_path="titan-3.0.pdf")
    db.commit()
    _rebuild(db, "p2")
    _rebuild(db, "p3")
    rels = _relations(db)
    cap = [r for r in rels.values() if r.relation_type == "capacity"]
    versions = {r.version_label for r in cap}
    assert len(versions) >= 2, "冲突版本关系分别保留"


# ---------------------------------------------------------------------------
# 15-19. ACL 相关（通过 API 端到端）
# ---------------------------------------------------------------------------

def _api_client(monkeypatch, tmp_path, seed=None):
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'j3.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    if seed:
        seed(db)
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _eng():
        return {"id": "u1", "username": "eng", "groups": ["engineering"], "is_admin": False}

    def _admin():
        return {"id": "u2", "username": "admin", "groups": ["admins"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _eng
    c = TestClient(app, raise_server_exceptions=False)
    yield c, _eng, _admin
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_graph(seed_fn):
    def _seed(db):
        from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
        seed_fn(db)
        db.commit()
        rebuild_page_graph(db, "p-eng", commit=True)
        rebuild_page_graph(db, "p-sales", commit=True)
    return _seed


def test_acl_out_of_scope_node_not_returned(monkeypatch, tmp_path):
    def seed(db):
        _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
        _seed_page(db, "p-sales", "机密", ["电池模块属于 Skywalker 50"], group="sales")

    gen = _api_client(monkeypatch, tmp_path, _seed_graph(seed))
    c, _eng, _admin = next(gen)
    r = c.get("/api/v4/graph/subgraph", params={"q": "电池模块"})
    assert r.status_code == 200
    body = r.json()
    names = [n["display_name"] for n in body["nodes"]]
    assert "Skywalker" not in "".join(names), "ACL 外实体节点不返回"


def test_acl_out_of_scope_edge_not_returned(monkeypatch, tmp_path):
    def seed(db):
        _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
        _seed_page(db, "p-sales", "机密", ["电池模块属于 Skywalker 50"], group="sales")

    gen = _api_client(monkeypatch, tmp_path, _seed_graph(seed))
    c, _eng, _admin = next(gen)
    r = c.get("/api/v4/graph/subgraph")
    assert r.status_code == 200
    body = r.json()
    # 所有边两端都在返回节点内
    node_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source"] in node_ids and e["target"] in node_ids


def test_acl_excluded_from_count_and_community(monkeypatch, tmp_path):
    def seed(db):
        _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
        _seed_page(db, "p-sales", "机密", ["电池模块属于 Skywalker 50"], group="sales")

    gen = _api_client(monkeypatch, tmp_path, _seed_graph(seed))
    c, _eng, _admin = next(gen)
    r = c.get("/api/v4/graph/subgraph")
    body = r.json()
    # 计数与节点数一致（不把 ACL 外节点计入）
    assert body["total_nodes"] == len(body["nodes"])
    # community 不含 sales 实体名
    comm_names = [cm["display_name"] for cm in body["communities"]]
    assert not any("Skywalker" in cm for cm in comm_names)


def test_acl_no_search_inference(monkeypatch, tmp_path):
    def seed(db):
        _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
        _seed_page(db, "p-sales", "机密", ["电池模块属于 Skywalker 50"], group="sales")

    gen = _api_client(monkeypatch, tmp_path, _seed_graph(seed))
    c, _eng, _admin = next(gen)
    r = c.get("/api/v4/graph/search", params={"q": "Skywalker"})
    body = r.json()
    names = [e["display_name"] for e in body["entities"]]
    assert "Skywalker" not in names, "不能通过搜索推断 ACL 外实体"


def test_limit_after_acl(monkeypatch, tmp_path):
    def seed(db):
        _seed_page(db, "p-eng", "Titan", ["电池模块属于 Titan 810"], group="engineering")
        _seed_page(db, "p-sales", "机密", ["电池模块属于 Skywalker 50"], group="sales")

    gen = _api_client(monkeypatch, tmp_path, _seed_graph(seed))
    c, _eng, _admin = next(gen)
    r = c.get("/api/v4/graph/subgraph", params={"limit": 1})
    body = r.json()
    assert len(body["nodes"]) <= 1
    # limit 后只剩授权节点
    names = [n["display_name"] for n in body["nodes"]]
    assert "Skywalker" not in "".join(names)


# ---------------------------------------------------------------------------
# 20. 深度和节点上限由后端强制
# ---------------------------------------------------------------------------

def test_depth_and_limit_enforced(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    rebuild_page_graph(db, "p1", commit=True)
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        # 请求超大 depth/limit，后端封顶
        r = client.get("/api/v4/graph/subgraph", params={"depth": 9999, "limit": 99999})
        assert r.status_code == 200
        body = r.json()
        assert len(body["nodes"]) <= 200
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 21. 管理员也不返回失效远程 Page 的图谱
# ---------------------------------------------------------------------------

def test_admin_excludes_skipped_remote_page(db):
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    from app.sources.service import compute_content_hash, compute_metadata_hash
    from app.sources.schemas import NormalizedSourceItem

    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    # active 远程 Page
    _seed_page(db, "p-ok", "Titan", ["电池模块属于 Titan 810"], group="engineering", source_type="dingtalk", source_id="ok")
    hist = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="ok", title="ok", content="正文",
        source_path="ok.pdf", metadata_json={"space_id": "s", "space_name": "k"},
    )
    db.add(SourceItem(id="si-ok", connection_id="conn", external_id="ok", state="active",
                      content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
                      source_path="ok.pdf", acl_json='{"space_id": "s"}', page_id="p-ok"))
    # skipped 远程 Page
    _seed_page(db, "p-bad", "机密", ["电池模块属于 Skywalker 50"], group="engineering", source_type="dingtalk", source_id="bad")
    db.add(SourceItem(id="si-bad", connection_id="conn", external_id="bad", state="skipped",
                      last_error="文件夹映射变化，需要重新归属", content_hash="x", metadata_hash=None,
                      source_path="bad.pdf", acl_json='{"space_id": "s"}', page_id="p-bad"))
    db.commit()
    rebuild_page_graph(db, "p-ok", commit=True)
    rebuild_page_graph(db, "p-bad", commit=True)

    admin = {"id": "u", "username": "admin", "groups": ["admins"], "is_admin": True}
    from app.core import access_control
    visible = access_control.get_visible_page_ids(db, admin)
    assert "p-bad" not in visible, "管理员也不得含失效远程 Page"


# ---------------------------------------------------------------------------
# 22. 多组 Notebook 授权正确
# ---------------------------------------------------------------------------

def test_multi_group_notebook_access(db):
    from app.models.database import NotebookGroup
    nb = Notebook(id="nb-multi", name="多组库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(NotebookGroup(id="ng1", notebook_id="nb-multi", group_name="sales"))
    db.add(Page(id="p1", notebook_id="nb-multi", title="Titan", content=""))
    db.flush()
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="电池模块属于 Titan 810"))
    db.commit()
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    rebuild_page_graph(db, "p1", commit=True)

    from app.core import access_control
    # 两个组成员都可见
    assert "p1" in access_control.get_visible_page_ids(db, {"groups": ["engineering"]})
    assert "p1" in access_control.get_visible_page_ids(db, {"groups": ["sales"]})


# ---------------------------------------------------------------------------
# 23. Community 名称确定且不包含管理员/社区编号/内部 ID
# ---------------------------------------------------------------------------

def test_community_name_deterministic_clean(db):
    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    _rebuild(db, "p1")
    comms = db.query(V4GraphCommunity).all()
    assert comms
    for c in comms:
        assert "管理员" not in c.display_name
        assert not re.match(r"^社区\s*\d+$", c.display_name)
        assert "|" not in c.display_name  # 不泄露内部 key
        assert c.display_name  # 非空


# ---------------------------------------------------------------------------
# 24. API 响应不泄露 Page/Chunk/Evidence/Notebook/scope
# ---------------------------------------------------------------------------

def test_api_no_internal_leak(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"], group="engineering")
    db.commit()
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    rebuild_page_graph(db, "p1", commit=True)
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph")
        assert r.status_code == 200
        body = json.dumps(r.json())
        for leak in ("page_id", "chunk_id", "evidence_id", "notebook", "acl_scope", "engineering", "source_page"):
            assert leak not in body, f"响应不应泄露 {leak}"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 25. 前端真实包含节点和带名称连线
# ---------------------------------------------------------------------------

def test_frontend_has_nodes_and_named_edges():
    canvas = (FRONTEND_ROOT / "components" / "graph" / "V4GraphCanvas.vue").read_text(encoding="utf-8")
    assert "linkLabelSel" in canvas  # 关系名称标签
    assert "relationLabel" in canvas  # 关系名映射
    hub = (FRONTEND_ROOT / "views" / "KnowledgeGraphHub.vue").read_text(encoding="utf-8")
    assert "V4GraphCanvas" in hub
    assert ":nodes" in hub and ":edges" in hub


# ---------------------------------------------------------------------------
# 26. 前端支持拖拽、缩放、平移、搜索、筛选和节点聚焦
# ---------------------------------------------------------------------------

def test_frontend_interactions():
    canvas = (FRONTEND_ROOT / "components" / "graph" / "V4GraphCanvas.vue").read_text(encoding="utf-8")
    for token in ("drag", "zoom", "clickDistance", "on('click'"):
        assert token in canvas, f"画布缺少 {token}"
    hub = (FRONTEND_ROOT / "views" / "KnowledgeGraphHub.vue").read_text(encoding="utf-8")
    for token in ("搜索实体", "实体类型", "版本", "分组", "onSelectNode", "focusNode"):
        assert token in hub, f"Hub 缺少 {token}"


# ---------------------------------------------------------------------------
# 27. Wiki 链接只在授权且存在时返回
# ---------------------------------------------------------------------------

def test_related_wiki_only_authorized(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    _seed_page(db, "p1", "Titan", ["电池模块属于 Titan 810"], group="engineering")
    db.commit()
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
    rebuild_page_graph(db, "p1", commit=True)
    # 授权 Wiki（engineering）
    wp = WikiPage(id="w1", title="电池模块", summary="", acl_scope='{"groups": ["engineering"]}', status="published")
    db.add(wp)
    db.flush()
    rev = WikiRevision(id="w1-rev", wiki_page_id="w1", title="电池模块", summary="", status="published")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id="w1-sec", revision_id=rev.id, section_type="facts", content="电池模块说明", order_index=0))
    wp.current_revision_id = rev.id
    # 未授权 Wiki（sales）
    wp2 = WikiPage(id="w2", title="电池模块机密", summary="", acl_scope='{"groups": ["sales"]}', status="published")
    db.add(wp2)
    db.flush()
    rev2 = WikiRevision(id="w2-rev", wiki_page_id="w2", title="电池模块机密", summary="", status="published")
    db.add(rev2)
    db.flush()
    db.add(WikiSection(id="w2-sec", revision_id=rev2.id, section_type="facts", content="机密", order_index=0))
    wp2.current_revision_id = rev2.id
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/subgraph", params={"q": "电池模块"})
        assert r.status_code == 200
        body = r.json()
        wiki_titles = []
        for n in body["nodes"]:
            wiki_titles.extend(w["title"] for w in n.get("related_wiki", []))
        assert "电池模块" in wiki_titles
        assert "电池模块机密" not in wiki_titles, "未授权 Wiki 不得返回"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 28. 空结果和请求失败状态正确显示
# ---------------------------------------------------------------------------

def test_frontend_empty_and_error_states():
    hub = (FRONTEND_ROOT / "views" / "KnowledgeGraphHub.vue").read_text(encoding="utf-8")
    assert "暂无实体关系" in hub  # 空结果
    assert "图谱加载失败" in hub  # 失败状态
    assert "error" in hub


# ---------------------------------------------------------------------------
# 29. migration 空库 upgrade/downgrade 一致
# ---------------------------------------------------------------------------

def test_migration_upgrade_downgrade_consistent(monkeypatch, tmp_path):
    import subprocess
    import sys

    db_path = tmp_path / "j3-mig.db"
    env = {"PYTHONIOENCODING": "utf-8"}
    # upgrade head
    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**__import__("os").environ, **env},
    )
    assert r1.returncode == 0, r1.stderr[-2000:]

    # downgrade 到 P35（只删 4 张新表，不触碰其他）
    r2 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "downgrade", "c9f4a5b6d7e8"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**__import__("os").environ, **env},
    )
    assert r2.returncode == 0, r2.stderr[-2000:]


# ---------------------------------------------------------------------------
# 30. 真实库副本 P35→新 head 数据保留、FK 完整
# ---------------------------------------------------------------------------

def test_copy_upgrade_preserves_data(monkeypatch, tmp_path):
    # 验证 P35 已有表数据在 upgrade 到 head 后保留（用临时库模拟：先造 P35 数据再 upgrade）
    import subprocess
    import sys

    db_path = tmp_path / "j3-data.db"
    # 先 upgrade 到 P35
    r0 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "c9f4a5b6d7e8"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**__import__("os").environ},
    )
    assert r0.returncode == 0

    # 造一条 P35 数据（notebook + page）
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO notebooks (id, name, group_id, created_at, updated_at) VALUES ('nb1','nb','engineering',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO pages (id, notebook_id, title, content, wiki_dirty, created_at, updated_at) VALUES ('p1','nb1','t','c',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

    # upgrade 到 head
    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**__import__("os").environ},
    )
    assert r1.returncode == 0, r1.stderr[-2000:]

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM pages WHERE id='p1'")
    assert cur.fetchone()[0] == 1, "P35 数据保留"
    # FK 完整性：v4_graph_relations 外键存在
    cur.execute("PRAGMA foreign_key_list(v4_graph_relations)")
    fks = cur.fetchall()
    assert any("v4_graph_entities" in row[2] for row in fks), "关系表 FK 指向实体表"
    conn.close()
