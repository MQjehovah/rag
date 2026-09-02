import asyncio
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


def _is_memory_db(db: Session) -> bool:
    """判断 Session 绑定的引擎是否为内存 SQLite（测试场景）。

    文件型库（生产）to_thread 安全：get_engine 已设 check_same_thread=False；
    内存库（SingletonThreadPool）连接绑定创建线程，跨线程使用会报错或拿到空库。
    """
    if db.bind is None:
        return False
    url = db.bind.url
    if url.database in (None, "", ":memory:"):
        return True
    return "mode=memory" in str(url)

try:
    import jieba
    import jieba.analyse
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


class EmbeddingService:
    PDF_PAGE_HEADING = re.compile(r"(?m)^## 第 \d+ 页(?: - [^\n]+)?$")

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self.model = settings.embedding_model
        self.api_url = settings.embedding_api_url
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["\n## ", "\n# ", "\n\n", "\n", " ", ""]
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

    async def encode_with_status(self, text: str) -> "EmbeddingBatchResult":
        """带状态编码（P13-MODEL-02）：返回 EmbeddingBatchResult。

        上层据此明确知道 Dense 路是否真正执行、是否降级、错误原因。
        失败不抛异常，返回 used=False + error_code。
        """
        from app.core.embedding.results import EmbeddingBatchResult, classify_http_error

        start = __import__("time").monotonic()
        try:
            if self._is_ollama:
                payload = {"input": text, "model": self.model}
                response = await self.client.post(self.api_url, json=payload)
            else:
                payload = {"input": text, "model": self.model}
                response = await self.client.post(self.api_url, json=payload)
            if response.status_code != 200:
                error_code = classify_http_error(response.status_code, response.text)
                return EmbeddingBatchResult(
                    used=False, degraded=True, model_uid=self.model,
                    error_code=error_code, error_message=response.text[:200],
                    latency_ms=int((__import__("time").monotonic() - start) * 1000),
                )
            data = response.json()
            embedding = (
                data.get("embeddings", [[]])[0]
                if self._is_ollama
                else data.get("data", [{}])[0].get("embedding", [])
            )
            if not embedding:
                return EmbeddingBatchResult(
                    used=False, degraded=True, model_uid=self.model,
                    error_code="INVALID_RESPONSE", error_message="空向量",
                    latency_ms=int((__import__("time").monotonic() - start) * 1000),
                )
            return EmbeddingBatchResult(
                embeddings=[embedding], used=True, degraded=False,
                model_uid=self.model, dimensions=len(embedding),
                latency_ms=int((__import__("time").monotonic() - start) * 1000),
            )
        except httpx.ConnectError:
            return EmbeddingBatchResult(
                used=False, degraded=True, model_uid=self.model,
                error_code="NETWORK_UNREACHABLE", error_message="连接失败",
                latency_ms=int((__import__("time").monotonic() - start) * 1000),
            )
        except httpx.TimeoutException:
            return EmbeddingBatchResult(
                used=False, degraded=True, model_uid=self.model,
                error_code="TIMEOUT", error_message="超时",
                latency_ms=int((__import__("time").monotonic() - start) * 1000),
            )
        except Exception as exc:
            return EmbeddingBatchResult(
                used=False, degraded=True, model_uid=self.model,
                error_code="SERVER_ERROR", error_message=str(exc)[:200],
                latency_ms=int((__import__("time").monotonic() - start) * 1000),
            )

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

    @staticmethod
    def _merge_short_chunks(chunks: List[str]) -> List[str]:
        """合并上下文不足的短分块，避免产生无意义向量。"""
        cleaned = [chunk.strip() for chunk in chunks if chunk.strip()]
        if not cleaned:
            return []

        min_size = min(160, max(120, settings.chunk_size // 4))
        max_size = settings.chunk_size + settings.chunk_overlap
        merged: List[str] = []
        pending = ""

        for chunk in cleaned:
            if pending:
                combined = f"{pending}\n\n{chunk}"
                if len(combined) <= max_size:
                    chunk = combined
                    pending = ""
                elif merged and len(merged[-1]) + len(pending) + 2 <= max_size:
                    merged[-1] = f"{merged[-1]}\n\n{pending}"
                    pending = ""
                else:
                    merged.append(pending)
                    pending = ""

            if len(chunk) < min_size:
                if merged and len(merged[-1]) + len(chunk) + 2 <= max_size:
                    merged[-1] = f"{merged[-1]}\n\n{chunk}"
                else:
                    pending = chunk
            else:
                merged.append(chunk)

        if pending:
            if merged and len(merged[-1]) + len(pending) + 2 <= max_size:
                merged[-1] = f"{merged[-1]}\n\n{pending}"
            else:
                merged.append(pending)
        return merged

    @classmethod
    def _is_low_value_pdf_chunk(cls, chunk: str) -> bool:
        """过滤只有分页标题、空页提示或结束语的 PDF 分块。"""
        body = cls.PDF_PAGE_HEADING.sub("", chunk)
        body = body.replace("_本页未提取到除标题外的文字。_", "")
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", body).lower()
        return len(normalized) < 12 or normalized in {
            "thankyou",
            "robotdeployment",
        }

    def split_text(self, content: str, title: str = "") -> List[str]:
        # 合并单元格元数据仅供前端还原表格外观，不参与向量化。
        content = re.sub(
            r"(?m)^<!-- rag-table-merges: \{.*\} -->\s*$",
            "",
            content,
        )
        full_text = f"# {title}\n\n{content}" if title else content
        page_matches = list(self.PDF_PAGE_HEADING.finditer(full_text))
        if not page_matches:
            return self._merge_short_chunks(self.splitter.split_text(full_text))

        result: List[str] = []
        preamble = full_text[:page_matches[0].start()].strip()
        for index, match in enumerate(page_matches):
            section_end = (
                page_matches[index + 1].start()
                if index + 1 < len(page_matches)
                else len(full_text)
            )
            heading = match.group(0).strip()
            section = full_text[match.start():section_end].strip()
            if index == 0 and preamble:
                section = f"{preamble}\n\n{section}"

            page_chunks = self.splitter.split_text(section)
            contextual_chunks = [
                chunk.strip()
                if heading in chunk
                else f"{heading}\n\n{chunk.strip()}"
                for chunk in page_chunks
                if chunk.strip()
            ]
            result.extend(
                chunk
                for chunk in self._merge_short_chunks(contextual_chunks)
                if not self._is_low_value_pdf_chunk(chunk)
            )
        return result

    async def encode_chunks(self, content: str, title: str = "") -> List[Tuple[str, List[float]]]:
        chunks = self.split_text(content, title)
        if not chunks:
            return []

        embeddings = await self.encode_batch(chunks)
        return [
            (chunk_text, embedding)
            for chunk_text, embedding in zip(chunks, embeddings)
            if embedding
        ]

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

    @staticmethod
    def _chunk_metadata(chunk_text: str) -> Dict[str, Any]:
        page_match = re.search(r"(?m)^## 第 (\d+) 页", chunk_text)
        page_number = int(page_match.group(1)) if page_match else None
        image_markers = (
            "### 本页重要图片",
            "### 本页重要业务图片",
            "**图片功能：**",
            "**图片操作顺序：**",
            "**标号与箭头对应关系：**",
            "**区域与位置关系：**",
            "**图片核对提示：**",
        )
        if any(marker in chunk_text for marker in image_markers):
            content_type = "image_caption"
        elif "### 本页识别表格" in chunk_text or re.search(
            r"(?m)^\|.+\|\s*$", chunk_text
        ):
            content_type = "table"
        else:
            content_type = "text"
        image_match = re.search(
            r"/api/upload/pdf-pages/[0-9a-f]{64}/(page-\d+(?:-image-\d+)?\.jpg)",
            chunk_text,
        )
        return {
            "content_type": content_type,
            "page_number": page_number,
            "image_id": image_match.group(1) if image_match else None,
        }

    async def add_page_chunks(self, page_id: str, chunks: List[Tuple[str, List[float]]]):
        self.delete_page_chunks(page_id)

        dialect = self.db.bind.dialect.name
        has_vector_col = dialect == "postgresql"

        for i, (chunk_text, embedding) in enumerate(chunks):
            import uuid
            chunk_id = str(uuid.uuid4())
            emb_json = json.dumps(embedding)
            emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
            metadata = self._chunk_metadata(chunk_text)

            if has_vector_col:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks "
                        "(id, page_id, chunk_index, content, content_type, page_number, image_id, embedding, embedding_vec) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :content_type, :page_number, :image_id, :embedding, :embedding_vec::vector)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        **metadata,
                        "embedding": emb_json,
                        "embedding_vec": emb_str,
                    }
                )
            else:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks "
                        "(id, page_id, chunk_index, content, content_type, page_number, image_id, embedding) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :content_type, :page_number, :image_id, :embedding)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        **metadata,
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

    def _search_sync(self, query_embedding: List[float], top_k: int = 50) -> List[Dict[str, Any]]:
        """同步全表扫描实现（P0-BE-04 保留的原始实现，回滚 = 直接调用本函数）。"""
        emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        dialect = self.db.bind.dialect.name

        if dialect == "postgresql":
            try:
                result = self.db.execute(text(
                    "SELECT pc.page_id, pc.content, pc.chunk_index, pc.content_type, pc.page_number, pc.image_id, "
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
                        "content_type": row[3] or "text",
                        "page_number": row[4],
                        "image_id": row[5],
                        "distance": float(row[6]),
                    })
                return results
            except Exception as e:
                logger.warning(f"pgvector search failed, falling back: {e}")

        result = self.db.execute(
            text(
                "SELECT id, page_id, content, chunk_index, embedding, "
                "content_type, page_number, image_id FROM page_chunks"
            )
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
                            "content_type": row[5] or "text",
                            "page_number": row[6],
                            "image_id": row[7],
                            "distance": dist,
                        })
            except Exception:
                continue

        candidates.sort(key=lambda x: x["distance"])
        return candidates[:top_k]

    async def search(self, query_embedding: List[float], top_k: int = 50) -> List[Dict[str, Any]]:
        """P0-BE-04：SQLite 全表扫描放入工作线程，避免阻塞事件循环。

        内存库（测试）保持同步执行：SingletonThreadPool 连接绑定创建线程，
        to_thread 会因 check_same_thread 报错或拿到空库。
        回滚：settings.vector_search_threading_enabled=False 直接同步调用 _search_sync。
        """
        from app.config import settings as _settings
        dialect = self.db.bind.dialect.name if self.db.bind is not None else "sqlite"
        if dialect != "sqlite" or _is_memory_db(self.db) or not _settings.vector_search_threading_enabled:
            return self._search_sync(query_embedding, top_k)
        return await asyncio.to_thread(self._search_sync, query_embedding, top_k)

    def _get_chunk_count_sync(self, page_id: str = None) -> int:
        if page_id:
            result = self.db.execute(
                text("SELECT COUNT(*) FROM page_chunks WHERE page_id = :pid"),
                {"pid": page_id}
            )
        else:
            result = self.db.execute(text("SELECT COUNT(*) FROM page_chunks"))
        return result.scalar()

    async def get_chunk_count(self, page_id: str = None) -> int:
        """P0-BE-04：与 search 同策略（文件库放线程，内存库同步；开关可回滚）。"""
        from app.config import settings as _settings
        if not _settings.vector_search_threading_enabled or _is_memory_db(self.db):
            return self._get_chunk_count_sync(page_id)
        return await asyncio.to_thread(self._get_chunk_count_sync, page_id)


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
