"""Chunk 级 / CardBlock 级 BM25（P4-BE-02/03，V3 计划 9.1）。

关键语义：文档单位是 chunk（page_chunks 行 / knowledge_card_blocks 行），
不是整篇 page——这样召回的是具体文本片段，而非整页，避免 Page 级实现
的粒度粗、引用定位不准。

纯 Python 内存倒排索引（数据量 5403 chunk，无需外部检索引擎）。
分词：jieba 可用则用 jieba，否则退化为空白/标点切分（英文可用）。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.retrieval.candidates import CandidateType, RetrievalCandidate

logger = logging.getLogger(__name__)

try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False

# BM25 参数
K1 = 1.5
B = 0.75

_INDEX_CACHE: dict[str, tuple[tuple, "BM25Index"]] = {}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")

_QUERY_SYNONYMS = {
    "部件": ("part", "parts", "component"),
    "清单": ("list", "manual"),
    "维修": ("repair", "maintenance"),
    "工具": ("tool", "tools"),
    "传感器": ("sensor", "sensors"),
    "工作站": ("workstation", "station"),
    "部署": ("deployment",),
    "手册": ("manual",),
    "电池": ("battery",),
    "容量": ("capacity",),
}


def tokenize(text: str) -> list[str]:
    """分词：中文用 jieba，英文/数字用正则切分。"""
    text = text or ""
    if _JIEBA_AVAILABLE:
        return [t for t in jieba.lcut(text) if t.strip()]
    return _TOKEN_RE.findall(text.lower())


@dataclass
class _Doc:
    doc_id: str
    content: str
    tokens: list[str]
    candidate_type: str
    parent_card_id: str | None = None
    source_page_id: str | None = None
    evidence_ids: list[str] | None = None


class BM25Index:
    """内存 BM25 索引。build(docs) 建索引，search(query) 返回 [(doc, score)]。"""

    def __init__(self):
        self.docs: list[_Doc] = []
        self.doc_freq: dict[str, int] = {}   # term → 出现该词的文档数
        self.avg_dl: float = 0.0
        self.idf: dict[str, float] = {}

    def build(self, docs: list[_Doc]) -> None:
        self.docs = docs
        self.doc_freq = {}
        total_len = 0
        for doc in docs:
            total_len += len(doc.tokens)
            for term in set(doc.tokens):
                self.doc_freq[term] = self.doc_freq.get(term, 0) + 1
        self.avg_dl = total_len / len(docs) if docs else 0.0
        n = len(docs)
        self.idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
            for term, freq in self.doc_freq.items()
        }

    def search(self, query: str, top_k: int = 20) -> list[tuple[_Doc, float]]:
        """返回 [(doc, score)] 按分数降序。"""
        query_tokens = tokenize(query)
        query_lower = (query or "").lower()
        for term, synonyms in _QUERY_SYNONYMS.items():
            if term in query_lower:
                query_tokens.extend(synonyms)
        if not query_tokens or not self.docs:
            return []
        scored: list[tuple[_Doc, float]] = []
        for doc in self.docs:
            score = 0.0
            dl = len(doc.tokens)
            for term in query_tokens:
                tf = doc.tokens.count(term)
                if tf == 0:
                    continue
                idf = self.idf.get(term, 0.0)
                denom = tf + K1 * (1 - B + B * dl / self.avg_dl) if self.avg_dl else 1.0
                score += idf * (tf * (K1 + 1)) / denom
            if score > 0:
                scored.append((doc, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


class ChunkBM25Retriever:
    """Chunk 级 BM25 检索（page_chunks）。实现 Retriever 接口。"""

    name = "chunk_bm25"

    def __init__(self, db: Session):
        self.db = db
        self._index: BM25Index | None = None

    def _build_index(self) -> BM25Index:
        from sqlalchemy import text

        signature = self.db.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(c.content)+LENGTH(COALESCE(p.title,''))), 0), "
            "COALESCE(MAX(c.id), '') FROM page_chunks c LEFT JOIN pages p ON p.id=c.page_id "
            "WHERE c.content != ''"
        )).fetchone()
        cache_key = f"{self.db.get_bind().url}:chunk"
        cached = _INDEX_CACHE.get(cache_key)
        if cached and cached[0] == tuple(signature):
            return cached[1]

        rows = self.db.execute(text(
            "SELECT c.id, c.content, c.content_type, c.page_id, c.image_id, p.title "
            "FROM page_chunks c LEFT JOIN pages p ON p.id=c.page_id WHERE c.content != ''"
        )).fetchall()
        docs = [
            _Doc(
                doc_id=row[0], content=row[1] or "",
                tokens=tokenize(f"{((row[5] or '') + ' ') * 4}{row[1] or ''}"),
                candidate_type=CandidateType.CHUNK,
                source_page_id=row[3],
                evidence_ids=[row[4]] if row[4] else None,
            )
            for row in rows
        ]
        index = BM25Index()
        index.build(docs)
        _INDEX_CACHE[cache_key] = (tuple(signature), index)
        return index

    def retrieve(self, db, question: str, *, top_k: int = 20) -> list[RetrievalCandidate]:
        if self._index is None:
            self._index = self._build_index()
        results = self._index.search(question, top_k)
        return [
            RetrievalCandidate(
                candidate_type=doc.candidate_type,
                candidate_id=doc.doc_id,
                content=doc.content,
                source_page_id=doc.source_page_id,
                evidence_ids=doc.evidence_ids or [],
                sparse_score=round(score, 4),
            ).with_source(self.name)
            for doc, score in results
        ]


