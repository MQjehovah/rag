import httpx
import json
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from app.config import settings
from app.core.llm import call_llm_json
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

    def split_text_structured(self, content: str, title: str = "") -> List[Dict[str, str]]:
        """Split markdown by headings, keeping the heading chain as context.

        Returns a list of {text, context}; every chunk knows which document and
        which section it belongs to, so retrieval can carry that context.
        """
        lines = (content or "").splitlines()
        sections: List[Dict[str, Any]] = []
        chain: List[str] = []
        buf: List[str] = []

        def flush():
            if not buf:
                return
            text = "\n".join(buf).strip()
            if text:
                sections.append({"chain": list(chain), "text": text})
            buf.clear()

        for line in lines:
            m = re.match(r'^(#{1,6})\s+(.*)$', line.strip())
            if m:
                flush()
                level = len(m.group(1))
                heading = m.group(2).strip()
                chain = chain[:level - 1] + [heading]
            else:
                buf.append(line)
        flush()

        if not sections and (content or "").strip():
            sections.append({"chain": [], "text": (content or "").strip()})

        chunks = []
        for sec in sections:
            context = " > ".join(sec["chain"])
            if title:
                context = f"{title} > {context}" if context else title
            for piece in self.splitter.split_text(sec["text"]):
                if piece.strip():
                    chunks.append({"text": piece.strip(), "context": context})
        return chunks

    async def _enrich_contexts(
        self,
        units: List[Dict[str, str]],
        title: str,
        content: str,
    ) -> List[Dict[str, str]]:
        """Optionally ask the LLM for a short context per chunk (max 10)."""
        enriched = []
        limit = 10
        for i, unit in enumerate(units[:limit]):
            prompt = (
                "你是文档分块助手。根据整篇文档，为下面的分块生成一句不超过50字的中文上下文描述，"
                "说明它在文档中的位置和主题，方便检索时理解该块的背景。\n\n"
                f"文档标题: {title or '无'}\n\n"
                f"文档内容: {content[:6000]}\n\n"
                f"分块内容:\n{unit['text'][:1000]}\n\n"
                '只返回 JSON: {"context": "..."}'
            )
            result = await call_llm_json(
                [{"role": "user", "content": prompt}],
                context="chunk-context",
            )
            ctx = (result.get("context") or "").strip()
            if ctx:
                unit = {"text": unit["text"], "context": f"{unit['context']}\n{ctx}".strip()}
            enriched.append(unit)
        enriched.extend(units[limit:])
        return enriched

    async def encode_chunks(
        self,
        content: str,
        title: str = "",
        enrich_context: bool = True,
    ) -> List[Tuple[str, List[float], Optional[str]]]:
        """Encode chunks with structure/context-aware text.

        Returns (chunk_text, embedding, context).  The embedding is computed
        over ``context + chunk`` so recall benefits from surrounding context.
        """
        units = self.split_text_structured(content, title)
        if not units:
            return []
        if enrich_context and settings.contextual_retrieval_enabled and settings.llm_api_url:
            units = await self._enrich_contexts(units, title, content)

        results = []
        for unit in units:
            chunk_text = unit["text"]
            ctx = unit.get("context") or ""
            embed_text = f"{ctx}\n\n{chunk_text}" if ctx else chunk_text
            try:
                emb = await self.encode(embed_text)
                if emb:
                    results.append((chunk_text, emb, ctx))
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

    async def add_page_chunks(
        self,
        page_id: str,
        chunks: List[Tuple[str, List[float], Optional[str]]],
    ):
        self.delete_page_chunks(page_id)

        dialect = self.db.bind.dialect.name
        has_vector_col = dialect == "postgresql"

        for i, item in enumerate(chunks):
            if len(item) == 2:
                chunk_text, embedding, context = item[0], item[1], None
            else:
                chunk_text, embedding, context = item[0], item[1], item[2]
            import uuid
            chunk_id = str(uuid.uuid4())
            emb_json = json.dumps(embedding)
            emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

            if has_vector_col:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding, context, embedding_vec) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :embedding, :context, :embedding_vec::vector)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        "embedding": emb_json,
                        "context": context,
                        "embedding_vec": emb_str,
                    }
                )
            else:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding, context) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :embedding, :context)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        "embedding": emb_json,
                        "context": context,
                    }
                )
        self.db.flush()

    def delete_page_chunks(self, page_id: str):
        self.db.execute(
            text("DELETE FROM page_chunks WHERE page_id = :page_id"),
            {"page_id": page_id}
        )
        self.db.flush()

    def _search_sync(
        self,
        query_embedding: List[float],
        top_k: int = 50,
        visible_page_ids=None,
    ) -> List[Dict[str, Any]]:
        emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        dialect = self.db.bind.dialect.name

        if dialect == "postgresql":
            try:
                params: Dict[str, Any] = {"query_emb": emb_str, "limit": top_k}
                where_sql = ""
                if visible_page_ids:
                    ids = list(visible_page_ids)
                    placeholders = ",".join(f":vid{i}" for i in range(len(ids)))
                    params.update({f"vid{i}": pid for i, pid in enumerate(ids)})
                    where_sql = f"WHERE pc.page_id IN ({placeholders})"
                result = self.db.execute(text(
                    f"SELECT pc.page_id, pc.content, pc.context, pc.chunk_index, "
                    f"pc.embedding_vec <=> :query_emb::vector AS distance "
                    f"FROM page_chunks pc {where_sql} "
                    f"ORDER BY pc.embedding_vec <=> :query_emb::vector "
                    f"LIMIT :limit"
                ), params)

                rows = result.fetchall()
                results = []
                for row in rows:
                    results.append({
                        "page_id": row[0],
                        "content": row[1],
                        "context": row[2],
                        "chunk_index": row[3],
                        "distance": float(row[4]),
                    })
                return results
            except Exception as e:
                logger.warning(f"pgvector search failed, falling back: {e}")

        if visible_page_ids:
            ids = list(visible_page_ids)
            rows_all = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join(f":vid{j}" for j in range(len(chunk)))
                result = self.db.execute(
                    text(
                        f"SELECT id, page_id, content, context, chunk_index, embedding "
                        f"FROM page_chunks WHERE page_id IN ({placeholders})"
                    ),
                    {f"vid{j}": pid for j, pid in enumerate(chunk)},
                )
                rows_all.extend(result.fetchall())
            rows = rows_all
        else:
            result = self.db.execute(
                text("SELECT id, page_id, content, context, chunk_index, embedding FROM page_chunks")
            )
            rows = result.fetchall()

        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        candidates = []
        for row in rows:
            try:
                emb = json.loads(row[5]) if row[5] else None
                if emb:
                    vec = np.array(emb)
                    vec_norm = np.linalg.norm(vec)
                    if vec_norm > 0:
                        sim = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
                        dist = 1.0 - sim
                    candidates.append({
                        "page_id": row[1],
                        "content": row[2],
                        "context": row[3],
                        "chunk_index": row[4],
                        "distance": dist,
                    })
            except Exception:
                continue

        candidates.sort(key=lambda x: x["distance"])
        return candidates[:top_k]

    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 50,
        visible_page_ids=None,
    ) -> List[Dict[str, Any]]:
        # The SQLite fallback scans every chunk and runs numpy similarity,
        # which can take seconds on a large corpus.  Run it in a thread so the
        # event loop is not blocked while note pages are being loaded.
        import asyncio
        return await asyncio.to_thread(self._search_sync, query_embedding, top_k, visible_page_ids)

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
