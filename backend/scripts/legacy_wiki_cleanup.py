"""旧 Community Wiki 一次性清理 CLI（J-2）。

默认只做 dry-run 报告，绝不写库。显式传 --apply 才会删除「可清理」的旧
Community Wiki（含人工编辑/锁定/保护内容的记录始终保留并报告）。

用法：
    python -m scripts.legacy_wiki_cleanup --db <sqlite 路径>          # dry-run 报告
    python -m scripts.legacy_wiki_cleanup --db <sqlite 路径> --apply  # 实际清理

注意：不要对真实 notes.db 执行 --apply；真实清理需另行授权（停服/备份/演练）。
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧 Community Wiki 一次性清理（默认 dry-run）")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    parser.add_argument("--apply", action="store_true", help="实际执行删除（默认只 dry-run）")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import cleanup_legacy_wikis

    url = f"sqlite:///{args.db}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()

    report = cleanup_legacy_wikis(session, apply=args.apply)

    output = {
        "apply": args.apply,
        "total_legacy": report.total_legacy,
        "cleanable_count": len(report.cleanable),
        "human_protected_count": len(report.human_protected),
        "applied_count": len(report.applied),
        "cleanable": [
            {"wiki_page_id": v.wiki_page_id, "title": v.title, "status": v.status}
            for v in report.cleanable
        ],
        "human_protected": [
            {
                "wiki_page_id": v.wiki_page_id,
                "title": v.title,
                "status": v.status,
                "reasons": v.reasons,
            }
            for v in report.human_protected
        ],
        "applied": report.applied,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    session.close()
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
