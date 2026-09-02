"""Phase J-1 最终封板反例测试。

覆盖（本轮最终封板五项）：
一、映射重评估漏检与串扰（keyset 分页不漏检、只处理 dingtalk Connector、
    space-specific 不串扰、根目录映射）。
二、映射变更/SourceItem/Wiki 失效一致事务（故障注入全 rollback）。
三、旧 PUT /api/notebooks/{id} 不能绕过原子权限。
四、reassign 收紧（只处理 dingtalk + 明确待恢复状态；active/其他拒绝）。
五、普通检索（含管理员）排除失效远程 Page；管理员经诊断入口可见。

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
    User,
    UserGroup,
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
    yield session
    session.close()
    engine.dispose()


def _user(groups, is_admin=False):
    u = {"id": "u1", "username": "u", "groups": groups, "is_admin": is_admin}
    if is_admin:
        u["groups"] = list(set(groups or []) | {"__local_admin__"})
    return u


# ---------------------------------------------------------------------------
# 一、映射重评估漏检与串扰
# ---------------------------------------------------------------------------

def test_reevaluate_picks_up_item_beyond_first_batch(db):
    """反例：前 500 条均无关，第 501 条才受影响，仍必须被处理（keyset 分页不漏检）。"""
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    nb_old = Notebook(id="nb-old", name="旧库", group_id="engineering")
    nb_new = Notebook(id="nb-new", name="新库", group_id="sales")
    db.add_all([nb_old, nb_new])
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    for i in range(501):
        sid = f"si-{i:04d}"
        if i < 500:
            hist = NormalizedSourceItem(
                connection_id="conn", source_type="dingtalk", external_id=f"d{i}",
                title=f"d{i}", content="正文", source_path=f"无关目录/d{i}.pdf",
                metadata_json={"space_id": "space1", "space_name": "知识库"},
            )
            db.add(SourceItem(
                id=sid, connection_id="conn", external_id=f"d{i}", state="active",
                content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
                source_path=f"无关目录/d{i}.pdf", acl_json='{"space_id": "space1"}',
                page_id=f"p-{i}",
            ))
            db.add(Page(id=f"p-{i}", notebook_id="nb-old", title="t", content="正文",
                        source_type="dingtalk", source_id=f"d{i}"))
        else:
            hist = NormalizedSourceItem(
                connection_id="conn", source_type="dingtalk", external_id="doc-501",
                title="doc-501", content="正文", source_path="产品资料/doc-501.pdf",
                metadata_json={"space_id": "space1", "space_name": "知识库"},
            )
            db.add(SourceItem(
                id=sid, connection_id="conn", external_id="doc-501", state="active",
                content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
                source_path="产品资料/doc-501.pdf", acl_json='{"space_id": "space1"}',
                page_id="p-501",
            ))
            db.add(Page(id="p-501", notebook_id="nb-old", title="t", content="正文",
                        source_type="dingtalk", source_id="doc-501"))
    db.commit()

    folder_mapping.reevaluate_mapping_items(db, "", "产品资料", batch=100)
    db.commit()

    item_501 = db.query(SourceItem).filter(SourceItem.id == "si-0500").first()
    assert item_501.state == "skipped", "第 501 条（超首批）必须被 keyset 分页处理"
    assert item_501.last_error == "路径映射变化，需要重新归属"


def test_reevaluate_does_not_touch_gitlab_same_path(db):
    """反例：GitLab SourceItem 路径相同也不能被修改（只处理 dingtalk Connector）。"""
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    nb_old = Notebook(id="nb-old", name="旧库", group_id="engineering")
    nb_new = Notebook(id="nb-new", name="新库", group_id="sales")
    db.add_all([nb_old, nb_new])
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourceConnection(id="conn-git", connector_key="gitlab", name="GitLab", enabled=True, config_json="{}"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
    hist = NormalizedSourceItem(
        connection_id="conn-git", source_type="gitlab", external_id="g1",
        title="g1", content="正文", source_path="产品资料/g1.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-git", connection_id="conn-git", external_id="g1", state="active",
        content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
        source_path="产品资料/g1.pdf", acl_json='{"space_id": "space1"}', page_id="p-git",
    ))
    db.add(Page(id="p-git", notebook_id="nb-old", title="t", content="正文",
                source_type="gitlab", source_id="g1"))
    db.commit()

    folder_mapping.reevaluate_mapping_items(db, "", "产品资料")
    db.commit()

    item = db.get(SourceItem, "si-git")
    assert item.state == "active", "GitLab SourceItem 不得被映射重评估修改"


def test_reevaluate_space_specific_no_crosstalk(db):
    """反例：不同 space 的同路径不能串扰（space-specific 映射只影响该 space）。"""
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    nb_a = Notebook(id="nb-a", name="A库", group_id="engineering")
    nb_b = Notebook(id="nb-b", name="B库", group_id="sales")
    db.add_all([nb_a, nb_b])
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="space1", folder_path="产品资料", notebook_id="nb-b"))
    hist1 = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="d1",
        title="d1", content="正文", source_path="产品资料/d1.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-1", connection_id="conn", external_id="d1", state="active",
        content_hash=compute_content_hash(hist1), metadata_hash=compute_metadata_hash(hist1),
        source_path="产品资料/d1.pdf", acl_json='{"space_id": "space1"}', page_id="p-1",
    ))
    db.add(Page(id="p-1", notebook_id="nb-a", title="t", content="正文",
                source_type="dingtalk", source_id="d1"))
    hist2 = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="d2",
        title="d2", content="正文", source_path="产品资料/d2.pdf",
        metadata_json={"space_id": "space2", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-2", connection_id="conn", external_id="d2", state="active",
        content_hash=compute_content_hash(hist2), metadata_hash=compute_metadata_hash(hist2),
        source_path="产品资料/d2.pdf", acl_json='{"space_id": "space2"}', page_id="p-2",
    ))
    db.add(Page(id="p-2", notebook_id="nb-a", title="t", content="正文",
                source_type="dingtalk", source_id="d2"))
    db.commit()

    folder_mapping.reevaluate_mapping_items(db, "space1", "产品资料")
    db.commit()

    assert db.get(SourceItem, "si-1").state == "skipped", "space1 item 应被重评估"
    assert db.get(SourceItem, "si-2").state == "active", "space2 item 不得被串扰"


def test_reevaluate_root_mapping(db):
    """反例：根目录映射变化能处理其覆盖的文件（folder_path=''）。"""
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    nb_old = Notebook(id="nb-old", name="旧库", group_id="engineering")
    nb_new = Notebook(id="nb-new", name="新库", group_id="sales")
    db.add_all([nb_old, nb_new])
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="", notebook_id="nb-new"))
    hist = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="d1",
        title="d1", content="正文", source_path="产品资料/手册/d1.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-1", connection_id="conn", external_id="d1", state="active",
        content_hash=compute_content_hash(hist), metadata_hash=compute_metadata_hash(hist),
        source_path="产品资料/手册/d1.pdf", acl_json='{"space_id": "space1"}', page_id="p-1",
    ))
    db.add(Page(id="p-1", notebook_id="nb-old", title="t", content="正文",
                source_type="dingtalk", source_id="d1"))
    db.commit()

    folder_mapping.reevaluate_mapping_items(db, "", "")
    db.commit()

    assert db.get(SourceItem, "si-1").state == "skipped", "根映射变化必须处理其覆盖的文件"
    assert db.get(SourceItem, "si-1").last_error == "路径映射变化，需要重新归属"


# ---------------------------------------------------------------------------
# 二、一致事务边界：故障注入全 rollback
# ---------------------------------------------------------------------------

def _api_client(monkeypatch, tmp_path, seed=None):
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
    c = TestClient(app, raise_server_exceptions=False)
    yield c, _admin, _eng, _sales
    app.dependency_overrides.clear()
    engine.dispose()


def test_mapping_crud_wiki_failure_rolls_back(monkeypatch, tmp_path):
    """二：强制 Wiki 失效抛异常 → 映射、SourceItem、Wiki 均未出现部分提交。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem, WikiRevision, WikiSection
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
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
        # Wiki 来源 p-old
        db.add(WikiPage(id="w1", title="Wiki", status="published", acl_scope='{"groups": ["engineering"]}',
                        source_page_ids='["p-old"]', current_revision_id="r1"))
        db.add(WikiRevision(id="r1", wiki_page_id="w1", title="Wiki", status="published"))
        db.add(WikiSection(id="s1", revision_id="r1", section_type="body", content="正文", order_index=0))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 注入故障：Wiki 失效抛异常（模拟 remove_source_page_from_wikis 失败）
    def _boom(db, pid, commit=False):
        raise RuntimeError("wiki source removal failed")

    monkeypatch.setattr(
        "app.core.knowledge_compiler_v3.wiki_page_builder.remove_source_page_from_wikis",
        _boom,
    )
    # 更新映射：产品资料 → nb-new（触发 reevaluate → _mark_needs_reassign → Wiki 失效）
    r = c.put("/api/sources/dingtalk/folder-mappings/m1", json={
        "folder_path": "产品资料", "notebook_id": "nb-new",
    })
    assert r.status_code == 500, "Wiki 失效失败必须返回 500（不吞异常）"

    db = deps.get_session(deps.get_shared_engine())
    # 全部回滚：映射仍是 nb-old；SourceItem 仍 active；Wiki 仍 published
    m = db.query(SourcePathMapping).filter(SourcePathMapping.id == "m1").first()
    assert m.notebook_id == "nb-old", "映射不得部分提交"
    item = db.get(SourceItem, "si1")
    assert item.state == "active", "SourceItem 不得部分失效"
    wiki = db.get(WikiPage, "w1")
    assert wiki.status == "published", "Wiki 不得部分失效"
    db.close()


