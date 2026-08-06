import hashlib

import pytest

from app.config import settings
from app.core.dingtalk_rag_importer import DingTalkRAGImporter
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.models.database import Notebook, Page, PageChunk, get_engine, get_session, init_db


class FakeEmbeddingService:
    def __init__(self, incomplete=False):
        self.incomplete = incomplete
        self.calls = 0

    def split_text(self, content, title=""):
        return [part.strip() for part in content.split("\n\n") if part.strip()]

    async def encode_batch(self, texts, batch_size=32):
        self.calls += 1
        vectors = [[0.1] * settings.embedding_dimensions for _ in texts]
        if self.incomplete and vectors:
            vectors[-1] = []
        return vectors

    async def close(self):
        return None


def prepare_entry(storage, node_id="node-001", body="正文内容"):
    document = {
        "id": node_id,
        "title": "测试文档.md",
        "extension": "md",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "设备资料/测试文档.md",
        "source_url": f"https://example.test/{node_id}",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, body.encode("utf-8"))
    entry = next(
        item for item in storage.read_manifest()["documents"]
        if item["document_id"] == node_id
    )
    markdown = (
        "---\n"
        'source_type: "dingtalk"\n'
        f'source_hash: "{entry["source_file_hash"]}"\n'
        "---\n\n"
        "# 测试文档\n\n"
        f"{body}\n"
    )
    storage.persist_markdown_file(document, markdown)
    return next(
        item for item in storage.read_manifest()["documents"]
        if item["document_id"] == node_id
    )


