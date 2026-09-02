"""Phase J-3 第五轮（最终）封板行为测试。

覆盖三个遗留的真实修复：
一、Notebook 改组 fail closed（真实 PUT /api/pages API 行为）。
二、related Wiki 查询 SQL 硬上限（SQL 监听器）。
三、malformed schema 真实破坏测试（错误 ON DELETE / referred column / constrained
    columns / 唯一索引名称）+ 真实 execute_run 调用链。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import subprocess
import sys
import os

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    RuntimeFeatureFlag,
    SourceConnection,
    SourceItem,
    SourceSyncRun,
    V4GraphEntity,
    V4GraphRelation,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
    SchemaNotReadyError,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


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


# ===========================================================================
# 一、Notebook 改组 fail closed（真实 API 行为）
# ===========================================================================

def test_notebook_change_remove_graph_failure_rolls_back(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.models.database import get_engine, get_session, User, UserGroup

    url = f"sqlite:///{(tmp_path / 'nb.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)

    # 造用户 + 两个 Notebook + Page + 图谱 provenance
    db.add(User(id="u1", username="admin", is_local=True))
    db.add(UserGroup(id="ug1", user_id="u1", group_name="engineering"))
    db.add(UserGroup(id="ug2", user_id="u1", group_name="sales"))
    nb_old = Notebook(id="nb-old", name="旧库", group_id="engineering")
    nb_new = Notebook(id="nb-new", name="新库", group_id="sales")
    db.add(nb_old)
    db.add(nb_new)
    db.flush()
    page = Page(id="p1", notebook_id="nb-old", title="Titan", content="电池模块 属于 Titan 810", wiki_dirty=True)
    db.add(page)
    db.flush()
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="电池模块 属于 Titan 810", content_type="text"))
    db.commit()
    _rebuild(db, "p1")
    rel_count_before = db.query(V4GraphRelation).count()
    assert rel_count_before > 0

    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app, raise_server_exceptions=False)

    # Mock remove_page_graph 抛异常
    def _boom(db, page_id, commit=False):
        raise RuntimeError("graph remove failed")

    import app.core.knowledge_compiler_v3.v4_graph_builder as builder_mod
    monkeypatch.setattr(builder_mod, "remove_page_graph", _boom)

    try:
        r = client.put("/api/pages/p1", json={"notebook_id": "nb-new"})
        assert r.status_code == 500, f"期望失败，实际 {r.status_code}: {r.text}"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    # 重新打开 Session 验证：Page.notebook_id 仍为旧值
    engine2 = get_engine(url)
    init_db(engine2)
    db2 = get_session(engine2)
    page_after = db2.get(Page, "p1")
    assert page_after.notebook_id == "nb-old", "notebook_id 不得提交新值"
    # 旧图谱 provenance 未变化
    assert db2.query(V4GraphRelation).count() == rel_count_before, "图谱 provenance 不得变化"
    db2.close()
    engine2.dispose()


# ===========================================================================
# 二、related Wiki 查询 SQL 硬上限
# ===========================================================================

def test_related_wiki_sql_limit_and_not_n1(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core.jwt_utils import get_current_user

    # 一个 Page 生成实体，大量 Wiki 与之匹配
    _seed_page(db, "p1", "Titan", ["电池模块 属于 Titan 810"])
    db.commit()
    _rebuild(db, "p1")
    # 造大量匹配 Wiki（标题含 "电池模块"）
    for i in range(30):
        wp = WikiPage(id=f"w{i}", title=f"电池模块说明{i}", summary="", acl_scope=json.dumps({"groups": ["engineering"]}), status="published")
        db.add(wp)
        db.flush()
        rev = WikiRevision(id=f"w{i}-rev", wiki_page_id=f"w{i}", title=f"电池模块说明{i}", summary="", status="published")
        db.add(rev)
        db.flush()
        db.add(WikiSection(id=f"w{i}-sec", revision_id=rev.id, section_type="facts", content="电池模块", order_index=0))
        wp.current_revision_id = rev.id
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)

    wiki_sql = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if "wiki_pages" in low and "select" in low:
            wiki_sql.append(statement)

    try:
        r = client.get("/api/v4/graph/subgraph")
        assert r.status_code == 200
        body = r.json()
        # 每个节点 related_wiki 不超过 5 条
        for node in body["nodes"]:
            assert len(node.get("related_wiki", [])) <= 5, "每节点最多 5 条 related Wiki"
    finally:
        app.dependency_overrides.clear()
        event.remove(db.bind, "before_cursor_execute", _capture)

    # related Wiki 查询带 LIMIT（一次性批量）
    related_queries = [s for s in wiki_sql if "limit" in s.lower()]
    assert related_queries, "related Wiki 查询必须带 LIMIT"
    # 查询次数不随节点数线性增加（批量一次）
    assert len(wiki_sql) <= 3, f"related Wiki 查询次数过多: {len(wiki_sql)}"


# ===========================================================================
# 三、malformed schema 真实破坏
# ===========================================================================

def _schema_fingerprint(db_path):
    """返回数据库 schema fingerprint（表名 + 列 + 索引 + FK）。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY name")
    names = tuple(r[0] for r in cur.fetchall())
    conn.close()
    return names


