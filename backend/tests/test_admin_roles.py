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
