"""P4-BE-02/03：BM25 测试。

覆盖：分词、BM25Index 检索排序、Chunk/CardBlock Retriever 接口、文档单位是 chunk。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.retrieval.bm25 import (
    BM25Index,
    ChunkBM25Retriever,
    _Doc,
    tokenize,
)
from app.core.retrieval.candidates import CandidateType
from app.models.database import (
    Page,
    PageChunk,
    get_engine,
    init_db,
)


def test_tokenize_chinese():
    tokens = tokenize("水箱安装步骤")
    assert len(tokens) >= 1
    assert any("水箱" in t or "安装" in t for t in tokens)


def test_tokenize_english():
    tokens = tokenize("Titan 810 battery")
    assert "titan" in tokens or "Titan" in tokens or "battery" in tokens


def test_bm25_index_search_ranking():
    docs = [
        _Doc("d1", "水箱安装固定", tokenize("水箱安装固定"), CandidateType.CHUNK),
        _Doc("d2", "电池容量参数", tokenize("电池容量参数"), CandidateType.CHUNK),
        _Doc("d3", "水箱容量是多少", tokenize("水箱容量是多少"), CandidateType.CHUNK),
    ]
    index = BM25Index()
    index.build(docs)
    results = index.search("水箱容量", top_k=10)
    assert results, "应有结果"
    # d3 同时含「水箱」「容量」，应排最前
    assert results[0][0].doc_id == "d3"


def test_bm25_index_empty_query():
    index = BM25Index()
    index.build([_Doc("d1", "内容", tokenize("内容"), CandidateType.CHUNK)])
    assert index.search("", 10) == []
    assert index.search("   ", 10) == []


@pytest.fixture()
def bm25_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'bm25.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    return url


def _session(url):
    engine = get_engine(url)
    return sessionmaker(bind=engine)()


def test_chunk_bm25_retriever(bm25_db):
    db = _session(bm25_db)
    page = Page(id="p1", title="t", content="c")
    db.add(page)
    db.flush()
    db.add(PageChunk(id="c1", page_id="p1", chunk_index=0, content="水箱安装固定步骤", content_type="text"))
    db.add(PageChunk(id="c2", page_id="p1", chunk_index=1, content="电池容量参数说明", content_type="text"))
    db.commit()

    retriever = ChunkBM25Retriever(db)
    results = retriever.retrieve(db, "水箱安装", top_k=10)
    db.close()

    assert results, "应有结果"
    assert results[0].candidate_type == CandidateType.CHUNK
    assert results[0].candidate_id == "c1"  # 水箱相关排前
    assert results[0].sparse_score is not None
    assert "chunk_bm25" in results[0].sources
