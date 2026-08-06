"""递归生成钉钉文档清单，并将原始文件下载到本地raw目录。"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.dingtalk import DingTalkClient  # noqa: E402
from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402


def document_from_manifest(entry: dict) -> dict:
    return {
        "id": entry.get("document_id") or "",
        "title": entry.get("name") or "未命名",
        "extension": entry.get("extension") or "",
        "node_type": entry.get("node_type") or "",
        "space_id": entry.get("space_id") or "",
        "space_name": entry.get("space_name") or "",
        "path": entry.get("dingtalk_path") or "",
        "source_url": entry.get("source_url") or "",
        "updated_at": entry.get("source_updated_at") or "",
        "file_size": entry.get("reported_file_size"),
    }


async def run(space_id: str | None, list_only: bool, from_manifest: bool) -> int:
    client = DingTalkClient()
    storage = DingTalkLocalStorage()
    storage.ensure_directories()
    try:
        if from_manifest:
            manifest = storage.read_manifest()
            documents = [
                document_from_manifest(entry)
                for entry in manifest["documents"]
                if entry.get("source_status", "active") == "active"
                and entry.get("download_status") != "downloaded"
            ]
            print(f"从现有清单读取 {len(documents)} 份待下载或待重试文档")
        else:
            documents = await client.list_all_docs(space_id)
            inventory_result = storage.record_inventory(
                documents,
                complete_snapshot=bool(client._last_inventory_complete),
                scope_space_ids=client._last_inventory_scope_ids,
            )
            print(f"已发现 {len(documents)} 份支持的钉钉文档")
            print(
                "增量核对："
                f"新增 {inventory_result['discovered']}，"
                f"移动 {inventory_result['moved']}，"
                f"恢复 {inventory_result['restored']}，"
                f"软删除 {inventory_result['deleted']}"
            )
        print(f"同步清单：{storage.manifest_path}")
        if list_only:
            return 0

        async def show_progress(document, index):
            title = document.get("title") or document.get("id") or "未命名"
            print(f"[{index}/{len(documents)}] 已处理：{title}")

        downloaded = await client.download_selected_raw_files(
            documents,
            on_progress=show_progress,
        )
        selected_ids = {str(item.get("id") or "") for item in documents}
        manifest = storage.read_manifest()
        statuses = Counter(
            str(item.get("status") or "unknown")
            for item in manifest["documents"]
            if str(item.get("document_id") or "") in selected_ids
        )
        print(f"成功下载或确认未变化：{len(downloaded)}")
        print(f"本次状态统计：{dict(statuses)}")
        print(f"原文件目录：{storage.raw_root}")
        return 1 if statuses.get("failed") else 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成钉钉文档清单并下载原始文件，不执行Markdown转换和RAG入库。"
    )
    parser.add_argument(
        "--space-id",
        default=None,
        help="临时指定知识库ID；未提供时使用.env中的同步范围配置。",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="只递归生成文档清单，不下载原文件。",
    )
    parser.add_argument(
        "--from-manifest",
        action="store_true",
        help="直接从现有清单断点续传，跳过重新扫描和已经下载成功的文档。",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.space_id, args.list_only, args.from_manifest))


if __name__ == "__main__":
    raise SystemExit(main())
