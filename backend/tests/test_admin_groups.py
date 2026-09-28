"""组注册表:列表引用计数、新建、删除拦截、成员管理(仅本地账号)。"""
import pytest

from app.models.database import (
    CompileTemplate, Group, Notebook, Pipeline, User, UserGroup, WikiPage, WikiSpace,
    get_session,
)


def test_list_create_delete_group(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/groups", json={"name": "测试组"})
    assert res.status_code == 200
    gid = res.json()["id"]
    listed = api_client.get("/api/admin/groups").json()
    assert any(g["name"] == "测试组" for g in listed["items"])
    assert api_client.delete(f"/api/admin/groups/{gid}").status_code == 200


def test_delete_group_in_use_rejected(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    gid = api_client.post("/api/admin/groups", json={"name": "使用中组"}).json()["id"]
    db = get_session(api_engine)
    try:
        db.add(Notebook(id="nb-g", name="n", group_id="使用中组"))
        db.commit()
    finally:
        db.close()
    assert api_client.delete(f"/api/admin/groups/{gid}").status_code == 409


def test_members_local_only(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(User(id="u-loc", username="loc", is_local=True))
        db.add(User(id="u-sso", username="sso", is_local=False))
        db.commit()
    finally:
        db.close()
    gid = api_client.post("/api/admin/groups", json={"name": "成员组"}).json()["id"]
    res = api_client.put(f"/api/admin/groups/{gid}/members", json={
        "add": ["u-loc", "u-sso"], "remove": [],
    })
    assert res.status_code == 400
    db = get_session(api_engine)
    try:
        # 拒绝的请求不得部分写入
        assert db.query(UserGroup).filter(UserGroup.group_name == "成员组").count() == 0
    finally:
        db.close()
    res = api_client.put(f"/api/admin/groups/{gid}/members", json={"add": ["u-loc"], "remove": []})
    assert res.status_code == 200
    members = api_client.get(f"/api/admin/groups/{gid}/members").json()
    assert [m["id"] for m in members["items"]] == ["u-loc"]


@pytest.mark.parametrize("method,path_tpl,body", [
    ("GET", "/api/admin/groups", None),
    ("POST", "/api/admin/groups", {"name": "无权组"}),
    ("DELETE", "/api/admin/groups/{gid}", None),
    ("GET", "/api/admin/groups/{gid}/members", None),
    ("PUT", "/api/admin/groups/{gid}/members", {"add": [], "remove": []}),
])
def test_groups_require_group_manage(api_client, as_user, method, path_tpl, body):
    """五个端点均须 group.manage:无权限一律 403(操作对象为已建组)。"""
    as_user(["__local_admin__"])
    gid = api_client.post("/api/admin/groups", json={"name": "无权组"}).json()["id"]
    as_user([])
    kwargs = {"json": body} if body is not None else {}
    assert api_client.request(method, path_tpl.format(gid=gid), **kwargs).status_code == 403


def test_create_group_validations(api_client, as_user):
    as_user(["__local_admin__"])
    assert api_client.post("/api/admin/groups", json={"name": "   "}).status_code == 400
    assert api_client.post("/api/admin/groups", json={"name": "__内部组"}).status_code == 400
    res = api_client.post("/api/admin/groups", json={"name": " 重复组 "})
    assert res.status_code == 200
    assert res.json()["name"] == "重复组" and res.json()["source"] == "local"
    assert api_client.post("/api/admin/groups", json={"name": "重复组"}).status_code == 409


def test_ref_count_covers_five_resource_tables(api_client, api_engine, as_user):
    """五张资源表(含 wiki_spaces)引用同口径计数,任一有引用即拦截删除。"""
    as_user(["__local_admin__"])
    gid = api_client.post("/api/admin/groups", json={"name": "五表组"}).json()["id"]
    db = get_session(api_engine)
    try:
        db.add(Notebook(id="nb-5", name="n", group_id="五表组"))
        db.add(WikiPage(id="wp-5", title="t", group_id="五表组"))
        db.add(WikiSpace(id="ws-5", name="s", group_id="五表组"))
        db.add(Pipeline(id="pl-5", name="p", group_id="五表组"))
        db.add(CompileTemplate(id="ct-5", name="c", group_id="五表组"))
        db.commit()
    finally:
        db.close()
    row = next(g for g in api_client.get("/api/admin/groups").json()["items"] if g["id"] == gid)
    assert row["ref_count"] == 5
    assert api_client.delete(f"/api/admin/groups/{gid}").status_code == 409


def test_list_shows_non_local_source(api_client, api_engine, as_user):
    """注册表可含 LDAP/SSO 同步来源组,列表原样展示 source。"""
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(Group(id="g-ldap", name="LDAP组", source="ldap"))
        db.commit()
    finally:
        db.close()
    row = next(g for g in api_client.get("/api/admin/groups").json()["items"] if g["id"] == "g-ldap")
    assert row["source"] == "ldap"
    assert row["member_count"] == 0 and row["ref_count"] == 0


def test_member_count_dedupes_legacy_duplicate_rows(api_client, api_engine, as_user):
    """历史重复成员行不应让 member_count 大于成员列表长度。"""
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(User(id="u-dup", username="dup", is_local=True))
        db.add(UserGroup(id="ug-dup-1", user_id="u-dup", group_name="重复成员组"))
        db.add(UserGroup(id="ug-dup-2", user_id="u-dup", group_name="重复成员组"))
        db.commit()
    finally:
        db.close()
    gid = api_client.post("/api/admin/groups", json={"name": "重复成员组"}).json()["id"]
    row = next(g for g in api_client.get("/api/admin/groups").json()["items"] if g["id"] == gid)
    assert row["member_count"] == 1
    assert len(api_client.get(f"/api/admin/groups/{gid}/members").json()["items"]) == 1


def test_delete_group_cleans_members_but_keeps_admin_marker(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(User(id="u-a", username="a", is_local=True))
        db.add(UserGroup(id="ug-a", user_id="u-a", group_name="清理组"))
        db.add(UserGroup(id="ug-a-admin", user_id="u-a", group_name="__local_admin__"))
        db.commit()
    finally:
        db.close()
    gid = api_client.post("/api/admin/groups", json={"name": "清理组"}).json()["id"]
    row = next(g for g in api_client.get("/api/admin/groups").json()["items"] if g["id"] == gid)
    assert row["member_count"] == 1
    assert api_client.delete(f"/api/admin/groups/{gid}").status_code == 200
    db = get_session(api_engine)
    try:
        assert db.query(Group).filter(Group.id == gid).first() is None
        assert db.query(UserGroup).filter(UserGroup.group_name == "清理组").count() == 0
        # __local_admin__ 标记行不受组删除影响
        assert db.query(UserGroup).filter(
            UserGroup.user_id == "u-a", UserGroup.group_name == "__local_admin__"
        ).count() == 1
    finally:
        db.close()


def test_delete_internal_group_defensively_rejected(api_client, api_engine, as_user):
    """注册表本不应有 __ 内部组(seed 已跳过);误入也不展示/不允许经 API 操作。"""
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(Group(id="g-internal", name="__local_admin__", source="local"))
        db.commit()
    finally:
        db.close()
    listed = api_client.get("/api/admin/groups").json()["items"]
    assert all(not g["name"].startswith("__") for g in listed)
    assert api_client.delete("/api/admin/groups/g-internal").status_code == 400
    assert api_client.put(
        "/api/admin/groups/g-internal/members", json={"add": [], "remove": []}
    ).status_code == 400


def test_members_add_remove_idempotent_and_unknown(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    db = get_session(api_engine)
    try:
        db.add(User(id="u-l1", username="l1", is_local=True))
        db.add(User(id="u-l2", username="l2", is_local=True))
        db.commit()
    finally:
        db.close()
    gid = api_client.post("/api/admin/groups", json={"name": "成员增删"}).json()["id"]
    assert api_client.put(f"/api/admin/groups/{gid}/members", json={
        "add": ["u-l1", "u-l2"],
    }).status_code == 200
    # 重复 add 幂等,不产生重复成员
    assert api_client.put(f"/api/admin/groups/{gid}/members", json={
        "add": ["u-l2"],
    }).status_code == 200
    items = api_client.get(f"/api/admin/groups/{gid}/members").json()["items"]
    assert [m["username"] for m in items] == ["l1", "l2"]
    assert api_client.put(f"/api/admin/groups/{gid}/members", json={
        "remove": ["u-l1"],
    }).status_code == 200
    items = api_client.get(f"/api/admin/groups/{gid}/members").json()["items"]
    assert [m["username"] for m in items] == ["l2"]
    # 同一用户同现于 add/remove → remove 胜出(先加后删)
    assert api_client.put(f"/api/admin/groups/{gid}/members", json={
        "add": ["u-l2"], "remove": ["u-l2"],
    }).status_code == 200
    assert api_client.get(f"/api/admin/groups/{gid}/members").json()["items"] == []
    # 不存在用户 → 400 且 detail 可读
    res = api_client.put(f"/api/admin/groups/{gid}/members", json={"add": ["u-nope"]})
    assert res.status_code == 400
    assert "用户不存在" in res.json()["detail"]
    assert api_client.put(f"/api/admin/groups/{gid}/members", json={
        "remove": ["u-nope"],
    }).status_code == 400


def test_unknown_group_404(api_client, as_user):
    as_user(["__local_admin__"])
    assert api_client.get("/api/admin/groups/nope/members").status_code == 404
    assert api_client.put(
        "/api/admin/groups/nope/members", json={"add": [], "remove": []}
    ).status_code == 404
    assert api_client.delete("/api/admin/groups/nope").status_code == 404
