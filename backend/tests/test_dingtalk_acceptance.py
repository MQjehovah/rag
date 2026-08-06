import pytest

from app.config import settings
from app.core.dingtalk_acceptance import DingTalkAcceptanceVerifier
from app.core.dingtalk_rag_importer import DingTalkRAGImporter
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.models.database import Page, PageChunk, get_engine, get_session, init_db


class FakeEmbeddingService:
    def split_text(self, content, title=""):
        return [content]

    async def encode_batch(self, texts, batch_size=32):
        return [[0.1] * settings.embedding_dimensions for _ in texts]


def prepare_document(storage: DingTalkLocalStorage) -> None:
    document = {
        "id": "acceptance-node",
        "title": "验收文档.md",
        "extension": "md",
        "node_type": "FILE",
        "space_id": "acceptance-space",
        "space_name": "验收知识库",
        "path": "验收目录/验收文档.md",
        "source_url": "https://example.test/acceptance-node",
    }
    storage.record_inventory(
        [document],
        complete_snapshot=True,
        scope_space_ids=["acceptance-space"],
    )
    storage.persist_raw_file(document, b"acceptance content")
    storage.persist_markdown_file(document, "# 验收文档\n\n验收正文\n")


@pytest.fixture
def database(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'acceptance.db'}")
    init_db(engine)
    db = get_session(engine)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_acceptance_passes_when_manifest_files_and_database_match(
    tmp_path,
    database,
):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_document(storage)
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())
    await importer.import_manifest()

    result = DingTalkAcceptanceVerifier(database, storage).verify()

    assert result["valid"] is True
    assert result["acceptance_status"] == "passed"
    assert result["active_documents"] == 1
    assert result["manifest_imported"] == 1
    assert result["database_dingtalk_pages"] == 1
    assert result["database_dingtalk_chunks"] == 1
    assert result["error_count"] == 0


@pytest.mark.asyncio
async def test_acceptance_reports_database_and_chunk_drift(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_document(storage)
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())
    await importer.import_manifest()
    page = database.query(Page).one()
    page.content = "被外部修改的正文"
    database.query(PageChunk).filter(PageChunk.page_id == page.id).delete()
    database.commit()

    result = DingTalkAcceptanceVerifier(database, storage).verify()

    assert result["valid"] is False
    assert result["acceptance_status"] == "failed"
    messages = [issue["message"] for issue in result["errors"]]
    assert "数据库页面正文已偏离本地Markdown" in messages
    assert any("RAG分块数不一致" in message for message in messages)
