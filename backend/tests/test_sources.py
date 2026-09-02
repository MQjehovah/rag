"""T6.5 检索适配器：retrieve_pages 复用 VectorStore。旧 KO 召回已删除。"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.retrieval import retrieve_pages
from app.models.database import Base, Page, PageChunk


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


class FakeEmbedding:
    async def encode(self, text: str):
        digest = hashlib.sha1(text.encode("utf-8")).digest()
        return [int(b) / 255.0 for b in digest[:32]]


def _embed_vec(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return json.dumps([int(b) / 255.0 for b in digest[:32]])


@pytest.fixture
def fake_emb():
    return FakeEmbedding()


@pytest.mark.asyncio
async def test_retrieve_pages_reuses_vector_store(db, fake_emb):
    page = Page(id=str(uuid.uuid4()), title="Titan 部署手册", content="部署相关内容")
    db.add(page)
    db.flush()
    db.add(PageChunk(id=str(uuid.uuid4()), page_id=page.id, chunk_index=0,
                     content="部署相关内容", embedding=_embed_vec("部署相关内容")))
    db.commit()

    results = await retrieve_pages(db, question="部署相关内容", top_k=5,
                                   embedding_service=fake_emb)
    assert len(results) >= 1
    assert results[0]["page_id"] == page.id


@pytest.mark.asyncio
async def test_retrieve_pages_requires_embedding_service(db):
    with pytest.raises(ValueError):
        await retrieve_pages(db, question="x", top_k=5)
