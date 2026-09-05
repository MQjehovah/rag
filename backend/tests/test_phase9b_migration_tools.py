"""Phase 9B 迁移工具箱单元测试（只合成 sqlite，绝不接触真实库 backend/data/notes.db）。

覆盖：guard / inventory / snapshot / backfill / migrate / postflight / recovery /
main 防误写入口。任何指向真实库的路径都以显式 real_db 注入假 real 文件替代。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.core.access_control import workspace_key_for_notebook
from app.models.database import Base

from phase9b_migration import (
    backfill,
    guard,
    inventory,
    main as main_mod,
    migrate,
    postflight,
    recovery,
    snapshot,
)

# ---------------------------------------------------------------------------
# 合成库辅助
# ---------------------------------------------------------------------------

# 固定可读 id（String(36) 主键接受任意字符串）。
NB_COMP = "nb-comp1"   # company（无 group），无 binding → candidate
NB_GRP = "nb-grp1"     # group:sales → candidate
NB_ADM = "nb-adm1"     # __local_admin__ → candidate
NB_MIX = "nb-mix1"     # __public__ + notebook_groups('sales') → blocked_unknown
NB_DIS = "nb-dis1"     # disabled binding → blocked_disabled
NB_ACT = "nb-act1"     # active binding → already active
NB_ARC = "nb-arc1"     # 默认 key workspace 已 archived → blocked_archived
WS_SHARED = "ws-shared"  # 手工共享 workspace（非默认 key）
WS_ARCH = "ws-arch"      # nb-arc1 的归档默认 workspace


def schema_db(path: Path) -> Path:
    """创建与 app.models.database.Base 一致的完整 schema 文件库。"""
    engine = create_engine("sqlite:///" + os.path.abspath(str(path)).replace("\\", "/"))
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def seed_small_scenario(path: Path) -> Path:
    """小场景：7 个 notebook（覆盖 5 种决策）+ 3 page + 3 wiki + 2 evidence。"""
    schema_db(path)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA foreign_keys=OFF")
    con.executemany(
        "INSERT INTO notebooks(id, name, group_id) VALUES (?, ?, ?)",
        [
            (NB_COMP, "comp", None),
            (NB_GRP, "grp", "sales"),
            (NB_ADM, "adm", "__local_admin__"),
            (NB_MIX, "mix", "__public__"),
            (NB_DIS, "dis", None),
            (NB_ACT, "act", None),
            (NB_ARC, "arc", None),
        ],
    )
    con.execute(
        "INSERT INTO notebook_groups(id, notebook_id, group_name) VALUES (?, ?, ?)",
        ("ng1", NB_MIX, "sales"),
    )
    key_arch = workspace_key_for_notebook(NB_ARC)
    con.executemany(
        "INSERT INTO wiki_workspaces(id, key, name, acl_scope, scope_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (WS_SHARED, "manual-shared-key", "共享", '{"groups": ["__public__"]}', "company", "active"),
            (WS_ARCH, key_arch, "归档默认", '{"groups": ["__public__"]}', "company", "archived"),
        ],
    )
    con.executemany(
        "INSERT INTO notebook_workspace_bindings(id, notebook_id, workspace_id, status) "
        "VALUES (?, ?, ?, ?)",
        [
            ("b-dis1", NB_DIS, WS_SHARED, "disabled"),
            ("b-act1", NB_ACT, WS_SHARED, "active"),
        ],
    )
    con.executemany(
        "INSERT INTO pages(id, notebook_id, title, content, index_dirty, wiki_dirty) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("p1", NB_COMP, "t1", "", 1, 1),
            ("p2", NB_COMP, "t2", "", 0, 1),
            ("p3", NB_GRP, "t3", "", None, None),
        ],
    )
    con.executemany(
        "INSERT INTO wiki_pages(id, title, status, workspace_id, acl_scope) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("wp1", "同名主题A", "published", None, '{"groups": ["__public__"]}'),
            ("wp2", "同名主题A", "published", WS_SHARED, '{"groups": ["__public__"]}'),
            ("wp3", "单例", "published", None, '{"groups": ["__public__"]}'),
        ],
    )
    con.executemany(
        "INSERT INTO evidence_items(id, evidence_type, content, status) VALUES (?, ?, ?, ?)",
        [("ev1", "text", "x1", "active"), ("ev2", "text", "x2", "active")],
    )
    con.commit()
    con.close()
    return path


def seed_no_null_wiki(path: Path) -> Path:
    """published wiki 全部有 workspace 的场景（含历史 nullable Section 可读验证）。"""
    schema_db(path)
    con = sqlite3.connect(str(path))
    con.executemany(
        "INSERT INTO notebooks(id, name, group_id) VALUES (?, ?, ?)",
        [("n1", "n1", None)],
    )
    con.executemany(
        "INSERT INTO wiki_workspaces(id, key, name, acl_scope, scope_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [("w1", "manual-w1", "w1", '{"groups": ["__public__"]}', "company", "active")],
    )
    con.executemany(
        "INSERT INTO notebook_workspace_bindings(id, notebook_id, workspace_id, status) "
        "VALUES (?, ?, ?, ?)",
        [("b1", "n1", "w1", "active")],
    )
    con.executemany(
        "INSERT INTO wiki_pages(id, title, status, workspace_id, acl_scope) "
        "VALUES (?, ?, ?, ?, ?)",
        [("wp1", "有归属", "published", "w1", '{"groups": ["__public__"]}')],
    )
    con.executemany(
        "INSERT INTO wiki_revisions(id, wiki_page_id, title, status, edit_type) "
        "VALUES (?, ?, ?, ?, ?)",
        [("rv1", "wp1", "有归属", "published", "auto")],
    )
    # 历史 nullable Section（heading/版本字段保持 NULL）应可读。
    con.executemany(
        "INSERT INTO wiki_sections(id, revision_id, section_type, content, order_index, "
        "heading, section_key, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("s1", "rv1", "evidence", "旧内容", 0, None, None, None)],
    )
    con.commit()
    con.close()
    return path


def tiny_old_schema_db(path: Path) -> Path:
    """旧 schema（缺 group_id / 缺绝大多数表）——盘点不崩、输出 unavailable。"""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE notebooks(id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL)"
    )
    con.execute(
        "CREATE TABLE notebook_groups(notebook_id VARCHAR(36), group_name VARCHAR(255))"
    )
    con.executemany(
        "INSERT INTO notebooks(id, name) VALUES (?, ?)",
        [("a1", "a"), ("a2", "b")],
    )
    con.commit()
    con.close()
    return path


def row_count(path: Path, table: str) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def file_sha(path) -> str:
    return guard.sha256_file(path)


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------

class TestGuard:
    def test_write_guard_rejects_real_db(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"sqlite fake")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        with pytest.raises(guard.WriteGuardError):
            guard.write_guard(str(real), str(allowed), real_db=str(real))

    def test_write_guard_rejects_outside_allowed(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"sqlite fake")
        other = tmp_path / "elsewhere" / "x.db"
        other.parent.mkdir()
        other.write_bytes(b"x")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        with pytest.raises(guard.WriteGuardError):
            guard.write_guard(str(other), str(allowed), real_db=str(real))

    def test_write_guard_accepts_inside_allowed(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"sqlite fake")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "copy.db"
        target.write_bytes(b"x")
        out = guard.write_guard(str(target), str(allowed), real_db=str(real))
        assert out == os.path.abspath(str(target))

    def test_write_guard_requires_allowed_dir(self, tmp_path):
        target = tmp_path / "x.db"
        target.write_bytes(b"x")
        with pytest.raises(guard.WriteGuardError):
            guard.write_guard(str(target), None)

    def test_is_real_db_case_insensitive_win32(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"sqlite fake")
        assert guard.is_real_db(str(real), real_db=str(real))
        if os.name == "nt":
            assert guard.is_real_db(str(real).upper(), real_db=str(real))
            assert guard.is_real_db(str(real), real_db=str(real).upper())

    def test_validate_db_file_rejects_non_regular(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(guard.WriteGuardError):
            guard.validate_db_file(str(d))
        with pytest.raises(guard.WriteGuardError):
            guard.validate_db_file(str(tmp_path / "missing.db"))
        # readonly_conn 同样拒绝非普通文件。
        with pytest.raises(guard.WriteGuardError):
            guard.readonly_conn(str(d))

    def test_sha256_file_matches_hashlib(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"hello \x00 world" * 100)
        import hashlib
        assert guard.sha256_file(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

class TestInventory:
    def test_full_small_scenario_counts_and_candidates(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        inv = inventory.inventory(str(db))

        assert inv["counts"]["notebooks"] == 7
        assert inv["counts"]["pages"] == 3
        assert inv["counts"]["wiki_pages"] == 3
        assert inv["counts"]["workspaces"] == 2
        assert inv["counts"]["bindings"] == 2
        assert inv["counts"]["evidence_items"] == 2
        assert inv["counts"]["wiki_revisions"] == 0

        ad = inv["acl_distribution"]
        assert ad == {
            "company": 4, "admin": 1, "group": 1, "unknown": 1,
            "missing_group": 4, "total": 7,
        }

        bf = inv["backfill"]
        assert bf["candidate"] == 3
        assert bf["already_active"] == 1
        assert bf["blocked_disabled"] == 1
        assert bf["blocked_unknown"] == 1
        assert bf["blocked_archived_default"] == 1

        sa = inv["same_acl"]
        assert sa["distinct_shared_acl_scopes"] == 1  # company
        assert sa["notebooks_in_shared_acl"] == 4

        wc = inv["wiki_name_conflicts"]
        assert wc["conflict_titles"] == 1
        assert wc["conflict_wiki_rows"] == 2

        assert inv["orphans"]["wiki_without_workspace"] == 2
        assert inv["orphans"]["bindings_without_notebook"] == 0

        dist = inv["dirty_distribution"]
        assert dist["pages_index_dirty"] == {"1": 1, "0": 1, "null": 1}
        assert dist["pages_wiki_dirty"] == {"1": 2, "0": 0, "null": 1}

        assert inv["version"]["alembic_head_expected"] == migrate.ALEMBIC_HEAD_EXPECTED

    def test_old_schema_unavailable_no_crash(self, tmp_path):
        db = tiny_old_schema_db(tmp_path / "old.db")
        inv = inventory.inventory(str(db))
        assert inv["tables"]["notebooks"]["rows"] == 2
        assert inv["tables"]["wiki_pages"]["rows"] == "unavailable"
        assert inv["acl_distribution"] == "unavailable"
        assert inv["backfill"]["unavailable"] is True
        assert inv["same_acl"] == "unavailable"
        assert inv["columns"]["wiki_pages.title"] == "unavailable"
        assert inv["dirty_distribution"]["pages_index_dirty"] == "unavailable"
        assert "notebooks.group_id" in inv["columns"]

    def test_inventory_md_report(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        report = tmp_path / "inv.md"
        inv = inventory.inventory(str(db), md_out=str(report))
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert "company" in text
        assert "回填" in text
        assert inv["report_path"] == str(report)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_baseline_with_wal_backup_matches_rows(self, tmp_path):
        p = tmp_path / "wal.db"
        con = sqlite3.connect(str(p))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        con.executemany("INSERT INTO t(v) VALUES (?)", [("a",), ("b",), ("c",)])
        con.commit()
        reader = sqlite3.connect(str(p))  # 保活 WAL，避免写方关闭时 checkpoint 吞掉内容
        con.executemany("INSERT INTO t(v) VALUES (?)", [("d",), ("e",)])
        con.commit()

        sess = snapshot.create_session(ts="T-wal", root=str(tmp_path / "sess"))
        dest = Path(sess["source_snapshot"]) / "baseline.db"
        snap = snapshot.snapshot_baseline(
            str(p), str(dest), allowed_dir=sess["source_snapshot"]
        )
        con.close()
        reader.close()

        assert row_count(Path(snap["dest"]), "t") == 5
        assert guard.sha256_file(snap["dest"]) == snap["sha256"]

    def test_working_copy_sha_consistent(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        sess = snapshot.create_session(ts="T-work", root=str(tmp_path / "sess"))
        baseline = Path(sess["source_snapshot"]) / "baseline.db"
        guard_ = snapshot.snapshot_baseline(
            str(db), str(baseline), allowed_dir=sess["source_snapshot"]
        )
        work = snapshot.make_working_copy(
            str(baseline), str(Path(sess["working"]) / "work.db"),
            allowed_dir=sess["working"],
        )
        assert guard.sha256_file(str(baseline)) == guard.sha256_file(work["dest"])
        assert guard_["sha256"] == work["sha256"]

    def test_snapshot_dest_outside_allowed_rejected(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside.db"
        with pytest.raises(guard.WriteGuardError):
            snapshot.snapshot_baseline(str(db), str(outside), allowed_dir=str(allowed))


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

class TestBackfill:
    def test_plan_dryrun_counts_and_no_change(self, tmp_path, capsys):
        db = seed_small_scenario(tmp_path / "small.db")
        before = file_sha(str(db))
        pl = backfill.plan(str(db))
        assert pl["counts"]["total"] == 7
        assert pl["counts"]["create"] == 3
        assert pl["counts"]["blocked_unknown"] == 1
        assert pl["counts"]["blocked_disabled"] == 1
        assert pl["counts"]["blocked_archived"] == 1
        assert pl["counts"]["active"] == 1
        assert len(pl["plan_hash"]) == 64
        assert file_sha(str(db)) == before  # 只读，文件字节不变

        backfill.dry_run(str(db))
        out = capsys.readouterr().out
        assert pl["plan_hash"] in out
        assert file_sha(str(db)) == before

    def test_apply_then_second_apply_zero(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        pl = backfill.plan(str(db))
        created = backfill.apply(str(db), plan_hash=pl["plan_hash"], allowed_dir=str(tmp_path))
        assert created == 3
        assert row_count(db, "notebook_workspace_bindings") == 2 + 3
        assert row_count(db, "wiki_workspaces") == 2 + 3
        # 幂等：第二次 apply created=0
        pl2 = backfill.plan(str(db))
        assert pl2["counts"]["create"] == 0
        assert backfill.apply(str(db), plan_hash=pl2["plan_hash"], allowed_dir=str(tmp_path)) == 0

    def test_apply_rejects_when_db_changed_after_dryrun(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        pl = backfill.plan(str(db))
        # dry-run 后外部新增候选 notebook → 计划 hash 变化 → 拒绝且不改库
        con = sqlite3.connect(str(db))
        con.execute("INSERT INTO notebooks(id, name, group_id) VALUES (?, ?, ?)",
                    ("nb-ext1", "ext", "sales"))
        con.commit()
        con.close()
        with pytest.raises(backfill.PlanChangedError):
            backfill.apply(str(db), plan_hash=pl["plan_hash"], allowed_dir=str(tmp_path))
        assert row_count(db, "notebook_workspace_bindings") == 2
        assert row_count(db, "wiki_workspaces") == 2

    def test_apply_rolls_back_on_injected_failure(self, tmp_path, monkeypatch):
        db = seed_small_scenario(tmp_path / "small.db")
        pl = backfill.plan(str(db))
        real = backfill.apply_item
        state = {"n": 0}

        def flaky(session, item):
            state["n"] += 1
            if state["n"] > 1:
                raise RuntimeError("注入失败")
            return real(session, item)

        monkeypatch.setattr(backfill, "apply_item", flaky)
        with pytest.raises(backfill.BackfillError):
            backfill.apply(str(db), plan_hash=pl["plan_hash"], allowed_dir=str(tmp_path))
        # 单事务整体回滚：不允许部分写入。
        assert row_count(db, "notebook_workspace_bindings") == 2
        assert row_count(db, "wiki_workspaces") == 2

    def test_apply_requires_allowed_dir(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        with pytest.raises(guard.WriteGuardError):
            backfill.apply(str(db), plan_hash="x", allowed_dir=None)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

class TestMigrate:
    def test_current_version_reads_alembic_version(self, tmp_path):
        p = tmp_path / "v.db"
        con = sqlite3.connect(str(p))
        con.execute("CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL)")
        con.execute("INSERT INTO alembic_version VALUES (?)", (migrate.ALEMBIC_HEAD_EXPECTED,))
        con.commit()
        con.close()
        assert migrate.current_version(str(p)) == migrate.ALEMBIC_HEAD_EXPECTED

    def test_current_version_none_when_no_table(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        assert migrate.current_version(str(db)) is None

    def test_upgrade_refuses_real_db_and_outside_allowed(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        real = tmp_path / "notes.db"
        real.write_bytes(b"fake")
        with pytest.raises(guard.WriteGuardError):
            migrate.upgrade(str(real), "head", allowed_dir=str(tmp_path), real_db=str(real))
        with pytest.raises(guard.WriteGuardError):
            migrate.upgrade(str(db), "head", allowed_dir=str(tmp_path / "no-such-dir"))
        with pytest.raises(guard.WriteGuardError):
            migrate.downgrade(str(db), "base", allowed_dir=str(tmp_path / "no-such-dir"))

    def test_preflight_on_small_db(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        res = migrate.preflight(str(db))
        assert res["quick_check"]["passed"] is True
        assert res["foreign_key_check"]["passed"] is True
        assert res["current_revision"] == "none"
        assert res["expected_head"] == migrate.ALEMBIC_HEAD_EXPECTED
        assert res["passed"] is True


# ---------------------------------------------------------------------------
# postflight
# ---------------------------------------------------------------------------

class TestPostflight:
    def test_postflight_after_apply(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        pl = backfill.plan(str(db))
        backfill.apply(str(db), plan_hash=pl["plan_hash"], allowed_dir=str(tmp_path))
        baseline = inventory.read_only_counts(str(db))
        res = postflight.run_postflight(
            str(db), baseline_counts=baseline, allowed_dir=str(tmp_path)
        )
        assert res["passed"] is True
        checks = res["checks"]
        assert checks["active_binding_unique"]["passed"] is True
        assert checks["active_binding_unique"]["violations"] == []
        blocked = checks["blocked_not_released"]
        assert blocked["blocked_disabled"] == 1   # nb-dis1
        assert blocked["blocked_unknown"] == 1    # nb-mix1
        assert blocked["anomalies"] == []
        assert checks["default_workspace_exclusive"]["anomalies"] == []
        wiki = checks["wiki_workspace_consistent"]
        assert wiki["wikis_bad_workspace_ref"] == 0
        assert wiki["published_wiki_null_workspace"] == 2  # 如实报告 2 行
        assert checks["section_readable"]["passed"] is True
        synth = res["synth_counterexample"]
        assert synth["rolled_back"] is True
        assert synth["counts_after"] == synth["counts_before"]

    def test_postflight_reports_zero_null_honestly_and_section_readable(self, tmp_path):
        db = seed_no_null_wiki(tmp_path / "nonull.db")
        res = postflight.run_postflight(str(db), allowed_dir=str(tmp_path))
        wiki = res["checks"]["wiki_workspace_consistent"]
        assert wiki["published_wiki_null_workspace"] == 0  # 0 行如实说
        assert res["checks"]["section_readable"]["note"].startswith("可读")
        assert res["synth_counterexample"]["rolled_back"] is True

    def test_postflight_detects_count_decrease(self, tmp_path):
        db = seed_small_scenario(tmp_path / "small.db")
        res = postflight.run_postflight(
            str(db), baseline_counts={"notebooks": 999}, allowed_dir=str(tmp_path)
        )
        chk = res["checks"]["counts_vs_baseline"]
        assert chk["passed"] is False
        assert "notebooks" in chk["decreased"]
        assert chk["decreased"]["notebooks"]["baseline"] == 999

    def test_postflight_refuses_real_db(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"fake")
        with pytest.raises(guard.WriteGuardError):
            postflight.run_postflight(str(real), allowed_dir=str(tmp_path), real_db=str(real))


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_copy_db_and_restore_verify(self, tmp_path):
        baseline = seed_small_scenario(tmp_path / "small.db")
        ver = recovery.restore_verify(str(baseline), allowed_dir=str(tmp_path))
        assert ver["passed"] is True
        assert ver["sha"]["identical"] is True
        assert ver["quick_check"]["passed"] is True
        assert ver["count_mismatches"] == {}
        assert ver["versions"]["baseline"] == ver["versions"]["restored"]

    def test_restore_verify_custom_dest(self, tmp_path):
        baseline = seed_small_scenario(tmp_path / "small.db")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        restored = allowed / "my-restored.db"
        ver = recovery.restore_verify(str(baseline), restored=str(restored), allowed_dir=str(allowed))
        assert ver["passed"] is True
        assert restored.exists()

    def test_copy_db_refuses_outside_allowed(self, tmp_path):
        src = seed_small_scenario(tmp_path / "small.db")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        out = tmp_path / "outside.db"
        with pytest.raises(guard.WriteGuardError):
            recovery.copy_db(str(src), str(out), allowed_dir=str(allowed))

    def test_table_delta_pure(self):
        before = {"x": 5, "y": 1, "z": 2}
        after = {"x": 4, "z": 2}
        d = recovery._table_delta(before, after)
        assert d["x"] == {"before": 5, "after": 4}
        assert d["y"] == {"before": 1, "after": "table_removed"}


# ---------------------------------------------------------------------------
# main（防误写出口）
# ---------------------------------------------------------------------------

class TestMain:
    def test_write_command_with_real_db_refused(self, tmp_path, monkeypatch):
        real = tmp_path / "notes.db"
        real.write_bytes(b"fake")
        called = {"upgrade": False}

        def _boom(*a, **k):
            called["upgrade"] = True
            raise AssertionError("不应真正执行 upgrade")

        monkeypatch.setattr(migrate, "upgrade", _boom)
        rc = main_mod.main([
            "upgrade", "--db", str(real), "--target", "head",
            "--allowed-dir", str(tmp_path), "--real-db", str(real),
        ])
        assert rc == 1
        assert called["upgrade"] is False

    def test_write_command_without_allowed_dir_refused(self, tmp_path):
        real = tmp_path / "notes.db"
        real.write_bytes(b"fake")
        rc = main_mod.main([
            "downgrade", "--db", str(real), "--rev", "base", "--real-db", str(real),
        ])
        assert rc == 2

    def test_backfill_dryrun_on_real_db_refused(self, tmp_path, monkeypatch):
        real = tmp_path / "notes.db"
        real.write_bytes(b"fake")
        called = {"dry": False}

        def _boom(*a, **k):
            called["dry"] = True
            raise AssertionError("不应真正执行 dry-run")

        monkeypatch.setattr(backfill, "dry_run", _boom)
        rc = main_mod.main([
            "backfill-dryrun", "--db", str(real), "--real-db", str(real),
        ])
        assert rc == 1
        assert called["dry"] is False
