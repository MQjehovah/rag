"""角色管理 API:CRUD、内置保护、使用人数、权限校验。"""


def test_permissions_catalog_requires_role_manage(api_client, as_user):
    as_user([])
    assert api_client.get("/api/admin/permissions").status_code == 403
    as_user(["__local_admin__"])
    res = api_client.get("/api/admin/permissions")
    assert res.status_code == 200
    keys = {g["key"] for grp in res.json() for g in grp["items"]}
    assert {"sources.manage", "user.manage", "role.manage", "group.manage"} <= keys


def test_create_and_list_role(api_client, as_user):
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/roles", json={
        "name": "ops", "display_name": "运维", "permissions": ["sources.manage"],
    })
    assert res.status_code == 200
    role_id = res.json()["id"]
    listed = api_client.get("/api/admin/roles").json()
    assert any(r["id"] == role_id and r["permissions"] == ["sources.manage"] for r in listed["items"])


def test_invalid_permission_key_rejected(api_client, as_user):
    as_user(["__local_admin__"])
    assert api_client.post("/api/admin/roles", json={
        "name": "bad", "display_name": "坏角色", "permissions": ["nope.key"],
    }).status_code == 400
    role_id = api_client.post("/api/admin/roles", json={
        "name": "ok", "display_name": "好角色", "permissions": ["sources.manage"],
    }).json()["id"]
    assert api_client.put(f"/api/admin/roles/{role_id}", json={
        "permissions": ["nope.key"],
    }).status_code == 400


def test_create_role_deduplicates_permissions(api_client, as_user):
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/roles", json={
        "name": "dedup", "display_name": "去重",
        "permissions": ["sources.manage", "sources.manage", "user.manage"],
    })
    assert res.status_code == 200
    assert res.json()["permissions"] == ["sources.manage", "user.manage"]


def test_update_role_partial_preserves_permissions(api_client, as_user):
    as_user(["__local_admin__"])
    role_id = api_client.post("/api/admin/roles", json={
        "name": "ops3", "display_name": "运维3", "permissions": ["sources.manage"],
    }).json()["id"]
    res = api_client.put(f"/api/admin/roles/{role_id}", json={"display_name": "改个名"})
    assert res.status_code == 200
    assert res.json()["display_name"] == "改个名"
    assert res.json()["permissions"] == ["sources.manage"]
    # 显式传空列表 → 清空权限(与缺省 None 的部分更新语义区分)
    res = api_client.put(f"/api/admin/roles/{role_id}", json={"permissions": []})
    assert res.status_code == 200
    assert res.json()["permissions"] == []


def test_builtin_admin_role_readonly(api_client, as_user):
    as_user(["__local_admin__"])
    roles = api_client.get("/api/admin/roles").json()["items"]
    admin = next(r for r in roles if r["name"] == "admin")
    assert admin["is_system"] is True
    assert api_client.put(f"/api/admin/roles/{admin['id']}", json={"display_name": "x"}).status_code == 400
    assert api_client.delete(f"/api/admin/roles/{admin['id']}").status_code == 400


def test_delete_role_in_use_rejected(api_client, api_engine, as_user):
    from app.models.database import User, UserRole, get_session
    as_user(["__local_admin__"])
    role_id = api_client.post("/api/admin/roles", json={
        "name": "ops2", "display_name": "运维2", "permissions": [],
    }).json()["id"]
    db = get_session(api_engine)
    try:
        db.add(User(id="u-x", username="x"))
        db.add(UserRole(id="ur-x", user_id="u-x", role_id=role_id))
        db.commit()
    finally:
        db.close()
    # 删除前先断言使用人数统计
    listed = api_client.get("/api/admin/roles").json()["items"]
    target = next(r for r in listed if r["id"] == role_id)
    assert target["user_count"] == 1
    assert api_client.delete(f"/api/admin/roles/{role_id}").status_code == 409


def test_wildcard_permission_rejected(api_client, as_user):
    """通配 * 仅内置 admin 持有;经 API 给自定义角色授予 * → 400,且不落库。"""
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/roles", json={
        "name": "star", "display_name": "通配", "permissions": ["*"],
    })
    assert res.status_code == 400
    assert res.json()["detail"] == "* 仅限内置管理员角色"

    role_id = api_client.post("/api/admin/roles", json={
        "name": "star-mix", "display_name": "混合", "permissions": ["sources.manage"],
    }).json()["id"]
    res = api_client.put(f"/api/admin/roles/{role_id}", json={"permissions": ["*", "sources.manage"]})
    assert res.status_code == 400
    listed = api_client.get("/api/admin/roles").json()["items"]
    target = next(r for r in listed if r["id"] == role_id)
    assert target["permissions"] == ["sources.manage"]


def test_user_manage_can_read_roles_but_not_edit(api_client, as_user):
    """用户管理页的角色下拉依赖:user.manage 可只读角色列表;权限目录与写端点仍限 role.manage。"""
    as_user([], permissions=["user.manage"])
    assert api_client.get("/api/admin/roles").status_code == 200
    assert api_client.get("/api/admin/permissions").status_code == 403
    assert api_client.post("/api/admin/roles", json={
        "name": "nope", "display_name": "越权", "permissions": [],
    }).status_code == 403
    # role.manage 原行为不变
    as_user([], permissions=["role.manage"])
    assert api_client.get("/api/admin/roles").status_code == 200
    # 无相关权限 → 403 且 detail 可读
    as_user([])
    res = api_client.get("/api/admin/roles")
    assert res.status_code == 403
    assert "role.manage" in res.json()["detail"]
