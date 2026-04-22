import httpx
import json
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from app.config import settings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

try:
    import jieba
    import jieba.analyse
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


class EmbeddingService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self.model = settings.embedding_model
        self.api_url = settings.embedding_api_url
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["##", "#", "\n\n", "\n", " ", ""]
        )

    async def encode(self, text: str) -> List[float]:
        payload = {"input": text, "model": self.model}
        response = await self.client.post(self.api_url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("data", [{}])[0].get("embedding", [])

    async def encode_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            payload = {"input": batch, "model": self.model}
            try:
                response = await self.client.post(self.api_url, json=payload)
                response.raise_for_status()
                data = response.json()
                embeddings = [d.get("embedding", []) for d in data.get("data", [])]
                results.extend(embeddings)
            except Exception as e:
                logger.error(f"Batch encode error: {e}")
                for _ in batch:
                    results.append([])
        return results

    def split_text(self, content: str, title: str = "") -> List[str]:
        full_text = f"# {title}\n\n{content}" if title else content
        chunks = self.splitter.split_text(full_text)
        return [chunk for chunk in chunks if chunk.strip()]

    async def encode_chunks(self, content: str, title: str = "") -> List[Tuple[str, List[float]]]:
        chunks = self.split_text(content, title)
        if not chunks:
            return []

        embeddings = await self.encode_batch(chunks)
        result = []
        for chunk_text, emb in zip(chunks, embeddings):
            if emb:
                result.append((chunk_text, emb))
        return result

    @staticmethod
    def extract_keywords(text: str, top_k: int = 15) -> set:
        if not text or not text.strip():
            return set()
        if JIEBA_AVAILABLE:
            tags = jieba.analyse.extract_tags(text, topK=top_k)
            return set(tags)
        words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text.lower())
        counter = Counter(words)
        return set(w for w, _ in counter.most_common(top_k))

    async def close(self):
        await self.client.aclose()


class VectorStore:
    def __init__(self, db: Session):
        self.db = db

    async def add_page_chunks(self, page_id: str, chunks: List[Tuple[str, List[float]]]):
        self.delete_page_chunks(page_id)

        for i, (chunk_text, embedding) in enumerate(chunks):
            import uuid
            chunk_id = str(uuid.uuid4())
            emb_json = json.dumps(embedding)
            emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

            self.db.execute(
                text(
                    "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding, embedding_vec) "
                    "VALUES (:id, :page_id, :chunk_index, :content, :embedding, :embedding_vec::vector)"
                ),
                {
                    "id": chunk_id,
                    "page_id": page_id,
                    "chunk_index": i,
                    "content": chunk_text,
                    "embedding": emb_json,
                    "embedding_vec": emb_str,
                }
            )
        self.db.flush()

    def delete_page_chunks(self, page_id: str):
        self.db.execute(
            text("DELETE FROM page_chunks WHERE page_id = :page_id"),
            {"page_id": page_id}
        )
        self.db.flush()

    async def search(self, query_embedding: List[float], top_k: int = 50) -> List[Dict[str, Any]]:
        emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        dialect = self.db.bind.dialect.name

        if dialect == "postgresql":
            try:
                result = self.db.execute(text(
                    "SELECT pc.page_id, pc.content, pc.chunk_index, "
                    "pc.embedding_vec <=> :query_emb::vector AS distance "
                    "FROM page_chunks pc "
                    "ORDER BY pc.embedding_vec <=> :query_emb::vector "
                    "LIMIT :limit"
                ), {"query_emb": emb_str, "limit": top_k})

                rows = result.fetchall()
                results = []
                for row in rows:
                    results.append({
                        "page_id": row[0],
                        "content": row[1],
                        "chunk_index": row[2],
                        "distance": float(row[3]),
                    })
                return results
            except Exception as e:
                logger.warning(f"pgvector search failed, falling back: {e}")

        result = self.db.execute(
            text("SELECT id, page_id, content, chunk_index, embedding FROM page_chunks")
        )
        rows = result.fetchall()

        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        candidates = []
        for row in rows:
            try:
                emb = json.loads(row[4]) if row[4] else None
                if emb:
                    vec = np.array(emb)
                    vec_norm = np.linalg.norm(vec)
                    if vec_norm > 0:
                        sim = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
                        dist = 1.0 - sim
                        candidates.append({
                            "page_id": row[1],
                            "content": row[2],
                            "chunk_index": row[3],
                            "distance": dist,
                        })
            except Exception:
                continue

        candidates.sort(key=lambda x: x["distance"])
        return candidates[:top_k]

    async def get_chunk_count(self, page_id: str = None) -> int:
        if page_id:
            result = self.db.execute(
                text("SELECT COUNT(*) FROM page_chunks WHERE page_id = :pid"),
                {"pid": page_id}
            )
        else:
            result = self.db.execute(text("SELECT COUNT(*) FROM page_chunks"))
        return result.scalar()


class RerankerService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.api_url = settings.reranker_api_url
        self.model = settings.reranker_model

    async def rerank(self, query: str, documents: List[str], top_k: int = None) -> List[Dict[str, Any]]:
        if not documents:
            return []

        if not self.api_url:
            return [{"index": i, "relevance_score": 1.0} for i in range(len(documents))]

        try:
            payload = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_k": top_k or len(documents),
            }
            response = await self.client.post(self.api_url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except Exception as e:
            logger.warning(f"Reranker call failed: {e}")
            return [{"index": i, "relevance_score": 1.0} for i in range(len(documents))]

    async def close(self):
        await self.client.aclose()
