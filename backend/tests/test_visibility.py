"""统一可见性(self / dept / public + legacy)与资源 ACL 追加授权端到端与单元测试。

覆盖:
- notebook:列表与详情的四种用户视角、创建/更新时的 owner/visibility/group 落库;
- 资源 ACL:管理员增改用户/部门追加授权,列表/详情/页面/检索随授权变化,替换式保存与删除清理;
- pages:可见性派生自所属笔记本(notebook_id IS NULL 保持公共);
- wiki space:列表过滤与增改的可见性落库,空间 ACL 对页面/列表/检索生效;
- wiki page:空间派生 + 页面 group 规则;
- 检索可见域:get_visible_page_ids 与 search_wiki 不含不可见资源页面;
- admin(*) 三种策略全见; SQL/Python 双实现一致(含 ACL 子句)。
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import text

from app.api.search_common import get_visible_page_ids, visible_wiki_filter
from app.core.visibility import load_acl, notebook_visible
from app.core.wiki_search import _visibility_sql, search_wiki
from app.models.database import Notebook, Page, ResourceAcl, WikiPage, WikiSpace, get_session

DIM = 1024


def _vec(*idxs):
    v = [0.0] * DIM
    for i in idxs:
        v[i] = 1.0
    return v


EMB = json.dumps(_vec(0))

# (user_id, groups)
USERS = {
    "owner": ("u-owner", ["研发部"]),
    "same": ("u-rd", ["研发部"]),
    "other": ("u-fin", ["财务部"]),
    "none": ("u-none", []),
}


def _as(as_user, key, **overrides):
    uid, groups = USERS[key]
    return as_user(list(groups), id=uid, **overrides)


def _seed(engine):
    db = get_session(engine)
    try:
        db.add_all([
            # --- notebooks:三种策略 + legacy 两种 ---
            Notebook(id="nb-self", name="自见本", group_id=None, visibility="self", owner_id="u-owner"),
            Notebook(id="nb-dept", name="部门本", group_id="研发部", visibility="dept", owner_id="u-owner"),
            Notebook(id="nb-pub", name="公开本", group_id=None, visibility="public", owner_id="u-owner"),
            Notebook(id="nb-legacy-pub", name="旧公共本", group_id=None, visibility=None, owner_id=None),
            Notebook(id="nb-legacy-rd", name="旧研发本", group_id="研发部", visibility=None, owner_id=None),
            # --- pages ---
            Page(id="p-self", notebook_id="nb-self", title="自见页", content="见 [[无归属页]]", keywords="自见机密"),
            Page(id="p-dept", notebook_id="nb-dept", title="部门页", content="x", keywords="部门词"),
            Page(id="p-pub", notebook_id="nb-pub", title="公开页", content="见 [[无归属页]]", keywords="公开词"),
            Page(id="p-legacy-rd", notebook_id="nb-legacy-rd", title="旧研发页", content="x"),
            Page(id="p-free", notebook_id=None, title="无归属页", content="x", keywords="公共词"),
            Page(id="p-trash-self", notebook_id="nb-self", title="自见回收页", content="x", deleted_at=datetime.now()),
            # --- wiki spaces ---
            WikiSpace(id="sp-self", name="私人空间", visibility="self", owner_id="u-owner"),
            WikiSpace(id="sp-dept", name="部门空间", visibility="dept", group_id="研发部"),
            WikiSpace(id="sp-pub", name="公开空间", visibility="public"),
            WikiSpace(id="sp-legacy", name="旧空间", visibility=None, group_id=None),
            # --- wiki pages ---
            WikiPage(id="w-self", title="私人空间页", space_id="sp-self", group_id=None, content="x", embedding=EMB),
            WikiPage(id="w-dept", title="部门空间页", space_id="sp-dept", group_id=None, content="x", embedding=EMB),
            WikiPage(id="w-pub", title="公开空间页", space_id="sp-pub", group_id=None, content="x", embedding=EMB),
            WikiPage(id="w-pub-fin", title="公开空间财务页", space_id="sp-pub", group_id="财务部", content="x", embedding=EMB),
            WikiPage(id="w-legacy", title="旧空间页", space_id="sp-legacy", group_id=None, content="x", embedding=EMB),
            WikiPage(id="w-nospace", title="无空间页", space_id=None, group_id=None, content="x", embedding=EMB),
        ])
        db.commit()
    finally:
        db.close()


def _nb_ids(client):
    return {n["id"] for n in client.get("/api/notebooks").json()["notebooks"]}


def _wiki_ids(client):
    return {p["id"] for p in client.get("/api/wiki").json()["items"]}


def _space_ids(client):
    return {s["id"] for s in client.get("/api/wiki/spaces").json()["spaces"]}


# ---------------- notebook:列表 / 详情 ----------------

def test_notebook_list_follows_visibility(api_engine, api_client, as_user):
    _seed(api_engine)
    expected = {
        "owner": {"nb-self", "nb-dept", "nb-pub", "nb-legacy-pub", "nb-legacy-rd"},
        "same": {"nb-dept", "nb-pub", "nb-legacy-pub", "nb-legacy-rd"},
        "other": {"nb-pub", "nb-legacy-pub"},
        "none": {"nb-pub", "nb-legacy-pub"},
    }
    for key, ids in expected.items():
        _as(as_user, key)
        assert _nb_ids(api_client) == ids, key


def test_notebook_detail_follows_visibility(api_engine, api_client, as_user):
    _seed(api_engine)
    expected = {
        "owner": {"nb-self": 200, "nb-dept": 200, "nb-pub": 200, "nb-legacy-pub": 200, "nb-legacy-rd": 200},
        "same": {"nb-self": 403, "nb-dept": 200, "nb-pub": 200, "nb-legacy-pub": 200, "nb-legacy-rd": 200},
        "other": {"nb-self": 403, "nb-dept": 403, "nb-pub": 200, "nb-legacy-pub": 200, "nb-legacy-rd": 403},
        "none": {"nb-self": 403, "nb-dept": 403, "nb-pub": 200, "nb-legacy-pub": 200, "nb-legacy-rd": 403},
    }
    for key, cases in expected.items():
        _as(as_user, key)
        for nb_id, status in cases.items():
            assert api_client.get(f"/api/notebooks/{nb_id}").status_code == status, (key, nb_id)


def test_notebook_admin_sees_all_strategies(api_engine, api_client, as_user):
    _seed(api_engine)
    all_ids = {"nb-self", "nb-dept", "nb-pub", "nb-legacy-pub", "nb-legacy-rd"}
    as_user(["__local_admin__"])
    assert _nb_ids(api_client) == all_ids
    as_user([], id="u-star", permissions=["*"])
    assert _nb_ids(api_client) == all_ids


def test_notebook_create_sets_owner_visibility_group(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "owner")

    res = api_client.post("/api/notebooks", json={"name": "私人", "visibility": "self"})
    assert res.status_code == 200
    body = res.json()
    assert (body["visibility"], body["owner_id"], body["group_id"]) == ("self", "u-owner", None)

    res = api_client.post("/api/notebooks", json={"name": "部门", "visibility": "dept"})
    assert (res.json()["visibility"], res.json()["group_id"]) == ("dept", "研发部")

    res = api_client.post("/api/notebooks", json={"name": "公开", "visibility": "public"})
    assert (res.json()["visibility"], res.json()["group_id"]) == ("public", None)

    # 缺省 dept
    res = api_client.post("/api/notebooks", json={"name": "缺省"})
    assert (res.json()["visibility"], res.json()["group_id"]) == ("dept", "研发部")

    assert api_client.post("/api/notebooks", json={"name": "非法", "visibility": "team"}).status_code == 400


def test_notebook_update_syncs_group_and_owner(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "owner")

    res = api_client.put("/api/notebooks/nb-legacy-pub", json={"visibility": "dept"})
    assert res.status_code == 200
    assert (res.json()["visibility"], res.json()["group_id"]) == ("dept", "研发部")

    res = api_client.put("/api/notebooks/nb-legacy-pub", json={"visibility": "public"})
    assert (res.json()["visibility"], res.json()["group_id"]) == ("public", None)

    # legacy 行无 owner,转 self 时补记操作者,否则无人可见
    res = api_client.put("/api/notebooks/nb-legacy-pub", json={"visibility": "self"})
    assert (res.json()["visibility"], res.json()["owner_id"], res.json()["group_id"]) == ("self", "u-owner", None)

    assert api_client.put("/api/notebooks/nb-legacy-pub", json={"visibility": "nope"}).status_code == 400


def test_notebook_invisible_user_cannot_write(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "other")
    assert api_client.put("/api/notebooks/nb-dept", json={"name": "越权"}).status_code == 403
    assert api_client.delete("/api/notebooks/nb-self").status_code == 403
    assert api_client.post("/api/notebooks/nb-dept/move", json={"position": 0}).status_code == 403


def test_notebook_visible_python_predicate_matches_sql(api_engine):
    _seed(api_engine)
    from app.core.visibility import notebook_visible_condition

    db = get_session(api_engine)
    try:
        all_rows = db.query(Notebook).all()
        for key, (uid, groups) in USERS.items():
            user = {"id": uid, "groups": groups}
            sql_ids = {n.id for n in db.query(Notebook).filter(notebook_visible_condition(user)).all()}
            py_ids = {n.id for n in all_rows if notebook_visible(user, n)}
            assert sql_ids == py_ids, key
        admin = {"groups": [], "permissions": ["*"]}
        assert {n.id for n in db.query(Notebook).filter(notebook_visible_condition(admin)).all()} == {
            n.id for n in all_rows
        }
    finally:
        db.close()


# ---------------- pages:派生自笔记本 ----------------

def test_pages_list_and_detail_follow_notebook(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "other")

    listed = {p["id"] for p in api_client.get("/api/pages", params={"page_size": 100}).json()["items"]}
    assert {"p-pub", "p-free"} <= listed
    assert not ({"p-self", "p-dept", "p-legacy-rd"} & listed)

    assert api_client.get("/api/pages/p-self").status_code == 403
    assert api_client.get("/api/pages/p-dept").status_code == 403
    assert api_client.get("/api/pages/p-pub").status_code == 200  # 公开本
    assert api_client.get("/api/pages/p-free").status_code == 200  # notebook_id NULL 公共


def test_page_tree_restricted_notebook_403(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "other")
    assert api_client.get("/api/pages/tree", params={"notebook_id": "nb-self"}).status_code == 403
    assert api_client.get("/api/pages/tree", params={"notebook_id": "nb-pub"}).status_code == 200
    _as(as_user, "owner")
    assert api_client.get("/api/pages/tree", params={"notebook_id": "nb-self"}).status_code == 200


def test_page_tags_trash_backlinks_scoped(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "other")

    tags = {t["tag"] for t in api_client.get("/api/pages/tags").json()["tags"]}
    assert "公共词" in tags and "公开词" in tags
    assert "自见机密" not in tags and "部门词" not in tags

    trash = {p["id"] for p in api_client.get("/api/pages/trash").json()["items"]}
    assert "p-trash-self" not in trash

    backlinks = {p["id"] for p in api_client.get("/api/pages/p-free/backlinks").json()["items"]}
    assert backlinks == {"p-pub"}  # p-self 不可见, 不进反向链接
    assert api_client.get("/api/pages/p-self/backlinks").status_code == 403

    _as(as_user, "owner")
    trash = {p["id"] for p in api_client.get("/api/pages/trash").json()["items"]}
    assert "p-trash-self" in trash
    backlinks = {p["id"] for p in api_client.get("/api/pages/p-free/backlinks").json()["items"]}
    assert backlinks == {"p-pub", "p-self"}


# ---------------- wiki spaces ----------------

def test_wiki_space_list_follows_visibility(api_engine, api_client, as_user):
    _seed(api_engine)
    expected = {
        "owner": {"sp-self", "sp-dept", "sp-pub", "sp-legacy"},
        "same": {"sp-dept", "sp-pub", "sp-legacy"},
        "other": {"sp-pub", "sp-legacy"},
        "none": {"sp-pub", "sp-legacy"},
    }
    for key, ids in expected.items():
        _as(as_user, key)
        assert _space_ids(api_client) == ids, key
    as_user(["__local_admin__"])
    assert _space_ids(api_client) == {"sp-self", "sp-dept", "sp-pub", "sp-legacy"}


def test_wiki_space_create_and_update_visibility(api_engine, api_client, as_user):
    _seed(api_engine)
    as_user([], id="u-star", permissions=["wiki.admin", "group.manage"])

    res = api_client.post("/api/wiki/spaces", json={"name": "私人空间2", "visibility": "self"})
    assert res.status_code == 200
    body = res.json()
    assert (body["visibility"], body["owner_id"], body["group_id"]) == ("self", "u-star", None)

    res = api_client.post("/api/wiki/spaces", json={"name": "部门空间2", "visibility": "dept"})
    assert res.json()["visibility"] == "dept"
    assert res.json()["group_id"] is None  # 管理角色无组

    res = api_client.post("/api/wiki/spaces", json={"name": "公开空间2"})
    assert (res.json()["visibility"], res.json()["group_id"]) == ("dept", None)  # 缺省 dept

    assert api_client.post("/api/wiki/spaces", json={"name": "非法", "visibility": "x"}).status_code == 400

    sid = api_client.post("/api/wiki/spaces", json={"name": "待改"}).json()["id"]
    res = api_client.put(f"/api/wiki/spaces/{sid}", json={"visibility": "public"})
    assert res.status_code == 200
    listed = {s["id"]: s for s in api_client.get("/api/wiki/spaces").json()["spaces"]}
    assert listed[sid]["visibility"] == "public"
    assert api_client.put(f"/api/wiki/spaces/{sid}", json={"visibility": "x"}).status_code == 400


# ---------------- wiki pages:空间派生 + 页面 group 规则 ----------------

def test_wiki_page_list_and_detail_follow_space(api_engine, api_client, as_user):
    _seed(api_engine)
    _as(as_user, "same")
    assert _wiki_ids(api_client) == {"w-dept", "w-pub", "w-legacy", "w-nospace"}
    assert api_client.get("/api/wiki/w-self").status_code == 404  # 私人空间
    assert api_client.get("/api/wiki/w-pub-fin").status_code == 404  # 公开空间但页面限财务组
    assert api_client.get("/api/wiki/w-dept").status_code == 200

    _as(as_user, "other")
    assert _wiki_ids(api_client) == {"w-pub", "w-pub-fin", "w-legacy", "w-nospace"}
    assert api_client.get("/api/wiki/w-pub-fin").status_code == 200  # 本组页面规则放行
    assert api_client.get("/api/wiki/w-dept").status_code == 404

    _as(as_user, "owner")
    assert _wiki_ids(api_client) == {"w-self", "w-dept", "w-pub", "w-legacy", "w-nospace"}


def test_wiki_page_admin_sees_all(api_engine, api_client, as_user):
    _seed(api_engine)
    as_user(["__local_admin__"])
    assert _wiki_ids(api_client) == {"w-self", "w-dept", "w-pub", "w-pub-fin", "w-legacy", "w-nospace"}
    for pid in ("w-self", "w-dept", "w-pub", "w-pub-fin", "w-legacy", "w-nospace"):
        assert api_client.get(f"/api/wiki/{pid}").status_code == 200


# ---------------- 检索可见域 ----------------

def test_visible_page_ids_excludes_invisible_notebooks(api_engine, as_user):
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        uid, groups = USERS["other"]
        visible = get_visible_page_ids(db, {"id": uid, "groups": groups})
        assert {"p-pub", "p-free"} <= visible
        assert not ({"p-self", "p-dept", "p-legacy-rd", "p-trash-self"} & visible)

        admin = {"groups": [], "permissions": ["*"]}
        assert {"p-self", "p-dept", "p-pub", "p-legacy-rd", "p-free"} <= get_visible_page_ids(db, admin)
        assert "p-trash-self" not in get_visible_page_ids(db, admin)  # 回收站始终排除
    finally:
        db.close()


def test_wiki_search_excludes_invisible_spaces_and_groups(api_engine):
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        rd = {"id": "u-rd", "groups": ["研发部"]}
        ids = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user=rd)}
        assert {"w-dept", "w-pub", "w-legacy", "w-nospace"} <= ids
        assert "w-self" not in ids
        assert "w-pub-fin" not in ids

        other = {"id": "u-fin", "groups": ["财务部"]}
        ids = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user=other)}
        assert {"w-pub", "w-legacy", "w-nospace", "w-pub-fin"} <= ids
        assert not ({"w-self", "w-dept"} & ids)

        admin = {"groups": [], "permissions": ["*"]}
        ids = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user=admin)}
        assert {"w-self", "w-dept", "w-pub", "w-pub-fin", "w-legacy", "w-nospace"} <= ids
    finally:
        db.close()


# ---------------- SQL 与 Python 双实现一致(含空间派生) ----------------

@pytest.mark.parametrize("user", [
    {"groups": ["__local_admin__"]},
    {"groups": ["研发部"]},
    {"groups": ["财务部"]},
    {"groups": []},
    {"groups": ["研发部"], "id": "u-owner"},
    {"groups": [], "id": "u-owner"},
    {"groups": [], "permissions": ["*"]},
])
def test_wiki_sql_matches_orm_with_spaces(api_engine, user):
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        orm_ids = {p.id for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}
        cond, params = _visibility_sql(user)
        rows = db.execute(text(f"SELECT id FROM wiki_pages WHERE {cond}"), params).fetchall()
        assert {r[0] for r in rows} == orm_ids
    finally:
        db.close()


# ---------------- 资源 ACL 追加授权(管理员配置) ----------------

def _set_acl(api_client, as_user, resource_type, resource_id, **acl):
    """以具备对应 manage 权限的管理员写资源 ACL,返回响应。"""
    perms = ["notebook.manage"] if resource_type == "notebook" else ["wiki.admin"]
    as_user([], id="u-admin", permissions=perms)
    path = f"/api/notebooks/{resource_id}" if resource_type == "notebook" else f"/api/wiki/spaces/{resource_id}"
    return api_client.put(path, json=acl)


def test_notebook_acl_user_grant(api_engine, api_client, as_user):
    """self 笔记本 + acl_users=[他人] → 被授权用户可见(列表+详情+所属页面),未列用户不可见。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "notebook", "nb-self", acl_users=["u-fin"])
    assert res.status_code == 200
    assert res.json()["acl_users"] == ["u-fin"]
    assert res.json()["acl_groups"] == []

    _as(as_user, "other")  # u-fin
    assert "nb-self" in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-self").status_code == 200
    listed = {p["id"] for p in api_client.get("/api/pages", params={"page_size": 100}).json()["items"]}
    assert "p-self" in listed
    assert api_client.get("/api/pages/p-self").status_code == 200

    _as(as_user, "same")  # u-rd 不在 ACL
    assert "nb-self" not in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-self").status_code == 403
    assert api_client.get("/api/pages/p-self").status_code == 403

    _as(as_user, "none")
    assert "nb-self" not in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-self").status_code == 403


