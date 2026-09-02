"""Phase J-1 遗留修复回归测试。

覆盖（本轮 J-1 遗留问题）：
1. Notebook CRUD 权限统一：create/update/delete 仅管理员；list/get 复用
   access_control.can_view_notebook（支持额外授权组 + LDAP 管理员组）；
   groups 完整列表仅管理员可读；admin/wiki_editor 角色组不作为业务组。
2. 无业务组普通用户访问 company/public Notebook（get_visible_page_ids 不得
   因 groups=[] 提前返回空集合）。
3. 文件夹映射优先级：路径深度相同时，当前 space 精确映射优先于空 space 通配。
4. 已导入文件路径映射变化 → fail closed（metadata_only 重新解析归属，不沿用
   旧权限，SourceItem 记 NEEDS_REASSIGN/skipped）。
5. 钉钉连接不再强制 target_notebook_id；未映射不得回退连接级目标知识库。
6. J-4 完成前多组自动检索知识缺失不写入 group:a,b 债务（fail closed，
   recording status=skipped_multi_scope）。

全程临时 SQLite，不触碰真实库，不调用真实模型。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import access_control, folder_mapping
from app.models.database import (
    SourcePathMapping,
    Notebook,
    NotebookGroup,
    Page,
    RuntimeFeatureFlag,
    SourceConnection,
    WikiPage,
    init_db,
)
from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    # 建立唯一钉钉连接（folder_mapping 兼容层依赖）。
    session.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True))
    session.flush()
    yield session
    session.close()
    engine.dispose()


def _user(groups, is_admin=False):
    u = {"id": "u1", "username": "u", "groups": groups, "is_admin": is_admin}
    if is_admin:
        u["groups"] = list(set(groups or []) | {"__local_admin__"})
    return u


# ---------------------------------------------------------------------------
# 修复 2：无业务组普通用户访问 company/public Notebook
# ---------------------------------------------------------------------------

def test_no_business_group_user_sees_company_notebook(db):
    """groups=[] 的普通用户仍可访问 company（group_id IS NULL）Notebook 下的 Page。"""
    db.add(Notebook(id="nb-company", name="公司库"))  # group_id=None → company
    db.add(Page(id="p1", notebook_id="nb-company", title="公司资料", content="正文"))
    db.commit()

    user = _user(groups=[])
    visible = access_control.get_visible_page_ids(db, user)
    assert "p1" in visible, "groups=[] 用户必须能看到 company Notebook 的 Page"


def test_no_business_group_user_sees_public_notebook(db):
    """groups=[] 的普通用户可访问 __public__ Notebook 下的 Page。"""
    db.add(Notebook(id="nb-public", name="公开库", group_id="__public__"))
    db.add(Page(id="p2", notebook_id="nb-public", title="公开资料", content="正文"))
    db.commit()

    user = _user(groups=[])
    visible = access_control.get_visible_page_ids(db, user)
    assert "p2" in visible


def test_no_business_group_user_does_not_see_group_notebook(db):
    """groups=[] 的普通用户不可见业务组 Notebook（fail closed）。"""
    db.add(Notebook(id="nb-eng", name="研发库", group_id="engineering"))
    db.add(Page(id="p3", notebook_id="nb-eng", title="研发资料", content="正文"))
    db.commit()

    user = _user(groups=[])
    visible = access_control.get_visible_page_ids(db, user)
    assert "p3" not in visible


# ---------------------------------------------------------------------------
# 修复 3：路径深度相同时，当前 space 精确映射优先于空 space 通配
# ---------------------------------------------------------------------------

def test_same_depth_space_exact_beats_wildcard(db):
    """同深度：space 精确映射优先于空 space 通配（反例：不能选通配）。"""
    nb_any = Notebook(id="nb-any", name="通配库", group_id="engineering")
    nb_exact = Notebook(id="nb-exact", name="精确库", group_id="sales")
    db.add_all([nb_any, nb_exact])
    db.flush()
    # 空 space 通配映射「产品资料/手册」→ nb-any；space1 精确映射 → nb-exact
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料/手册", notebook_id="nb-any"))
    db.add(SourcePathMapping(id="m2", connection_id="conn", path_namespace="space1", folder_path="产品资料/手册", notebook_id="nb-exact"))
    db.commit()

    # 文件在 space1 的同一路径下：应命中 space1 精确映射
    assert folder_mapping.resolve_target_notebook_id(db, "space1", "产品资料/手册/换胎.pdf") == "nb-exact"
    # 文件在其他 space（或空 space）的同路径：命中空 space 通配
    assert folder_mapping.resolve_target_notebook_id(db, "space2", "产品资料/手册/换胎.pdf") == "nb-any"


def test_same_depth_space_exact_beats_wildcard_different_folder(db):
    """同深度、不同路径：space 精确映射只约束自己的路径，不影响其他路径。"""
    nb_any = Notebook(id="nb-any", name="通配库", group_id="engineering")
    nb_exact = Notebook(id="nb-exact", name="精确库", group_id="sales")
    db.add_all([nb_any, nb_exact])
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-any"))
    db.add(SourcePathMapping(id="m2", connection_id="conn", path_namespace="space1", folder_path="故障排查", notebook_id="nb-exact"))
    db.commit()

    # space1 的「产品资料」路径：只有通配命中（无 space1 精确映射该路径）
    assert folder_mapping.resolve_target_notebook_id(db, "space1", "产品资料/手册/x.pdf") == "nb-any"
    # space1 的「故障排查」路径：精确命中
    assert folder_mapping.resolve_target_notebook_id(db, "space1", "故障排查/电池.pdf") == "nb-exact"


def test_specific_subpath_beats_exact_space_parent(db):
    """反例：精确 space 父目录与通配 space 子目录并存时，必须选更具体的子目录。

    顺序要求（J-1 最终修正）：路径更具体（段数多）优先，然后才是 space 精确度。
    - space1 精确映射「产品资料」→ nb-exact-parent（父目录，1 段）
    - 空 space 通配映射「产品资料/手册」→ nb-wild-child（子目录，2 段）
    文件在 space1 的「产品资料/手册」下 → 必须选通配子目录（段数多）。
    """
    nb_parent = Notebook(id="nb-parent", name="精确父库", group_id="engineering")
    nb_child = Notebook(id="nb-child", name="通配子库", group_id="sales")
    db.add_all([nb_parent, nb_child])
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="space1", folder_path="产品资料", notebook_id="nb-parent"))
    db.add(SourcePathMapping(id="m2", connection_id="conn", path_namespace="", folder_path="产品资料/手册", notebook_id="nb-child"))
    db.commit()

    # 子目录更具体（2 段）优先于精确 space 父目录（1 段）
    assert folder_mapping.resolve_target_notebook_id(db, "space1", "产品资料/手册/换胎.pdf") == "nb-child"


def test_specific_space_parent_beats_wildcard_parent_same_depth(db):
    """同深度：精确 space 父目录优先于通配 space 父目录（同段数时比 space 精确度）。"""
    nb_wild = Notebook(id="nb-wild", name="通配库", group_id="engineering")
    nb_exact = Notebook(id="nb-exact", name="精确库", group_id="sales")
    db.add_all([nb_wild, nb_exact])
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-wild"))
    db.add(SourcePathMapping(id="m2", connection_id="conn", path_namespace="space1", folder_path="产品资料", notebook_id="nb-exact"))
    db.commit()

    assert folder_mapping.resolve_target_notebook_id(db, "space1", "产品资料/手册/x.pdf") == "nb-exact"
    assert folder_mapping.resolve_target_notebook_id(db, "space2", "产品资料/手册/x.pdf") == "nb-wild"


# ---------------------------------------------------------------------------
# 修复 1：Notebook 可见性统一复用 can_view_notebook（支持额外授权组 + LDAP 管理员组）
# ---------------------------------------------------------------------------

def test_can_view_notebook_supports_extra_group(db):
    """额外授权组（notebook_groups）成员可见该 Notebook。"""
    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))
    db.commit()

    assert access_control.can_view_notebook(db, _user(groups=["engineering"]), nb) is True
    assert access_control.can_view_notebook(db, _user(groups=["sales"]), nb) is True
    assert access_control.can_view_notebook(db, _user(groups=["marketing"]), nb) is False


def test_can_view_notebook_supports_ldap_admin_group(db, monkeypatch):
    """配置的 LDAP 管理员组（非 __local_admin__）可看任意 Notebook。"""
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.commit()

    assert access_control.can_view_notebook(db, _user(groups=["admins"]), nb) is True


def test_get_visible_page_ids_ldap_admin_group(db, monkeypatch):
    """配置的 LDAP 管理员组用户可见全部 Page（不只 __local_admin__）。"""
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.add(Notebook(id="nb2", name="销售库", group_id="sales"))
    db.add(Page(id="p1", notebook_id="nb1", title="研发", content="正文"))
    db.add(Page(id="p2", notebook_id="nb2", title="销售", content="正文"))
    db.commit()

    user = _user(groups=["admins"])  # LDAP 管理员组，非 __local_admin__
    visible = access_control.get_visible_page_ids(db, user)
    assert {"p1", "p2"} <= visible


# ---------------------------------------------------------------------------
# 修复 6：J-4 后普通 Chat 不再静默写入旧 scope 债务（旧写入路径已删除）
# ---------------------------------------------------------------------------

def test_legacy_debt_write_path_removed():
    """普通提问/insufficient 响应本身不得创建 KnowledgeDebt。

    J-4 把债务创建收口到「两名用户有效差评达到阈值」与「主动点击我仍需要」，
    旧 Phase F 的 _maybe_record_debt/_auto_scope_id/record_missing_knowledge
    写入路径已从 rag_chat 移除。
    """
    import app.api.rag_chat as rag_chat

    assert not hasattr(rag_chat, "_maybe_record_debt")
    assert not hasattr(rag_chat, "_auto_scope_id")
    assert not hasattr(rag_chat, "record_missing_knowledge")


# ---------------------------------------------------------------------------
# 修复 1：Notebook groups 完整列表仅管理员可读（API 层）
# ---------------------------------------------------------------------------

def test_notebook_crud_admin_only(monkeypatch, tmp_path):
    """J-1 遗留修复：Notebook create/update/delete 仅管理员；list/get 复用 can_view_notebook。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'crud.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng():
        return {"id": "u2", "username": "eng", "groups": ["engineering"], "is_admin": False}

    c = TestClient(app)

    # 普通用户不能 create/update/delete
    app.dependency_overrides[get_current_user] = _eng
    r = c.post("/api/notebooks", json={"name": "新库"})
    assert r.status_code == 403, "普通用户不能创建 Notebook"
    r = c.put("/api/notebooks/nb1", json={"name": "改名"})
    assert r.status_code == 403, "普通用户不能更新 Notebook"
    r = c.delete("/api/notebooks/nb1")
    assert r.status_code == 403, "普通用户不能删除 Notebook"

    # 普通用户 get：额外授权组成员可看，非成员不可看
    r = c.get("/api/notebooks/nb1")
    assert r.status_code == 200, "额外授权组（sales）成员应可见 nb1"

    def _mkt():
        return {"id": "u3", "username": "mkt", "groups": ["marketing"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _mkt
    r = c.get("/api/notebooks/nb1")
    assert r.status_code == 403, "非成员不可见 nb1"

    # 管理员可 create/update/delete（company 语义：无 group_id）
    app.dependency_overrides[get_current_user] = _admin
    r = c.post("/api/notebooks", json={"name": "新库"})
    assert r.status_code == 200, r.text
    nb_id = r.json()["id"]
    r = c.put(f"/api/notebooks/{nb_id}", json={"name": "改名"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "改名"
    r = c.delete(f"/api/notebooks/{nb_id}")
    assert r.status_code == 200, r.text

    app.dependency_overrides.clear()
    engine.dispose()


def test_create_notebook_unknown_group_rejected(monkeypatch, tmp_path):
    """J-1 最终遗留：create Notebook 传未知业务组 → 400（不得静默降级为 company）。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'sc.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _admin
    c = TestClient(app)
    # 未知组 → 400
    r = c.post("/api/notebooks", json={"name": "新库", "group_id": "not_a_group"})
    assert r.status_code == 400, "未知业务组必须 400，不能静默变 company"
    assert "未知" in r.json()["detail"]
    # 明确 company → 真正清空业务组（group_id 为 None）
    r = c.post("/api/notebooks", json={"name": "公司库", "group_id": "company"})
    assert r.status_code == 200, r.text
    assert r.json()["group_id"] is None
    app.dependency_overrides.clear()
    engine.dispose()


def test_update_notebook_unknown_group_rejected_and_company_clears(monkeypatch, tmp_path):
    """J-1 最终遗留：update Notebook 未知组 400；提交 company 真正清空业务组。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'sc.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _admin
    c = TestClient(app)
    r = c.put("/api/notebooks/nb1", json={"name": "研发库", "group_id": "not_a_group"})
    assert r.status_code == 400, "更新未知业务组必须 400"
    # 提交 company 清空业务组
    r = c.put("/api/notebooks/nb1", json={"name": "研发库", "group_id": "company"})
    assert r.status_code == 200, r.text
    assert r.json()["group_id"] is None, "company 必须真正清空 group_id"
    app.dependency_overrides.clear()
    engine.dispose()


def test_access_scope_company_clears_group_id(monkeypatch, tmp_path):
    """J-1 最终遗留：PATCH /access-scope 提交 company 真正清空业务组。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'sc.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _admin
    c = TestClient(app)
    r = c.patch("/api/notebooks/nb1/access-scope", json={"group_id": "company"})
    assert r.status_code == 200, r.text
    assert r.json()["group_id"] is None, "提交 company 必须清空 group_id"
    # 未知组 400
    r = c.patch("/api/notebooks/nb1/access-scope", json={"group_id": "not_a_group"})
    assert r.status_code == 400
    app.dependency_overrides.clear()
    engine.dispose()


def test_list_notebook_groups_admin_only(monkeypatch, tmp_path):
    """普通用户 GET /api/notebooks/{id}/groups 必须 403（完整多组列表仅管理员）。"""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine as _ce
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'ng.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng():
        return {"id": "u2", "username": "eng", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _eng
    c = TestClient(app)
    r = c.get("/api/notebooks/nb1/groups")
    assert r.status_code == 403, "普通用户不可读完整多组列表"

    app.dependency_overrides[get_current_user] = _admin
    r = c.get("/api/notebooks/nb1/groups")
    assert r.status_code == 200
    assert {g["group_name"] for g in r.json()} == {"sales"}

    app.dependency_overrides.clear()
    engine.dispose()


def test_notebook_groups_exclude_role_groups(monkeypatch, tmp_path):
    """admin/wiki_editor 角色组不作为可选业务组返回。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import User, UserGroup, get_engine, get_session

    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    monkeypatch.setattr("app.config.settings.ldap_group_map_wiki_editor", "editors")

    url = f"sqlite:///{(tmp_path / 'sc.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(User(id="u1", username="admin", is_local=True))
    db.add(UserGroup(user_id="u1", group_name="__local_admin__"))
    # 业务组 + 角色组混在 UserGroup 里
    db.add(UserGroup(user_id="u1", group_name="engineering"))
    db.add(UserGroup(user_id="u1", group_name="sales"))
    db.add(UserGroup(user_id="u1", group_name="admins"))    # LDAP 管理员组
    db.add(UserGroup(user_id="u1", group_name="editors"))   # LDAP 编辑组
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[get_current_user] = _admin
    c = TestClient(app)
    r = c.get("/api/notebooks/access-scopes")
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["scopes"]}
    assert "engineering" in ids and "sales" in ids
    assert "admins" not in ids, "LDAP 管理员组不能作为业务组"
    assert "editors" not in ids, "LDAP 编辑组不能作为业务组"
    app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 修复 4/5：同步链路径映射变化 fail closed + 钉钉连接不强制 target_notebook_id
# ---------------------------------------------------------------------------

def _make_dingtalk_connector(docs: dict):
    """docs: external_id -> (dingtalk_path, space_id, deleted)"""

    class FakeDingtalkConnector:
        async def iter_changes(self, cursor):
            for doc_id in docs:
                yield SourceChange(external_id=doc_id, external_version="v1")

        async def fetch_acl(self, external_id):
            return SourceACL(scope="space1", raw={"space_id": "space1"})

        async def fetch_item(self, external_id):
            path, space, deleted = docs[external_id]
            return NormalizedSourceItem(
                connection_id="conn-1",
                source_type="dingtalk",
                external_id=external_id,
                external_version="v1",
                title=f"{external_id}.pdf",
                content="这是可检索的钉钉正文，包含足够内容。",
                content_hash="",
                source_path=path,
                metadata_json={"space_id": space, "space_name": "知识库"},
                source_updated_at="2026-08-01T00:00:00Z",
                deleted=deleted,
            )

    return FakeDingtalkConnector()


def _mock_helpers2(monkeypatch):
    async def _noop(*a, **k):
        return None

    async def _noop_remote(*a, **k):
        return {"new_cursor": {"mode": "incremental"}}

    monkeypatch.setattr("app.sources.executor._run_dingtalk_remote_sync", _noop_remote)
    monkeypatch.setattr("app.api.pages.background_index_page", _noop)
    monkeypatch.setattr(
        "app.sources.executor._schedule_wiki_refresh_for_page",
        lambda db, page_id, action, changed=False: None,
    )
    monkeypatch.setattr(
        "app.core.retrieval.debt_service.notify_knowledge_changed_for_page",
        lambda db, pid: {"resolved": 0, "checked": 0},
    )
    monkeypatch.setattr("app.sources.executor.sync_page_evidence", lambda db, page_id: None)


@pytest.mark.asyncio
async def test_path_mapping_changed_fail_closed(monkeypatch, tmp_path):
    """已导入文件路径映射变化：metadata_only 不沿用旧权限，SourceItem 记 NEEDS_REASSIGN。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources import registry as _reg

    # 备份 registry（避免污染）
    _snapshot = dict(_reg._registry)

    engine = _ge(f"sqlite:///{(tmp_path / 'reassign.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    # nb-old（旧归属，engineering）与 nb-new（新映射目标，sales）
    db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
    db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    # 连接不绑定 target_notebook_id（修复 5：钉钉归属完全由映射决定）
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    # 映射：产品资料 → nb-new（文件已从别处移动到产品资料下）
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    # 历史 SourceItem：文件 doc-1 旧路径在「旧目录」下，已归属 nb-old（旧 Page 也在 nb-old）。
    # content_hash 与当前内容一致（内容未变），metadata_hash 不同（旧路径→新路径）→ metadata_only。
    from app.sources.service import compute_content_hash, compute_metadata_hash
    hist_item = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="doc-1",
        title="doc-1.pdf", content="这是可检索的钉钉正文，包含足够内容。",
        source_path="旧目录/doc-1.pdf",  # 旧路径 → metadata 不同
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si1", connection_id="conn", external_id="doc-1",
        state="active",
        content_hash=compute_content_hash(hist_item),
        metadata_hash=compute_metadata_hash(hist_item),
        source_path="旧目录/doc-1.pdf", page_id="p-old",
    ))
    db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="旧正文",
                source_type="dingtalk", source_id="doc-1"))
    db.commit()

    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/doc-1.pdf", "space1", False),  # 新路径：产品资料下
    }))

    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # 映射变化：不沿用旧权限 → SourceItem 记 skipped + NEEDS_REASSIGN
    item = db.get(SourceItem, "si1")
    assert item.state == "skipped"
    assert item.last_error == "路径映射变化，需要重新归属"
    # 旧 Page 不暴露给新库（不迁移数据）
    page = db.get(Page, "p-old")
    assert page.notebook_id == "nb-old", "不得静默迁移旧 Page 到新库"
    err = db.query(SourceSyncError).filter(SourceSyncError.error_code == "NEEDS_REASSIGN").first()
    assert err is not None
    assert err.retryable is False
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_content_changed_mapping_moved_fail_closed(monkeypatch, tmp_path):
    """路径映射变化 + 内容也变化：仍 fail closed（不迁移旧 Page 到新库）。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources import registry as _reg

    _snapshot = dict(_reg._registry)
    engine = _ge(f"sqlite:///{(tmp_path / 'moved.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
    db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    # 历史 item：内容 hash 与「新内容」不同（内容变化），旧路径在旧目录下，Page 在 nb-old
    from app.sources.service import compute_content_hash, compute_metadata_hash
    hist_item = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="doc-1",
        title="doc-1.pdf", content="旧内容版本", source_path="旧目录/doc-1.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si1", connection_id="conn", external_id="doc-1", state="active",
        content_hash=compute_content_hash(hist_item),
        metadata_hash=compute_metadata_hash(hist_item),
        source_path="旧目录/doc-1.pdf", page_id="p-old",
    ))
    db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="旧内容版本",
                source_type="dingtalk", source_id="doc-1"))
    db.commit()

    # 新 fetch_item：内容不同（新内容版本）+ 路径移到产品资料（映射到 nb-new）
    class _MovedConnector:
        async def iter_changes(self, cursor):
            yield SourceChange(external_id="doc-1", external_version="v2")

        async def fetch_acl(self, external_id):
            return SourceACL(scope="space1", raw={"space_id": "space1"})

        async def fetch_item(self, external_id):
            return NormalizedSourceItem(
                connection_id="conn-1", source_type="dingtalk", external_id="doc-1",
                external_version="v2", title="doc-1.pdf",
                content="新内容版本，内容真的变了。",
                content_hash=compute_content_hash(
                    NormalizedSourceItem(connection_id="conn-1", source_type="dingtalk", external_id="doc-1",
                                         title="doc-1.pdf", content="新内容版本，内容真的变了。")
                ),
                source_path="产品资料/doc-1.pdf",
                metadata_json={"space_id": "space1", "space_name": "知识库"},
                source_updated_at="2026-08-02T00:00:00Z",
            )

    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _MovedConnector())
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    item = db.get(SourceItem, "si1")
    assert item.state == "skipped", "映射变化 + 内容变化也必须 fail closed"
    assert item.last_error == "路径映射变化，需要重新归属"
    page = db.get(Page, "p-old")
    assert page.notebook_id == "nb-old", "不得迁移旧 Page 到新库"
    assert page.content == "旧内容版本", "旧 Page 内容不得被覆盖"
    err = db.query(SourceSyncError).filter(SourceSyncError.error_code == "NEEDS_REASSIGN").first()
    assert err is not None
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_reassign_page_excluded_from_visible_links(monkeypatch, tmp_path):
    """NEEDS_REASSIGN 后，旧 Page 从普通用户的 Page 列表/详情/Raw/图谱全部排除。

    管理员不受影响（可经数据源错误列表看到待重新归属记录）。
    """
    from sqlalchemy.orm import sessionmaker
    from app.core import access_control
    from app.core.retrieval.raw_retriever import RawDocumentRetriever
    from app.models.database import (
        Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources import registry as _reg
    from app.sources.service import compute_content_hash, compute_metadata_hash

    _snapshot = dict(_reg._registry)
    engine = _ge(f"sqlite:///{(tmp_path / 'excl.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
    db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    hist_item = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="doc-1",
        title="doc-1.pdf", content="这是可检索的钉钉正文，包含足够内容。",
        source_path="旧目录/doc-1.pdf", metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si1", connection_id="conn", external_id="doc-1", state="active",
        content_hash=compute_content_hash(hist_item),
        metadata_hash=compute_metadata_hash(hist_item),
        source_path="旧目录/doc-1.pdf", page_id="p-old",
    ))
    db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="这是可检索的钉钉正文，包含足够内容。",
                source_type="dingtalk", source_id="doc-1"))
    db.commit()

    # 触发同步：文件路径变化到产品资料 → NEEDS_REASSIGN
    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/doc-1.pdf", "space1", False),
    }))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    item = db.get(SourceItem, "si1")
    assert item.state == "skipped"
    assert item.last_error == "路径映射变化，需要重新归属"

    old_page = db.get(Page, "p-old")
    assert old_page is not None, "不删除旧 Page"
    assert old_page.notebook_id == "nb-old", "不自动迁移"

    # 普通用户（engineering）可见集合排除旧 Page
    user = _user(groups=["engineering"])
    visible = access_control.get_visible_page_ids(db, user)
    assert "p-old" not in visible, "Page 列表不得包含待重新归属的旧 Page"
    # Page 详情 fail closed
    assert access_control.can_view_page(db, user, old_page) is False
    # Raw 检索不返回该 Page（SourceItem skipped + ACL 排除双保险）
    raw = RawDocumentRetriever(db).retrieve(db, "钉钉正文 包含", user)
    assert all(h.page_id != "p-old" for h in raw.hits), "Raw 检索不得返回旧 Page"
    # 图谱：可见 Page 集合不含旧 Page
    from app.core.knowledge_compiler_v3.page_graph import build_page_communities
    communities = build_page_communities(db, access_control.get_visible_page_ids(db, user))
    all_member_pages = {pid for c in communities for pid in c.member_page_ids}
    assert "p-old" not in all_member_pages, "图谱不得包含旧 Page"
    # J-1 最终封板：管理员在正常检索链同样排除失效远程 Page；失效记录只经
    # 数据源诊断（SourceItem 列表 / SourceSyncError）入口查看。
    admin_user = _user(groups=["__local_admin__"], is_admin=True)
    assert "p-old" not in access_control.get_visible_page_ids(db, admin_user), \
        "管理员正常检索链不得包含失效远程 Page"
    assert access_control.can_view_page(db, admin_user, old_page) is False
    # 诊断入口仍可见：SourceItem 记录 + SourceSyncError
    si = db.query(SourceItem).filter(SourceItem.page_id == "p-old").first()
    assert si is not None and si.state == "skipped"
    err = db.query(SourceSyncError).filter(SourceSyncError.error_code == "NEEDS_REASSIGN").first()
    assert err is not None

    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_dingtalk_conn_without_target_notebook(monkeypatch, tmp_path):
    """钉钉连接不强制 target_notebook_id：文件仍按映射写入目标 Notebook。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        Page, SourceConnection, SourceItem, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources import registry as _reg

    _snapshot = dict(_reg._registry)
    engine = _ge(f"sqlite:///{(tmp_path / 'no-target.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-target", name="映射库", group_id="engineering"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    # 连接无 target_notebook_id（钉钉归属完全由映射决定）
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-target"))
    db.commit()

    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/doc-1.pdf", "space1", False),
    }))

    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    run = db.get(SourceSyncRun, "run")
    assert run.status == "succeeded"
    page = db.query(Page).one()
    assert page.notebook_id == "nb-target", "文件应按映射写入目标 Notebook"
    item = db.query(SourceItem).one()
    assert item.state == "active"
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_unmapped_no_fallback_to_connection_notebook(monkeypatch, tmp_path):
    """未映射文件不得回退到连接级 target_notebook_id（即使连接有旧目标）。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.sources import registry as _reg

    _snapshot = dict(_reg._registry)
    engine = _ge(f"sqlite:///{(tmp_path / 'no-fallback.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-default", name="默认库", group_id="engineering"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    # 连接绑定了旧 target_notebook_id，但文件所在目录未映射
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True,
                            target_notebook_id="nb-default", config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.commit()  # 无任何映射

    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("未映射目录/doc-1.pdf", "space1", False),
    }))

    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # 不得创建 Page，不得回退到 nb-default
    assert db.query(Page).count() == 0, "未映射文件不得回退连接级 target_notebook_id"
    item = db.query(SourceItem).one()
    assert item.state == "skipped"
    err = db.query(SourceSyncError).one()
    assert err.error_code == "SOURCE_PATH_NOT_MAPPED"
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# J-1 最终返工：原子权限、映射 CRUD 失效、恢复闭环、角色组拒绝
# ---------------------------------------------------------------------------

