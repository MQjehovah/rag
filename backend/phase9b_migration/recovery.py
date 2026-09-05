"""恢复演练（recovery）：降级演练 + 恢复校验。

- copy_db()：受 guard 保护的字节拷贝（新目标必须在 allowed_dir 内）；
- downgrade_drill()：把已迁移的工作副本再拷一份 downgrade.db → alembic 降级到
  给定 rev → quick/fk/版本 → 记录“数据丢失属于预期还是缺陷”（迁移新增的表/
  核心表行数是否减少）→ 再 upgrade head → quick/fk/版本/行数回到降级前；
- restore_verify()：从 baseline 复制出 restored/restored.db，要求与 baseline
  SHA-256 完全一致，且 quick/版本/核心行数一致。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from phase9b_migration import guard
from phase9b_migration import inventory as _inv
from phase9b_migration import migrate

# 降级演练中的“核心数据表”：这类表行数若减少视为缺陷（迁移新增的托管表不在此列）。
CORE_KEEP_TABLES = {
    "notebooks", "notebook_groups", "pages", "page_chunks",
    "wiki_pages", "wiki_revisions", "wiki_sections",
    "wiki_workspaces", "notebook_workspace_bindings",
    "evidence_items", "users", "user_groups",
}


def copy_db(src, dst, *, allowed_dir=None, real_db=None) -> dict:
    """guard 保护的目标拷贝（源只读打开校验，目标必须位于 allowed_dir 内）。"""
    guard.write_guard(dst, allowed_dir, real_db)
    src = guard.validate_db_file(src)
    dst = os.path.abspath(str(dst))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    sha = guard.sha256_file(dst)
    return {"source": src, "dest": dst, "sha256": sha}


def _table_delta(before: dict, after: dict) -> dict:
    """before → after 的行数变化（只列减少/缺失项）。"""
    delta = {}
    for table, base in before.items():
        cur = after.get(table)
        if base is None:
            continue
        if cur is None:
            delta[table] = {"before": base, "after": "table_removed"}
        elif cur < base:
            delta[table] = {"before": base, "after": cur}
    return delta


def downgrade_drill(
    migrated_db,
    rev: str,
    *,
    allowed_dir=None,
    real_db=None,
    dest=None,
    log_path=None,
) -> dict:
    """降级 → 评估数据丢失 → 重新升回 head 的完整演练（全程在副本上进行）。"""
    migrated_db = os.path.abspath(str(migrated_db))
    dest = dest or _default_drill_dest(allowed_dir, "downgrade.db")
    out: dict = {"migrated_db": migrated_db, "rev": rev, "dest": dest}

    copy = copy_db(migrated_db, dest, allowed_dir=allowed_dir, real_db=real_db)
    out["copy"] = copy
    before = _inv.read_only_counts(dest)
    out["version_before_downgrade"] = migrate.current_version(dest)

    # 降级
    downgraded = migrate.downgrade(dest, rev, allowed_dir=allowed_dir, real_db=real_db, log_path=log_path)
    out["downgrade"] = downgraded
    after_dn = _inv.read_only_counts(dest)
    out["current_after_downgrade"] = migrate.current_version(dest)

    # 数据丢失评估：核心表行数减少/被删 → 缺陷；纯迁移新增表消失 → 预期。
    delta = _table_delta(before, after_dn)
    defects = {t: v for t, v in delta.items() if t in CORE_KEEP_TABLES}
    expected = {t: v for t, v in delta.items() if t not in CORE_KEEP_TABLES}
    quick_dn = _inv.run_quick_check(dest)
    fk_dn = _inv.run_fk_check(dest)
    out["loss_eval"] = {
        "defects": defects,
        "expected_removals": expected,
        "passed": not defects,
        "note": "核心表行数减少=缺陷；迁移托管新表消失=预期；降级后通过 quick/fk",
    }
    out["after_downgrade"] = {
        "quick_check": quick_dn, "foreign_key_check": fk_dn,
    }

    # 重新升回 head
    upgraded = migrate.upgrade(dest, "head", allowed_dir=allowed_dir, real_db=real_db, log_path=log_path)
    out["re_upgrade"] = upgraded
    after_up = _inv.read_only_counts(dest)
    quick_up = _inv.run_quick_check(dest)
    fk_up = _inv.run_fk_check(dest)
    roundtrip = _table_delta(before, after_up)
    version_now = migrate.current_version(dest)
    out["roundtrip"] = {
        "delta": roundtrip,
        "passed": not roundtrip and version_now == migrate.ALEMBIC_HEAD_EXPECTED
        and quick_up["passed"] and fk_up["passed"],
    }
    out["final"] = {
        "current_version": version_now,
        "expected_head": migrate.ALEMBIC_HEAD_EXPECTED,
        "quick_check": quick_up, "foreign_key_check": fk_up,
    }
    out["passed"] = out["loss_eval"]["passed"] and out["roundtrip"]["passed"]
    return out


def restore_verify(
    baseline_db,
    restored=None,
    *,
    allowed_dir=None,
    real_db=None,
) -> dict:
    """从 baseline 恢复出 restored 副本并逐字节校验（sha 完全一致 + quick/版本/计数）。"""
    baseline_db = os.path.abspath(str(baseline_db))
    restored = restored or _default_drill_dest(allowed_dir, "restored.db")
    out: dict = {"baseline_db": baseline_db, "restored": restored}
    out["copy"] = copy_db(baseline_db, restored, allowed_dir=allowed_dir, real_db=real_db)

    sha_src = guard.sha256_file(baseline_db)
    sha_dst = guard.sha256_file(restored)
    quick = _inv.run_quick_check(restored)
    v_src = migrate.current_version(baseline_db)
    v_dst = migrate.current_version(restored)
    counts_src = _inv.read_only_counts(baseline_db)
    counts_dst = _inv.read_only_counts(restored)
    counts_match = {
        t: v for t, v in counts_src.items()
        if counts_dst.get(t) != v
    }
    out["sha"] = {"baseline": sha_src, "restored": sha_dst, "identical": sha_src == sha_dst}
    out["quick_check"] = quick
    out["versions"] = {"baseline": v_src, "restored": v_dst}
    out["count_mismatches"] = counts_match
    out["passed"] = (
        sha_src == sha_dst and quick["passed"]
        and v_src == v_dst and not counts_match
    )
    return out


def _default_drill_dest(allowed_dir, filename: str) -> str:
    """演练默认落点：allowed_dir 下（真实使用场景由 main 传 session 目录）。"""
    base = guard.require_allowed_dir(allowed_dir)
    return os.path.join(base, filename)
