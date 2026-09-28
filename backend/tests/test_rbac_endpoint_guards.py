"""端点权限键:无权限 403;有权限放行;__local_admin__ 仍全通;自定义角色按模块生效。

迁移原则见 docs/plans/2026-09-28-rag-rbac.md Task 6:
- 功能管理语义 → 模块权限键;数据可见域(chat/search/wiki_search)保持不动
- 普通用户页面(Editor/Wiki)使用的读接口与组内 CRUD 保持可用
- 所有权旁路(评论删除)仅内置/SSO 管理员放行,即 has_permission(user, "*")

Task 12 审查跟进:可见性 helper 为权限驱动——显式 * 权限(无标记)与
__local_admin__ 标记(无 permissions 键,经桥接)行为等效,判定不看组名。
"""
import pytest

from app.api.chat import _get_kb_context
from app.api.search_common import get_visible_page_ids, visible_wiki_filter
from app.api.wiki import _wiki_visible
from app.core.wiki_search import _visibility_sql
from app.models.database import Notebook, Page, PageComment, WikiPage, get_session


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


def _seed_wiki_pages(engine):
    """三页 wiki:公共 / 研发部 / 财务部。"""
    from app.models.database import WikiPage

    db = get_session(engine)
    try:
        db.add_all([
            WikiPage(id="wiki-pub", title="公共页", content="x", group_id=None),
            WikiPage(id="wiki-rd", title="研发页", content="x", group_id="研发部"),
            WikiPage(id="wiki-fin", title="财务页", content="x", group_id="财务部"),
        ])
        db.commit()
    finally:
        db.close()


def _wiki_titles(client):
    data = client.get("/api/wiki").json()
    return {p["title"] for cat in data["categories"] for p in cat["pages"]}


def test_wiki_visibility_follows_star_permission_not_group_marker(api_client, api_engine, as_perm):
    """显式 * 权限(permissions=["*"],无 __local_admin__ 标记)在 wiki 列表同样全量可见。"""
    _seed_wiki_pages(api_engine)

    as_perm([], groups=["研发部"])
    assert _wiki_titles(api_client) == {"公共页", "研发页"}

    as_perm(["*"], groups=["研发部"])
    assert _wiki_titles(api_client) == {"公共页", "研发页", "财务页"}


def _seed_visibility_fixtures(engine):
    """公共/研发部/财务部各一条笔记本+笔记+wiki 页,供可见性 helper 单元断言。"""
    db = get_session(engine)
    try:
        db.add_all([
            Notebook(id="vis-nb-pub", name="公共本", group_id=None),
            Notebook(id="vis-nb-rd", name="研发本", group_id="研发部"),
            Notebook(id="vis-nb-fin", name="财务本", group_id="财务部"),
            Page(id="vis-p-pub", notebook_id="vis-nb-pub", title="公共笔记"),
            Page(id="vis-p-rd", notebook_id="vis-nb-rd", title="研发笔记"),
            Page(id="vis-p-fin", notebook_id="vis-nb-fin", title="财务笔记"),
            WikiPage(id="vis-w-pub", title="公共页", content="x", group_id=None),
            WikiPage(id="vis-w-rd", title="研发页", content="x", group_id="研发部"),
            WikiPage(id="vis-w-fin", title="财务页", content="x", group_id="财务部"),
        ])
        db.commit()
    finally:
        db.close()


def test_visibility_helpers_permission_driven_with_marker_bridge(api_engine):
    """可见性 helper 判定只看权限:显式 *(无标记)与 __local_admin__ 标记(无 permissions)等效。

    直接对 helper 断言(不走 HTTP),覆盖 Task 12 改动的 6 处中的 5 处
    (chat._agentic_search_notes 的社区回退在 test_chat_scoping.py 单独覆盖)。
    """
    _seed_visibility_fixtures(api_engine)
    db = get_session(api_engine)
    try:
        star = {"groups": ["研发部"], "permissions": ["*"]}     # 无标记,显式 *
        marker = {"groups": ["__local_admin__"]}               # 无 permissions 键,靠桥接补 *
        member = {"groups": ["研发部"]}                        # 对照组:普通成员
        all_wiki = {"vis-w-pub", "vis-w-rd", "vis-w-fin"}
        rd_wiki = {"vis-w-pub", "vis-w-rd"}
        all_pages = {"vis-p-pub", "vis-p-rd", "vis-p-fin"}
        rd_pages = {"vis-p-pub", "vis-p-rd"}

        # 1) wiki_search._visibility_sql(裸 SQL):管理员恒真,普通成员带组条件
        assert _visibility_sql(star) == ("1 = 1", {})
        assert _visibility_sql(marker) == ("1 = 1", {})
        assert _visibility_sql(member)[0] != "1 = 1"

        # 2) wiki._wiki_visible:他组页面在 * / 标记下可见,普通成员不可见
        fin_wiki = db.query(WikiPage).filter(WikiPage.id == "vis-w-fin").one()
        assert _wiki_visible(fin_wiki, star) is True
        assert _wiki_visible(fin_wiki, marker) is True
        assert _wiki_visible(fin_wiki, member) is False

        # 3) search_common.visible_wiki_filter
        def _wiki_ids(user):
            return {p.id for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}

        assert _wiki_ids(star) == all_wiki
        assert _wiki_ids(marker) == all_wiki
        assert _wiki_ids(member) == rd_wiki

        # 4) search_common.get_visible_page_ids
        assert get_visible_page_ids(db, star) == all_pages
        assert get_visible_page_ids(db, marker) == all_pages
        assert get_visible_page_ids(db, member) == rd_pages

        # 5) chat._get_kb_context:提示词上下文同样全量
        kb = _get_kb_context(db, star)
        assert {n["name"] for n in kb["notebooks"]} == {"公共本", "研发本", "财务本"}
        assert {p["title"] for p in kb["pages"]} == {"公共笔记", "研发笔记", "财务笔记"}
        kb = _get_kb_context(db, marker)
        assert {n["name"] for n in kb["notebooks"]} == {"公共本", "研发本", "财务本"}
        assert {p["title"] for p in kb["pages"]} == {"公共笔记", "研发笔记", "财务笔记"}
    finally:
        db.close()


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