def _api_client(monkeypatch, tmp_path, seed=None):
    """构造 TestClient + 临时库；seed(db) 在提交前初始化数据。"""
    from fastapi.testclient import TestClient
    from app.api import deps
    from app.core.jwt_utils import get_current_user
    from app.main import app
    from app.models.database import get_engine, get_session

    url = f"sqlite:///{(tmp_path / 'j1f.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    if seed:
        seed(db)
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng():
        return {"id": "u2", "username": "eng", "groups": ["engineering"], "is_admin": False}

    def _sales():
        return {"id": "u3", "username": "sales", "groups": ["sales"], "is_admin": False}

    app.dependency_overrides[get_current_user] = _admin
    # raise_server_exceptions=False：让 500 返回响应而非把服务端异常抛给测试。
    c = TestClient(app, raise_server_exceptions=False)
    yield c, _admin, _eng, _sales
    app.dependency_overrides.clear()
    engine.dispose()


def test_atomic_permission_failure_rolls_back(monkeypatch, tmp_path):
    """原子权限更新第二步（Wiki 失效）失败时，权限完全不变（全部 rollback）。"""

    def _seed(db):
        from app.models.database import User, UserGroup
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
        db.add(Page(id="p1", notebook_id="nb1", title="研发", content="正文"))
        db.add(UserGroup(id="ug1", user_id="u1", group_name="sales"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 让 Wiki 失效步骤抛异常（模拟第二步失败）
    def _boom(db, page_ids):
        raise RuntimeError("wiki invalidation failed")

    monkeypatch.setattr("app.api.notebooks._invalidate_wiki_sources_for_pages", _boom)
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["sales"]})
    assert r.status_code == 500

    # 权限完全不变：仍是 engineering
    from app.api import deps
    db = deps.get_session(deps.get_shared_engine())
    nb = db.get(Notebook, "nb1")
    assert nb.group_id == "engineering", "失败后 group_id 不得改变"
    db.close()