def test_notebook_acl_group_grant(api_engine, api_client, as_user):
    """self 笔记本 + acl_groups=[G] → G 组用户可见, 非 G 不可见。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "notebook", "nb-self", acl_groups=["财务部"])
    assert res.status_code == 200
    assert res.json()["acl_groups"] == ["财务部"]

    _as(as_user, "other")  # 财务部
    assert "nb-self" in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-self").status_code == 200

    _as(as_user, "same")  # 研发部
    assert "nb-self" not in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-self").status_code == 403

    _as(as_user, "none")
    assert "nb-self" not in _nb_ids(api_client)


def test_notebook_acl_is_additive_on_dept(api_engine, api_client, as_user):
    """dept 笔记本 + acl_users 追加 → 原部门通道与 ACL 追加通道双通道可见。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "notebook", "nb-dept", acl_users=["u-none"])
    assert res.status_code == 200

    _as(as_user, "none")  # 原 dept 域外, ACL 追加后被授权
    assert "nb-dept" in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-dept").status_code == 200

    _as(as_user, "same")  # 原 dept 通道不受影响
    assert "nb-dept" in _nb_ids(api_client)
    assert api_client.get("/api/notebooks/nb-dept").status_code == 200

    _as(as_user, "other")  # 未授权且非本组
    assert "nb-dept" not in _nb_ids(api_client)


