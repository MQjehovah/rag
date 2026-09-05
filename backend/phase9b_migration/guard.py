"""写保护与真实库识别（guard）。

核心约束：本仓库真实库为 backend/data/notes.db，工具包任何环节都不允许对它做
写操作（连只读都应尽量避免，读只发生在 operator 显式快照那一刻）。

- real_notes_db()：定位并校验真实库路径（存在/普通文件/非 reparse-symlink/非目录）。
- is_real_db()：win32 大小写不敏感比较；支持注入 real_db 以便测试用假 real。
- write_guard()：一切写库（升级/降级/回填/恢复目标等）的前置闸门——
  target 是真实库或不在 allowed_dir 内一律抛 WriteGuardError。
- readonly_conn()：标准库 sqlite3 以 `file:...?mode=ro` URI 打开（真只读）。
- sha256_file()：流式文件哈希。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

# Windows reparse point（目录 junction / symlink）属性位。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class WriteGuardError(RuntimeError):
    """写保护拒绝：目标为真实库或越出 allowed_dir。"""


def _backend_dir() -> Path:
    """backend/ 目录（本文件位于 backend/phase9b_migration/guard.py）。"""
    return Path(__file__).resolve().parent.parent


def _is_reparse_point(path: str) -> bool:
    """是否 symlink / Windows reparse point（含 junction）。"""
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def validate_db_file(path) -> str:
    """校验 path 是可用于打开的普通 DB 文件，返回绝对路径。

    - 不存在 / 是目录 / 是 symlink/reparse → 抛 WriteGuardError。
    """
    p = os.path.abspath(str(path))
    if not os.path.exists(p):
        raise WriteGuardError(f"数据库文件不存在：{p}")
    if os.path.isdir(p):
        raise WriteGuardError(f"目标是目录而非数据库文件：{p}")
    if _is_reparse_point(p):
        raise WriteGuardError(f"目标是 symlink/reparse 点，拒绝：{p}")
    if not os.path.isfile(p):
        raise WriteGuardError(f"目标不是普通文件：{p}")
    return p


def real_notes_db() -> str:
    """返回真实库 backend/data/notes.db 的绝对路径并做存在性/普通文件校验。"""
    return validate_db_file(_backend_dir() / "data" / "notes.db")


def is_real_db(path, real_db=None) -> bool:
    """path 是否等于真实库路径。

    win32 下做大小写不敏感比较（os.path.normcase）；real_db 可注入用于测试。
    """
    target = os.path.abspath(str(path))
    real = os.path.abspath(str(real_db if real_db is not None else real_notes_db()))
    if os.name == "nt":
        return os.path.normcase(target) == os.path.normcase(real)
    return target == real


def is_within_dir(target: str, base_dir: str) -> bool:
    """target 绝对路径是否位于 base_dir 内（win32 大小写不敏感，不同盘符 False）。"""
    t = os.path.normcase(os.path.abspath(str(target)))
    b = os.path.normcase(os.path.abspath(str(base_dir)))
    try:
        return os.path.commonpath([t, b]) == b
    except ValueError:
        # 不同盘符（Windows）无法求 commonpath → 必然不在内。
        return False


def require_allowed_dir(allowed_dir) -> str:
    """allowed_dir 必须是非空且已存在的目录，否则抛 WriteGuardError。"""
    if not allowed_dir:
        raise WriteGuardError("写操作必须提供 --allowed-dir（安全工作目录）")
    p = os.path.abspath(str(allowed_dir))
    if not os.path.isdir(p):
        raise WriteGuardError(f"allowed_dir 不是已存在的目录：{p}")
    return p


def write_guard(target, allowed_dir, real_db=None) -> str:
    """写目标闸门：target 为真实库或不在 allowed_dir 内 → 抛 WriteGuardError。

    返回规范化后的绝对路径。real_db 缺省时取 real_notes_db()（存在性校验兜底）。
    """
    allowed = require_allowed_dir(allowed_dir)
    target = os.path.abspath(str(target))
    if is_real_db(target, real_db):
        raise WriteGuardError(f"目标即真实库，禁止写操作：{target}")
    if not is_within_dir(target, allowed):
        raise WriteGuardError(
            f"写目标不在 allowed_dir 内，拒绝：target={target} allowed_dir={allowed}"
        )
    return target


def readonly_conn(path) -> sqlite3.Connection:
    """以只读模式打开 sqlite 文件（`file:<abs>/...?mode=ro`），调用方负责 close。

    仅允许打开普通文件（validate_db_file 兜底），杜绝误写。
    """
    p = validate_db_file(path)
    uri = "file:" + Path(p).as_posix() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def sha256_file(path, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 SHA-256（16 进制）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