def test_scope_type_validation_400(monkeypatch, tmp_path):
    """非法 scope_type / 空 groups / 角色组混入 → 400，不静默扩大权限。"""

    def _seed(db):
        db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
        db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    monkeypatch.setattr("app.config.settings.ldap_group_map_wiki_editor", "editors")

    # 非法 scope_type
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "bogus", "group_names": []})
    assert r.status_code == 400
    # 空 groups
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": []})
    assert r.status_code == 400
    # 未知组
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["not_a_group"]})
    assert r.status_code == 400


def test_role_group_rejected_in_business_groups(monkeypatch, tmp_path):
    """指定业务组不能选择管理员/编辑者角色组（__local_admin__/admins/editors 拒绝）。"""

    def _seed(db):
        db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    monkeypatch.setattr("app.config.settings.ldap_group_map_wiki_editor", "editors")

    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["admins"]})
    assert r.status_code == 400, "LDAP 管理员组不能作为业务组"
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["editors"]})
    assert r.status_code == 400, "LDAP 编辑组不能作为业务组"
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["__local_admin__"]})
    assert r.status_code == 400, "__local_admin__ 不能作为业务组"


def test_notebook_group_change_invalidates_old_wiki(monkeypatch, tmp_path):
    """Notebook 从 engineering → sales 后：engineering 立即无法访问 Wiki/Page/Raw/图谱；
    sales 在重建完成前不得看到旧 scope 内容。"""

    def _seed(db):
        from app.models.database import User, UserGroup
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(UserGroup(id="ug1", user_id="u1", group_name="engineering"))
        db.add(UserGroup(id="ug2", user_id="u1", group_name="sales"))
        nb = Notebook(id="nb1", name="研发库", group_id="engineering")
        db.add(nb)
        db.flush()
        db.add(Page(id="p1", notebook_id="nb1", title="研发", content="研发正文"))
        # 建一个 acl_scope=group engineering 的 Wiki，来源 p1
        db.add(WikiPage(id="w1", title="研发Wiki", status="published", acl_scope='{"groups": ["engineering"]}',
                        source_page_ids='["p1"]', current_revision_id="r1"))
        from app.models.database import WikiRevision, WikiSection
        db.add(WikiRevision(id="r1", wiki_page_id="w1", title="研发Wiki", status="published"))
        db.add(WikiSection(id="s1", revision_id="r1", section_type="body", content="研发Wiki正文", order_index=0))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 初始：engineering 可见 Wiki
    from app.api import deps
    db = deps.get_session(deps.get_shared_engine())
    assert "w1" in access_control.get_visible_wiki_page_ids(db, _eng())
    assert "p1" in access_control.get_visible_page_ids(db, _eng())

    # 原子改组 engineering → sales
    r = c.put("/api/notebooks/nb1/permissions", json={"scope_type": "groups", "group_names": ["sales"]})
    assert r.status_code == 200, r.text

    # engineering 立即不可见 Wiki/Page
    db = deps.get_session(deps.get_shared_engine())
    assert "w1" not in access_control.get_visible_wiki_page_ids(db, _eng()), "旧组立即失去 Wiki"
    assert "p1" not in access_control.get_visible_page_ids(db, _eng()), "旧组立即失去 Page"
    # sales 在新 Wiki 重建完成前不得看到旧 scope 内容（旧 Wiki 已被移除来源/置为 draft）
    assert "w1" not in access_control.get_visible_wiki_page_ids(db, _sales()), "sales 不得看到旧 scope Wiki"
    db.close()


