"""统一用户体系: users 表启动幂等迁移与 SSO 登录资料回写测试。

PG 专属迁移(display_name→name, 补 work_id/phone/department)在 SQLite 测试库上应
整段 no-op;真实 PG 路径由部署启动时的 init_db(engine) 调用。
SSO 回写用临时 RSA 自签 token + file:// JWKS(helper 见 tests/conftest.py)。
"""

import uuid

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.jwt_utils import get_current_user
from app.models.database import (
    User, UserGroup, get_engine, get_session, init_db, run_user_column_migrations,
)
from tests.conftest import sign_token, valid_claims

SSO_EMP_NO = "202202100024"


@pytest.fixture
def db(tmp_path):
    """每测试一个独立 sqlite 文件库 + 单 session。"""
    engine = get_engine(f"sqlite:///{tmp_path / 'user_sync.db'}")
    init_db(engine)
    session: Session = get_session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _sso_token(key, emp_no=SSO_EMP_NO, **claims):
    return sign_token(valid_claims(sub=emp_no, **claims), key)


def test_user_column_migrations_noop_on_sqlite(tmp_path):
    # 测试库为 SQLite: 迁移应跳过(PG 专用), 且不抛异常
    engine = get_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        run_user_column_migrations(engine)
    finally:
        engine.dispose()


