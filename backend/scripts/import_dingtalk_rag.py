"""将本地钉钉Markdown批量导入RAG知识库。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.core.dingtalk_rag_importer import DingTalkRAGImporter  # noqa: E402
from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402
from app.core.rag import EmbeddingService  # noqa: E402
from app.models.database import get_engine, get_session, init_db  # noqa: E402


def dry_run(
    storage: DingTalkLocalStorage,
    document_ids,
    limit: int,
    include_deleted: bool = False,
) -> int:
    manifest = storage.read_manifest()
    selected_ids = set(document_ids)
    entries = [
        entry for entry in manifest["documents"]
        if entry.get("source_status", "active") == "active"
        and entry.get("conversion_status") == "converted"
        and (
            not selected_ids
            or str(entry.get("document_id") or "") in selected_ids
        )
    ]
    if limit > 0:
        entries = entries[:limit]
    service = EmbeddingService()
    chunks = 0
    try:
        for entry in entries:
            path = storage.root / str(entry.get("markdown_path") or "")
            markdown = path.read_text(encoding="utf-8")
            body = DingTalkRAGImporter._strip_frontmatter(markdown)
            chunks += len(service.split_text(body))
    finally:
        asyncio.run(service.close())
    print(f"预检查完成：文档 {len(entries)} 份，预计分块 {chunks} 个", flush=True)
    if include_deleted:
        deleted = [
            entry for entry in manifest["documents"]
            if entry.get("source_status") == "deleted"
            and entry.get("rag_status") != "deleted"
            and (
                not selected_ids
                or str(entry.get("document_id") or "") in selected_ids
            )
        ]
        print(f"待显式清理的已删除RAG文档：{len(deleted)} 份", flush=True)
    return 0


async def run(args) -> int:
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    importer = DingTalkRAGImporter(db)

    def show_progress(entry, index, total, status, chunks):
        title = entry.get("name") or entry.get("document_id") or "未命名"
        print(
            f"[{index}/{total}] {status}: {title}（{chunks} 个分块）",
            flush=True,
        )

    try:
        prune_result = None
        if args.prune_deleted:
            prune_result = importer.prune_deleted(args.document_id)
        result = await importer.import_manifest(
            notebook_name=args.notebook_name,
            force=args.force,
            document_ids=args.document_id,
            limit=args.limit,
            on_progress=show_progress,
        )
    finally:
        await importer.close()
        db.close()

    if prune_result is not None:
        result["prune_deleted"] = prune_result
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 1 if result["failed"] or (prune_result or {}).get("failed") else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="读取本地markdown和manifest.json，分块并导入现有RAG知识库。"
    )
    parser.add_argument(
        "--prune-deleted",
        action="store_true",
        help="显式删除已被完整快照标记为源端删除的RAG页面和分块；本地原文件仍保留。",
    )
    parser.add_argument(
        "--notebook-name",
        default="钉钉知识库",
        help="导入目标知识库名称，默认：钉钉知识库。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使正文和索引未变化，也重新生成Embedding。",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="只导入指定钉钉文档ID，可重复传入。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="仅处理清单前N份可导入文档，0表示不限制。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计文档和分块，不访问Embedding服务、不写数据库。",
    )
    args = parser.parse_args()

    if args.dry_run:
        return dry_run(
            DingTalkLocalStorage(),
            args.document_id,
            args.limit,
            include_deleted=args.prune_deleted,
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