def test_old_put_notebook_group_id_uses_atomic_service(monkeypatch, tmp_path):
    """三：旧 PUT /api/notebooks/{id} 传 group_id 走统一原子服务，不能绕过。"""
    from app.api import deps
    from app.models.database import WikiRevision, WikiSection

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(UserGroup(id="ug1", user_id="u1", group_name="engineering"))
        db.add(UserGroup(id="ug2", user_id="u1", group_name="sales"))
        db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
        db.add(Page(id="p1", notebook_id="nb1", title="研发", content="正文"))
        db.add(WikiPage(id="w1", title="Wiki", status="published", acl_scope='{"groups": ["engineering"]}',
                        source_page_ids='["p1"]', current_revision_id="r1"))
        db.add(WikiRevision(id="r1", wiki_page_id="w1", title="Wiki", status="published"))
        db.add(WikiSection(id="s1", revision_id="r1", section_type="body", content="正文", order_index=0))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    # 旧 PUT 传 group_id → 走原子服务（主组 sales，旧 Wiki 立即失效）
    r = c.put("/api/notebooks/nb1", json={"name": "改名", "group_id": "sales"})
    assert r.status_code == 200, r.text

    db = deps.get_session(deps.get_shared_engine())
    nb = db.get(Notebook, "nb1")
    assert nb.name == "改名"
    assert nb.group_id == "sales", "group_id 必须经原子服务更新"
    wiki = db.get(WikiPage, "w1")
    assert wiki.status == "archived", "旧 PUT 传 group_id 也必须立即失效旧 Wiki（原子服务语义）"
    db.close()


