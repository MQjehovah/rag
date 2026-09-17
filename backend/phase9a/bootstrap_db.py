# -*- coding: utf-8 -*-
"""Phase 9A bootstrap_db：隔离 DB 真实 alembic upgrade head（CONTRACT §1）。

用法（主 Agent orchestrator 调用）：
    cd backend
    python phase9a/bootstrap_db.py --db <绝对 sqlite 路径>

- 启动前断言 resolve 后路径严格位于 <repo>\\.phase9a\\<session>\\db 下，
  否则非零退出并拒启动；
- 以子进程在 backend/ 下运行 alembic upgrade head（env DATABASE_URL 注入），
  并在执行前打印 alembic 单 head 检查结果；
- 成功后打印 "ALEMBIC_OK <db>"。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# 保证无论从仓库根还是 backend/ 启动都能 import phase9a/app。
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from phase9b_migration.migrate import ALEMBIC_HEAD_EXPECTED


def _repo_root() -> Path:
    return _BACKEND.parent


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def validate_db_path(db_path: Path) -> str:
    """校验 resolve 后的路径位于 <repo>\\.phase9a\\<session>\\db 下。

    返回错误说明（None=通过）。绝不指向 backend/data/notes.db 或任何真实库。
    """
    if not db_path.is_absolute():
        db_path = db_path.resolve()
    try:
        resolved = db_path.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - 极难触发
        return f"cannot resolve db path: {exc}"
    session_root = _repo_root() / ".phase9a"
    # 需要至少一层 session 目录：<repo>\.phase9a\<session>\db\xxx.db
    parts = resolved.parts
    try:
        idx = parts.index(".phase9a")
    except ValueError:
        return "DB 路径必须位于 <repo>\\.phase9a\\<session>\\db 下"
    if idx + 3 >= len(parts):
        return "DB 路径必须位于 <repo>\\.phase9a\\<session>\\db 下"
    if parts[idx + 1] == "db":
        return "DB 路径缺少 session 目录层：<repo>\\.phase9a\\<session>\\db 下"
    allowed_parent = session_root / parts[idx + 1] / "db"
    if not resolved.is_relative_to(allowed_parent.resolve(strict=False)):
        return f"DB 路径越界：{resolved} 不在 {allowed_parent} 下"
    return ""


def alembic_single_head(cwd: Path, env: dict) -> str:
    """子进程读取 alembic head（真实仓库链），返回 head 列表字符串。"""
    script = (
        "import sys; sys.path.insert(0, __import__('pathlib').Path.cwd().as_posix());\n"
        "from alembic.config import Config\n"
        "from alembic.script import ScriptDirectory\n"
        "cfg = Config('alembic.ini')\n"
        "cfg.set_main_option('script_location', 'alembic')\n"
        "heads = ScriptDirectory.from_config(cfg).get_heads()\n"
        "print('|'.join(sorted(heads)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic heads check failed: {proc.stderr}")
    return (proc.stdout or "").strip()


def alembic_upgrade_head(cwd: Path, env: dict) -> None:
    """子进程 cwd=backend 执行 alembic upgrade head（env DATABASE_URL 注入）。"""
    script = (
        "import sys, os; sys.path.insert(0, __import__('pathlib').Path.cwd().as_posix());\n"
        "from alembic.config import Config\n"
        "from alembic import command\n"
        "cfg = Config('alembic.ini')\n"
        "cfg.set_main_option('script_location', 'alembic')\n"
        "command.upgrade(cfg, 'head')\n"
        "print('UPGRADE_DONE')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed: {proc.stderr}")
    if "UPGRADE_DONE" not in (proc.stdout or ""):
        raise RuntimeError(f"alembic upgrade head unexpected output: {proc.stdout}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9A alembic bootstrap")
    parser.add_argument("--db", required=True, help="绝对 sqlite 路径")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    problem = validate_db_path(db_path)
    if problem:
        print(f"[phase9a] DB 路径守卫失败: {problem}", file=sys.stderr)
        return 2
    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = _sqlite_url(db_path)
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PHASE9A_BE_PORT", None)

    # 打印 alembic 单 head 检查；预期 head 与 Phase9B 运维常量对齐（显式固定值）。
    try:
        heads = alembic_single_head(_BACKEND, env)
    except RuntimeError as exc:
        print(f"[phase9a] alembic head 检查失败: {exc}", file=sys.stderr)
        return 3
    print(f"[phase9a] alembic heads = {heads}")
    head_list = [h for h in heads.split("|") if h]
    if len(head_list) != 1:
        print(f"[phase9a] alembic 非单 head: {heads}", file=sys.stderr)
        return 4
    if head_list[0] != ALEMBIC_HEAD_EXPECTED:
        print(
            f"[phase9a] alembic head 与运维预期不一致: {head_list[0]}",
            file=sys.stderr)
        return 5

    try:
        alembic_upgrade_head(_BACKEND, env)
    except RuntimeError as exc:
        print(f"[phase9a] alembic upgrade 失败: {exc}", file=sys.stderr)
        return 6

    print(f"ALEMBIC_OK {db_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