@pytest.fixture
def database(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'rag.db'}")
    init_db(engine)
    db = get_session(engine)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_converted_markdown_is_imported_with_chunks(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_entry(storage)
    embedding = FakeEmbeddingService()
    importer = DingTalkRAGImporter(database, storage, embedding)

    result = await importer.import_manifest()

    assert result["imported"] == 1
    assert result["failed"] == 0
    page = database.query(Page).one()
    assert page.source_type == "dingtalk"
    assert page.source_id == "node-001"
    assert page.source_file_hash == entry["source_file_hash"]
    assert page.content == "# 测试文档\n\n正文内容"
    assert "source_type" not in page.content
    assert 'source_type: "dingtalk"' in page.source_content
    assert page.source_content_hash == entry["markdown_hash"]
    assert page.content_hash == page.current_content_hash
    assert page.indexed_content_hash == page.current_content_hash
    assert page.index_status == "current"
    chunks = database.query(PageChunk).filter(PageChunk.page_id == page.id).all()
    assert chunks
    assert all("source_hash" not in chunk.content for chunk in chunks)
    assert database.query(Notebook).filter(Notebook.name == "钉钉知识库").count() == 1
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["status"] == "converted"
    assert manifest_entry["rag_status"] == "imported"
    assert manifest_entry["pipeline_status"] == "imported"
    assert manifest_entry["rag_page_id"] == page.id
    assert manifest_entry["rag_chunk_count"] == len(chunks)
    assert manifest_entry["rag_imported_markdown_hash"] == entry["markdown_hash"]


@pytest.mark.asyncio
async def test_unchanged_document_is_skipped_without_embedding(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_entry(storage)
    embedding = FakeEmbeddingService()
    importer = DingTalkRAGImporter(database, storage, embedding)

    first = await importer.import_manifest()
    second = await importer.import_manifest()

    assert first["imported"] == 1
    assert second["skipped"] == 1
    assert embedding.calls == 1
    assert database.query(Page).count() == 1
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["rag_write_result"] == "unchanged"
    assert manifest_entry["rag_attempts"] == 2


@pytest.mark.asyncio
async def test_legacy_frontmatter_page_is_cleaned_without_reembedding(
    tmp_path,
    database,
):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_entry(storage)
    embedding = FakeEmbeddingService()
    importer = DingTalkRAGImporter(database, storage, embedding)
    await importer.import_manifest()
    page = database.query(Page).one()
    legacy_markdown = page.source_content
    legacy_hash = hashlib.sha256(legacy_markdown.encode("utf-8")).hexdigest()
    page.content = legacy_markdown
    page.content_hash = legacy_hash
    page.indexed_content_hash = legacy_hash
    page.index_dirty = False
    database.commit()

    result = await importer.import_manifest()

    assert result["skipped"] == 1
    assert embedding.calls == 1
    database.refresh(page)
    assert page.content == "# 测试文档\n\n正文内容"
    assert "source_type" not in page.content
    assert page.source_content == legacy_markdown
    assert page.source_content_hash == legacy_hash
    assert page.content_hash == page.current_content_hash
    assert page.indexed_content_hash == page.current_content_hash
    assert page.index_status == "current"


@pytest.mark.asyncio
async def test_markdown_hash_mismatch_does_not_write_database(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_entry(storage)
    path = storage.root / entry["markdown_path"]
    path.write_text("被外部修改", encoding="utf-8")
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())

    result = await importer.import_manifest()

    assert result["failed"] == 1
    assert "哈希" in result["failures"][0]["error"]
    assert database.query(Page).count() == 0
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["rag_status"] == "failed"
    assert "哈希" in manifest_entry["rag_error"]


@pytest.mark.asyncio
async def test_incomplete_embeddings_are_rolled_back(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_entry(storage, body="第一段\n\n第二段")
    importer = DingTalkRAGImporter(
        database,
        storage,
        FakeEmbeddingService(incomplete=True),
    )

    result = await importer.import_manifest()

    assert result["failed"] == 1
    assert "完整分块向量" in result["failures"][0]["error"]
    assert database.query(Page).count() == 0
    assert database.query(PageChunk).count() == 0


@pytest.mark.asyncio
async def test_changed_document_updates_existing_page_in_place(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_entry(storage, body="第一版")
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())
    await importer.import_manifest()
    page_id = database.query(Page).one().id
    document = storage.document_from_manifest(entry)

    storage.persist_raw_file(document, "第二版".encode("utf-8"))
    storage.persist_markdown_file(document, "# 测试文档\n\n第二版\n")
    result = await importer.import_manifest()

    assert result["imported"] == 1
    assert database.query(Page).count() == 1
    page = database.query(Page).one()
    assert page.id == page_id
    assert "第二版" in page.content
    assert storage.read_manifest()["documents"][0]["rag_status"] == "imported"


@pytest.mark.asyncio
async def test_same_title_with_different_source_ids_stays_distinct(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_entry(storage, node_id="node-one")
    prepare_entry(storage, node_id="node-two")
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())

    result = await importer.import_manifest()

    assert result["imported"] == 2
    assert database.query(Page).count() == 2
    assert {
        page.source_id for page in database.query(Page).all()
    } == {"node-one", "node-two"}


@pytest.mark.asyncio
async def test_manual_page_with_same_title_is_not_overwritten(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    prepare_entry(storage)
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())
    notebook = importer.get_or_create_notebook("钉钉知识库")
    manual = Page(
        id="manual-page",
        notebook_id=notebook.id,
        title="测试文档.md",
        content="手工内容",
    )
    database.add(manual)
    database.commit()

    result = await importer.import_manifest()

    assert result["imported"] == 1
    assert database.query(Page).count() == 2
    assert database.get(Page, "manual-page").content == "手工内容"


@pytest.mark.asyncio
async def test_deleted_source_requires_explicit_rag_prune(tmp_path, database):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_entry(storage, node_id="node-prune")
    importer = DingTalkRAGImporter(database, storage, FakeEmbeddingService())
    await importer.import_manifest()
    raw_path = storage.root / entry["raw_path"]

    storage.record_inventory(
        [], complete_snapshot=True, scope_space_ids=["space-001"]
    )
    skipped = await importer.import_manifest()
    assert skipped["total"] == 0
    assert database.query(Page).count() == 1

    result = importer.prune_deleted()

    assert result == {
        "total": 1,
        "deleted": 1,
        "not_found": 0,
        "failed": 0,
        "failures": [],
    }
    assert database.query(Page).count() == 0
    assert database.query(PageChunk).count() == 0
    assert raw_path.is_file()
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["source_status"] == "deleted"
    assert manifest_entry["rag_status"] == "deleted"


def test_strip_frontmatter_keeps_markdown_body():
    markdown = "---\ntitle: test\n---\n\n# 标题\n\n正文"

    result = DingTalkRAGImporter._strip_frontmatter(markdown)

    assert result == "# 标题\n\n正文"
    assert "title: test" not in result
