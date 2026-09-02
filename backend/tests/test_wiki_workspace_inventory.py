"""Phase 3.1：WikiWorkspace inventory（dry-run）测试。

覆盖：只读不写库、候选 key 按 notebook/显式绑定（不再由 ACL scope 单一派生）、
同 ACL 多 notebook 多候选、orphan、blocked（跨 scope/跨 workspace 来源）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiWorkspace,
    init_db,
)
from scripts.inventory_wiki_workspaces import run_inventory


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed_eng(db):
    nb1 = Notebook(id="nb1", name="工程一", group_id="engineering")
    nb2 = Notebook(id="nb2", name="工程二", group_id="engineering")
    db.add_all([nb1, nb2]); db.flush()
    p1 = Page(id="p1", notebook_id="nb1", title="t1", content="c")
    p2 = Page(id="p2", notebook_id="nb2", title="t2", content="c")
    db.add_all([p1, p2]); db.flush()
    w = WikiPage(id="w1", title="水箱", acl_scope='{"groups": ["engineering"]}',
                 status="published", source_page_ids='["p1", "p2"]', dirty=False)
    db.add(w)
    db.commit()


def test_dry_run_reads_only(db):
    _seed_eng(db)
    ws_before = db.query(WikiWorkspace).count()
    bind_before = db.query(NotebookWorkspaceBinding).count()
    page_before = db.query(Page).count()

    data = run_inventory(db)
    db.expire_all()

    # 不写库：workspace / binding / page 数量不变
    assert db.query(WikiWorkspace).count() == ws_before
    assert db.query(NotebookWorkspaceBinding).count() == bind_before
    assert db.query(Page).count() == page_before
    # session 无任何 pending 写入（零写入强断言）
    assert not db.new and not db.dirty and not db.deleted

    # 同 ACL 两个 notebook 无绑定 → 各自一个默认候选（key 按 notebook）
    assert data["stats"]["workspace_candidates"] == 2
    keys = {ws["key"] for ws in data["workspaces"]}
    assert keys == {
        access_control.workspace_key_for_notebook("nb1"),
        access_control.workspace_key_for_notebook("nb2"),
    }
    # scope 信息保留（权限/审计），但不再是唯一候选 key
    assert {ws["scope_id"] for ws in data["workspaces"]} == {"group:engineering"}


def test_inventory_key_not_derived_only_from_acl(db):
    """inventory 对同 ACL 多 notebook 产生多个候选 key（不是单一 ACL key）。"""
    _seed_eng(db)
    data = run_inventory(db)
    keys = {ws["key"] for ws in data["workspaces"]}
    assert len(keys) == 2
    # 无任何 candidate 使用旧的 scope 派生语义（无此函数 + 无 scope 全局 key）
    assert all(k.startswith("ws_nb_") for k in keys)
    assert not hasattr(access_control, "deterministic_workspace_key")
    # 同 ACL 多 notebook 默认候选给出合并提示
    hints = [ws.get("merge_hint") for ws in data["workspaces"]]
    assert all(h for h in hints)


def test_explicit_binding_component_single_candidate(db):
    """显式绑定（admin 建 workspace + 绑定两 notebook）→ 单一候选（key = workspace.key）。"""
    nb1 = Notebook(id="nb1", name="工程一", group_id="engineering")
    nb2 = Notebook(id="nb2", name="工程二", group_id="engineering")
    db.add_all([nb1, nb2]); db.flush()
    p1 = Page(id="p1", notebook_id="nb1", title="t1", content="c")
    p2 = Page(id="p2", notebook_id="nb2", title="t2", content="c")
    db.add_all([p1, p2]); db.flush()
    ws = WikiWorkspace(id="ws-shared", key="ws_shared_key", name="共享",
                       acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering", status="active")
    db.add(ws)
    db.flush()
    db.add_all([
        NotebookWorkspaceBinding(id="b1", notebook_id="nb1", workspace_id="ws-shared", status="active"),
        NotebookWorkspaceBinding(id="b2", notebook_id="nb2", workspace_id="ws-shared", status="active"),
    ])
    w = WikiPage(id="w1", title="水箱", acl_scope='{"groups": ["engineering"]}',
                 status="published", source_page_ids='["p1", "p2"]', dirty=False)
    db.add(w)
    db.commit()

    data = run_inventory(db)
    assert data["stats"]["workspace_candidates"] == 1
    cand = data["workspaces"][0]
    assert cand["key"] == "ws_shared_key"
    assert cand["merged_by"] == "explicit_binding"
    assert set(cand["notebook_ids"]) == {"nb1", "nb2"}
    assert cand["wiki_ids"] == ["w1"]
    assert cand["existing"] == "ws-shared"


def test_no_wiki_notebook_independent_candidate(db):
    nb1 = Notebook(id="nb1", name="工程一", group_id="engineering")
    nb2 = Notebook(id="nb2", name="销售", group_id="sales")
    db.add_all([nb1, nb2])
    p1 = Page(id="p1", notebook_id="nb1", title="t1", content="c")
    p2 = Page(id="p2", notebook_id="nb2", title="t2", content="c")
    db.add_all([p1, p2])
    db.commit()

    data = run_inventory(db)
    scopes = {ws["scope_id"] for ws in data["workspaces"]}
    assert "group:engineering" in scopes and "group:sales" in scopes
    # 不同 scope notebook 各自候选
    assert data["stats"]["workspace_candidates"] == 2


def test_orphan_wiki_reported(db):
    db.add(WikiPage(id="orphan1", title="无来源", acl_scope='{"groups": ["engineering"]}',
                    status="published", source_page_ids="[]", dirty=False))
    db.commit()
    data = run_inventory(db)
    assert data["stats"]["orphans"] == 1
    assert data["orphans"][0]["wiki_id"] == "orphan1"


def test_scope_conflict_blocked(db):
    # 同一 Wiki 挂两个不同 scope 的来源 Page → 来源 notebook 分属两个不同 ACL → blocked
    nb_eng = Notebook(id="nb-eng", name="工程", group_id="engineering")
    nb_sales = Notebook(id="nb-sales", name="销售", group_id="sales")
    db.add_all([nb_eng, nb_sales]); db.flush()
    p1 = Page(id="p1", notebook_id="nb-eng", title="t1", content="c")
    p2 = Page(id="p2", notebook_id="nb-sales", title="t2", content="c")
    db.add_all([p1, p2]); db.flush()
    w = WikiPage(id="w1", title="混合", acl_scope='{"groups": ["engineering"]}',
                 status="published", source_page_ids='["p1", "p2"]', dirty=False)
    db.add(w)
    db.commit()

    data = run_inventory(db)
    assert data["stats"]["blocked"] >= 1
    assert any(b["wiki_ids"] == ["w1"] for b in data["blocked"])


def test_cross_default_workspace_source_blocked(db):
    """同 ACL 但分属两个不同默认 notebook 候选的 Wiki（未显式绑定）→ blocked（跨 workspace 来源）。"""
    nb1 = Notebook(id="nb1", name="工程一", group_id="engineering")
    nb2 = Notebook(id="nb2", name="工程二", group_id="engineering")
    db.add_all([nb1, nb2]); db.flush()
    p1 = Page(id="p1", notebook_id="nb1", title="t1", content="c")
    p2 = Page(id="p2", notebook_id="nb2", title="t2", content="c")
    db.add_all([p1, p2]); db.flush()
    w = WikiPage(id="w1", title="跨默认", acl_scope='{"groups": ["engineering"]}',
                 status="published", source_page_ids='["p1", "p2"]', dirty=False)
    db.add(w)
    db.commit()

    data = run_inventory(db)
    blocked_w1 = [b for b in data["blocked"] if b.get("wiki_ids") == ["w1"]]
    assert blocked_w1, "跨两个默认 workspace 的 Wiki 必须 blocked（提示需显式绑定合并）"
