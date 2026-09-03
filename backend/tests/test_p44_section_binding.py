"""Phase 7C.1 P44 migration 测试（WikiSection 结构字段 + Evidence Binding 表）。

- 临时 SQLite：upgrade head → 字段/表存在；downgrade -1 → 移除；upgrade head → 恢复；
- 单一 Alembic head（P44 = a9b8c7d6e5f4）；
- 历史 WikiSection nullable 兼容（新增字段全 NULL，不虚假回填）；
- validation_status / usage_type CHECK；
- (revision_id, section_key) 部分唯一；binding 四元组唯一；
- FK CASCADE；create_all/schema guard 一致。
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
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    check_managed_migrations,
    init_db,
)

_BACKEND = Path(__file__).resolve().parent.parent
_P44_REVISION = "a9b8c7d6e5f4"
_NEW_COLUMNS = [
    "section_key", "skill_key", "skill_version", "content_hash",
    "validation_status", "structure_json",
]
_BINDING_TABLE = "wiki_section_evidence_bindings"


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


def _new_section(s, *, revision=None, section_key=None):
    if revision is None:
        wp = WikiPage(title="page", id="wp-t-1")
        s.add(wp)
        s.flush()
        revision = WikiRevision(wiki_page_id=wp.id, title="rev", id="rev-t-1")
        s.add(revision)
        s.flush()
    sec = WikiSection(
        id=revision.id + "-sec",
        revision_id=revision.id,
        section_type="summary",
        heading="h",
        section_key=section_key,
    )
    s.add(sec)
    s.flush()
    return sec


def _new_evidence(s, page_id="page-e-1", eid="ev-1"):
    from app.models.database import EvidenceItem, Page

    if s.get(Page, page_id) is None:
        s.add(Page(id=page_id, title="src"))
        s.flush()
    s.add(EvidenceItem(
        id=eid, source_page_id=page_id, status="active",
        content="GET /users", evidence_type="text",
        content_hash="a" * 64, source_doc_hash="a" * 64))
    s.flush()
    return eid


# ---------------------------------------------------------------------------
# Alembic 往返（临时 SQLite 文件库）
# ---------------------------------------------------------------------------


def test_p44_alembic_roundtrip(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    script = f"""
import os, sys, sqlite3
sys.path.insert(0, {str(_BACKEND)!r})
os.environ["DATABASE_URL"] = {url!r}
from alembic.config import Config
from alembic import command
cfg = Config({str(_BACKEND / 'alembic.ini')!r})
cfg.set_main_option("script_location", {str(_BACKEND / 'alembic')!r})

def tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

dbfile = {url!r}[len("sqlite:///"):]
command.upgrade(cfg, "head")
with sqlite3.connect(dbfile) as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(wiki_sections)").fetchall()]
    tbls = tables(c)
for col in {_NEW_COLUMNS!r}:
    assert col in cols, f"missing after upgrade: {{col}}"
assert {_BINDING_TABLE!r} in tbls, "binding table missing after upgrade"
command.downgrade(cfg, "-1")
with sqlite3.connect(dbfile) as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(wiki_sections)").fetchall()]
    tbls = tables(c)
for col in {_NEW_COLUMNS!r}:
    assert col not in cols, f"still present after downgrade: {{col}}"
assert {_BINDING_TABLE!r} not in tbls, "binding table still present after downgrade"
command.upgrade(cfg, "head")
with sqlite3.connect(dbfile) as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(wiki_sections)").fetchall()]
    tbls = tables(c)
for col in {_NEW_COLUMNS!r}:
    assert col in cols, f"missing after re-upgrade: {{col}}"
