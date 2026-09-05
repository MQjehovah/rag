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