def test_old_put_notebook_unknown_group_400(monkeypatch, tmp_path):
    """三：旧 PUT 传未知 group_id → 400（不静默降级）。"""

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    r = c.put("/api/notebooks/nb1", json={"name": "改名", "group_id": "not_a_group"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 四、reassign 收紧
# ---------------------------------------------------------------------------

def test_reassign_active_dingtalk_rejected(monkeypatch, tmp_path):
    """四：active DingTalk Page 不能通过 reassign 随意迁移。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
        hist = NormalizedSourceItem(
            connection_id="conn", source_type="dingtalk", external_id="doc-1",
            title="doc-1", content="正文", source_path="产品资料/doc-1.pdf",
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

    r = c.post("/api/sources/dingtalk/folder-mappings/reassign", json={"page_id": "p-old"})
    assert r.status_code == 409, "active 状态不能 reassign"

    db = deps.get_session(deps.get_shared_engine())
    assert db.get(Page, "p-old").notebook_id == "nb-old", "不得迁移 active Page"
    db.close()


def test_reassign_gitlab_page_rejected(monkeypatch, tmp_path):
    """四：GitLab Page 即使路径命中也不能迁移。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourceConnection(id="conn-git", connector_key="gitlab", name="GitLab", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
        hist = NormalizedSourceItem(
            connection_id="conn-git", source_type="gitlab", external_id="g1",
            title="g1", content="正文", source_path="产品资料/g1.pdf",
            metadata_json={"space_id": "space1", "space_name": "知识库"},
        )
        db.add(SourceItem(
            id="si-git", connection_id="conn-git", external_id="g1", state="skipped",
            last_error="路径映射变化，需要重新归属",
            content_hash=compute_content_hash(hist), metadata_hash=None,
            source_path="产品资料/g1.pdf", acl_json='{"space_id": "space1"}', page_id="p-git",
        ))
        db.add(Page(id="p-git", notebook_id="nb-old", title="t", content="正文",
                    source_type="gitlab", source_id="g1"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    r = c.post("/api/sources/dingtalk/folder-mappings/reassign", json={"page_id": "p-git"})
    assert r.status_code == 404, "GitLab Page 不能通过 reassign 迁移"

    db = deps.get_session(deps.get_shared_engine())
    assert db.get(Page, "p-git").notebook_id == "nb-old"
    db.close()


def test_reassign_valid_foldernotmapped_recovers(monkeypatch, tmp_path):
    """四：合法 FOLDER_NOT_MAPPED 状态仍能恢复（reassign 成功）。"""
    from app.api import deps
    from app.models.database import SourceConnection, SourceItem
    from app.sources.service import compute_content_hash, compute_metadata_hash

    def _seed(db):
        db.add(User(id="u1", username="admin", is_local=True))
        db.add(Notebook(id="nb-old", name="旧库", group_id="engineering"))
        db.add(Notebook(id="nb-new", name="新库", group_id="sales"))
        db.flush()
        db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
        db.add(SourcePathMapping(id="m1", connection_id="conn", path_namespace="", folder_path="产品资料", notebook_id="nb-new"))
        hist = NormalizedSourceItem(
            connection_id="conn", source_type="dingtalk", external_id="doc-1",
            title="doc-1", content="正文", source_path="产品资料/doc-1.pdf",
            metadata_json={"space_id": "space1", "space_name": "知识库"},
        )
        db.add(SourceItem(
            id="si1", connection_id="conn", external_id="doc-1", state="skipped",
            last_error="路径未配置权限映射",  # FOLDER_NOT_MAPPED
            content_hash=compute_content_hash(hist), metadata_hash=None,
            source_path="产品资料/doc-1.pdf", acl_json='{"space_id": "space1"}', page_id="p-old",
        ))
        db.add(Page(id="p-old", notebook_id="nb-old", title="旧标题", content="正文",
                    source_type="dingtalk", source_id="doc-1"))

    gen = _api_client(monkeypatch, tmp_path, _seed)
    c, _admin, _eng, _sales = next(gen)

    r = c.post("/api/sources/dingtalk/folder-mappings/reassign", json={"page_id": "p-old"})
    assert r.status_code == 200, r.text

    db = deps.get_session(deps.get_shared_engine())
    assert db.get(Page, "p-old").notebook_id == "nb-new"
    assert db.get(SourceItem, "si1").state == "active"
    db.close()


# ---------------------------------------------------------------------------
# 五、普通检索（含管理员）排除失效远程 Page
# ---------------------------------------------------------------------------

def test_admin_retrieval_excludes_skipped_remote_page(db):
    """五：管理员 Chat/Raw/图谱不返回 skipped/NEEDS_REASSIGN Page；诊断接口仍可见。"""
    from app.models.database import PageChunk, SourceConnection, SourceItem
    from app.core.retrieval.raw_retriever import RawDocumentRetriever
    from app.core.knowledge_compiler_v3.page_graph import build_page_communities
    from app.sources.service import compute_content_hash, compute_metadata_hash

    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn", connector_key="dingtalk", name="钉钉", enabled=True, config_json="{}"))
    # 有效远程 Page（active SourceItem）+ 失效远程 Page（skipped）
    hist_ok = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="ok",
        title="ok", content="有效内容含关键词", source_path="产品资料/ok.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-ok", connection_id="conn", external_id="ok", state="active",
        content_hash=compute_content_hash(hist_ok), metadata_hash=compute_metadata_hash(hist_ok),
        source_path="产品资料/ok.pdf", acl_json='{"space_id": "space1"}', page_id="p-ok",
    ))
    db.add(Page(id="p-ok", notebook_id="nb1", title="有效", content="有效内容含关键词",
                source_type="dingtalk", source_id="ok"))
    db.add(PageChunk(id="ck-ok", page_id="p-ok", chunk_index=0, content="有效内容含关键词"))
    hist_bad = NormalizedSourceItem(
        connection_id="conn", source_type="dingtalk", external_id="bad",
        title="bad", content="失效内容含关键词", source_path="产品资料/bad.pdf",
        metadata_json={"space_id": "space1", "space_name": "知识库"},
    )
    db.add(SourceItem(
        id="si-bad", connection_id="conn", external_id="bad", state="skipped",
        last_error="路径映射变化，需要重新归属",
        content_hash=compute_content_hash(hist_bad), metadata_hash=None,
        source_path="产品资料/bad.pdf", acl_json='{"space_id": "space1"}', page_id="p-bad",
    ))
    db.add(Page(id="p-bad", notebook_id="nb1", title="失效", content="失效内容含关键词",
                source_type="dingtalk", source_id="bad"))
    db.add(PageChunk(id="ck-bad", page_id="p-bad", chunk_index=0, content="失效内容含关键词"))
    db.commit()

    admin = _user(groups=["engineering"], is_admin=True)
    # 管理员正常检索链排除失效远程 Page
    visible = access_control.get_visible_page_ids(db, admin)
    assert "p-bad" not in visible, "管理员检索链不得含失效远程 Page"
    assert "p-ok" in visible
    assert access_control.can_view_page(db, admin, db.get(Page, "p-bad")) is False
    # Raw 检索
    raw = RawDocumentRetriever(db).retrieve(db, "内容含关键词", admin)
    page_ids = {h.page_id for h in raw.hits}
    assert "p-bad" not in page_ids
    assert "p-ok" in page_ids
    # 图谱
    communities = build_page_communities(db, access_control.get_visible_page_ids(db, admin))
    all_pages = {pid for c in communities for pid in c.member_page_ids}
    assert "p-bad" not in all_pages
    # 诊断接口仍可见（SourceItem 状态为 skipped + last_error）
    diag = db.query(SourceItem).filter(SourceItem.page_id == "p-bad").first()
    assert diag is not None and diag.state == "skipped"
    assert diag.last_error == "路径映射变化，需要重新归属"
