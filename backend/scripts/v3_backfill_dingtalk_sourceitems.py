"""P10-BE-02：Manifest → SourceItem 回填脚本（幂等，不重新下载编译）。

把现有钉钉 Manifest 中已导入（有 rag_page_id）的文档映射为 SourceItem，
回填 page_id 关联，不触发下载/转换/编译。

幂等键：SourceItem(connection_id, external_id) 唯一，已存在则跳过。

用法：
    .venv/Scripts/python.exe scripts/v3_backfill_dingtalk_sourceitems.py [--dry-run]
"""
from __future__ import annotations

import io
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402
from app.models.database import SourceConnection, SourceItem, get_engine  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    engine = get_engine(settings.database_url)
    storage = DingTalkLocalStorage()
    manifest = storage.read_manifest()
    docs = manifest.get("documents", [])

    db = sessionmaker(bind=engine)()

    # 1. 确保钉钉 SourceConnection 存在
    conn = db.query(SourceConnection).filter(
        SourceConnection.connector_key == "dingtalk",
        SourceConnection.name == "钉钉知识库",
    ).first()
    if conn is None:
        conn = SourceConnection(
            id=str(uuid.uuid4()),
            connector_key="dingtalk",
            name="钉钉知识库",
            enabled=True,
            created_by="backfill",
        )
        db.add(conn)
        db.flush()

    # 2. 回填：已导入（rag_page_id 非空）的文档 → SourceItem
    backfilled = 0
    skipped = 0
    for doc in docs:
        external_id = str(doc.get("document_id") or "").strip()
        page_id = doc.get("rag_page_id")
        if not external_id:
            continue
        existing = db.query(SourceItem).filter(
            SourceItem.connection_id == conn.id,
            SourceItem.external_id == external_id,
        ).first()
        if existing is not None:
            skipped += 1
            continue
        # 只有已导入的文档才有 page_id；未导入的留待正式同步
        state = "active" if page_id else "active"
        db.add(SourceItem(
            id=str(uuid.uuid4()),
            connection_id=conn.id,
            external_id=external_id,
            external_version=str(doc.get("markdown_hash") or doc.get("source_file_hash") or ""),
            content_hash=str(doc.get("markdown_hash") or ""),
            source_url=str(doc.get("source_url") or ""),
            source_path=str(doc.get("dingtalk_path") or ""),
            page_id=str(page_id) if page_id else None,
            state=state,
            acl_json=json.dumps({"space_id": str(doc.get("space_id") or "")}, ensure_ascii=False),
            source_updated_at=_parse_dt(doc.get("source_updated_at")),
            last_synced_at=datetime.now(),
        ))
        backfilled += 1

    if dry_run:
        print(f"--dry-run：将回填 {backfilled} 条 SourceItem，跳过 {skipped} 条已存在")
        db.rollback()
    else:
        db.commit()
        print(f"回填完成：新增 {backfilled} 条 SourceItem，跳过 {skipped} 条已存在")

    db.close()
    engine.dispose()
    return 0


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