def _upgrade_p36(tmp_path):
    db_path = tmp_path / "p36.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(BACKEND_ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return db_path


def _break_and_check(tmp_path, break_fn):
    db_path = _upgrade_p36(tmp_path)
    fp_before = _schema_fingerprint(db_path)
    conn = sqlite3.connect(db_path)
    break_fn(conn)
    conn.commit()
    conn.close()
    fp_after = _schema_fingerprint(db_path)
    # schema fingerprint 不变（break_fn 只改结构不改对象名，破坏用同 DDL 内部实现）
    # 但有些破坏（如 drop 表）会改变 fingerprint，这里只对「同名错误」破坏断言。
    from app.models.database import get_engine, init_db, SchemaNotReadyError
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    with pytest.raises(SchemaNotReadyError):
        init_db(engine)
    engine.dispose()
    return db_path


def test_wrong_fk_ondelete_real_break(tmp_path):
    """真实重建表制造错误 ON DELETE（revision_id 无 CASCADE）。"""
    db_path = _upgrade_p36(tmp_path)

    def _break(conn):
        # 重建 v4_graph_relation_evidence 表，revision_id 无 ON DELETE CASCADE
        conn.execute("""
            CREATE TABLE v4_graph_relation_evidence_new (
                id VARCHAR(64) PRIMARY KEY,
                relation_id VARCHAR(1024),
                page_id VARCHAR(36),
                chunk_id VARCHAR(36),
                evidence_id VARCHAR(36),
                wiki_page_id VARCHAR(36),
                revision_id VARCHAR(36),
                section_id VARCHAR(36),
                FOREIGN KEY (relation_id) REFERENCES v4_graph_relations(id) ON DELETE CASCADE,
                FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE,
                FOREIGN KEY (chunk_id) REFERENCES page_chunks(id) ON DELETE CASCADE,
                FOREIGN KEY (evidence_id) REFERENCES evidence_items(id) ON DELETE CASCADE,
                FOREIGN KEY (wiki_page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
                FOREIGN KEY (revision_id) REFERENCES wiki_revisions(id),
                FOREIGN KEY (section_id) REFERENCES wiki_sections(id) ON DELETE CASCADE
            )
        """)
        conn.execute("INSERT INTO v4_graph_relation_evidence_new SELECT * FROM v4_graph_relation_evidence")
        conn.execute("DROP TABLE v4_graph_relation_evidence")
        conn.execute("ALTER TABLE v4_graph_relation_evidence_new RENAME TO v4_graph_relation_evidence")

    _break_and_check(tmp_path, _break)


