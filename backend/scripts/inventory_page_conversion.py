"""Phase 2.1：Page 转换契约只读 inventory（真正只读，不写数据库）。

规则：
- 不调用 init_db（不建表、不运行迁移、不 commit）；
- 默认不指向真实 notes.db；必须显式传入 --db；
- SQLite 使用只读连接 mode=ro；
- schema 缺少 P40 字段时只报告缺失，不补字段；
- 数据库文件不存在 → 报错且不得创建文件。

用法：
    python scripts/inventory_page_conversion.py --db sqlite:///<path>.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# 把 backend 根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 必须存在的 P40 字段（缺失则报告 schema 落后，不补字段）
_P40_COLUMNS = [
    "note_schema_version", "content_format", "content_kind",
    "converter_key", "converter_version", "conversion_status",
    "conversion_warnings_json",
]


def _connect_readonly(url: str) -> sqlite3.Connection:
    """SQLite 只读连接（mode=ro）；不存在的文件不会创建。"""
    if not url.startswith("sqlite:///"):
        raise ValueError("仅支持 sqlite:/// 只读模式（本 inventory 为 dry-run）")
    path = url[len("sqlite:///"):]
    if not os.path.exists(path):
        raise FileNotFoundError(f"数据库文件不存在（不会创建）: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="sqlite:/// 只读数据库路径（必填）")
    args = parser.parse_args()

    try:
        con = _connect_readonly(args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"无法只读打开数据库: {exc}", file=sys.stderr)
        return 1

    try:
        cur = con.cursor()
        # schema 校验：pages 表是否存在
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pages'"
        ).fetchall()]
        if "pages" not in tables:
            print("pages 表不存在（schema 未初始化），仅报告缺失。", file=sys.stderr)
            return 0

        cols = {c[1] for c in cur.execute("PRAGMA table_info(pages)").fetchall()}
        missing = [c for c in _P40_COLUMNS if c not in cols]

        # alembic_version
        alembic_version = None
        try:
            row = cur.execute("SELECT version_num FROM alembic_version").fetchone()
            alembic_version = row[0] if row else None
        except sqlite3.Error:
            alembic_version = None

        total = cur.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

        if missing:
            # schema 落后：只报告缺失，不补字段
            print(json.dumps({
                "total_pages": total,
                "schema_ready": False,
                "missing_columns": missing,
                "alembic_version": alembic_version,
                "note": "schema 缺少 P40 字段，未补字段，仅报告。",
            }, ensure_ascii=False, indent=2))
            return 0

        # 统计
        legacy = 0
        canonical = 0
        by_converter: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for row in cur.execute(
            "SELECT note_schema_version, converter_key, conversion_status, content_kind FROM pages"
        ):
            schema, converter, status, kind = row
            schema = schema or "legacy-note/v0"
            if schema == "canonical-note/v1":
                canonical += 1
            else:
                legacy += 1
            by_converter[converter or "(none)"] = by_converter.get(converter or "(none)", 0) + 1
            by_status[status or "(none)"] = by_status.get(status or "(none)", 0) + 1
            by_kind[kind or "(unknown)"] = by_kind.get(kind or "(unknown)", 0) + 1

        print(json.dumps({
            "total_pages": total,
            "schema_ready": True,
            "alembic_version": alembic_version,
            "legacy_note_v0": legacy,
            "canonical_note_v1": canonical,
            "by_converter_key": by_converter,
            "by_conversion_status": by_status,
            "by_content_kind": by_kind,
        }, ensure_ascii=False, indent=2))
        print("\n[dry-run] 只读统计（mode=ro），未写入任何数据。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
