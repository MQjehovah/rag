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


def settings_spec() -> Dict[str, Any]:
    """来自 .env 的兜底嵌入档案(id=None, 代表"旧/环境默认"这一档)。"""
    kind = "ollama" if "/api/embed" in (settings.embedding_api_url or "") else "openai"
    return {
        "id": None,
        "name": "环境默认",
        "kind": kind,
        "api_url": settings.embedding_api_url,
        "api_key": settings.llm_api_key or "",
        "model": settings.embedding_model,
        "dimensions": int(settings.embedding_dimensions or 1024),
    }


def _profile_spec(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind or "openai",
        "api_url": row.api_url,
        "api_key": row.api_key or "",
        "model": row.model,
        "dimensions": int(row.dimensions or 1024),
    }


def resolve_embedding_spec(db, profile_id: Optional[str] = None) -> Dict[str, Any]:
    """解析嵌入档案：指定 id 优先，否则库内默认档案，最后回退 .env。"""
    if db is not None:
        try:
            from app.models.database import EmbeddingProfile
            row = None
            if profile_id:
                row = db.query(EmbeddingProfile).filter(EmbeddingProfile.id == profile_id).first()
            if row is None:
                row = db.query(EmbeddingProfile).filter(EmbeddingProfile.is_default.is_(True)).first()
            if row is not None:
                return _profile_spec(row)
        except Exception as e:  # noqa: BLE001
            logger.warning("解析嵌入档案失败,回退环境默认: %s", e)
    return settings_spec()


def profile_id_of(spec: Optional[Dict[str, Any]]) -> Optional[str]:
    return (spec or {}).get("id")


def notebook_profile_id(db, notebook_id: Optional[str]) -> Optional[str]:
    """笔记本指定的嵌入档案 id(未指定返回 None=用默认)。"""
    if not notebook_id or db is None:
        return None
    try:
        from app.models.database import Notebook
        nb = db.query(Notebook).filter(Notebook.id == notebook_id).first()
        return nb.embedding_profile_id if nb else None
    except Exception as e:  # noqa: BLE001
        logger.warning("解析笔记本嵌入档案失败: %s", e)
        return None


def embedding_spec_for_notebook(db, notebook_id: Optional[str]) -> Dict[str, Any]:
    return resolve_embedding_spec(db, notebook_profile_id(db, notebook_id))


