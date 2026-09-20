"""越权加固回归：图谱重建、钉钉同步、笔记改归属。

三处端点都要求本地管理员。测试用 monkeypatch 把真正的后台工作替换成
no-op，避免触发真实的重建/同步（真重建会先删光 GraphEdge，真同步会连钉钉）。
"""
import uuid

import app.api.dingtalk as dingtalk
import app.api.graph as graph
import app.api.pages as pages
from app.config import settings
from app.models.database import Notebook, Page, get_session


async def _noop_async(*args, **kwargs):
    """替代真实后台任务：不同步钉钉、不触发 embedding/索引。"""
    return None


def _seed_page(engine, notebook_id=None):
    db = get_session(engine)
    pid = str(uuid.uuid4())
    db.add(Page(id=pid, notebook_id=notebook_id, title="笔记", content=""))
    db.commit()
    db.close()
    return pid


def _seed_notebooks(engine):
    db = get_session(engine)
    db.add_all([
        Notebook(id="nb-pub", name="公共", group_id=None),
        Notebook(id="nb-rd", name="研发", group_id="研发部"),
        Notebook(id="nb-fin", name="财务", group_id="财务部"),
    ])
    db.commit()
    db.close()


def _enable_dingtalk(monkeypatch):
    """打开钉钉配置并打桩两个后台同步函数，使端点只走到被拦截的伪路径。"""
    monkeypatch.setattr(settings, "dingtalk_app_key", "test-key")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "test-secret")
    monkeypatch.setattr(dingtalk, "_do_sync", _noop_async)
    monkeypatch.setattr(dingtalk, "_do_sync_selected", _noop_async)
    dingtalk.SYNC_STATUS.update(running=False, imported=0, errors=0, total=0)


# ---- POST /api/graph/rebuild ----

def test_graph_rebuild_non_admin_forbidden(api_client, as_user):
    """非管理员触发全量重建必须被守卫拦下（否则会先删光 GraphEdge）。"""
    as_user(["研发部"])
    assert api_client.post("/api/graph/rebuild").status_code == 403


def test_graph_rebuild_admin_reaches_stubbed_builder(api_client, api_engine, as_user, monkeypatch):
    """管理员越权校验通过后进入构建路径；GraphBuilder 被打桩，不真的重建。"""
    _seed_page(api_engine)

    class _FakeBuilder:
        def build_graph(self, pages, db):
            assert pages  # 确认确实走到了构建调用，而非空库短路
            return 0

    monkeypatch.setattr(graph, "GraphBuilder", _FakeBuilder)
    as_user(["__local_admin__"])
    res = api_client.post("/api/graph/rebuild")
    assert res.status_code == 200


# ---- POST /api/dingtalk/sync 与 /sync-selected ----

def test_dingtalk_sync_non_admin_forbidden(api_client, as_user, monkeypatch):
    _enable_dingtalk(monkeypatch)
    as_user(["研发部"])
    assert api_client.post("/api/dingtalk/sync", json={"notebook_name": "x"}).status_code == 403


def test_dingtalk_sync_selected_non_admin_forbidden(api_client, as_user, monkeypatch):
    _enable_dingtalk(monkeypatch)
    as_user(["研发部"])
    res = api_client.post("/api/dingtalk/sync-selected", json={"docs": [{"id": "d1"}]})
    assert res.status_code == 403


def test_dingtalk_sync_admin_reaches_stubbed_path(api_client, as_user, monkeypatch):
    _enable_dingtalk(monkeypatch)
    as_user(["__local_admin__"])
    res = api_client.post("/api/dingtalk/sync", json={"notebook_name": "钉钉测试"})
    assert res.status_code == 200
    assert res.json()["notebook_id"]


def test_dingtalk_sync_selected_admin_reaches_stubbed_path(api_client, as_user, monkeypatch):
    _enable_dingtalk(monkeypatch)
    as_user(["__local_admin__"])
    res = api_client.post(
        "/api/dingtalk/sync-selected",
        json={"notebook_name": "钉钉测试", "docs": [{"id": "d1"}]},
    )
    assert res.status_code == 200
    assert res.json()["notebook_id"]


def test_dingtalk_status_stays_readable_for_non_admin(api_client, as_user):
    """只读接口不受管理员守卫影响，普通用户仍可查看同步状态。"""
    as_user(["研发部"])
    assert api_client.get("/api/dingtalk/status").status_code == 200


# ---- PUT /api/pages/{id} 改归属 ----

def _put_notebook(api_client, pid, notebook_id):
    return api_client.put(f"/api/pages/{pid}", json={"notebook_id": notebook_id})


def test_update_page_rejects_foreign_notebook(api_client, api_engine, as_user, monkeypatch):
    """把笔记改到他组笔记本必须 403。

    非空洞性：去掉目标笔记本校验后端点会 200 并写入，故 403 断言正是守卫生效点。
    """
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_notebooks(api_engine)
    pid = _seed_page(api_engine, notebook_id="nb-pub")
    as_user(["研发部"])
    assert _put_notebook(api_client, pid, "nb-fin").status_code == 403


def test_update_page_rejects_missing_notebook(api_client, api_engine, as_user, monkeypatch):
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_notebooks(api_engine)
    pid = _seed_page(api_engine, notebook_id="nb-pub")
    as_user(["研发部"])
    assert _put_notebook(api_client, pid, "nb-nonexistent").status_code == 403


def test_update_page_allows_public_and_own_notebook(api_client, api_engine, as_user, monkeypatch):
    """NULL 组笔记本与本人组笔记本都应放行。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_notebooks(api_engine)
    pid = _seed_page(api_engine, notebook_id="nb-pub")
    as_user(["研发部"])
    assert _put_notebook(api_client, pid, "nb-pub").status_code == 200
    assert _put_notebook(api_client, pid, "nb-rd").status_code == 200


def test_update_page_admin_can_move_to_foreign_notebook(api_client, api_engine, as_user, monkeypatch):
    """管理员豁免组校验，可把笔记改到他组笔记本。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_notebooks(api_engine)
    pid = _seed_page(api_engine, notebook_id="nb-pub")
    as_user(["__local_admin__"])
    assert _put_notebook(api_client, pid, "nb-fin").status_code == 200
