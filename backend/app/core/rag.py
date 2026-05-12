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

    @property
    def _is_ollama(self) -> bool:
        return "/api/embed" in self.api_url

    async def encode(self, text: str) -> List[float]:
        if self._is_ollama:
            payload = {"input": text, "model": self.model}
            response = await self.client.post(self.api_url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("embeddings", [[]])[0]
        else:
            payload = {"input": text, "model": self.model}
            response = await self.client.post(self.api_url, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("data", [{}])[0].get("embedding", [])

    async def encode_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        if self._is_ollama:
            results = []
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i + batch_size]
                payload = {"input": batch, "model": self.model}
                try:
                    response = await self.client.post(self.api_url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    results.extend(data.get("embeddings", []))
                except Exception as e:
                    logger.error(f"Batch encode error: {e}")
                    for _ in batch:
                        results.append([])
            return results
        else:
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

        results = []
        for chunk_text in chunks:
            try:
                emb = await self.encode(chunk_text)
                if emb:
                    results.append((chunk_text, emb))
            except Exception as e:
                logger.error(f"Encode chunk error: {e}")
        return results

    STOP_WORDS = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "her", "was", "one", "our", "out", "has", "have", "from", "been",
        "some", "them", "than", "its", "over", "such", "that", "with", "will",
        "this", "each", "make", "like", "into", "many", "then", "they",
        "what", "about", "which", "their", "would", "there", "could",
        "other", "after", "first", "well", "also", "back", "class", "void",
        "public", "static", "return", "final", "import", "null", "true",
        "false", "override", "system", "error", "info", "warn", "debug",
        "trace", "long", "int", "string", "bool", "float", "double",
        "item", "list", "map", "set", "get", "put", "add", "new", "del",
        "self", "def", "func", "var", "let", "const", "log", "timestamp",
        "description", "name", "value", "key", "data", "result", "content",
        "type", "text", "field", "table", "column", "row", "index",
        "create", "update", "delete", "select", "insert", "default",
        "com", "org", "http", "https", "www", "png", "jpg", "svg", "img",
        "src", "href", "div", "span", "class", "style", "width", "height",
        "padding", "margin", "border", "color", "font", "size", "align",
        "aliyuncs", "zhangjiakou", "img", "image", "aliyun", "oss",
        "void", "class", "override", "public", "private", "protected",
        "system", "out", "println", "string", "integer", "boolean",
        "datetime", "varchar", "bigint", "float", "double", "text",
        "create", "update", "summary", "operation", "timestamp",
        "postmapping", "validated", "user", "users", "userservice",
        "logback", "mdc", "pattern", "response", "filter", "boot",
        "spring", "bean", "config", "component", "service", "controller",
        "repository", "entity", "mapper", "dto", "vo", "pojo",
        "xxx", "aaa", "bbb", "ccc", "ddd", "eee", "fff", "ggg",
        "res", "req", "resp", "ctx", "ctx", "cfg", "env", "tmp",
        "pause", "echo", "bash", "logs", "opt", "upload", "download",
        "zip", "tar", "gz", "file", "files", "path", "dir", "mkdir",
        "clean", "test", "main", "app", "run", "start", "stop",
    }

    @staticmethod
    def extract_keywords(text: str, top_k: int = 15, fine_grained: bool = False) -> set:
        if not text or not text.strip():
            return set()
        import re as _re
        cleaned = _re.sub(r'```[\s\S]*?```', '', text)
        cleaned = _re.sub(r'`[^`]*`', '', cleaned)
        cleaned = _re.sub(r'https?://\S+', '', cleaned)
        cleaned = _re.sub(r'[^\u4e00-\u9fff\w]', ' ', cleaned)
        if JIEBA_AVAILABLE:
            import jieba.analyse
            import jieba as _jieba
            tags = jieba.analyse.extract_tags(cleaned, topK=top_k * 3, withWeight=True)
            result = set()
            for tag, weight in tags:
                if tag.lower() not in EmbeddingService.STOP_WORDS and len(tag) >= 2:
                    result.add(tag)
                if len(result) >= top_k:
                    break
            if fine_grained:
                seg_words = _jieba.lcut(text)
                for w in seg_words:
                    w = w.strip()
                    if len(w) >= 2 and w.lower() not in EmbeddingService.STOP_WORDS:
                        if _re.match(r'[\u4e00-\u9fff]+', w) or _re.match(r'[a-zA-Z]{3,}', w):
                            result.add(w)
                for kw in list(result):
                    if len(kw) >= 4 and _re.match(r'[\u4e00-\u9fff]+', kw):
                        sub_words = _jieba.lcut(kw)
                        for sw in sub_words:
                            if len(sw) >= 2 and sw != kw and sw.lower() not in EmbeddingService.STOP_WORDS:
                                result.add(sw)
            return result
        words = _re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{4,}', cleaned.lower())
        counter = Counter(w for w in words if w not in EmbeddingService.STOP_WORDS)
        return set(w for w, _ in counter.most_common(top_k))

    async def close(self):
        await self.client.aclose()


class VectorStore:
    def __init__(self, db: Session):
        self.db = db

    async def add_page_chunks(self, page_id: str, chunks: List[Tuple[str, List[float]]]):
        self.delete_page_chunks(page_id)

        dialect = self.db.bind.dialect.name
        has_vector_col = dialect == "postgresql"

        for i, (chunk_text, embedding) in enumerate(chunks):
            import uuid
            chunk_id = str(uuid.uuid4())
            emb_json = json.dumps(embedding)
            emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

            if has_vector_col:
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
            else:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :embedding)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        "embedding": emb_json,
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
