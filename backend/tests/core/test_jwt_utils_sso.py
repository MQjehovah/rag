"""get_current_user 双轨鉴权(HS256 | SSO OIDC)测试。

- SSO 轨:合法 RS256 token(sub=工号)自动建号并同步 groups;
  坏 token -> 401;已禁用用户 -> 403。
- HS256 老轨:自家 RAG token 仍按原逻辑解析(防回归)。

建库用 sqlite 临时文件 + get_engine/get_session/init_db(参考 auth.py startup 写法)。
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.jwt_utils import create_access_token, get_current_user
from app.models.database import User, UserGroup, get_engine, get_session, init_db
from tests.conftest import sign_token, valid_claims

SSO_EMP_NO = "202202100024"
SSO_GROUPS = ["研发部", "仪表盘-只读"]


@pytest.fixture
def db(tmp_path):
    """每测试一个独立 sqlite 文件库 + 单 session。"""
    db_path = tmp_path / "test_jwt_utils.db"
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    session: Session = get_session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _sso_token(key, emp_no=SSO_EMP_NO, groups=None):
    claims = {"sub": emp_no, "name": "测试用户", "email": f"{emp_no}@example.com"}
    if groups is not None:
        claims["groups"] = groups
    return sign_token(valid_claims(**claims), key)


def test_get_current_user_sso_provisions_and_does_not_duplicate(sso_env, db):
    """双轨命中 SSO:用户不存在 -> 自动建 User(username=sub)并返回 payload;
    再次调用不重复建号(计数不变)。"""
    key, _ = sso_env
    token = _sso_token(key, groups=SSO_GROUPS)

    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["username"] == SSO_EMP_NO
    assert payload["display_name"] == "测试用户"
    assert payload["is_local"] is False
    assert payload["is_active"] is True
    assert sorted(payload["groups"]) == sorted(SSO_GROUPS)

    user = db.query(User).filter(User.username == SSO_EMP_NO).first()
    assert user is not None
    assert user.email == f"{SSO_EMP_NO}@example.com"

    get_current_user(credentials=_bearer(token), db=db)
    assert db.query(User).count() == 1
    assert db.query(UserGroup).filter(UserGroup.user_id == user.id).count() == len(SSO_GROUPS)


def test_get_current_user_sso_rejects_invalid_token(sso_env, db):
    """双轨坏 token:无效字符串 -> 401。"""
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(credentials=_bearer("not.a.valid.jwt"), db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_sso_disabled_user_forbidden(sso_env, db):
    """SSO 命中但用户被禁用 -> 403。"""
    key, _ = sso_env
    disabled = User(
        id=str(uuid.uuid4()),
        username="202202100025",
        is_local=False,
        is_active=False,
    )
    db.add(disabled)
    db.commit()

    token = _sso_token(key, emp_no="202202100025")
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(credentials=_bearer(token), db=db)
    assert exc_info.value.status_code == 403


def test_get_current_user_hs256_local_user_still_works(db):
    """HS256 老路径:现有 RAG token 仍能通过(防回归)。"""
    user = User(
        id=str(uuid.uuid4()),
        username="admin",
        display_name="Admin",
        is_local=True,
        is_active=True,
    )
    db.add(user)
    db.add(UserGroup(id=str(uuid.uuid4()), user_id=user.id, group_name="__local_admin__"))
    db.commit()

    token = create_access_token(user.id, ["__local_admin__"])
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["id"] == user.id
    assert payload["username"] == "admin"
    assert payload["groups"] == ["__local_admin__"]
    assert payload["is_admin"] is False


def test_get_current_user_hs256_missing_user_unauthorized(db):
    """HS256 老路径:token 对应用户不存在 -> 401(保持原逻辑)。"""
    token = create_access_token(str(uuid.uuid4()), [])
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(credentials=_bearer(token), db=db)
    assert exc_info.value.status_code == 401


def test_get_current_user_hs256_disabled_user_unauthorized(db):
    """HS256 老路径:token 对应用户已禁用 -> 401(与不存在同样文案)。"""
    user = User(
        id=str(uuid.uuid4()),
        username="admin_disabled",
        is_local=True,
        is_active=False,
    )
    db.add(user)
    db.commit()

    token = create_access_token(user.id, [])
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(credentials=_bearer(token), db=db)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "用户不存在或已禁用"


def test_get_current_user_sso_keeps_groups_when_no_groups_claim(sso_env, db):
    """SSO 命中已有用户且 claims 无 groups 声明 -> 现有组不被触碰。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()),
        username=SSO_EMP_NO,
        email="old@example.com",
        display_name="旧名字",
        is_local=False,
        is_active=True,
    )
    db.add(existing)
    for g in ["研发部", "旧组"]:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=existing.id, group_name=g))
    db.commit()

    token = _sso_token(key)
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert sorted(payload["groups"]) == ["旧组", "研发部"]
    rows = db.query(UserGroup).filter(UserGroup.user_id == existing.id).all()
    assert sorted(r.group_name for r in rows) == ["旧组", "研发部"]


def test_get_current_user_sso_matches_existing_account_by_email(db, sso_env):
    """SSO token 的邮箱命中系统自建账号时复用该账号(不新建、不改 username)。

    与网关控制台/market 一致: 邮箱是首选唯一标识, 避免同一人两份账号。
    """
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()),
        username="jimingqing",  # 系统自建账号: username 不是工号
        email="jimingqing@xzrobot.com",
        display_name="旧名字",
        is_local=False,
        is_active=True,
    )
    db.add(existing)
    db.commit()

    token = sign_token(
        valid_claims(sub="202202100024", name="季明清", email="jimingqing@xzrobot.com"), key
    )
    payload = get_current_user(_bearer(token), db)

    assert payload["id"] == existing.id
    assert payload["username"] == "jimingqing"
    assert payload["display_name"] == "季明清"
    assert db.query(User).filter(User.email == "jimingqing@xzrobot.com").count() == 1


def test_normalize_claims_groups_includes_dept_as_group():
    """dept 即 group: SSO 只发 dept 时, 部门作为可见性组名纳入。"""
    from app.core.jwt_utils import _normalize_claims_groups

    groups = _normalize_claims_groups({"sub": "10086", "dept": "研发部", "roles": "user"})
    assert "研发部" in groups
    assert "user" in groups
    # department 别名同样采纳, 且不与 dept 去重前后重复
    groups2 = _normalize_claims_groups({"department": "财务部"})
    assert "财务部" in groups2


def test_get_current_user_sso_dept_becomes_visibility_group(sso_env, db):
    """SSO token 仅含 dept(无 groups)时, 该部门被同步为该用户的可见性组。"""
    key, _ = sso_env
    token = sign_token(
        valid_claims(sub="202202100099", name="部门用户", dept="研发部"), key
    )

    payload = get_current_user(_bearer(token), db)

    assert "研发部" in payload["groups"]
    user = db.query(User).filter(User.username == "202202100099").first()
    rows = db.query(UserGroup).filter(UserGroup.user_id == user.id).all()
    assert "研发部" in [r.group_name for r in rows]
