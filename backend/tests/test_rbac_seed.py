"""RBAC 表结构与启动 seed:内置 admin 角色、本地管理员补角色、组注册表回填。"""
import json

from app.models.database import (
    CompileTemplate, Group, Notebook, Pipeline, Role, User, UserGroup, UserRole,
    WikiSpace, get_engine, init_db, get_session,
)
from app.core.rbac_seed import seed_rbac


def _mk_engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'rbac.db'}")
    init_db(engine)
    return engine


def test_seed_creates_admin_role(tmp_path):
    engine = _mk_engine(tmp_path)
    seed_rbac(engine)
    db = get_session(engine)
    try:
        admin = db.query(Role).filter(Role.name == "admin").first()
        assert admin is not None
        assert admin.is_system is True
        assert json.loads(admin.permissions) == ["*"]
    finally:
        db.close()


def test_seed_grants_admin_role_to_local_admin(tmp_path):
    engine = _mk_engine(tmp_path)
    db = get_session(engine)
    try:
        u = User(id="u-admin", username="admin", is_local=True, is_active=True)
        db.add(u)
        db.add(UserGroup(id="g1", user_id="u-admin", group_name="__local_admin__"))
        db.commit()
    finally:
        db.close()
    seed_rbac(engine)
    db = get_session(engine)
    try:
        admin_role = db.query(Role).filter(Role.name == "admin").first()
        assert db.query(UserRole).filter(
            UserRole.user_id == "u-admin", UserRole.role_id == admin_role.id
        ).first() is not None
    finally:
        db.close()


def test_seed_backfills_group_registry_and_skips_internal(tmp_path):
    engine = _mk_engine(tmp_path)
    db = get_session(engine)
    try:
        u = User(id="u-1", username="zhangsan")
        db.add(u)
        db.add(UserGroup(id="g1", user_id="u-1", group_name="研发部"))
        db.add(UserGroup(id="g2", user_id="u-1", group_name="__local_admin__"))
        db.add(Notebook(id="nb-1", name="共享", group_id="产品部"))
        db.commit()
    finally:
        db.close()
    seed_rbac(engine)
    db = get_session(engine)
    try:
        names = {g.name for g in db.query(Group).all()}
        assert "研发部" in names and "产品部" in names
        assert "__local_admin__" not in names
    finally:
        db.close()


def test_seed_is_idempotent(tmp_path):
    engine = _mk_engine(tmp_path)
    seed_rbac(engine)
    seed_rbac(engine)
    db = get_session(engine)
    try:
        assert db.query(Role).filter(Role.name == "admin").count() == 1
    finally:
        db.close()


def test_seed_idempotent_with_preexisting_rows(tmp_path):
    """预置同名组/管理员标记等既有行后重复 seed:不抛异常、不产生重复行。"""
    engine = _mk_engine(tmp_path)
    db = get_session(engine)
    try:
        u = User(id="u-adm", username="admin", is_local=True, is_active=True)
        db.add(u)
        db.add(UserGroup(id="ug-1", user_id="u-adm", group_name="__local_admin__"))
        # 预置同名组,覆盖回填时的去重/唯一约束路径
        db.add(Group(id="grp-1", name="研发部", source="local"))
        db.add(Notebook(id="nb-1", name="笔记本", group_id="研发部"))
        db.add(WikiSpace(id="ws-1", name="空间", group_id="产品部"))
        db.add(Pipeline(id="pl-1", name="管道", group_id="运营部"))
        db.add(CompileTemplate(id="ct-1", name="模板", group_id="运营部"))
        db.commit()
    finally:
        db.close()

    seed_rbac(engine)
    db = get_session(engine)
    try:
        first_names = {g.name for g in db.query(Group).all()}
    finally:
        db.close()

    seed_rbac(engine)
    db = get_session(engine)
    try:
        assert db.query(Role).filter(Role.name == "admin").count() == 1
        admin_role = db.query(Role).filter(Role.name == "admin").one()
        assert db.query(UserRole).filter(
            UserRole.user_id == "u-adm", UserRole.role_id == admin_role.id
        ).count() == 1
        assert db.query(UserRole).count() == 1
        assert {g.name for g in db.query(Group).all()} == first_names
        assert {"研发部", "产品部", "运营部"} <= first_names
    finally:
        db.close()


def test_seed_backfill_skips_null_and_blank_group_ids(tmp_path):
    """资源表 NULL/空 group_id 不得混入注册表。

    生产回归:Postgres 中大量 group_id IS NULL 的历史行曾使 sorted() 混合 None 崩溃,
    导致启动 seed 失败、组注册表为空。
    """
    engine = _mk_engine(tmp_path)
    db = get_session(engine)
    try:
        db.add(Notebook(id="nb-null", name="n1"))                   # group_id = None
        db.add(Notebook(id="nb-blank", name="n2", group_id=""))     # 空串
        db.add(Notebook(id="nb-real", name="n3", group_id="研发部"))
        db.add(WikiSpace(id="ws-null", name="s1"))                  # None
        db.add(Pipeline(id="pl-null", name="p1"))                   # None
        db.add(CompileTemplate(id="ct-null", name="t1"))            # None
        db.commit()
    finally:
        db.close()

    seed_rbac(engine)  # 不应抛异常
    db = get_session(engine)
    try:
        names = {g.name for g in db.query(Group).all()}
        assert names == {"研发部"}
        assert None not in names
    finally:
        db.close()
