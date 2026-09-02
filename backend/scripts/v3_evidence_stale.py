"""P1-BE-08：文档 Hash 变化时旧 Evidence 标记 stale（幂等）。

V3 计划 6.3 P1-BE-08「文档 Hash 变化时，将旧 Evidence 标记 stale，
不直接物理删除」。

三步（均可重复执行，幂等）：
1. 回填：source_doc_hash 为 NULL 的 Evidence → 其来源 page 当前 content_hash。
   （历史 Evidence 由 P1-BE-02 转换时未记录快照，此处补齐；
    未来新转换的 Evidence 已由 v3_convert_chunks_to_evidence.py 直接写入。）
2. 判定：source_doc_hash != page.content_hash → 文档已更新 → stale。
3. 标记：UPDATE status='stale'；hash 一致的保持 'active'。

不物理删除任何 Evidence（硬约束 1）；只改 status，保留 content/locator/溯源。

用法：
    .venv/Scripts/python.exe scripts/v3_evidence_stale.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.evidence_stale import is_stale  # noqa: E402
from app.models.database import get_engine  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    engine = get_engine(settings.database_url)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT e.id, e.source_page_id, e.source_doc_hash, e.status, p.content_hash "
            "FROM evidence_items e "
            "LEFT JOIN pages p ON p.id = e.source_page_id"
        )).fetchall()

    # 回填：source_doc_hash 为空 → 取当前 page.content_hash
    backfilled = 0
    for ev_id, page_id, doc_hash, _status, current_hash in rows:
        if not doc_hash and current_hash:
            if not dry_run:
                with engine.begin() as conn:
                    conn.execute(text(
                        "UPDATE evidence_items SET source_doc_hash = :h WHERE id = :id"
                    ), {"h": current_hash, "id": ev_id})
            backfilled += 1

    # 判定 + 标记
    marked_stale = 0
    marked_active = 0
    skipped = 0
    for ev_id, page_id, doc_hash, status, current_hash in rows:
        if status == "rejected":
            continue  # 已人工否决的 Evidence 不参与 stale 判定
        target = "stale" if is_stale(doc_hash, current_hash) else "active"
        if status == target:
            skipped += 1
        else:
            if not dry_run:
                with engine.begin() as conn:
                    conn.execute(text(
                        "UPDATE evidence_items SET status = :st WHERE id = :id"
                    ), {"st": target, "id": ev_id})
            if target == "stale":
                marked_stale += 1
            else:
                marked_active += 1

    print(f"Evidence 总数: {len(rows)}，回填 source_doc_hash: {backfilled}")
    print(
        f"标记 stale: {marked_stale}，恢复 active: {marked_active}，"
        f"无需变更: {skipped}" + ("（--dry-run 未写库）" if dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
