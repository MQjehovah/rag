"""SQLite 安全备份与恢复工具（Phase H-1 返工）。

一致性快照：
- 使用 sqlite3.Connection.backup()（SQLite Backup API）创建一致快照，绝不
  顺序复制活跃的 db/wal/shm 冒充一致备份。
- 允许指定独立的绝对备份目标路径（-o / --output）。

manifest 与校验：
- manifest 记录每个文件的大小与 SHA-256；
- restore 前先校验每个文件的大小与 SHA-256，任何不一致立即拒绝恢复。

安全约束：
- 拒绝空路径 / 目录 / 不存在文件作为源；
- 拒绝源与目标相同；
- 恢复拒绝覆盖已存在目标文件；
- 恢复后执行 SQLite integrity_check。

用法：
    python scripts/backup_restore.py backup <db_path> [-o <backup_dir>]
    python scripts/backup_restore.py restore <backup_dir> <new_db_path>
    python scripts/backup_restore.py verify <db_path>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_db_path(path: str) -> Path:
    if not path:
        raise SystemExit("错误：数据库路径不能为空")
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        raise SystemExit(f"错误：{path} 是目录，不是数据库文件")
    if not p.exists():
        raise SystemExit(f"错误：数据库文件不存在：{p}")
    home = Path.home().resolve()
    if p == home:
        raise SystemExit("错误：禁止以用户主目录作为数据库路径")
    return p


def _consistent_snapshot(src: Path, dest: Path) -> None:
    """用 SQLite Backup API 创建一致快照（不顺序复制 db/wal/shm）。

    源连接使用只读 URI（在平台支持范围内），避免误触发源库 checkpoint。
    """
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def backup(db_path: str, output_dir: str | None) -> dict:
    src = _validate_db_path(db_path)
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    if output_dir:
        backup_dir = Path(output_dir).expanduser().resolve()
        # 允许指定独立的绝对备份目标路径；若已存在，追加时间戳子目录。
        backup_dir = backup_dir / f"{src.stem}-{timestamp}"
    else:
        backup_dir = src.parent / "backups" / f"{src.stem}-{timestamp}"

    if backup_dir.exists():
        raise SystemExit(f"错误：备份目录已存在，拒绝覆盖：{backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "source": str(src),
        "backup_dir": str(backup_dir),
        "created_at": timestamp,
        "method": "sqlite_backup_api",
        "files": [],
    }
    try:
        # 用 Backup API 生成单个一致快照文件（含 WAL 已 checkpoint 到主文件）。
        dest = backup_dir / src.name
        _consistent_snapshot(src, dest)
        entry = {
            "file": dest.name,
            "size": dest.stat().st_size,
            "sha256": _sha256(dest),
        }
        manifest["files"].append(entry)
        manifest_path = backup_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"备份完成：{backup_dir}")
        for f in manifest["files"]:
            print(f"  {f['file']}  {f['size']} 字节  sha256={f['sha256'][:16]}…")
        # 机器可读结果：backup_dir / manifest 路径 / 快照 SHA-256，供 runbook 精确加载。
        return {
            "backup_dir": str(backup_dir),
            "manifest": str(manifest_path),
            "snapshot_file": str(dest),
            "snapshot_sha256": entry["sha256"],
        }
    except Exception:
        shutil_rmtree(backup_dir)
        raise


def shutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _validate_manifest(manifest: dict, backup_path: Path) -> list[dict]:
    """严格校验 manifest schema：
    - 只能有一个数据库快照文件；
    - file 必须是纯文件名且解析后位于 backup_dir 内（拒绝 ../、绝对路径）；
    - 无重复 entry、无缺字段、无额外快照。
    返回规范化后的 files 列表。
    """
    if not isinstance(manifest, dict):
        raise SystemExit("错误：manifest 必须是 JSON 对象")
    if "files" not in manifest or not isinstance(manifest["files"], list):
        raise SystemExit("错误：manifest 缺少 files 列表")
    if len(manifest["files"]) != 1:
        raise SystemExit(f"错误：manifest 必须恰好有 1 个数据库快照，实际 {len(manifest['files'])}")

    entry = manifest["files"][0]
    if not isinstance(entry, dict):
        raise SystemExit("错误：manifest files[0] 必须是对象")
    for required in ("file", "size", "sha256"):
        if required not in entry:
            raise SystemExit(f"错误：manifest files[0] 缺少字段 {required}")

    fname = entry["file"]
    if not isinstance(fname, str) or not fname:
        raise SystemExit("错误：manifest file 必须是非空字符串")
    # 拒绝路径穿越/绝对路径：file 必须是纯文件名
    fpath = Path(fname)
    if fpath.is_absolute() or fpath.name != fname or ".." in fname or "/" in fname or "\\" in fname:
        raise SystemExit(f"错误：manifest file 必须是纯文件名，实际 {fname!r}")
    resolved = (backup_path / fname).resolve()
    if resolved.parent != backup_path.resolve():
        raise SystemExit(f"错误：manifest file 解析后不在备份目录内：{fname}")

    if not isinstance(entry["size"], int) or entry["size"] < 0:
        raise SystemExit("错误：manifest size 必须是非负整数")
    if not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64:
        raise SystemExit("错误：manifest sha256 必须是 64 位十六进制字符串")

    return [entry]


def restore(backup_dir: str, new_db_path: str) -> None:
    backup_path = Path(backup_dir).expanduser().resolve()
    if not backup_path.is_dir():
        raise SystemExit(f"错误：备份目录不存在：{backup_dir}")
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"错误：备份目录缺少 manifest.json：{backup_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if not new_db_path:
        raise SystemExit("错误：restore 目标路径不能为空")
    target = Path(new_db_path).expanduser().resolve()
    if target.is_dir():
        raise SystemExit(f"错误：restore 目标路径是目录：{new_db_path}")
    if target.exists():
        raise SystemExit(f"错误：restore 目标路径已存在，拒绝覆盖：{new_db_path}")
    source = Path(manifest.get("source", "")).expanduser().resolve()
    if source == target:
        raise SystemExit("错误：restore 目标不得等于源库")

    # 严格 manifest 校验
    entries = _validate_manifest(manifest, backup_path)

    # 恢复前校验每个文件的大小与 SHA-256；任何不一致立即拒绝。
    for entry in entries:
        f = backup_path / entry["file"]
        if not f.exists():
            raise SystemExit(f"错误：备份文件缺失：{f}")
        if f.stat().st_size != entry["size"]:
            raise SystemExit(
                f"错误：备份文件大小不一致（manifest 记录 {entry['size']}，实际 {f.stat().st_size}）：{f}"
            )
        actual_sha = _sha256(f)
        if actual_sha != entry["sha256"]:
            raise SystemExit(
                f"错误：备份文件 SHA-256 不一致（manifest 记录 {entry['sha256'][:16]}…，实际 {actual_sha[:16]}…）：{f}"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    restored = []
    try:
        for entry in entries:
            src_file = backup_path / entry["file"]
            import shutil
            shutil.copy2(src_file, target)
            restored.append(target)
        conn = sqlite3.connect(str(target))
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise SystemExit(f"错误：恢复后 integrity_check 未通过：{ok}")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise SystemExit(f"错误：恢复后 foreign_key_check 未通过：{fk}")
        finally:
            conn.close()
        print(f"恢复完成：{target}")
        print("integrity_check: ok")
        print("foreign_key_check: ok")
    except Exception:
        for dest in restored:
            if dest.exists():
                dest.unlink()
        raise


def verify(db_path: str) -> None:
    p = _validate_db_path(db_path)
    conn = sqlite3.connect(str(p))
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    print(f"数据库：{p}")
    print(f"integrity_check：{ok}")
    print(f"表数量：{len(tables)}")
    print(f"外键违规：{len(fk)}")
    if fk:
        for row in fk:
            print(f"  FK_VIOLATION: {row}")
    if ok != "ok" or fk:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite 安全备份与恢复（Phase H-1）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bp = sub.add_parser("backup", help="用 Backup API 生成一致快照")
    bp.add_argument("db_path")
    bp.add_argument("-o", "--output", help="独立绝对备份目标路径（可选）")
    bp.add_argument("--result-json", help="写入机器可读结果 JSON（含 backup_dir/manifest/snapshot_sha256）")
    rp = sub.add_parser("restore", help="校验后恢复到新路径")
    rp.add_argument("backup_dir")
    rp.add_argument("new_db_path")
    sub.add_parser("verify", help="integrity_check + 外键检查").add_argument("db_path")

    args = parser.parse_args()
    if args.cmd == "backup":
        result = backup(args.db_path, args.output)
        if args.result_json:
            Path(args.result_json).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    elif args.cmd == "restore":
        restore(args.backup_dir, args.new_db_path)
    elif args.cmd == "verify":
        verify(args.db_path)


if __name__ == "__main__":
    main()
