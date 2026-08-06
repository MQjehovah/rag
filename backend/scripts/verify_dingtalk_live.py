"""选择一份体积较小的真实钉钉文档，验收下载、转换和RAG增量链路。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.dingtalk_sync_service import DingTalkSyncService, DingTalkSyncState


def select_document(storage: DingTalkLocalStorage, document_id: str | None) -> dict:
    entries = [
        entry
        for entry in storage.read_manifest()["documents"]
        if entry.get("source_status", "active") == "active"
        and entry.get("rag_status") == "imported"
    ]
    if document_id:
        entry = next(
            (
                item
                for item in entries
                if str(item.get("document_id") or "") == document_id
            ),
            None,
        )
        if entry is None:
            raise ValueError("指定文档不存在，或尚未成功写入RAG")
        return storage.document_from_manifest(entry)

    if not entries:
        raise ValueError("manifest中没有可用于验收的已入库文档")
    entry = min(
        entries,
        key=lambda item: int(item.get("source_file_size") or 2**63 - 1),
    )
    return storage.document_from_manifest(entry)


async def run(document_id: str | None) -> int:
    storage = DingTalkLocalStorage()
    document = select_document(storage, document_id)
    state = DingTalkSyncState(storage.root / "acceptance-task.json")
    state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
        request={
            "original_mode": "selected",
            "notebook_name": "钉钉知识库",
            "space_id": None,
            "selected_docs": [document],
        },
    )
    service = DingTalkSyncService(state)
    await service.run(
        mode="selected",
        notebook_name="钉钉知识库",
        selected_docs=[document],
    )
    result = state.snapshot()
    output = {
        "document": {
            "id": document.get("id"),
            "title": document.get("title"),
            "path": document.get("path"),
        },
        "task": result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return (
        0
        if result.get("stage") == "completed" and not result.get("errors")
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", help="指定一份已入库钉钉文档ID")
    args = parser.parse_args()
    return asyncio.run(run(args.document_id))


if __name__ == "__main__":
    raise SystemExit(main())
