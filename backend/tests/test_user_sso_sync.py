"""统一用户体系: users 表启动幂等迁移测试。

PG 专属迁移(display_name→name, 补 work_id/phone)在 SQLite 测试库上应
整段 no-op;真实 PG 路径由部署启动时的 init_db(engine) 调用。
"""

from app.models.database import get_engine, init_db, run_user_column_migrations


def test_user_column_migrations_noop_on_sqlite(tmp_path):
    # 测试库为 SQLite: 迁移应跳过(PG 专用), 且不抛异常
    engine = get_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        run_user_column_migrations(engine)
    finally:
        engine.dispose()


def test_init_db_calls_user_column_migrations_on_sqlite(tmp_path):
    """启动路径冒烟: init_db 内部调用迁移, SQLite 下不炸且建出 users 新列。"""
    from sqlalchemy import inspect

    engine = get_engine(f"sqlite:///{tmp_path / 'init.db'}")
    try:
        init_db(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
    finally:
        engine.dispose()
    assert {"name", "work_id", "phone"} <= cols
