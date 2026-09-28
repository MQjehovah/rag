"""统一可见性(self / dept / public + legacy)端到端与单元测试。

覆盖:
- notebook:列表与详情的四种用户视角、创建/更新时的 owner/visibility/group 落库;
- pages:可见性派生自所属笔记本(notebook_id IS NULL 保持公共);
- wiki space:列表过滤与增改的可见性落库;
- wiki page:空间派生 + 页面 group 规则;
- 检索可见域:get_visible_page_ids 与 search_wiki 不含不可见资源页面;
- admin(*) 三种策略全见; SQL/Python 双实现一致。
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import text

from app.api.search_common import get_visible_page_ids, visible_wiki_filter
from app.core.visibility import notebook_visible
from app.core.wiki_search import _visibility_sql, search_wiki
from app.models.database import Notebook, Page, WikiPage, WikiSpace, get_session

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