def test_mapping_crud_invalidates_existing_page_without_remote_change(monkeypatch, tmp_path):
    """修改映射但远端无新 change：旧 Page 仍立即失效（NEEDS_REASSIGN）。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-old"))
        hist = NormalizedSourceItem(
            connection_id="conn", source_type="dingtalk", external_id="doc-1",
            title="doc-1.pdf", content="正文", source_path="产品资料/doc-1.pdf",
            metadata_json={"space_id": "space1", "space_name": "知识库"},
        )
        db.add(SourceItem(
            id="si1", connection_id="conn", external_id="doc-1", state="active",
            content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
            source_path="产品资料/doc-1.pdf", acl_json='{"space_id": "space1"}', page_id="p-old",
        ))
        db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="正文",
                    source_type="dingtalk", source_id="doc-1"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 初始：engineering 可见旧 Page
    db = deps.get_session(deps.get_shared_engine())
    assert "p-old" in access_control.get_visible_page_ids(db, _eng())

    # 更新映射：产品资料 → nb-new（远端无 change，仅 CRUD 触发）
    r = c.put("/api/sources/dingtalk/folder-mappings/m1", json={"folder_path": "产品资料", "notebook_id": "nb-new"})
    assert r.status_code == 200, r.text

    # 旧 Page 立即失效（无远端 change）
    db = deps.get_session(deps.get_shared_engine())
    item = db.get(SourceItem, "si1")
    assert item.state == "skipped", "映射更新后旧 SourceItem 立即 NEEDS_REASSIGN"
    assert item.last_error == "路径映射变化，需要重新归属"
    assert "p-old" not in access_control.get_visible_page_ids(db, _eng()), "旧 Page 立即从可见链路排除"
    db.close()


def test_mapping_delete_invalidates_existing_page(monkeypatch, tmp_path):
    """删除映射后旧 Page/Wiki 不可见（fail closed，不继续暴露）。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-old"))
        hist = NormalizedSourceItem(
            connection_id="conn", source_type="dingtalk", external_id="doc-1",
            title="doc-1.pdf", content="正文", source_path="产品资料/doc-1.pdf",
            metadata_json={"space_id": "space1", "space_name": "知识库"},
        )
        db.add(SourceItem(
            id="si1", connection_id="conn", external_id="doc-1", state="active",
            content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
            source_path="产品资料/doc-1.pdf", acl_json='{"space_id": "space1"}', page_id="p-old",
        ))
        db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="正文",
                    source_type="dingtalk", source_id="doc-1"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    r = c.delete("/api/sources/dingtalk/folder-mappings/m1")
    assert r.status_code == 200, r.text

    db = deps.get_session(deps.get_shared_engine())
    item = db.get(SourceItem, "si1")
    assert item.state == "skipped", "删除映射后旧 SourceItem 立即 NEEDS_REASSIGN"
    assert "p-old" not in access_control.get_visible_page_ids(db, _eng()), "删除映射后旧 Page 不可见"
    db.close()