def test_notebook_acl_only_admin_writes(api_engine, api_client, as_user):
    """非 notebook.manage 即使可见也不能配置 ACL; 非 ACL 字段不受影响。"""
    _seed(api_engine)
    _as(as_user, "owner")
    assert api_client.put("/api/notebooks/nb-self", json={"acl_users": ["u-fin"]}).status_code == 403
    assert api_client.put("/api/notebooks/nb-self", json={"acl_groups": ["财务部"]}).status_code == 403
    assert api_client.put("/api/notebooks/nb-self", json={"name": "改名"}).status_code == 200


def test_notebook_acl_replace_and_delete_cleanup(api_engine, api_client, as_user):
    """PUT 替换式保存(去重/去空);删除资源清理 ACL 行。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "notebook", "nb-self", acl_users=["u-fin", "u-fin", "  "])
    assert res.status_code == 200
    assert res.json()["acl_users"] == ["u-fin"]

    res = _set_acl(api_client, as_user, "notebook", "nb-self", acl_users=["u-none"])
    assert res.status_code == 200
    assert res.json()["acl_users"] == ["u-none"]

    _as(as_user, "other")  # u-fin 授权已被替换掉
    assert "nb-self" not in _nb_ids(api_client)
    _as(as_user, "none")  # u-none 现被授权
    assert "nb-self" in _nb_ids(api_client)

    as_user([], id="u-admin", permissions=["notebook.manage"])
    assert api_client.delete("/api/notebooks/nb-self").status_code == 200
    db = get_session(api_engine)
    try:
        left = db.query(ResourceAcl).filter(
            ResourceAcl.resource_type == "notebook",
            ResourceAcl.resource_id == "nb-self",
        ).count()
        assert left == 0
    finally:
        db.close()


def test_wiki_space_acl_user_grant(api_engine, api_client, as_user):
    """self 空间 + acl_users → 被授权用户可见空间与空间下页面(列表/详情/检索)。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "wiki_space", "sp-self", acl_users=["u-fin"])
    assert res.status_code == 200

    _as(as_user, "other")  # u-fin
    spaces = {s["id"]: s for s in api_client.get("/api/wiki/spaces").json()["spaces"]}
    assert "sp-self" in spaces
    assert spaces["sp-self"]["acl_users"] == ["u-fin"]
    assert "w-self" in _wiki_ids(api_client)
    assert api_client.get("/api/wiki/w-self").status_code == 200
    db = get_session(api_engine)
    try:
        hits = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user={"id": "u-fin", "groups": ["财务部"]})}
        assert "w-self" in hits
    finally:
        db.close()

    _as(as_user, "same")
    assert "sp-self" not in _space_ids(api_client)
    assert "w-self" not in _wiki_ids(api_client)
    assert api_client.get("/api/wiki/w-self").status_code == 404

    _as(as_user, "none")
    assert "sp-self" not in _space_ids(api_client)


