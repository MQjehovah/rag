"""执行小规模、非破坏性的钉钉文档转 Markdown 并写入 RAG 的联调测试。"""

import asyncio
import sys
from pathlib import Path
import uuid


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.api.dingtalk import _import_document  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.dingtalk import DingTalkClient  # noqa: E402
from app.core.rag import EmbeddingService, VectorStore  # noqa: E402
from app.models.database import (  # noqa: E402
    Notebook,
    Page,
    PageChunk,
    get_engine,
    get_session,
    init_db,
)


async def find_small_sample(client, workspace):
    allowed_extensions = {"md", "xlsx", "txt"}

    async def walk(parent_id, path=""):
        for node in await client.list_nodes(parent_id):
            name = node.get("name") or "无标题"
            node_id = node["node_id"]
            current_path = f"{path}/{name}" if path else name
            if node.get("type") == "FOLDER" or node.get("has_children"):
                found = await walk(node_id, current_path)
                if found:
                    return found
                continue

            extension = (node.get("extension") or "").lower()
            if extension not in allowed_extensions:
                continue

            metadata = {
                "id": node_id,
                "title": name,
                "extension": extension,
                "node_type": node.get("type") or "",
                "space_id": workspace["id"],
                "space_name": workspace["name"],
                "path": current_path,
            }
            collected = await client.collect_selected_docs([metadata])
            if collected and len(collected[0]["content"]) <= 50_000:
                return collected[0]
        return None

    root_id = workspace.get("root_node_id") or workspace["id"]
    return await walk(root_id)


async def main():
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    client = DingTalkClient()
    embedding = EmbeddingService()

    try:
        print("STAGE=connecting", flush=True)
        workspaces = await client.list_workspaces()
        workspace = next(
            (
                item
                for item in workspaces
                if item["id"] == settings.dingtalk_knowledge_base_id
            ),
            None,
        )
        if workspace is None:
            raise RuntimeError("未找到配置的钉钉知识库")

        print("STAGE=finding-small-sample", flush=True)
        sample = await find_small_sample(client, workspace)
        if sample is None:
            raise RuntimeError("未找到五万字符以内的 Markdown、文本或 Excel 样本文档")
        docs = [sample]

        notebook_name = "钉钉知识库-联调验证"
        notebook = db.query(Notebook).filter(Notebook.name == notebook_name).first()
        if notebook is None:
            notebook = Notebook(id=str(uuid.uuid4()), name=notebook_name)
            db.add(notebook)
            db.commit()

        store = VectorStore(db)
        first_results = [
            await _import_document(db, store, embedding, notebook.id, doc)
            for doc in docs
        ]
        second_results = [
            await _import_document(db, store, embedding, notebook.id, doc)
            for doc in docs
        ]

        source_ids = [doc["id"] for doc in docs]
        pages = db.query(Page).filter(
            Page.source_type == "dingtalk",
            Page.source_id.in_(source_ids),
        ).all()
        page_ids = [page.id for page in pages]
        chunk_count = (
            db.query(PageChunk).filter(PageChunk.page_id.in_(page_ids)).count()
            if page_ids
            else 0
        )
        markdown_ok = all(
            page.content.startswith("# ")
            and "> 来源：钉钉知识库" in page.content
            and "> 路径：" in page.content
            for page in pages
        )

        query_embedding = await embedding.encode(docs[0]["title"])
        search_results = await store.search(query_embedding, top_k=10)
        searchable = any(result["page_id"] in page_ids for result in search_results)

        print(f"SAMPLE_DOCUMENTS={len(docs)}")
        print(f"FIRST_SYNC={','.join(first_results)}")
        print(f"SECOND_SYNC={','.join(second_results)}")
        print(f"STORED_PAGES={len(pages)}")
        print(f"STORED_CHUNKS={chunk_count}")
        print(f"MARKDOWN_OK={markdown_ok}")
        print(f"VECTOR_SEARCH_OK={searchable}")
    finally:
        db.close()
        await client.close()
        await embedding.close()


if __name__ == "__main__":
    asyncio.run(main())