class EmbeddingService:
    def __init__(self, spec: Optional[Dict[str, Any]] = None):
        self.spec = spec or settings_spec()
        self.client = httpx.AsyncClient(timeout=60.0)
        self.model = self.spec.get("model") or settings.embedding_model
        self.api_url = self.spec.get("api_url") or settings.embedding_api_url
        self.api_key = self.spec.get("api_key") or ""
        self.profile_id = self.spec.get("id")
        self.dimensions = int(self.spec.get("dimensions") or 1024)
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["##", "#", "\n\n", "\n", " ", ""]
        )

    @classmethod
    def from_db(cls, db, profile_id: Optional[str] = None) -> "EmbeddingService":
        return cls(resolve_embedding_spec(db, profile_id))

    @property
    def _is_ollama(self) -> bool:
        # 以实际 api_url 为准(spec.kind 仅作档案元数据)
        return "/api/embed" in self.api_url

    @property
    def _headers(self) -> Dict[str, str]:
        """服务端到网关的鉴权头。

        走公司 LLM 网关时必须在请求上带 Bearer,否则网关 401(此前缺失导致
        检索查询向量为空、答案无参考来源);Ollama 等本地服务无需鉴权,
        LLM_API_KEY 为空时不加头。
        """
        key = (self.api_key or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def encode(self, text: str) -> List[float]:
        if self._is_ollama:
            payload = {"input": text, "model": self.model}
            response = await self.client.post(self.api_url, json=payload, headers=self._headers)
            response.raise_for_status()
            data = response.json()
            return data.get("embeddings", [[]])[0]
        else:
            payload = {"input": text, "model": self.model}
            response = await self.client.post(self.api_url, json=payload, headers=self._headers)
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
                    response = await self.client.post(self.api_url, json=payload, headers=self._headers)
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
                    response = await self.client.post(self.api_url, json=payload, headers=self._headers)
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

        # Batch the embedding calls (32 at a time) instead of one HTTP request
        # per chunk: reindexing a large corpus otherwise takes tens of
        # thousands of sequential round-trips to the embedding service.
        embed_inputs = []
        unit_refs = []
        for unit in units:
            chunk_text = unit["text"]
            ctx = unit.get("context") or ""
            embed_text = f"{ctx}\n\n{chunk_text}" if ctx else chunk_text
            embed_inputs.append(embed_text)
            unit_refs.append((chunk_text, ctx))

        results = []
        batch_size = 32
        for start in range(0, len(embed_inputs), batch_size):
            batch = embed_inputs[start:start + batch_size]
            embs = await self.encode_batch(batch)
            for (chunk_text, ctx), emb in zip(unit_refs[start:start + batch_size], embs):
                if emb:
                    results.append((chunk_text, emb, ctx))
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
        profile_id: Optional[str] = None,
    ):
        self.delete_page_chunks(page_id)

        dialect = self.db.bind.dialect.name

        for i, item in enumerate(chunks):
            if len(item) == 2:
                chunk_text, embedding, context = item[0], item[1], None
            else:
                chunk_text, embedding, context = item[0], item[1], item[2]
            import uuid
            chunk_id = str(uuid.uuid4())
            emb_json = json.dumps(embedding)
            # pgvector 列固定 vector(1024)：仅当该档案维度为 1024 时才写向量列，
            # 其它维度只存 JSON(检索走 numpy 精确比对)。
            use_vec = dialect == "postgresql" and len(embedding) == 1024

            if use_vec:
                emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding, context, embedding_profile, embedding_vec) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :embedding, :context, :profile, CAST(:embedding_vec AS vector))"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        "embedding": emb_json,
                        "context": context,
                        "profile": profile_id,
                        "embedding_vec": emb_str,
                    }
                )
            else:
                self.db.execute(
                    text(
                        "INSERT INTO page_chunks (id, page_id, chunk_index, content, embedding, context, embedding_profile) "
                        "VALUES (:id, :page_id, :chunk_index, :content, :embedding, :context, :profile)"
                    ),
                    {
                        "id": chunk_id,
                        "page_id": page_id,
                        "chunk_index": i,
                        "content": chunk_text,
                        "embedding": emb_json,
                        "context": context,
                        "profile": profile_id,
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
        profile_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # 查询向量为空(embedding 服务不可用/鉴权失败)时直接跳过向量路:
        # 既避免 CAST('[]' AS vector) 报错,也避免该报错污染事务导致
        # 后续 BM25/实体扩展全部 InFailedSqlTransaction。
        if not query_embedding:
            logger.warning("查询向量为空(embedding 不可用?),跳过向量检索,仅用 BM25")
            return []

        dialect = self.db.bind.dialect.name
        # 档案过滤：不同嵌入模型的向量互不可比,只比对同一档案(旧数据 profile 为 NULL)
        profile_cond = "pc.embedding_profile = :profile" if profile_id else "pc.embedding_profile IS NULL"
        plain_cond = "embedding_profile = :profile" if profile_id else "embedding_profile IS NULL"

        if dialect == "postgresql" and len(query_embedding) == 1024:
            try:
                emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
                params: Dict[str, Any] = {"query_emb": emb_str, "limit": top_k}
                conds = ["pc.embedding_vec IS NOT NULL", profile_cond]
                if profile_id:
                    params["profile"] = profile_id
                if visible_page_ids:
                    ids = list(visible_page_ids)
                    placeholders = ",".join(f":vid{i}" for i in range(len(ids)))
                    params.update({f"vid{i}": pid for i, pid in enumerate(ids)})
                    conds.append(f"pc.page_id IN ({placeholders})")
                where_sql = "WHERE " + " AND ".join(conds)
                result = self.db.execute(text(
                    f"SELECT pc.page_id, pc.content, pc.context, pc.chunk_index, "
                    f"pc.embedding_vec <=> CAST(:query_emb AS vector) AS distance "
                    f"FROM page_chunks pc {where_sql} "
                    f"ORDER BY pc.embedding_vec <=> CAST(:query_emb AS vector) "
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
                # 关键:失败后回滚被污染的事务,否则同一 session 里的
                # BM25/实体等后续查询会全部报 InFailedSqlTransaction
                try:
                    self.db.rollback()
                except Exception:
                    logger.exception("回滚失败后仍继续降级")

        profile_params: Dict[str, Any] = {}
        if profile_id:
            profile_params["profile"] = profile_id
        if visible_page_ids:
            ids = list(visible_page_ids)
            rows_all = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join(f":vid{j}" for j in range(len(chunk)))
                result = self.db.execute(
                    text(
                        f"SELECT id, page_id, content, context, chunk_index, embedding "
                        f"FROM page_chunks WHERE page_id IN ({placeholders}) AND {plain_cond}"
                    ),
                    {**{f"vid{j}": pid for j, pid in enumerate(chunk)}, **profile_params},
                )
                rows_all.extend(result.fetchall())
            rows = rows_all
        else:
            result = self.db.execute(
                text("SELECT id, page_id, content, context, chunk_index, embedding FROM page_chunks WHERE " + plain_cond),
                profile_params,
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
        profile_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # The SQLite fallback scans every chunk and runs numpy similarity,
        # which can take seconds on a large corpus.  Run it in a thread so the
        # event loop is not blocked while note pages are being loaded.
        import asyncio
        return await asyncio.to_thread(self._search_sync, query_embedding, top_k, visible_page_ids, profile_id)

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