def test_wiki_space_acl_group_grant(api_engine, api_client, as_user):
    """self 空间 + acl_groups → 组内用户可见空间及页面, 组外不可见。"""
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "wiki_space", "sp-self", acl_groups=["研发部"])
    assert res.status_code == 200

    _as(as_user, "same")  # 研发部
    assert "sp-self" in _space_ids(api_client)
    assert "w-self" in _wiki_ids(api_client)
    assert api_client.get("/api/wiki/w-self").status_code == 200

    _as(as_user, "other")
    assert "sp-self" not in _space_ids(api_client)
    assert "w-self" not in _wiki_ids(api_client)
    assert api_client.get("/api/wiki/w-self").status_code == 404


def test_wiki_space_acl_delete_cleanup(api_engine, api_client, as_user):
    _seed(api_engine)
    res = _set_acl(api_client, as_user, "wiki_space", "sp-self", acl_users=["u-fin"])
    assert res.status_code == 200
    as_user([], id="u-admin", permissions=["wiki.admin"])
    assert api_client.delete("/api/wiki/spaces/sp-self").status_code == 200
    db = get_session(api_engine)
    try:
        left = db.query(ResourceAcl).filter(
            ResourceAcl.resource_type == "wiki_space",
            ResourceAcl.resource_id == "sp-self",
        ).count()
        assert left == 0
    finally:
        db.close()


