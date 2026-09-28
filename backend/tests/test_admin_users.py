"""用户管理:列表/建本地号/禁用/重置密码/角色分配/SSO 组只读/自我保护。"""
from app.core.jwt_utils import create_access_token
from app.models.database import Role, User, UserGroup, UserRole, get_session


def _user(db, username, is_local=False, active=True, groups=()):
    u = User(id=f"u-{username}", username=username, is_local=is_local, is_active=active,
             password_hash="", display_name=username)
    db.add(u)
    for g in groups:
        db.add(UserGroup(id=f"g-{username}-{g}", user_id=u.id, group_name=g))
    db.commit()
    return u


def _grant_admin(db, user_id):
    role = db.query(Role).filter(Role.name == "admin").first()
    db.add(UserRole(id=f"ur-{user_id}", user_id=user_id, role_id=role.id))
    db.commit()


def test_list_and_create_local_user(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    res = api_client.post("/api/admin/users", json={
        "username": "lisi", "password": "Passw0rd!", "display_name": "李四",
        "email": "lisi@example.com", "roles": [], "groups": ["研发部"],
    })
    assert res.status_code == 200
    listed = api_client.get("/api/admin/users?query=lisi").json()
    item = next(u for u in listed["items"] if u["username"] == "lisi")
    assert item["is_local"] is True
    assert item["roles"] == [] and item["groups"] == ["研发部"]
    assert item["is_marked_admin"] is False
    login = api_client.post("/api/auth/login", json={"username": "lisi", "password": "Passw0rd!"})
    assert login.status_code == 200
    assert api_client.post("/api/admin/users", json={
        "username": "lisi", "password": "x", "display_name": "x",
    }).status_code == 409


def test_create_user_rejects_blank_password_and_strips_fields(api_client, as_user):
    as_user(["__local_admin__"])
    assert api_client.post("/api/admin/users", json={
        "username": "blank", "password": "   ",
    }).status_code == 400
    res = api_client.post("/api/admin/users", json={
        "username": "  spaced  ", "password": "Passw0rd!",
        "display_name": " 名字 ", "email": " e@example.com ",
    })
    assert res.status_code == 200
    assert res.json()["username"] == "spaced"
    assert res.json()["display_name"] == "名字"
    assert res.json()["email"] == "e@example.com"


def test_disable_user_blocks_login_and_token(api_client, api_engine, as_user):
    as_user(["__local_admin__"])
    uid = api_client.post("/api/admin/users", json={
        "username": "wangwu", "password": "Passw0rd!", "display_name": "王五",
    }).json()["id"]
    assert api_client.put(f"/api/admin/users/{uid}", json={"is_active": False}).status_code == 200
    assert api_client.post("/api/auth/login", json={"username": "wangwu", "password": "Passw0rd!"}).status_code == 403


def test_disabled_user_token_rejected(api_client, api_engine):
    """禁用后已签发的 HS256 token 立即失效(get_current_user 查库判 is_active)。"""
    db = get_session(api_engine)
    try:
        _user(db, "disabled", active=False)
    finally:
        db.close()
    token = create_access_token("u-disabled", [])
    res = api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401


def test_reset_password_local_only(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "sso-user", is_local=False)
        _user(db, "local-user", is_local=True)
    finally:
        db.close()
    as_user(["__local_admin__"])
    assert api_client.post(
        "/api/admin/users/u-sso-user/password", json={"password": "NewPass1!"}
    ).status_code == 400
    assert api_client.post(
        "/api/admin/users/u-local-user/password", json={"password": "   "}
    ).status_code == 400
    assert api_client.post(
        "/api/admin/users/u-local-user/password", json={"password": "NewPass1!"}
    ).status_code == 200
    assert api_client.post(
        "/api/auth/login", json={"username": "local-user", "password": "NewPass1!"}
    ).status_code == 200


def test_set_roles_and_guard_self(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "target")
        _user(db, "me", groups=["__local_admin__"])
    finally:
        db.close()
    me = as_user(["__local_admin__"], id="u-me")
    res = api_client.put("/api/admin/users/u-target/roles", json={"roles": ["admin"]})
    assert res.status_code == 200 and res.json()["roles"][0]["name"] == "admin"
    # 有 __local_admin__ 标记的自己清空角色 → 标记仍等效管理员,允许
    res = api_client.put(f"/api/admin/users/{me['id']}/roles", json={"roles": []})
    assert res.status_code == 200
    assert res.json()["roles"] == []
    assert res.json()["is_marked_admin"] is True
    # 禁用自己 → 400
    assert api_client.put(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 400


def test_self_role_admin_clearing_roles_blocked(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "self-admin")
        _grant_admin(db, "u-self-admin")
    finally:
        db.close()
    as_user([], permissions=["*"], roles=[{"name": "admin", "display_name": "管理员"}],
            id="u-self-admin")
    assert api_client.put(
        "/api/admin/users/u-self-admin/roles", json={"roles": []}
    ).status_code == 400


def test_non_admin_self_role_change_allowed(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "plain")
    finally:
        db.close()
    as_user(["__local_admin__"], id="u-plain")
    res = api_client.put("/api/admin/users/u-plain/roles", json={"roles": []})
    assert res.status_code == 200


def test_disable_last_admin_blocked(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "admin-a")
        _user(db, "admin-b")
        _grant_admin(db, "u-admin-a")
        _grant_admin(db, "u-admin-b")
    finally:
        db.close()
    as_user(["__local_admin__"], id="op")
    # 系统还有另一位 admin-capable → 允许禁用
    assert api_client.put("/api/admin/users/u-admin-a", json={"is_active": False}).status_code == 200
    # 仅剩最后一位 → 400
    assert api_client.put("/api/admin/users/u-admin-b", json={"is_active": False}).status_code == 400


def test_remove_last_admin_role_blocked(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "solo-admin")
        _user(db, "other")
        _grant_admin(db, "u-solo-admin")
    finally:
        db.close()
    as_user(["__local_admin__"], id="op")
    # solo-admin 是唯一 admin-capable:被他人去角色 → 400
    assert api_client.put(
        "/api/admin/users/u-solo-admin/roles", json={"roles": []}
    ).status_code == 400
    # 另一位也具备 admin 后 → 允许
    db = get_session(api_engine)
    try:
        _grant_admin(db, "u-other")
    finally:
        db.close()
    res = api_client.put("/api/admin/users/u-solo-admin/roles", json={"roles": []})
    assert res.status_code == 200 and res.json()["roles"] == []


def test_unknown_target_404_before_role_resolve(api_client, as_user):
    as_user(["__local_admin__"])
    assert api_client.put(
        "/api/admin/users/u-nope/roles", json={"roles": ["nope"]}
    ).status_code == 404
    assert api_client.put(
        "/api/admin/users/u-nope/groups", json={"groups": ["研发部"]}
    ).status_code == 404


def test_sso_user_groups_readonly(api_client, api_engine, as_user):
    db = get_session(api_engine)
    try:
        _user(db, "10086", is_local=False, groups=["研发部"])
    finally:
        db.close()
    as_user(["__local_admin__"])
    res = api_client.put("/api/admin/users/u-10086/groups", json={"groups": ["产品部"]})
    assert res.status_code == 400
