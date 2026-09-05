"""Alembic 迁移（migrate）。

- upgrade(db_path, target) / downgrade(db_path, rev)：
  在 backend/ 下以子进程运行 `python -m alembic -x database_url=<url> <cmd> <arg>`，
  env 显式携带 DATABASE_URL。任何写库路径先过 guard.write_guard；
- current_version(db_path)：只读读 alembic_version 表（无该表/空 → None）；
- preflight(db_path)：升级前只读体检（quick_check / foreign_key_check / revision）。

真实库 head 的“预期”常量集中在此，inventory/README 引用同一来源。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from phase9b_migration import guard

# 唯一 head（backend/alembic/versions/a9b8c7d6e5f4_p44_...py）。若仓库 head 变更须同步更新。
ALEMBIC_HEAD_EXPECTED = "a9b8c7d6e5f4"

BACKEND_DIR = Path(__file__).resolve().parent.parent


class AlembicError(RuntimeError):
    """alembic 子进程失败。"""


def _sqlite_url(db_path) -> str:
    """文件型 SQLite 的 SQLAlchemy URL（绝对路径）。"""
    return "sqlite:///" + Path(os.path.abspath(str(db_path))).as_posix()


def _run_alembic(
    db_path,
    argv_tail: list[str],
    *,
    allowed_dir=None,
    real_db=None,
    log_path=None,
    cmd_name: str,
) -> dict:
    """统一执行 alembic 子进程；非 0 退出码抛 AlembicError 并写日志。"""
    db_path = guard.write_guard(db_path, allowed_dir, real_db)
    url = _sqlite_url(db_path)
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        sys.executable, "-m", "alembic",
        "-x", f"database_url={url}",
        cmd_name, *argv_tail,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    result = {"cmd_name": cmd_name, "returncode": proc.returncode, "log_tail": combined[-4000:]}
    if log_path:
        log_path = os.path.abspath(str(log_path))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"\n===== alembic {cmd_name} {' '.join(argv_tail)} "
                    f"rc={proc.returncode} =====\n")
            f.write(combined)
        result["log_path"] = log_path
    if proc.returncode != 0:
        raise AlembicError(
            f"alembic {cmd_name} {' '.join(argv_tail)} 失败 rc={proc.returncode}\n"
            f"{combined[-2000:]}"
        )
    return result


def current_version(db_path) -> str | None:
    """只读返回当前 alembic 版本号；无 alembic_version 表/空表 → None。

    读失败（例如损坏库）抛 AlembicError。
    """
    conn = guard.readonly_conn(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None
    except Exception as exc:
        raise AlembicError(f"读取 alembic_version 失败：{exc}") from exc
    finally:
        conn.close()


def upgrade(db_path, target: str = "head", *, allowed_dir=None, real_db=None, log_path=None) -> dict:
    """升级到 target（默认 head）。写库 → 必须提供 allowed_dir。"""
    return _run_alembic(
        db_path, [target],
        allowed_dir=allowed_dir, real_db=real_db, log_path=log_path,
        cmd_name="upgrade",
    )


def downgrade(db_path, rev: str, *, allowed_dir=None, real_db=None, log_path=None) -> dict:
    """降级到 rev。写库 → 必须提供 allowed_dir。"""
    return _run_alembic(
        db_path, [rev],
        allowed_dir=allowed_dir, real_db=real_db, log_path=log_path,
        cmd_name="downgrade",
    )


def preflight(db_path) -> dict:
    """升级前只读体检：quick_check / foreign_key_check / revision。"""
    from phase9b_migration import inventory as _inv
    quick = _inv.run_quick_check(db_path)
    fk = _inv.run_fk_check(db_path)
    current = current_version(db_path)
    passed = quick["passed"] and fk["passed"]
    return {
        "db_path": os.path.abspath(str(db_path)),
        "quick_check": quick,
        "foreign_key_check": fk,
        "current_revision": current if current is not None else "none",
        "expected_head": ALEMBIC_HEAD_EXPECTED,
        "head_matches": current == ALEMBIC_HEAD_EXPECTED,
        "passed": passed,
    }
