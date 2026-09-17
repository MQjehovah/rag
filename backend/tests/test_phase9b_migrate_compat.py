# -*- coding: utf-8 -*-
"""Phase 9B 副本迁移兼容修复 反例测试（合成 SQLite，cwd=backend）。

覆盖（配合 backend/migration_compat.py 与 P40/P41/P43/P44 的兼容分支）：
1. 干净 P38 → head（正常路径，不回归）。
2. 部分新列提前存在且等价 → 补齐缺失结构后成功、既有值不丢。
3. 同名列类型/nullable 不兼容 → 受控失败（版本仍 P38、不删数据）。
4. 列已存在但 FK/索引缺失 → 正确补齐（P41 workspace_id）。
5. 数据违反新增约束（P43 skill_confidence）→ 失败且数据不被清理。
6. 兼容迁移不触发任何业务调用（仅 DDL 运行，自然满足）。

真实 baseline 副本 → head 的完整演练在 phase9b README 第 9 节记录（主 Agent 以副本执行，
不在此重复跑）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.alembic_head import current_alembic_head

_BACKEND = Path(__file__).resolve().parent.parent
_P38 = "e6f7a8b9c0d1"
_P40_COLS = [
    ("note_schema_version", "VARCHAR(64)"),
    ("content_format", "VARCHAR(64)"),
    ("content_kind", "VARCHAR(64)"),
    ("converter_key", "VARCHAR(127)"),
    ("converter_version", "VARCHAR(127)"),
    ("conversion_status", "VARCHAR(32)"),
    ("conversion_warnings_json", "TEXT"),
]


def _alembic(db_path: str, cmd: str, rev: str) -> subprocess.CompletedProcess:
    url = "sqlite:///" + Path(db_path).as_posix()
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url={url}", cmd, rev],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_BACKEND), env=env, timeout=600,
    )


def _upgrade_to(db_path: str, rev: str) -> None:
    proc = _alembic(db_path, "upgrade", rev)
    assert proc.returncode == 0, f"upgrade {rev} failed:\n{proc.stderr}"


def _version(db_path: str) -> str:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def _exec(db_path: str, sql: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _fresh(tmp_path) -> str:
    db = str(tmp_path / "db.sqlite")
    return db


def test_clean_p38_to_head(tmp_path):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode == 0, proc.stderr
    assert _version(db) == current_alembic_head()


def test_partial_ahead_p40_columns_upgrade_head_and_values_kept(tmp_path):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    # 模拟“物理超前”：P40 的 7 列已存在且等价；写入一行 canonical 值。
    for name, typ in _P40_COLS:
        _exec(db, f"ALTER TABLE pages ADD COLUMN {name} {typ}")
    _exec(db, "INSERT INTO pages (id, title, note_schema_version, content_format, "
              "converter_key, conversion_status) VALUES ('p-ahead', 't', "
              "'legacy-note/v0', 'markdown', 'conv-x', 'converted')")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode == 0, proc.stderr
    assert _version(db) == current_alembic_head()
    # 提前存在的值与行不丢（迁移不重写既有值）。
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT note_schema_version, content_format, converter_key, conversion_status "
            "FROM pages WHERE id='p-ahead'"
        ).fetchone()
        assert row == ("legacy-note/v0", "markdown", "conv-x", "converted")
    finally:
        conn.close()


def test_incompatible_column_controlled_fail(tmp_path):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    # 同名列但 NOT NULL（target nullable）→ 受控失败。
    _exec(db, "ALTER TABLE pages ADD COLUMN note_schema_version VARCHAR(64) NOT NULL")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode != 0, "不等价列应受控失败"
    assert "migration_compat_nullable_mismatch" in proc.stderr or \
           "migration_compat_type_mismatch" in proc.stderr
    assert _version(db) == _P38, "失败后版本不得前进"


def test_column_preexisting_index_and_fk_backfilled(tmp_path):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    # 模拟：workspace_id 已物理存在（无 FK/索引）→ P41 补齐索引与 FK。
    _exec(db, "ALTER TABLE wiki_pages ADD COLUMN workspace_id VARCHAR(36)")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode == 0, proc.stderr
    assert _version(db) == current_alembic_head()
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        idx = {r[1] for r in conn.execute("PRAGMA index_list(wiki_pages)")}
        assert "ix_wiki_pages_workspace_id" in idx
        fks = conn.execute(
            "SELECT 1 FROM pragma_foreign_key_list('wiki_pages') WHERE \"from\"='workspace_id'"
        ).fetchone()
        assert fks is not None, "FK 必须补齐"
    finally:
        conn.close()


def test_constraint_violating_data_fails_without_cleanup(tmp_path):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    # 模拟：skill_confidence 提前存在且某行取值越界（1.5 > 1.0）→ P43 加 CHECK 失败、数据不被清理。
    _exec(db, "ALTER TABLE wiki_pages ADD COLUMN skill_confidence FLOAT")
    _exec(db, "INSERT INTO wiki_pages (id, title, skill_confidence) VALUES ('violate-1', 't', 1.5)")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode != 0, "违反新 CHECK 的既有数据必须阻止迁移"
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE id='violate-1' AND skill_confidence=1.5"
        ).fetchone()[0] == 1, "数据不得被自动清理"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 等价判断纯函数反例（索引 / FK / server_default / CHECK）
# ---------------------------------------------------------------------------


def test_index_equivalence_rejects_wrong_columns_unique_predicate():
    import migration_compat as mc
    target_cols = ["workspace_id"]
    target_where = None
    # 列集错误
    assert not mc._indexes_equivalent(
        {"columns": ["id"], "unique": False, "where": None},
        target_cols, False, target_where)
    # unique 错误
    assert not mc._indexes_equivalent(
        {"columns": ["workspace_id"], "unique": True, "where": None},
        target_cols, False, target_where)
    # 谓词错误：既有有谓词、目标无谓词
    assert not mc._indexes_equivalent(
        {"columns": ["workspace_id"], "unique": False,
         "where": "workspace_id IS NOT NULL"},
        target_cols, False, target_where)
    # 谓词错误：文字不同（保留字面量）
    assert not mc._indexes_equivalent(
        {"columns": ["workspace_id"], "unique": False,
         "where": "dirty = 'x'"},
        ["workspace_id"], False, "dirty = 'X'")
    # 正确等价 → 可复用
    assert mc._indexes_equivalent(
        {"columns": ["workspace_id"], "unique": False, "where": None},
        target_cols, False, None)


def test_fk_relation_status_rejects_ondelete_diff_and_reuse_equal():
    import migration_compat as mc
    base = {
        "constrained_columns": ["workspace_id"],
        "referred_table": "wiki_workspaces",
        "referred_columns": ["id"],
        "options": {"ondelete": "SET NULL"},
    }
    assert mc._fk_relation_status(
        base, ["workspace_id"], "wiki_workspaces", ["id"], "SET NULL") == "equal"
    assert mc._fk_relation_status(
        dict(base, options={"ondelete": "CASCADE"}),
        ["workspace_id"], "wiki_workspaces", ["id"], "SET NULL") == "ondeletes_diff"
    assert mc._fk_relation_status(
        dict(base, constrained_columns=["other_id"]),
        ["workspace_id"], "wiki_workspaces", ["id"], "SET NULL") == "none"


def test_server_default_equivalence_rejects_diffs_and_maps_boolean():
    import sqlalchemy as sa
    import migration_compat as mc
    # 无默认 == 无默认
    assert mc._server_default_equal({"default": None},
                                    sa.Column("c", sa.String(10), nullable=True))
    # 一端有默认另一端无 → 不等价
    assert not mc._server_default_equal({"default": "0"},
                                        sa.Column("c", sa.String(10), nullable=True))
    assert not mc._server_default_equal({"default": None},
                                        sa.Column("c", sa.String(10),
                                                  server_default=sa.text("'x'")))
    # 布尔 0/false 同义
    bool_col = sa.Column("skill_locked", sa.Boolean(), nullable=True,
                         server_default=sa.sql.expression.false())
    assert mc._server_default_equal({"default": "0"}, bool_col)
    assert not mc._server_default_equal({"default": "1"}, bool_col)
    # 字符串默认：大小写/空白语义保留
    str_col = sa.Column("k", sa.String(64), server_default=sa.text("'abc'"))
    assert mc._server_default_equal({"default": "'abc'"}, str_col)
    assert not mc._server_default_equal({"default": "'ABC'"}, str_col)


def test_check_extraction_handles_nested_parentheses_and_in():
    import migration_compat as mc
    p43 = ("CREATE TABLE t (id INTEGER, "
           "skill_confidence FLOAT, "
           "CONSTRAINT ck_skill "
           "CHECK (skill_confidence IS NULL OR "
           "(skill_confidence >= 0.0 AND skill_confidence <= 1.0)))")
    checks = mc.extract_check_constraints(p43)
    assert checks and checks[0][0] == "ck_skill"
    assert "skill_confidence >= 0.0" in checks[0][1]
    assert checks[0][1].endswith("skill_confidence <= 1.0)")  # 内层括号完整
    p44 = ("CREATE TABLE s (id INTEGER, validation_status VARCHAR(32), "
           "CHECK (validation_status IS NULL OR "
           "validation_status IN ('pass','fail')))")
    ch44 = mc.extract_check_constraints(p44)
    assert ch44 and ch44[0][0] is None and "IN ('pass','fail')" in ch44[0][1]
    # 同条件（换行/大小写）归一相等
    assert (mc._cond_norm("validation_status IS NULL OR validation_status IN ('pass','fail')")
            == mc._cond_norm("validation_status IS NULL OR\n validation_status in ('pass','fail')"))
    # 字符串常量大小写不同 → 不等价
    assert (mc._cond_norm("validation_status IN ('pass','fail')")
            != mc._cond_norm("validation_status IN ('pass','Fail')"))


# ---------------------------------------------------------------------------
# 迁移路径级：P41 索引错误定义被拒 / 正确索引复用 / 失败不改业务值
# ---------------------------------------------------------------------------


def _wiki_ahead_index_case(tmp_path, index_sql: str):
    db = _fresh(tmp_path)
    _upgrade_to(db, _P38)
    _exec(db, "ALTER TABLE wiki_pages ADD COLUMN workspace_id VARCHAR(36)")
    _exec(db, "INSERT INTO wiki_pages (id, title) VALUES ('keep-row', 't')")
    _exec(db, index_sql)
    return db


def test_p41_wrong_unique_index_rejected_row_kept(tmp_path):
    db = _wiki_ahead_index_case(
        tmp_path,
        "CREATE UNIQUE INDEX ix_wiki_pages_workspace_id "
        "ON wiki_pages (workspace_id)")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode != 0
    assert "migration_compat_index_conflict" in proc.stderr
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM wiki_pages WHERE id='keep-row'"
                            ).fetchone()[0] == 1, "失败不得修改业务值"
    finally:
        conn.close()


def test_p41_wrong_predicate_index_rejected(tmp_path):
    db = _wiki_ahead_index_case(
        tmp_path,
        "CREATE INDEX ix_wiki_pages_workspace_id ON wiki_pages (workspace_id) "
        "WHERE workspace_id IS NOT NULL")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode != 0
    assert "migration_compat_index_conflict" in proc.stderr


def test_p41_wrong_columns_index_rejected(tmp_path):
    db = _wiki_ahead_index_case(
        tmp_path,
        "CREATE INDEX ix_wiki_pages_workspace_id ON wiki_pages (id)")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode != 0
    assert "migration_compat_index_conflict" in proc.stderr


def test_p41_correct_index_reused_not_duplicated(tmp_path):
    db = _wiki_ahead_index_case(
        tmp_path,
        "CREATE INDEX ix_wiki_pages_workspace_id ON wiki_pages (workspace_id)")
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode == 0, proc.stderr
    assert _version(db) == current_alembic_head()
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='ix_wiki_pages_workspace_id'").fetchone()[0]
        assert cnt == 1, "等价索引必须复用而不是重复创建"
        assert conn.execute(
            "SELECT 1 FROM pragma_foreign_key_list('wiki_pages') "
            "WHERE \"from\"='workspace_id'").fetchone() is not None
    finally:
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