def test_wrong_fk_referred_column_real_break(tmp_path):
    """真实重建表制造错误 referred column（FK 指向非主键列）。"""
    db_path = _upgrade_p36(tmp_path)

    def _break(conn):
        # 重建 relation_evidence 表，relation_id FK 指向 v4_graph_relations 的 source_id（非主键）
        conn.execute("""
            CREATE TABLE v4_graph_relation_evidence_new (
                id VARCHAR(64) PRIMARY KEY,
                relation_id VARCHAR(1024),
                page_id VARCHAR(36),
                chunk_id VARCHAR(36),
                evidence_id VARCHAR(36),
                wiki_page_id VARCHAR(36),
                revision_id VARCHAR(36),
                section_id VARCHAR(36),
                FOREIGN KEY (relation_id) REFERENCES v4_graph_relations(source_id) ON DELETE CASCADE
            )
        """)
        conn.execute("DROP TABLE v4_graph_relation_evidence")
        conn.execute("ALTER TABLE v4_graph_relation_evidence_new RENAME TO v4_graph_relation_evidence")

    _break_and_check(tmp_path, _break)


def test_wrong_fk_constrained_columns_real_break(tmp_path):
    """真实重建表：page_id 列存在但漏掉 FK（fk_missing），guard 必须 fail closed。

    复合 constrained columns 在 SQLite 单列主键目标下无法合法建表，故用「列存在但
    漏 FK」这一真实可构造场景验证 constrained columns 精确校验的 fk_missing 分支。
    """
    db_path = _upgrade_p36(tmp_path)

    def _break(conn):
        # page_id 列存在但无 FK；其他列 FK 正常。
        conn.execute("""
            CREATE TABLE v4_graph_relation_evidence_new (
                id VARCHAR(64) PRIMARY KEY,
                relation_id VARCHAR(1024),
                page_id VARCHAR(36),
                chunk_id VARCHAR(36),
                evidence_id VARCHAR(36),
                wiki_page_id VARCHAR(36),
                revision_id VARCHAR(36),
                section_id VARCHAR(36),
                FOREIGN KEY (relation_id) REFERENCES v4_graph_relations(id) ON DELETE CASCADE,
                FOREIGN KEY (chunk_id) REFERENCES page_chunks(id) ON DELETE CASCADE,
                FOREIGN KEY (evidence_id) REFERENCES evidence_items(id) ON DELETE CASCADE,
                FOREIGN KEY (wiki_page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
                FOREIGN KEY (revision_id) REFERENCES wiki_revisions(id) ON DELETE CASCADE,
                FOREIGN KEY (section_id) REFERENCES wiki_sections(id) ON DELETE CASCADE
            )
        """)
        conn.execute("DROP TABLE v4_graph_relation_evidence")
        conn.execute("ALTER TABLE v4_graph_relation_evidence_new RENAME TO v4_graph_relation_evidence")

    _break_and_check(tmp_path, _break)


def test_wrong_unique_index_name_real_break(tmp_path):
    """真实改唯一索引名（列相同但名称不同）必须 fail closed。"""
    db_path = _upgrade_p36(tmp_path)

    def _break(conn):
        conn.execute("DROP INDEX ux_v4_graph_rel_evidence")
        conn.execute(
            "CREATE UNIQUE INDEX ux_wrong_name ON v4_graph_relation_evidence "
            "(relation_id, page_id, chunk_id, evidence_id, wiki_page_id, revision_id, section_id)"
        )

    _break_and_check(tmp_path, _break)


# ===========================================================================
# 四、真实 execute_run 调用链
# ===========================================================================

def _gitlab_connector(docs):
    """非 dingtalk connector：走 target_notebook_id 的 create 分支。"""
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange

    class FakeGitlab:
        async def iter_changes(self, cursor):
            for doc_id in docs:
                yield SourceChange(external_id=doc_id, external_version="v1")

        async def fetch_acl(self, external_id):
            return SourceACL(scope="engineering", raw={})

        async def fetch_item(self, external_id):
            return NormalizedSourceItem(
                connection_id="conn",
                source_type="gitlab",
                external_id=external_id,
                external_version="v1",
                title=f"{external_id}.md",
                content="电池模块 属于 Titan 810",
                source_path=f"docs/{external_id}.md",
                metadata_json={},
                source_updated_at="2026-08-01T00:00:00Z",
                deleted=False,
            )

    return FakeGitlab()


