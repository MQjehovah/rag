"""端点权限键:无权限 403;有权限放行;__local_admin__ 仍全通;自定义角色按模块生效。

迁移原则见 docs/plans/2026-09-28-rag-rbac.md Task 6:
- 功能管理语义 → 模块权限键;数据可见域(chat/search/wiki_search)保持不动
- 普通用户页面(Editor/Wiki)使用的读接口与组内 CRUD 保持可用
- 所有权旁路(评论删除)仅内置/SSO 管理员放行,即 has_permission(user, "*")
"""
import pytest

from app.models.database import Notebook, Page, PageComment, get_session


async def _noop_async(*args, **kwargs):
    """替代真实后台索引,避免端点测试触发 embedding/LLM。"""
    return None


def _seed_notebooks(engine):
    db = get_session(engine)
    try:
        db.add_all([
            Notebook(id="nb-pub", name="公共", group_id=None),
            Notebook(id="nb-rd", name="研发", group_id="研发部"),
            Notebook(id="nb-fin", name="财务", group_id="财务部"),
        ])
        db.commit()
    finally:
        db.close()


def _seed_page(engine, page_id="p-fin", notebook_id="nb-fin"):
    db = get_session(engine)
    try:
        db.add(Page(id=page_id, notebook_id=notebook_id, title="财务笔记", content="x"))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def as_perm(api_client):
    """覆盖 get_current_user 为带 permissions 的自定义角色用户。"""
    from app.main import app
    from app.core.jwt_utils import get_current_user

    def _login(permissions, groups=(), user_id="u-perm"):
        user = {
            "id": user_id,
            "username": "perm",
            "display_name": "自定义用户",
            "email": "",
            "is_local": False,
            "is_active": True,
            "groups": list(groups),
            "roles": [{"name": "custom", "display_name": "自定义角色"}],
            "permissions": list(permissions),
        }
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _login
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("method,path,key", [
    ("get", "/api/sources", "sources.manage"),
    ("get", "/api/pipelines", "pipeline.manage"),
    ("get", "/api/compile-templates", "pipeline.manage"),
    ("get", "/api/admin/permissions", "role.manage"),
    ("get", "/api/admin/users", "user.manage"),
    ("get", "/api/admin/groups", "group.manage"),
])
def test_endpoint_requires_permission(api_client, as_user, method, path, key):
    as_user([])
    res = getattr(api_client, method)(path)
    assert res.status_code == 403
    assert key in res.json()["detail"]
    as_user(["__local_admin__"])
    assert getattr(api_client, method)(path).status_code == 200


def test_sources_write_requires_sources_manage(api_client, as_perm):
    as_perm([])
    assert api_client.post("/api/sources/nope/test").status_code == 403
    as_perm(["sources.manage"])
    # 守卫放行后才走到"数据源不存在";404 证明是通过了权限层
    assert api_client.post("/api/sources/nope/test").status_code == 404


def test_embeddings_write_gated_but_read_visible(api_client, as_user):
    """普通用户可读档案列表(Editor 笔记本设置需要),但写操作 403。"""
    profile_body = {"name": "p1", "api_url": "http://embed.local", "model": "bge", "dimensions": 8}
    as_user([])
    assert api_client.get("/api/embeddings/profiles").status_code == 200
    assert api_client.post("/api/embeddings/reindex", json={}).status_code == 403
    assert api_client.post("/api/embeddings/profiles", json=profile_body).status_code == 403
    as_user(["__local_admin__"])
    assert api_client.get("/api/embeddings/profiles").status_code == 200
    assert api_client.post("/api/embeddings/profiles", json=profile_body).status_code == 200


def test_custom_role_permission_grants_single_module(api_client, as_perm):
    as_perm(["sources.manage"])
    assert api_client.get("/api/sources").status_code == 200
    assert api_client.get("/api/pipelines").status_code == 403
    # 读接口保持登录可见,不随"数据与管道"权限收紧
    assert api_client.get("/api/embeddings/profiles").status_code == 200

    # 反向对照:pipeline.manage 只放行管道/模板域,不放行数据源
    as_perm(["pipeline.manage"])
    assert api_client.get("/api/pipelines").status_code == 200
    assert api_client.get("/api/compile-templates").status_code == 200
    assert api_client.get("/api/sources").status_code == 403


def test_compile_template_write_requires_pipeline_manage(api_client, as_perm):
    body = {"name": "t1", "compiler_kind": "wiki"}
    as_perm([])
    assert api_client.post("/api/compile-templates", json=body).status_code == 403
    as_perm(["pipeline.manage"])
    assert api_client.post("/api/compile-templates", json=body).status_code == 200


