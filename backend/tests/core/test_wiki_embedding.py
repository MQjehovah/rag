"""wiki 页面嵌入工具的行为测试(TDD)。"""
import json

import pytest

from app.core.wiki_embedding import embed_wiki_pages
from app.models.database import WikiPage, get_engine, get_session, init_db

DIM = 1024


class FakeEmbeddingService:
    """固定 1024 维向量的假服务,记录收到的文本与调用次数。"""

    def __init__(self, fail_texts=None, empty_texts=None):
        self.fail_texts = set(fail_texts or [])
        self.empty_texts = set(empty_texts or [])
        self.calls = []
        self.closed = False

    async def encode_batch(self, texts, batch_size=32):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if t in self.fail_texts:
                raise RuntimeError("fake embed failure")
            out.append([] if t in self.empty_texts else [0.1] * DIM)
        return out

    async def close(self):
        self.closed = True


@pytest.fixture
def engine(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'embed.db'}")
    init_db(eng)
    yield eng
    eng.dispose()


def _seed(engine, rows):
    db = get_session(engine)
    try:
        for i, (title, summary) in enumerate(rows):
            db.add(WikiPage(id=f"w{i}", title=title, summary=summary, content="x"))
        db.commit()
    finally:
        db.close()


def _embeddings(engine):
    db = get_session(engine)
    try:
        return {p.id: p.embedding for p in db.query(WikiPage).all()}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_embeds_pages_missing_vectors(engine):
    _seed(engine, [("标题A", "摘要A"), ("标题B", "摘要B")])
    fake = FakeEmbeddingService()

    stats = await embed_wiki_pages(engine, embedding_svc=fake)

    assert stats == {"embedded": 2, "errors": 0, "total": 2}
    stored = _embeddings(engine)
    assert all(v is not None for v in stored.values())
    assert len(json.loads(stored["w0"])) == DIM
    assert fake.closed is False


@pytest.mark.asyncio
async def test_embedded_text_is_title_newline_summary(engine):
    _seed(engine, [("标题A", "摘要A")])
    fake = FakeEmbeddingService()

    await embed_wiki_pages(engine, embedding_svc=fake)

    assert fake.calls == [["标题A\n摘要A"]]


@pytest.mark.asyncio
async def test_second_run_is_idempotent(engine):
    _seed(engine, [("标题A", "摘要A"), ("标题B", "摘要B")])
    fake = FakeEmbeddingService()

    await embed_wiki_pages(engine, embedding_svc=fake)
    stats = await embed_wiki_pages(engine, embedding_svc=fake)

    assert stats["embedded"] == 0
    assert stats["total"] == 0
    assert len(fake.calls) == 1  # 第二次没有需要补向量的页面,不再调用嵌入


@pytest.mark.asyncio
async def test_page_ids_filters_targets(engine):
    _seed(engine, [("标题A", "摘要A"), ("标题B", "摘要B"), ("标题C", "摘要C")])
    fake = FakeEmbeddingService()

    stats = await embed_wiki_pages(engine, page_ids=["w1"], embedding_svc=fake)

    assert stats == {"embedded": 1, "errors": 0, "total": 1}
    stored = _embeddings(engine)
    assert stored["w1"] is not None
    assert stored["w0"] is None and stored["w2"] is None


@pytest.mark.asyncio
async def test_empty_and_failed_vectors_counted_without_aborting(engine):
    _seed(engine, [("正常", "s"), ("失败", "s"), ("空", "s"), ("正常2", "s")])
    fake = FakeEmbeddingService(fail_texts={"失败\ns"}, empty_texts={"空\ns"})

    # 每页一个批次,单页失败/空向量只影响自己
    stats = await embed_wiki_pages(engine, embedding_svc=fake, batch_size=1)

    assert stats == {"embedded": 2, "errors": 2, "total": 4}
    db = get_session(engine)
    try:
        rows = db.query(WikiPage).all()
    finally:
        db.close()
    with_vec = {r.title for r in rows if r.embedding is not None}
    assert with_vec == {"正常", "正常2"}
