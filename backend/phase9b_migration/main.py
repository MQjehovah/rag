"""Phase 9B 迁移工具 CLI（main）。

子命令：
  inventory / snapshot / preflight / upgrade / downgrade / backfill-dryrun /
  backfill-apply / postflight / downgrade-drill / restore-verify

安全约定：
- 写库类命令（snapshot 落盘、upgrade/downgrade、backfill-apply、postflight、
  downgrade-drill、restore-verify、backfill-dryrun 的只读计划也拒绝真库）一律要求
  --allowed-dir，且模块层 guard.write_guard 兜底；
- 显式传入真实库（默认 backend/data/notes.db，可用 --real-db 注入测试假 real）
  或路径越出 allowed_dir → 非 0 退出（这就是主 Agent 的“防误写测试”出口）；
- 只读命令 inventory / preflight 允许针对真实库（operator 手动只读盘点）。

注意：本模块只做编排与打印，真实动作全部委托给同包各模块；不 import
app.main / worker / startup。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from phase9b_migration import backfill, guard, migrate, postflight, recovery, snapshot
from phase9b_migration.inventory import inventory

# Windows 控制台默认 GBK：强制 stdout/stderr 为 UTF-8 + replace，避免打印含
# U+FFFD/中文的 alembic 合并输出时抛 UnicodeEncodeError（不影响文件写入）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 非交互/无 reconfigure 环境忽略
        pass

# 写库/回填类命令：传入真实库一律拒绝（防误写）。
_WRITE_LIKE_COMMANDS = {
    "snapshot", "upgrade", "downgrade", "backfill-dryrun", "backfill-apply",
    "postflight", "downgrade-drill", "restore-verify",
}
# 需要 --allowed-dir 的命令。
_ALLOWED_DIR_COMMANDS = {
    "snapshot", "upgrade", "downgrade", "backfill-apply",
    "postflight", "downgrade-drill", "restore-verify",
}
# downgrade-drill 默认目标 rev：当前 head 4f83c9e2a1d7（P53）的直接父版本 P52。
DEFAULT_DRILL_REV = "4f83c9e2a1d7"


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phase9b_migration.main",
        description="真实库副本迁移 / Workspace 回填 / 恢复演练工具（写库受 guard 保护）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p, with_allowed=True):
        p.add_argument("--db", required=True, help="数据库文件路径")
        p.add_argument("--real-db", default=None,
                       help="真实库路径（默认 backend/data/notes.db；测试注入假 real 用）")
        p.add_argument("--session-root", default=None,
                       help="会话根目录（默认取 --allowed-dir）")
        if with_allowed:
            p.add_argument("--allowed-dir", default=None,
                           help="安全写目录：写目标必须位于其内")

    p = sub.add_parser("inventory", help="只读盘点（可针对真实库）")
    _add_common(p, with_allowed=False)
    p.add_argument("--out", default=None, help="markdown 报告路径（可选）")

    p = sub.add_parser("snapshot", help="建会话 + 快照 baseline + working copy")
    _add_common(p)
    p.add_argument("--ts", default=None, help="会话时间戳（默认 UTC now）")

    p = sub.add_parser("preflight", help="升级前只读体检")
    _add_common(p, with_allowed=False)

    p = sub.add_parser("upgrade", help="alembic upgrade")
    _add_common(p)
    p.add_argument("--target", default="head")
    p.add_argument("--log-path", default=None)

    p = sub.add_parser("downgrade", help="alembic downgrade 到指定 rev")
    _add_common(p)
    p.add_argument("--rev", required=True)

    p = sub.add_parser("backfill-dryrun", help="回填 dry-run（只读输出 plan+hash）")
    _add_common(p)

    p = sub.add_parser("backfill-apply", help="回填 apply（单事务）")
    _add_common(p)
    p.add_argument("--plan-hash", default=None, help="dry-run 输出的 plan_hash")

    p = sub.add_parser("postflight", help="回填/迁移后置检查")
    _add_common(p)
    p.add_argument("--baseline-json", default=None,
                   help="baseline 行数 JSON（inventory 输出格式）")

    p = sub.add_parser("downgrade-drill", help="降级 → 评估 → 升回 head 演练")
    _add_common(p)
    p.add_argument("--rev", default=DEFAULT_DRILL_REV)
    p.add_argument("--baseline-db", default=None,
                   help="迁移前 baseline 库（用于区分预存在表 vs 迁移新增表；默认取会话 "
                        "source-snapshot/baseline.db）")

    p = sub.add_parser("restore-verify", help="从 baseline 恢复并校验 sha/quick/版本")
    _add_common(p)
    p.add_argument("--restored", default=None, help="恢复产物路径（默认会话 restored/）")
    return parser


def _real_db_arg(args) -> str | None:
    """解析 --real-db；未给则用默认真实库路径（不存在会抛错，主 Agent 应给正确路径）。"""
    if getattr(args, "real_db", None):
        return os.path.abspath(args.real_db)
    return guard.real_notes_db()


def _refuse_real(db_path, args) -> bool:
    """写库/回填类命令若指向真实库 → 打印拒绝并返回 True。"""
    real = _real_db_arg(args)
    if guard.is_real_db(db_path, real):
        print(
            f"[拒绝] {args.command}：db={db_path} 是真实库，禁止写入/回填。"
            "请先在副本上操作（snapshot → working）。",
            file=sys.stderr,
        )
        return True
    return False


def _session(args) -> dict:
    """会话目录：root = --session-root 或 --allowed-dir。"""
    root = getattr(args, "session_root", None) or getattr(args, "allowed_dir", None)
    return snapshot.create_session(ts=getattr(args, "ts", None), root=root)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str), flush=True)


def _cmd_inventory(args) -> int:
    db = os.path.abspath(args.db)
    out = inventory(db, md_out=args.out)
    if args.out:
        print(f"盘点完成，报告已写入：{out['report_path']}")
    _print({
        "version": out["version"],
        "missing_tables": out["missing_tables"],
        "counts": out["counts"],
        "acl_distribution": out["acl_distribution"],
        "backfill": out["backfill"],
        "same_acl": out["same_acl"],
        "wiki_name_conflicts": out["wiki_name_conflicts"],
        "orphans": out["orphans"],
    })
    return 0


def _cmd_snapshot(args) -> int:
    # snapshot 允许把真实库作为只读源（工具的存在意义）；写目标受 guard 保护。
    sess = _session(args)
    allowed = guard.require_allowed_dir(args.allowed_dir)
    snap = snapshot.snapshot_baseline(args.db, os.path.join(sess["source_snapshot"], "baseline.db"), allowed_dir=allowed, real_db=_real_db_arg(args))
    work = snapshot.make_working_copy(snap["dest"], os.path.join(sess["working"], "work.db"), allowed_dir=allowed, real_db=_real_db_arg(args))
    inv_report = os.path.join(sess["reports"], "baseline-inventory.md")
    inv = inventory(snap["dest"], md_out=inv_report)
    _print({
        "session": sess["root"],
        "baseline": snap,
        "working": work,
        "inventory_report": inv_report,
        "backfill_candidates": inv.get("backfill"),
    })
    return 0


def _cmd_preflight(args) -> int:
    _print(migrate.preflight(os.path.abspath(args.db)))
    return 0


def _cmd_upgrade(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    allowed = guard.require_allowed_dir(args.allowed_dir)
    res = migrate.upgrade(
        args.db, args.target, allowed_dir=allowed, real_db=_real_db_arg(args),
        log_path=args.log_path,
    )
    _print({"upgrade": res, "current": migrate.current_version(args.db)})
    return 0


def _cmd_downgrade(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    allowed = guard.require_allowed_dir(args.allowed_dir)
    res = migrate.downgrade(args.db, args.rev, allowed_dir=allowed, real_db=_real_db_arg(args))
    _print({"downgrade": res, "current": migrate.current_version(args.db)})
    return 0


def _cmd_backfill_dryrun(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    plan = backfill.dry_run(os.path.abspath(args.db))
    return 0


def _cmd_backfill_apply(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    allowed = guard.require_allowed_dir(args.allowed_dir)
    created = backfill.apply(
        args.db, plan_hash=args.plan_hash, allowed_dir=allowed,
        real_db=_real_db_arg(args),
    )
    print(f"backfill-apply 完成，创建 binding 数：{created}", flush=True)
    return 0


def _cmd_postflight(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    baseline = None
    if args.baseline_json:
        with open(args.baseline_json, encoding="utf-8") as f:
            baseline = json.load(f)
    res = postflight.run_postflight(
        os.path.abspath(args.db), baseline_counts=baseline,
        allowed_dir=guard.require_allowed_dir(args.allowed_dir),
        real_db=_real_db_arg(args),
    )
    _print(res)
    return 0 if res.get("passed") else 1


def _cmd_downgrade_drill(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    allowed = guard.require_allowed_dir(args.allowed_dir)
    sess = _session(args)
    baseline_db = args.baseline_db
    if not baseline_db:
        cand = os.path.join(sess["source_snapshot"], "baseline.db")
        if os.path.exists(cand):
            baseline_db = cand
    res = recovery.downgrade_drill(
        os.path.abspath(args.db), args.rev, allowed_dir=allowed,
        real_db=_real_db_arg(args),
        dest=os.path.join(sess["restored"], "downgrade.db"),
        log_path=os.path.join(sess["logs"], "alembic-downgrade-drill.log"),
        baseline_db=baseline_db,
    )
    _print(res)
    return 0 if res.get("passed") else 1


def _cmd_restore_verify(args) -> int:
    if _refuse_real(args.db, args):
        return 1
    allowed = guard.require_allowed_dir(args.allowed_dir)
    sess = _session(args)
    restored = args.restored or os.path.join(sess["restored"], "restored.db")
    res = recovery.restore_verify(
        os.path.abspath(args.db), restored=restored,
        allowed_dir=allowed, real_db=_real_db_arg(args),
    )
    _print(res)
    return 0 if res.get("passed") else 1


_HANDLERS = {
    "inventory": _cmd_inventory,
    "snapshot": _cmd_snapshot,
    "preflight": _cmd_preflight,
    "upgrade": _cmd_upgrade,
    "downgrade": _cmd_downgrade,
    "backfill-dryrun": _cmd_backfill_dryrun,
    "backfill-apply": _cmd_backfill_apply,
    "postflight": _cmd_postflight,
    "downgrade-drill": _cmd_downgrade_drill,
    "restore-verify": _cmd_restore_verify,
}


def main(argv=None) -> int:
    """CLI 入口。返回退出码（不调 sys.exit，便于测试）。"""
    args = build_parser().parse_args(argv)
    # 命令级前置约束。
    allowed_dir = getattr(args, "allowed_dir", None)
    if args.command in _ALLOWED_DIR_COMMANDS and not allowed_dir:
        print(f"[拒绝] {args.command} 需要 --allowed-dir（写保护安全目录）。",
              file=sys.stderr)
        return 2
    if not allowed_dir and getattr(args, "session_root", None):
        print("[拒绝] --session-root 仅随 --allowed-dir 使用。", file=sys.stderr)
        return 2
    try:
        handler = _HANDLERS[args.command]
    except KeyError:
        print(f"[错误] 未知子命令：{args.command}", file=sys.stderr)
        return 2
    try:
        return handler(args)
    except guard.WriteGuardError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 —— CLI 顶层统一收口
        print(f"[错误] {args.command} 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