assert {_BINDING_TABLE!r} in tbls
print("P44_ROUNDTRIP_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "P44_ROUNDTRIP_OK" in proc.stdout


def test_single_head(tmp_path):
    url = f"sqlite:///{(tmp_path / 'head.db').as_posix()}"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
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
assert heads[0] == {_P44_REVISION!r}, heads
print("SINGLE_HEAD_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "SINGLE_HEAD_OK" in proc.stdout


# ---------------------------------------------------------------------------
# ORM：nullable / CHECK / 部分唯一 / binding 约束
# ---------------------------------------------------------------------------


def test_create_all_matches_guard(db):
    s, engine = db
    assert check_managed_migrations(engine) == []
    cols = {c["name"] for c in engine.dialect.get_columns(engine.connect(), "wiki_sections")}
    for col in _NEW_COLUMNS:
        assert col in cols
    assert engine.dialect.has_table(engine.connect(), _BINDING_TABLE)


def test_guard_flags_missing_check_constraints(db):
    from sqlalchemy import inspect

    import app.models.database as db_mod

    s, engine = db

    class _NoChecksInspector:
        """包装真实 inspector：结构完整但 CHECK 约束被抹除（guard 反例）。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_check_constraints(self, table):
            return []

    inner = inspect(engine)
    problems = db_mod._check_p44_schema(_NoChecksInspector(inner))
    assert any("wiki_sections:check_missing=ck_wiki_sections_validation_status"
               in p for p in problems)
    assert any("wiki_sections:check_missing=ck_wiki_sections_section_key_nonempty"
               in p for p in problems)
    assert any("check_missing=ck_wiki_section_evidence_bindings_usage_type"
               in p for p in problems)
    assert any("check_missing=ck_wiki_section_evidence_bindings_field_path_nonempty"
               in p for p in problems)
    assert any("check_missing=ck_wiki_section_evidence_bindings_evidence_hash_len"
               in p for p in problems)


def test_guard_flags_wrong_section_field_type(db):
    from sqlalchemy import inspect

    import app.models.database as db_mod

    s, engine = db

    class _WrongTypeInspector:
        """结构看似存在但 validation_status 字段类型错误（VARCHAR→INTEGER）。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_columns(self, table):
            cols = self._inner.get_columns(table)
            if table == "wiki_sections":
                return [
                    {"name": c["name"],
                     "type": __import__("sqlalchemy").Integer()
                     if c["name"] == "validation_status" else c["type"],
                     "nullable": c["nullable"]}
                    for c in cols
                ]
            return cols

    problems = db_mod._check_p44_schema(_WrongTypeInspector(inspect(engine)))
    assert any("wiki_sections.validation_status:type=" in p for p in problems)


def test_historical_section_fields_nullable(db):
    s, _ = db
    sec = _new_section(s)
    s.commit()
    loaded = s.get(WikiSection, sec.id)
    assert loaded.section_key is None
    assert loaded.skill_key is None
    assert loaded.skill_version is None
    assert loaded.content_hash is None
    assert loaded.validation_status is None
    assert loaded.structure_json is None


def test_validation_status_check(db):
    s, _ = db
    sec = _new_section(s, section_key="overview")
    sec.validation_status = "weird"
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    ok = _new_section(s, section_key="auth")
    ok.validation_status = "pass"
    s.commit()
    assert s.get(WikiSection, ok.id).validation_status == "pass"


def test_section_key_partial_unique(db):
    s, _ = db
    wp = WikiPage(title="p", id="wp-uniq-1")
    s.add(wp)
    s.flush()
    rev = WikiRevision(wiki_page_id=wp.id, title="r", id="rev-uniq-1")
    s.add(rev)
    s.commit()  # 先提交 revision，失败后 rollback 不影响已提交的 revision
    a = WikiSection(id="sec-u1", revision_id=rev.id, section_type="x",
                    heading="a", section_key="endpoint.1")
    b = WikiSection(id="sec-u2", revision_id=rev.id, section_type="x",
                    heading="b", section_key="endpoint.1")
    s.add_all([a, b])
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    # NULL section_key 的历史 Section 允许重复；非空不同 key 允许。
    s.add(WikiSection(id="sec-n1", revision_id=rev.id, section_type="x",
                      heading="a", section_key=None))
    s.add(WikiSection(id="sec-n2", revision_id=rev.id, section_type="x",
                      heading="b", section_key=None))
    s.add(WikiSection(id="sec-n3", revision_id=rev.id, section_type="x",
                      heading="c", section_key="endpoint.2"))
    s.commit()


def test_binding_unique_and_usage_check(db):
    s, _ = db
    sec = _new_section(s, section_key="endpoint.1")
    _new_evidence(s, page_id="page-e-1", eid="ev-1")
    s.commit()

    def add(usage):
        binding = WikiSectionEvidenceBinding(
            id=f"bind-{usage}-1", section_id=sec.id, evidence_id="ev-1",
            field_path="responses.200", usage_type=usage,
            evidence_content_hash="b" * 64)
        s.add(binding)

    add("support")
    s.commit()
    # 四元组唯一：同 usage 重复 → 失败；support/conflict 共存 → 允许。
    add("support")
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    add("conflict")
    s.commit()
    add("weird")
    with pytest.raises(IntegrityError):
        s.commit()


def test_binding_fk_cascade(db):
    from app.models.database import EvidenceItem

    s, _ = db
    wp = WikiPage(title="p", id="wp-cas-1")
    s.add(wp)
    s.flush()
    rev = WikiRevision(wiki_page_id=wp.id, title="r", id="rev-cas-1")
    s.add(rev)
    s.flush()
    sec = WikiSection(id="sec-cas-1", revision_id=rev.id, section_type="x",
                      heading="h", section_key="overview")
    s.add(sec)
    _new_evidence(s, page_id="page-cas-1", eid="ev-cas-1")
    s.add(WikiSectionEvidenceBinding(
        id="bind-cas-1", section_id=sec.id, evidence_id="ev-cas-1",
        field_path="overview", usage_type="support",
        evidence_content_hash="c" * 64))
    _new_evidence(s, page_id="page-cas-1", eid="ev-cas-2")
    s.add(WikiSectionEvidenceBinding(
        id="bind-cas-3", section_id=sec.id, evidence_id="ev-cas-2",
        field_path="overview", usage_type="support",
        evidence_content_hash="c" * 64))
    s.commit()
    # 删除 Evidence → 其 binding CASCADE 删除；另一条 evidence 的 binding 保留。
    s.delete(s.get(EvidenceItem, "ev-cas-1"))
    s.commit()
    assert s.get(WikiSectionEvidenceBinding, "bind-cas-1") is None
    assert s.get(WikiSectionEvidenceBinding, "bind-cas-3") is not None
    # 删除 Section → 剩余 binding CASCADE 删除。
    s.delete(s.get(WikiSection, sec.id))
    s.commit()
    assert s.get(WikiSectionEvidenceBinding, "bind-cas-3") is None