def test_admin_sees_all_with_acl_rows(api_engine, api_client, as_user):
    """存在 ACL 行时管理员(*)仍全见,三种策略不变。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        db.add(ResourceAcl(resource_type="notebook", resource_id="nb-self",
                           subject_type="user", subject_id="u-fin"))
        db.add(ResourceAcl(resource_type="wiki_space", resource_id="sp-self",
                           subject_type="group", subject_id="财务部"))
        db.commit()
    finally:
        db.close()
    all_nb = {"nb-self", "nb-dept", "nb-pub", "nb-legacy-pub", "nb-legacy-rd"}
    as_user(["__local_admin__"])
    assert _nb_ids(api_client) == all_nb
    assert _space_ids(api_client) == {"sp-self", "sp-dept", "sp-pub", "sp-legacy"}
    assert _wiki_ids(api_client) == {"w-self", "w-dept", "w-pub", "w-pub-fin", "w-legacy", "w-nospace"}


@pytest.mark.parametrize("key", ["owner", "same", "other", "none"])
def test_notebook_acl_sql_matches_python(api_engine, key):
    """带 ACL 子句的 SQLAlchemy 条件与 Python 谓词等价。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        db.add_all([
            ResourceAcl(resource_type="notebook", resource_id="nb-self",
                        subject_type="user", subject_id="u-fin"),
            ResourceAcl(resource_type="notebook", resource_id="nb-dept",
                        subject_type="group", subject_id="财务部"),
        ])
        db.commit()
        uid, groups = USERS[key]
        user = {"id": uid, "groups": groups}
        from app.core.visibility import notebook_visible_condition

        all_rows = db.query(Notebook).all()
        sql_ids = {n.id for n in db.query(Notebook).filter(notebook_visible_condition(user)).all()}
        py_ids = {n.id for n in all_rows if notebook_visible(user, n, acl=load_acl(db, "notebook", n.id))}
        assert sql_ids == py_ids, key
    finally:
        db.close()

def test_wiki_spaces_includes_default_space_meta(api_client, as_user):
    """spaces 响应带默认空间元数据(前端「默认空间」编辑入口预填 可见性/ACL 用)。"""
    as_user(["__local_admin__"])
    res = api_client.get("/api/wiki/spaces")
    assert res.status_code == 200
    ds = res.json().get("default_space")
    assert ds and ds.get("id")
    assert "visibility" in ds and "acl_users" in ds and "acl_groups" in ds