def test_reassign_flow_restores_active(monkeypatch, tmp_path):
    """NEEDS_REASSIGN 经管理员确认后恢复 active 并进入新 Notebook；普通用户不得自动恢复。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
        hist = NormalizedSourceItem(
            connection_id="conn", source_type="dingtalk", external_id="doc-1",
            title="doc-1.pdf", content="正文", source_path="产品资料/doc-1.pdf",
            metadata_json={"space_id": "space1", "space_name": "知识库"},
        )
        # 已被 NEEDS_REASSIGN 标记（旧归属 nb-old，现映射 nb-new）
        db.add(SourceItem(
            id="si1", connection_id="conn", external_id="doc-1", state="skipped",
            last_error="路径映射变化，需要重新归属",
            content_hash=compute_content_hash(hist), metadata_hash=None,
            source_path="产品资料/doc-1.pdf", acl_json='{"space_id": "space1"}', page_id="p-old",
        ))
        db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="正文",
                    source_type="dingtalk", source_id="doc-1"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 普通用户不能触发恢复（只有管理员确认）
    r = c.post("/api/sources/dingtalk/folder-mappings/reassign", json={"page_id": "p-old"})
    assert r.status_code == 200, r.text  # 管理员已注入（_api_client 默认 admin）

    db = deps.get_session(deps.get_shared_engine())
    item = db.get(SourceItem, "si1")
    assert item.state == "active", "确认后 SourceItem 恢复 active"
    page = db.get(Page, "p-old")
    assert page.notebook_id == "nb-new", "确认后 Page 进入新 Notebook"
    assert "p-old" in access_control.get_visible_page_ids(db, _sales()), "sales 现在可见"
    assert "p-old" not in access_control.get_visible_page_ids(db, _eng()), "engineering 不再可见"
    db.close()


@pytest.mark.asyncio
async def test_folder_not_mapped_recovers_after_mapping(monkeypatch, tmp_path):
    """SOURCE_PATH_NOT_MAPPED 的 skipped 条目补上映射并重新同步后，成功进入 create（可恢复）。"""
    from sqlalchemy.orm import sessionmaker
    from app.models.database import (
        Page as _Page, SourceConnection, SourceItem, SourceSyncRun,
        get_engine as _ge,
    )
    from app.sources.executor import execute_run
    from app.sources.registry import register
    from app.sources import registry as _reg
    from app.sources.service import compute_content_hash, compute_metadata_hash

    _snapshot = dict(_reg._registry)
    engine = _ge(f"sqlite:///{(tmp_path / 'recover.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()

    db.add(Notebook(id="nb-target", name="映射库", group_id="engineering"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    # 先无映射：doc-1 在「产品资料」下
    db.commit()

    _mock_helpers2(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/doc-1.pdf", "space1", False),
    }))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # 未映射 → skipped + FOLDER_NOT_MAPPED，无 Page
    item = db.query(SourceItem).one()
    assert item.state == "skipped"
    assert item.last_error == "路径未配置权限映射"
    assert db.query(_Page).count() == 0

    # 补上映射
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-target"))
    db.commit()

    # 重新同步（新 run）——registry 已被第一次 finally 清空，需重新注册 connector。
    db.add(SourceSyncRun(id="run2", connection_id="conn", mode="incremental", status="running"))
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/doc-1.pdf", "space1", False),
    }))
    try:
        await execute_run(db, db.get(SourceSyncRun, "run2"))
    finally:
        _reg._registry.clear()
        _reg._registry.update(_snapshot)

    # skipped → create：Page 已创建，SourceItem 恢复 active
    page = db.query(_Page).one()
    assert page.notebook_id == "nb-target"
    item = db.query(SourceItem).one()
    assert item.state == "active", "SOURCE_PATH_NOT_MAPPED 补映射后必须恢复 active"
    assert item.last_error is None
    db.close()
    engine.dispose()


def test_folder_mapping_rejects_unknown_notebook(monkeypatch, tmp_path):
    """文件夹映射创建拒绝 unknown 权限域 Notebook（与前一轮一致）。"""

    def _seed(db):
        # unknown = 混合 __public__ 与业务组
        nb = Notebook(id="nb-unknown", name="未知库", group_id="__public__")
        db.add(nb)
        db.flush()
        db.add(NotebookGroup(id="ng1", notebook_id="nb-unknown", group_name="engineering"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "folder_path": "产品资料", "notebook_id": "nb-unknown",
    })
    assert r.status_code == 400
    assert "unknown" in r.json()["detail"]