def test_init_db_calls_user_column_migrations_on_sqlite(tmp_path, monkeypatch):
    """启动路径: init_db 必须以 engine 调用迁移(SQLite 下迁移自身 no-op)。"""
    from sqlalchemy import inspect

    from app.models import database as database_module

    calls = []
    monkeypatch.setattr(
        database_module, "run_user_column_migrations", lambda engine: calls.append(engine)
    )
    engine = get_engine(f"sqlite:///{tmp_path / 'init.db'}")
    try:
        init_db(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
    finally:
        engine.dispose()
    assert calls == [engine]
    assert {"name", "work_id", "phone", "department"} <= cols


def test_sso_provisions_all_profile_fields(sso_env, db):
    """全字段建号: name/work_id(恒 sub)/phone/department/email 均按 claims 落库。"""
    key, _ = sso_env
    token = _sso_token(
        key, name="张三", email="zhangsan@example.com", mobile="13800000000", dept="研发部"
    )
    payload = get_current_user(credentials=_bearer(token), db=db)

    user = db.query(User).filter(User.username == SSO_EMP_NO).first()
    assert user is not None
    assert user.name == "张三"
    assert user.work_id == SSO_EMP_NO
    assert user.phone == "13800000000"
    assert user.department == "研发部"
    assert user.email == "zhangsan@example.com"
    assert payload["name"] == "张三"
    assert payload["work_id"] == SSO_EMP_NO
    assert payload["phone"] == "13800000000"
    assert payload["department"] == "研发部"


def test_sso_provision_falls_back_name_to_work_id(sso_env, db):
    """建号缺资料: name 空白回退工号, phone 空白落空串。"""
    key, _ = sso_env
    token = _sso_token(key, name="  ", mobile=" ")
    get_current_user(credentials=_bearer(token), db=db)

    user = db.query(User).filter(User.username == SSO_EMP_NO).first()
    assert user.name == SSO_EMP_NO
    assert user.phone == ""
    assert user.work_id == SSO_EMP_NO


def test_sso_matches_existing_account_by_work_id(sso_env, db):
    """工号命中: work_id=sub 的已有账号直接复用(不新建, username 不改)。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username="jimingqing", email="jmq@example.com",
        name="旧名字", work_id=SSO_EMP_NO,
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.commit()

    token = _sso_token(key, name="季明清", email="new@example.com")
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["id"] == existing.id
    assert payload["username"] == "jimingqing"
    assert db.query(User).filter(User.username == SSO_EMP_NO).count() == 0
    db.refresh(existing)
    assert existing.work_id == SSO_EMP_NO


def test_sso_matches_by_email_and_backfills_work_id(sso_env, db):
    """邮箱命中老账号: 复用账号并补 work_id=sub(此后可按工号直达, 与 market 一致)。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username="jimingqing", email="jimingqing@example.com",
        name="旧名字", work_id="",
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.commit()

    token = _sso_token(key, name="季明清", email="jimingqing@example.com")
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["id"] == existing.id
    assert db.query(User).count() == 1
    db.refresh(existing)
    assert existing.work_id == SSO_EMP_NO

    # 补号后二次登录按 work_id 直达, 仍不新建
    get_current_user(credentials=_bearer(token), db=db)
    assert db.query(User).count() == 1


def test_sso_matches_legacy_account_by_username(sso_env, db):
    """老账号兼容: 工号/邮箱均未命中时回退 username, 并补工号。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username=SSO_EMP_NO, email="old@example.com",
        name="旧名字", work_id="",
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.commit()

    token = _sso_token(key, email="new@example.com")
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["id"] == existing.id
    assert db.query(User).count() == 1
    db.refresh(existing)
    assert existing.work_id == SSO_EMP_NO


def test_sso_match_priority_work_id_over_email_and_username(sso_env, db):
    """匹配顺序: 工号优先于邮箱与用户名(三账号并存时命中 work_id 账号)。"""
    key, _ = sso_env
    by_work_id = User(
        id="u-work", username="acct-work", email="work@example.com",
        name="工号账号", work_id=SSO_EMP_NO,
        is_local=False, is_active=True,
    )
    by_email = User(
        id="u-mail", username="acct-mail", email="mail@example.com",
        name="邮箱账号", work_id="",
        is_local=False, is_active=True,
    )
    by_username = User(
        id="u-name", username=SSO_EMP_NO, email="name@example.com",
        name="用户账号", work_id="",
        is_local=False, is_active=True,
    )
    for u in (by_work_id, by_email, by_username):
        db.add(u)
    db.commit()

    token = _sso_token(key, email="mail@example.com")
    payload = get_current_user(credentials=_bearer(token), db=db)

    assert payload["id"] == "u-work"
    assert db.query(User).count() == 3
    # 邮箱账号未被误匹配, 其 work_id 不被改写
    db.refresh(by_email)
    assert by_email.work_id == ""


def test_sso_repeat_login_writes_back_changed_fields(sso_env, db):
    """重复登录: name/phone/department/email 变化回写, work_id 对齐 sub, 组不受影响。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username=SSO_EMP_NO, email="old@example.com",
        name="旧名字", work_id="stale", phone="111", department="旧部门",
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.add(UserGroup(id=str(uuid.uuid4()), user_id=existing.id, group_name="研发部"))
    db.commit()

    token = _sso_token(
        key, name="季明清", email="new@example.com", mobile="13900000000", dept="研发部"
    )
    payload = get_current_user(credentials=_bearer(token), db=db)

    db.refresh(existing)
    assert existing.name == "季明清"
    assert existing.work_id == SSO_EMP_NO
    assert existing.phone == "13900000000"
    assert existing.department == "研发部"
    assert existing.email == "new@example.com"
    assert payload["groups"] == ["研发部"]


def test_sso_empty_claims_keep_existing_profile(sso_env, db):
    """空 claim 保留: 无 name/mobile/dept 声明时不清空库中已有值, work_id 仍对齐。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username=SSO_EMP_NO, email="keep@example.com",
        name="保留名字", work_id="", phone="13800000000", department="保留部门",
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.commit()

    token = _sso_token(key)  # 仅 sub(工号)
    get_current_user(credentials=_bearer(token), db=db)

    db.refresh(existing)
    assert existing.name == "保留名字"
    assert existing.phone == "13800000000"
    assert existing.department == "保留部门"
    assert existing.email == "keep@example.com"
    assert existing.work_id == SSO_EMP_NO


def test_sso_work_id_always_aligns_to_sub(sso_env, db):
    """work_id 恒对齐: 即使 claims 无任何资料字段, 也校正为当前 sub。"""
    key, _ = sso_env
    existing = User(
        id=str(uuid.uuid4()), username=SSO_EMP_NO, email="x@example.com",
        name="名字", work_id="202000000000",
        is_local=False, is_active=True,
    )
    db.add(existing)
    db.commit()

    token = _sso_token(key, emp_no=SSO_EMP_NO)
    get_current_user(credentials=_bearer(token), db=db)

    db.refresh(existing)
    assert existing.work_id == SSO_EMP_NO


def test_sso_unchanged_claims_skip_commit(sso_env, db, monkeypatch):
    """同 claims 二次登录: 字段无变化时不 commit(避免每请求无谓写入)。"""
    key, _ = sso_env
    token = _sso_token(key, name="张三", email="z@example.com", mobile="13800000000")
    get_current_user(credentials=_bearer(token), db=db)

    commits = []
    original_commit = Session.commit
    monkeypatch.setattr(Session, "commit", lambda self: commits.append(1) or original_commit(self))
    get_current_user(credentials=_bearer(token), db=db)

    assert commits == []
