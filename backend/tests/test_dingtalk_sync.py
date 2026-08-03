import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.dingtalk import _import_document, _markdown_document
from app.core.rag import VectorStore
from app.models.database import Base, Notebook, Page, PageChunk


class FakeEmbeddingService:
    def __init__(self):
        self.calls = 0

    async def encode_chunks(self, content, title=""):
        self.calls += 1
        return [(content, [0.1, 0.2, 0.3])]


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    notebook = Notebook(id="notebook-1", name="钉钉同步测试")
    session.add(notebook)
    session.commit()
    try:
        yield session
    finally:
        session.close()


def make_doc(node_id="node-1", content="正文"):
    return {
        "id": node_id,
        "title": "测试文档",
        "content": content,
        "space_id": "space-1",
        "space_name": "交付服务部知识库",
        "path": "内部/测试文档",
    }


def test_markdown_document_keeps_source_metadata():
    markdown = _markdown_document(make_doc())

    assert markdown.startswith("# 测试文档\n")
    assert "> 来源：钉钉知识库 / 交付服务部知识库" in markdown
    assert "> 路径：内部/测试文档" in markdown
    assert markdown.endswith("正文\n")


@pytest.mark.asyncio
async def test_unchanged_document_is_skipped_by_dingtalk_node_id(db):
    embedding = FakeEmbeddingService()
    vector_store = VectorStore(db)

    first = await _import_document(
        db, vector_store, embedding, "notebook-1", make_doc()
    )
    second = await _import_document(
        db, vector_store, embedding, "notebook-1", make_doc()
    )

    assert first == "imported"
    assert second == "skipped"
    assert embedding.calls == 1
    assert db.query(Page).count() == 1
    assert db.query(PageChunk).count() == 1
    assert db.query(Page).one().source_id == "node-1"
    assert db.query(Page).one().source_space_id == "space-1"


@pytest.mark.asyncio
async def test_same_title_with_different_node_ids_creates_distinct_pages(db):
    embedding = FakeEmbeddingService()
    vector_store = VectorStore(db)

    await _import_document(
        db, vector_store, embedding, "notebook-1", make_doc("node-1")
    )
    await _import_document(
        db, vector_store, embedding, "notebook-1", make_doc("node-2")
    )

    assert db.query(Page).count() == 2


@pytest.mark.asyncio
async def test_manual_page_with_same_title_is_not_overwritten(db):
    manual = Page(
        id="manual-page",
        notebook_id="notebook-1",
        title="测试文档",
        content="手工内容",
    )
    db.add(manual)
    db.commit()

    await _import_document(
        db,
        VectorStore(db),
        FakeEmbeddingService(),
        "notebook-1",
        make_doc(),
    )

    assert db.query(Page).count() == 2
    assert db.get(Page, "manual-page").content == "手工内容"


@pytest.mark.asyncio
async def test_page_and_chunks_roll_back_together_on_index_failure(db):
    class FailingVectorStore:
        async def add_page_chunks(self, _page_id, _chunks):
            raise RuntimeError("index failed")

    with pytest.raises(RuntimeError, match="index failed"):
        await _import_document(
            db,
            FailingVectorStore(),
            FakeEmbeddingService(),
            "notebook-1",
            make_doc(),
        )

    assert db.query(Page).count() == 0
    assert db.query(PageChunk).count() == 0