def test_wiki_admin_endpoints_require_wiki_admin(api_client, as_perm):
    as_perm([])
    assert api_client.post("/api/wiki/rebuild").status_code == 403
    assert api_client.post("/api/wiki/reindex-embeddings").status_code == 403
    assert api_client.post("/api/wiki/refresh-stale").status_code == 403


def test_notebook_manage_grants_cross_group_but_keeps_member_access(api_client, api_engine, as_perm):
    """notebook.manage 是组可见域的旁路:普通成员组内 CRUD 保留,管理者跨组。"""
    _seed_notebooks(api_engine)
    as_perm([], groups=["研发部"])
    listed = {n["id"] for n in api_client.get("/api/notebooks").json()["notebooks"]}
    assert listed == {"nb-pub", "nb-rd"}
    assert api_client.put("/api/notebooks/nb-fin", json={"name": "x"}).status_code == 403

    as_perm(["notebook.manage"], groups=["研发部"])
    listed = {n["id"] for n in api_client.get("/api/notebooks").json()["notebooks"]}
    assert listed == {"nb-pub", "nb-rd", "nb-fin"}
    assert api_client.get("/api/notebooks/nb-fin").status_code == 200
    assert api_client.put("/api/notebooks/nb-fin", json={"name": "财务改"}).status_code == 200


def test_page_manage_grants_cross_group_page_access(api_client, api_engine, as_perm, monkeypatch):
    import app.api.pages as pages_module

    monkeypatch.setattr(pages_module, "background_index_page", _noop_async)
    _seed_notebooks(api_engine)
    _seed_page(api_engine)

    as_perm([], groups=["研发部"])
    assert api_client.get("/api/pages/p-fin").status_code == 403

    as_perm([], groups=["财务部"])
    assert api_client.get("/api/pages/p-fin").status_code == 200  # 本组成员照常可读

    as_perm(["page.manage"], groups=["研发部"])
    assert api_client.get("/api/pages/p-fin").status_code == 200
    assert api_client.put("/api/pages/p-fin", json={"title": "跨组改名"}).status_code == 200


def test_comment_delete_ownership_bypass_stays_star_only(api_client, api_engine, as_perm):
    """评论"自己可删/管理员可删"豁免只认 *,page.manage 不足以删他人评论。"""
    _seed_notebooks(api_engine)
    _seed_page(api_engine, page_id="p-rd", notebook_id="nb-rd")
    db = get_session(api_engine)
    try:
        db.add(PageComment(id="c-mine", page_id="p-rd", author_id="u-perm", author_name="我", content="1"))
        db.add(PageComment(id="c-other", page_id="p-rd", author_id="u-other", author_name="他", content="2"))
        db.commit()
    finally:
        db.close()

    as_perm(["page.manage"], groups=["研发部"], user_id="u-perm")
    assert api_client.delete("/api/pages/p-rd/comments/c-other").status_code == 403
    assert api_client.delete("/api/pages/p-rd/comments/c-mine").status_code == 200

    as_perm(["*"], groups=[], user_id="u-perm")
    assert api_client.delete("/api/pages/p-rd/comments/c-other").status_code == 200


def test_wiki_space_crud_requires_wiki_admin(api_client, as_perm):
    as_perm([])
    assert api_client.post("/api/wiki/spaces", json={"name": "新空间"}).status_code == 403

    as_perm(["wiki.admin"])
    created = api_client.post("/api/wiki/spaces", json={"name": "新空间"})
    assert created.status_code == 200
    space_id = created.json()["id"]
    assert api_client.put(f"/api/wiki/spaces/{space_id}", json={"name": "改名"}).status_code == 200
    assert api_client.delete(f"/api/wiki/spaces/{space_id}").status_code == 200


def test_graph_rebuild_requires_graph_manage(api_client, as_perm):
    as_perm([])
    assert api_client.post("/api/graph/rebuild").status_code == 403
    as_perm(["graph.manage"])
    # 空库短路:通过权限层后返回"没有笔记,跳过构建"
    assert api_client.post("/api/graph/rebuild").status_code == 200


def test_unmapped_admin_ops_require_star_only(api_client, as_user, as_perm, monkeypatch):
    """organize/dingtalk/jira 无细分权限键,仅内置/SSO 管理员(*)放行。"""
    import app.api.organize as organize

    as_perm(["page.manage", "sources.manage"])
    assert api_client.post("/api/organize").status_code == 403
    assert api_client.post("/api/jira/sync").status_code == 403
    assert api_client.post("/api/dingtalk/sync", json={"notebook_name": "x"}).status_code == 403

    async def fake_run(db):
        yield 'data: {"type": "done"}\n\n'

    monkeypatch.setattr(organize, "run_organize", fake_run)
    as_user(["__local_admin__"])
    assert api_client.post("/api/organize").status_code == 200
