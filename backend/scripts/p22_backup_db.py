"""P22：删除旧 KO 表前的数据库备份与哈希记录。

SQLite：复制数据库文件并记录 SHA-256。
PostgreSQL：提示使用 pg_dump（本脚本只记录连接目标，不执行远程 dump）。

用法：
    .venv/Scripts/python.exe scripts/p22_backup_db.py
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = BACKEND_ROOT / "reports"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    raw = database_url.split("sqlite:///", 1)[-1]
    if raw.startswith("/") and sys.platform == "win32":
        raw = raw.lstrip("/")
    path = Path(raw)
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    return path


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    url = settings.database_url
    sqlite_path = _sqlite_path(url)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database_url_scheme": urlparse(url).scheme,
        "backup_path": None,
        "sha256": None,
        "size_bytes": None,
        "note": "",
    }
    if sqlite_path is not None:
        if not sqlite_path.exists():
            print(f"SQLite 文件不存在：{sqlite_path}")
            return 1
        dest = REPORTS_DIR / f"p22_backup_{stamp}_{sqlite_path.name}"
        shutil.copy2(sqlite_path, dest)
        manifest["backup_path"] = str(dest)
        manifest["sha256"] = _sha256(dest)
        manifest["size_bytes"] = dest.stat().st_size
        manifest["note"] = "SQLite 文件已复制。删除 knowledge_objects 前保留此备份。"
        print(f"备份完成：{dest}")
        print(f"SHA-256：{manifest['sha256']}")
        print(f"大小：{manifest['size_bytes']} bytes")
    else:
        manifest["note"] = (
            "当前不是 SQLite。请在删除旧 KO 表前执行 pg_dump，并把 dump 文件哈希写入本清单。"
        )
        print(manifest["note"])
        print(f"DATABASE_URL scheme={urlparse(url).scheme}")

    manifest_path = REPORTS_DIR / f"p22_backup_manifest_{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"清单：{manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
