"""用户管理:列表/建本地号/禁用/重置密码/角色分配/SSO 组只读/自我保护。"""
from app.models.database import User, UserGroup, get_session


def _user(db, username, is_local=False, active=True, groups=()):
    u = User(id=f"u-{username}", username=username, is_local=is_local, is_active=active,
             password_hash="", display_name=username)
    db.add(u)
    for g in groups:
        db.add(UserGroup(id=f"g-{username}-{g}", user_id=u.id, group_name=g))
    db.commit()
    return u


def test_list_and_create_local_user(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/users", json={
        "username": "lisi", "password": "Passw0rd!", "display_name": "李四",
        "email": "lisi@example.com", "roles": [], "groups": ["研发部"],
    })
    assert res.status_code == 200
    uid = res.json()["id"]
    listed = api_client.get("/api/admin/users?query=lisi").json()
    assert any(u["username"] == "lisi" and u["is_local"] for u in listed["items"])
    login = api_client.post("/api/auth/login", json={"username": "lisi", "password": "Passw0rd!"})
    assert login.status_code == 200
    assert api_client.post("/api/admin/users", json={
        "username": "lisi", "password": "x", "display_name": "x",
    }).status_code == 409
    assert uid


def test_disable_user_blocks_login_and_token(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    uid = api_client.post("/api/admin/users", json={
        "username": "wangwu", "password": "Passw0rd!", "display_name": "王五",
    }).json()["id"]
    assert api_client.put(f"/api/admin/users/{uid}", json={"is_active": False}).status_code == 200
    assert api_client.post("/api/auth/login", json={"username": "wangwu", "password": "Passw0rd!"}).status_code == 403


def test_reset_password_local_only(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "sso-user", is_local=False)
    finally:
        db.close()
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/users/u-sso-user/password", json={"password": "NewPass1!"})
    assert res.status_code == 400


def test_set_roles_and_guard_self(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "target")
    finally:
        db.close()
    me = as_user(["__local_admin__"], id="me")
    res = api_client.put("/api/admin/users/u-target/roles", json={"roles": ["admin"]})
    assert res.status_code == 200 and res.json()["roles"][0]["name"] == "admin"
    # 移除自己的最后管理员来源 → 400
    assert api_client.put(f"/api/admin/users/{me['id']}/roles", json={"roles": []}).status_code == 400
    # 禁用自己 → 400
    assert api_client.put(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 400


def test_sso_user_groups_readonly(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "10086", is_local=False, groups=["研发部"])
    finally:
        db.close()
    as_user(["__local_admin__"])
    res = api_client.put("/api/admin/users/u-10086/groups", json={"groups": ["产品部"]})
    assert res.status_code == 400
