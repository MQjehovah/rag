"""V4 图谱首次回填工具（J-3 封板）。

按 Page.id keyset/batch 分页回填 V4 实体关系图谱：
- 默认 dry-run（零写入）；
- 显式 --apply 才写入；
- 只处理有效且有明确 Notebook 权限的 Page（失效远程 Page 跳过）；
- 按 id 稳定排序 + last_id 游标，超过一个 batch 不漏 Page；
- 幂等（重复运行结果一致）；
- 不调用真实模型（纯确定性 SQL 抽取）。

用法：
    .venv/Scripts/python.exe scripts/backfill_v4_graph.py --db <sqlite 路径>            # dry-run
    .venv/Scripts/python.exe scripts/backfill_v4_graph.py --db <sqlite 路径> --apply    # 实际回填

注意：不要对真实 notes.db 执行 --apply；真实回填需另行授权（停服/备份/演练）。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models.database import get_engine, Page  # noqa: E402


def _eligible_page_ids(db, *, last_id=None, batch=200):
    """keyset 分页返回有效且有明确 Notebook 权限的 Page id。"""
    q = db.query(Page.id).filter(Page.notebook_id.isnot(None))
    if last_id:
        q = q.filter(Page.id > last_id)
    return q.order_by(Page.id).limit(batch).all()


def _page_eligible(db, page) -> bool:
    """远程 Page 必须存在同 Connector active SourceItem；手工 Page 放行。"""
    if not page.source_type:
        return True
    from app.models.database import SourceConnection, SourceItem
    row = (
        db.query(SourceItem.id)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(
            SourceItem.page_id == page.id,
            SourceItem.state == "active",
            SourceConnection.connector_key == page.source_type,
        )
        .first()
    )
    return row is not None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="V4 图谱首次回填（默认 dry-run）")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    parser.add_argument("--batch", type=int, default=200, help="每批 Page 数量")
    args = parser.parse_args(argv)

    engine = get_engine(f"sqlite:///{args.db}")
    from app.models.database import init_db
    init_db(engine)
    db = sessionmaker(bind=engine)()

    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph

    scanned = 0
    built = 0
    skipped = 0
    last_id = None
    while True:
        rows = _eligible_page_ids(db, last_id=last_id, batch=args.batch)
        if not rows:
            break
        last_id = rows[-1][0]
        for (page_id,) in rows:
            scanned += 1
            page = db.get(Page, page_id)
            if page is None or not _page_eligible(db, page):
                skipped += 1
                continue
            if args.apply:
                result = rebuild_page_graph(db, page_id, commit=True)
                if result.get("status") == "rebuilt":
                    built += 1
            else:
                built += 1  # dry-run 计数：预计可回填
        if len(rows) < args.batch:
            break

    print(json.dumps({
        "apply": args.apply,
        "scanned": scanned,
        "buildable": built,
        "skipped": skipped,
    }, ensure_ascii=False))
    db.close()
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
