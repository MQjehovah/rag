"""会话目录创建与快照拷贝（snapshot）。

目录布局（create_session 建在 <root>/<ts> 下）：
- source-snapshot/  真实库一致快照（sqlite3 backup API，只读源 → 单文件）
- working/          升级/回填的工作副本（baseline 的字节拷贝）
- restored/         恢复演练产物（restored.db / downgrade.db）
- reports/          markdown/文本报告
- logs/             alembic 等命令日志

所有「写 DB 文件」的落盘目标都必须通过 guard.write_guard 落到 allowed_dir 内。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from phase9b_migration import guard

# 默认会话根目录：backend/.phase9b_migration（测试可通过 root 参数指向临时目录）。
SESSION_ROOT = Path(__file__).resolve().parent.parent / ".phase9b_migration"


def create_session(ts: str | None = None, root=None) -> dict:
    """在 root（默认 backend/.phase9b_migration）下创建 <ts>/ 会话目录。

    返回目录路径 dict：root/ts/source-snapshot/working/restored/reports/logs。
    """
    root_path = Path(root).resolve() if root else SESSION_ROOT
    ts = ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root_path / ts
    subdirs = {
        "source_snapshot": "source-snapshot",
        "working": "working",
        "restored": "restored",
        "reports": "reports",
        "logs": "logs",
    }
    paths = {name: base / rel for name, rel in subdirs.items()}
    base.mkdir(parents=True, exist_ok=True)
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return {
        "root": str(base),
        "ts": ts,
        **{name: str(p) for name, p in paths.items()},
    }


def snapshot_baseline(source_path, dest, allowed_dir=None, real_db=None) -> dict:
    """从只读源做 sqlite3 backup API 一致快照 → dest（必须位于 allowed_dir 内）。

    - 源只通过 guard.readonly_conn 打开（mode=ro，绝不写源）；
    - backup API 对 WAL/有未 checkpoint 内容的数据也返回一致视图；
    - 返回 {source, dest, sha256}。
    """
    guard.write_guard(dest, allowed_dir, real_db)
    dest = os.path.abspath(str(dest))
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    src = guard.readonly_conn(source_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return {
        "source": os.path.abspath(str(source_path)),
        "dest": dest,
        "sha256": guard.sha256_file(dest),
    }


def make_working_copy(baseline, working_path, allowed_dir=None, real_db=None) -> dict:
    """把 baseline 字节拷贝为工作副本，并校验源/副本 SHA-256 完全一致。"""
    guard.write_guard(working_path, allowed_dir, real_db)
    working_path = os.path.abspath(str(working_path))
    baseline = os.path.abspath(str(baseline))
    Path(working_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline, working_path)
    sha_src = guard.sha256_file(baseline)
    sha_dst = guard.sha256_file(working_path)
    if sha_src != sha_dst:
        raise RuntimeError(
            f"working copy SHA 校验不一致：{baseline} {sha_src} vs {working_path} {sha_dst}"
        )
    return {"source": baseline, "dest": working_path, "sha256": sha_dst}