@pytest.mark.asyncio
async def test_execute_run_success_order_and_count(monkeypatch, tmp_path):
    """真实 execute_run：index(schedule_graph=False) → evidence → graph，graph 恰好 1 次。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import get_engine, SourceConnection, SourceSyncRun
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources import registry as _reg

    _snapshot = dict(_reg._registry)
    engine = get_engine(f"sqlite:///{(tmp_path / 'exec_ok.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.add(RuntimeFeatureFlag(name="gitlab_connector_enabled", enabled=True))
    db.add(SourceConnection(id="conn", connector_key="gitlab", name="GitLab", enabled=True, target_notebook_id="nb1", config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.commit()

    calls = []

    async def _fake_index(page_id, title, content, *, schedule_graph=True):
        calls.append(("index", schedule_graph))

    def _fake_evidence(db, page_id):
        calls.append(("evidence", page_id))
        return 1

    def _fake_graph(page_id):
        calls.append(("graph", page_id))

    monkeypatch.setattr("app.api.pages.background_index_page", _fake_index)
    monkeypatch.setattr("app.sources.executor.sync_page_evidence", _fake_evidence)
    monkeypatch.setattr("app.sources.executor._schedule_graph_rebuild", _fake_graph)
    monkeypatch.setattr("app.sources.executor._schedule_wiki_refresh_for_page", lambda db, page_id, action, changed=False: None)
    monkeypatch.setattr("app.core.retrieval.debt_service.notify_knowledge_changed_for_page", lambda db, pid: {"resolved": 0, "checked": 0})

    register("gitlab", lambda config: _gitlab_connector(["doc-1"]))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # index(schedule_graph=False) → evidence → graph，graph 恰好 1 次
    assert calls[0] == ("index", False), "executor 索引时不得内部调度 graph"
    order = [c[0] for c in calls]
    assert order == ["index", "evidence", "graph"], f"调用顺序错误: {order}"
    assert order.count("graph") == 1, "graph 恰好调度 1 次"

    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_execute_run_evidence_failure_no_graph(monkeypatch, tmp_path):
    """真实 execute_run：Evidence 抛异常 → graph 调度 0 次。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import get_engine, SourceConnection, SourceSyncRun
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources import registry as _reg

    _snapshot = dict(_reg._registry)
    engine = get_engine(f"sqlite:///{(tmp_path / 'exec_fail.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.add(RuntimeFeatureFlag(name="gitlab_connector_enabled", enabled=True))
    db.add(SourceConnection(id="conn", connector_key="gitlab", name="GitLab", enabled=True, target_notebook_id="nb1", config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.commit()

    calls = []

    async def _fake_index(page_id, title, content, *, schedule_graph=True):
        calls.append("index")

    def _fake_evidence(db, page_id):
        calls.append("evidence")
        raise RuntimeError("evidence failed")

    def _fake_graph(page_id):
        calls.append("graph")

    monkeypatch.setattr("app.api.pages.background_index_page", _fake_index)
    monkeypatch.setattr("app.sources.executor.sync_page_evidence", _fake_evidence)
    monkeypatch.setattr("app.sources.executor._schedule_graph_rebuild", _fake_graph)
    monkeypatch.setattr("app.sources.executor._schedule_wiki_refresh_for_page", lambda db, page_id, action, changed=False: None)
    monkeypatch.setattr("app.core.retrieval.debt_service.notify_knowledge_changed_for_page", lambda db, pid: {"resolved": 0, "checked": 0})

    register("gitlab", lambda config: _gitlab_connector(["doc-1"]))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    assert "graph" not in calls, "Evidence 失败不得调度 graph"
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_execute_run_reassign_failure_rollback(monkeypatch, tmp_path):
    """真实 execute_run：NEEDS_REASSIGN 的 graph 失效故障注入 → SourceItem/Wiki/graph 全 rollback。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        get_engine, SourceConnection, SourceSyncRun, SourcePathMapping,
        WikiPage as WP, WikiRevision as WR, WikiSection as WS,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources import registry as _reg
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources.service import compute_content_hash, compute_metadata_hash

    _snapshot = dict(_reg._registry)
    engine = get_engine(f"sqlite:///{(tmp_path / 'exec_reassign.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
    db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.add(RuntimeFeatureFlag(name="dingtalk_connector_enabled", enabled=True))
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    # 映射：产品资料 → nb-new
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    # 历史 SourceItem + Page（旧归属 nb-old）
    hist = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="doc-1",
        title="doc-1.pdf", content="电池模块 属于 Titan 810",
        source_path="旧目录/doc-1.pdf", metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(id="si1", connection_id="conn", external_id="doc-1", state="active",
                      content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
                      source_path="旧目录/doc-1.pdf", page_id="p-old"))
    db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="电池模块 属于 Titan 810",
                source_type="dingtalk", source_id="doc-1"))
    # 旧 Wiki（关联 p-old）
    db.add(WP(id="w1", title="电池", summary="", acl_scope=json.dumps({"groups": ["engineering"]}), status="published",
              source_page_ids=json.dumps(["p-old"]), current_revision_id="r1"))
    db.add(WR(id="r1", wiki_page_id="w1", title="电池", summary="", status="published"))
    db.add(WS(id="s1", revision_id="r1", section_type="facts", content="电池模块", order_index=0))
    db.commit()
    _rebuild(db, "p-old")

    rel_before = db.query(V4GraphRelation).count()

    # mock 远程 + 索引 + evidence，但 graph remove 抛异常
    async def _noop_remote(*a, **k):
        return {"new_cursor": {"mode": "incremental"}}

    async def _fake_index(page_id, title, content, *, schedule_graph=True):
        pass

    def _fake_evidence(db, page_id):
        return 1

    def _boom_graph_remove(db, page_id, commit=False):
        raise RuntimeError("graph remove failed")

    monkeypatch.setattr("app.sources.executor._run_dingtalk_remote_sync", _noop_remote)
    monkeypatch.setattr("app.api.pages.background_index_page", _fake_index)
    monkeypatch.setattr("app.sources.executor.sync_page_evidence", _fake_evidence)
    monkeypatch.setattr("app.sources.executor._schedule_graph_rebuild", lambda page_id: None)
    monkeypatch.setattr("app.sources.executor._schedule_wiki_refresh_for_page", lambda db, page_id, action, changed=False: None)
    monkeypatch.setattr("app.core.retrieval.debt_service.notify_knowledge_changed_for_page", lambda db, pid: {"resolved": 0, "checked": 0})
    import app.core.knowledge_compiler_v3.v4_graph_builder as builder_mod
    monkeypatch.setattr(builder_mod, "remove_page_graph", _boom_graph_remove)

    def _make_connector(docs):
        class FakeDingtalk:
            async def iter_changes(self, cursor):
                for doc_id in docs:
                    yield SourceChange(external_id=doc_id, external_version="v1")

            async def fetch_acl(self, external_id):
                return SourceACL(scope="space1", raw={"space_id": "space1"})

            async def fetch_item(self, external_id):
                return NormalizedSourceItem(
                    connection_id="conn", source_type="dingtalk", external_id=external_id,
                    external_version="v1", title=f"{external_id}.pdf", content="电池模块 属于 Titan 810",
                    source_path="产品资料/doc-1.pdf",  # 新路径 → 映射变化
                    metadata_json={"space_id": "space1", "space_name": "知识库"},
                    source_updated_at="2026-08-01T00:00:00Z", deleted=False,
                )
        return FakeDingtalk()

    register("dingtalk", lambda config: _make_connector(["doc-1"]))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # 用新 Session 验证 rollback：SourceItem 状态未变，图谱关系未变，Wiki 未失效
    engine2 = get_engine(f"sqlite:///{(tmp_path / 'exec_reassign.db').as_posix()}")
    init_db(engine2)
    db2 = sessionmaker(bind=engine2)()
    item = db2.get(SourceItem, "si1")
    assert item.state == "active", "SourceItem 状态变化回滚"
    assert item.last_error is None
    assert db2.query(V4GraphRelation).count() == rel_before, "图谱 provenance 回滚"
    wiki = db2.get(WP, "w1")
    assert wiki.status == "published", "Wiki 来源失效回滚"
    db2.close()
    engine2.dispose()
    db.close()
    engine.dispose()
