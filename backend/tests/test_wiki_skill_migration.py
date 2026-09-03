"""Phase 6 P43 migration 测试（20.6）。

- 临时 SQLite：upgrade head → 列存在；downgrade -1 → 列移除；upgrade head → 列恢复；
- ORM create_all：字段 nullable + CHECK（confidence/selected_by）生效；
- create_all 与 migration guard（check_managed_migrations）一致；
- PostgreSQL 方言编译校验（如实声明：仅编译验证，未真实运行 PG 实例）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.database import (
    WikiPage,
    check_managed_migrations,
    init_db,
)

_BACKEND = Path(__file__).resolve().parent.parent
_NEW_COLUMNS = [
    "content_skill", "skill_version", "skill_selected_by",
    "skill_confidence", "skill_locked", "skill_decision_json",
]


def _pg_ddl_compiles() -> None:
    """验证 P43 使用的 sa.Column DDL 可在 PostgreSQL 方言编译（不真实运行）。"""
    import importlib.util

    from sqlalchemy.schema import CreateColumn
    from sqlalchemy.dialects import postgresql

    path = _BACKEND / "alembic" / "versions" / "d3e4f5a6b7c8_p43_wiki_skill_fields.py"
    spec = importlib.util.spec_from_file_location("p43_migration", path)
    p43 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p43)

    for column in p43._SKILL_COLUMNS:
        sql = str(CreateColumn(column).compile(dialect=postgresql.dialect()))
        assert sql.strip()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    s = sessionmaker(bind=engine)()
    yield s, engine
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Alembic 往返（临时 SQLite 文件库）
# ---------------------------------------------------------------------------


def test_p43_alembic_roundtrip(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    script = f"""
import os, sys
sys.path.insert(0, {str(_BACKEND)!r})
os.environ["DATABASE_URL"] = {url!r}
from alembic.config import Config
from alembic import command
cfg = Config({str(_BACKEND / 'alembic.ini')!r})
cfg.set_main_option("script_location", {str(_BACKEND / 'alembic')!r})

def cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]

import sqlite3
dbfile = {url!r}[len("sqlite:///"):]
# upgrade head：列全部存在
command.upgrade(cfg, "head")
with sqlite3.connect(dbfile) as c:
    present = cols(c)
for col in {_NEW_COLUMNS!r}:
    assert col in present, f"missing after upgrade: {{col}}"
# downgrade -1：列移除
command.downgrade(cfg, "-1")
with sqlite3.connect(dbfile) as c:
    after_down = cols(c)
for col in {_NEW_COLUMNS!r}:
    assert col not in after_down, f"still present after downgrade: {{col}}"
# 再 upgrade head
command.upgrade(cfg, "head")
with sqlite3.connect(dbfile) as c:
    present2 = cols(c)
for col in {_NEW_COLUMNS!r}:
    assert col in present2, f"missing after re-upgrade: {{col}}"
print("P43_ROUNDTRIP_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "P43_ROUNDTRIP_OK" in proc.stdout


def test_single_head(tmp_path):
    url = f"sqlite:///{(tmp_path / 'head.db').as_posix()}"
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    script = f"""
import os, sys
sys.path.insert(0, {str(_BACKEND)!r})
os.environ["DATABASE_URL"] = {url!r}
from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
cfg = Config({str(_BACKEND / 'alembic.ini')!r})
cfg.set_main_option("script_location", {str(_BACKEND / 'alembic')!r})
heads = ScriptDirectory.from_config(cfg).get_heads()
assert len(heads) == 1, heads
assert heads[0] == "d3e4f5a6b7c8", heads
print("SINGLE_HEAD_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "SINGLE_HEAD_OK" in proc.stdout


# ---------------------------------------------------------------------------
# ORM：nullable / CHECK / guard
# ---------------------------------------------------------------------------


def test_orm_columns_nullable(db):
    s, engine = db
    from app.models.database import WikiWorkspace, WikiRevision

    ws = WikiWorkspace(key="k1", name="ws", acl_scope="company", scope_id="company")
    s.add(ws)
    s.flush()
    wp = WikiPage(
        title="legacy", workspace_id=ws.id,
        content_skill=None, skill_version=None, skill_selected_by=None,
        skill_confidence=None, skill_locked=None, skill_decision_json=None,
    )
    s.add(wp)
    s.commit()
    loaded = s.get(WikiPage, wp.id)
    assert loaded.content_skill is None  # 历史 Wiki 字段保持 nullable


def test_confidence_check(db):
    s, _ = db
    from app.models.database import WikiWorkspace

    ws = WikiWorkspace(key="k2", name="ws", acl_scope="company", scope_id="company")
    s.add(ws)
    s.commit()
    wp = WikiPage(title="x", workspace_id=ws.id, skill_confidence=1.5)
    s.add(wp)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    wp2 = WikiPage(title="x", workspace_id=ws.id, skill_confidence=-0.1)
    s.add(wp2)
    with pytest.raises(IntegrityError):
        s.commit()


def test_selected_by_check(db):
    s, _ = db
    from app.models.database import WikiWorkspace

    ws = WikiWorkspace(key="k3", name="ws", acl_scope="company", scope_id="company")
    s.add(ws)
    s.commit()
    wp = WikiPage(title="x", workspace_id=ws.id, skill_selected_by="hacker")
    s.add(wp)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    for ok in ("auto", "manual", "migration", "default_fallback", "locked", "sticky"):
        wp = WikiPage(title=ok, workspace_id=ws.id, skill_selected_by=ok)
        s.add(wp)
        s.commit()


def test_create_all_matches_guard(db):
    s, engine = db
    # create_all 后托管字段齐全 → check_managed_migrations 无缺失。
    assert check_managed_migrations(engine) == []
    cols = {c["name"] for c in engine.dialect.get_columns(engine.connect(), "wiki_pages")}
    for col in _NEW_COLUMNS:
        assert col in cols


def test_postgresql_dialect_compile():
    _pg_ddl_compiles()  # 如实声明：未连接真实 PostgreSQL，仅做方言编译校验。
