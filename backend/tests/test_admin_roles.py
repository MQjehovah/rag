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
    assert api_client.delete(f"/api/admin/roles/{role_id}").status_code == 409
