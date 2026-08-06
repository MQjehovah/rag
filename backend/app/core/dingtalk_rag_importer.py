import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.rag import EmbeddingService, VectorStore
from app.models.database import GraphEdge, Notebook, Page, PageChunk


class DingTalkRAGImporter:
    """将本地钉钉Markdown幂等导入现有RAG数据库。"""

    def __init__(
        self,
        db: Session,
        storage: DingTalkLocalStorage | None = None,
        embedding_service: EmbeddingService | None = None,
    ):
        self.db = db
        self.storage = storage or DingTalkLocalStorage()
        self.embedding_service = embedding_service or EmbeddingService()
        self.vector_store = VectorStore(db)

    @staticmethod
    def _strip_frontmatter(markdown: str) -> str:
        """索引正文时去掉同步元数据，避免路径和哈希干扰语义检索。"""
        if not markdown.startswith("---"):
            return markdown.strip()
        return re.sub(
            r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)",
            "",
            markdown,
            count=1,
            flags=re.DOTALL,
        ).strip()

    def _markdown_path(self, entry: Dict[str, Any]) -> Path:
        relative = str(entry.get("markdown_path") or "").strip()
        if not relative:
            raise ValueError("同步清单缺少markdown_path")
        path = (self.storage.root / relative).resolve()
        self.storage._assert_contained(path, self.storage.markdown_root)
        if not path.is_file():
            raise FileNotFoundError(f"本地Markdown不存在: {path}")
        return path

    @staticmethod
    def _source_id(entry: Dict[str, Any]) -> str:
        source_id = str(entry.get("document_id") or "").strip()
        if not source_id:
            raise ValueError("同步清单缺少钉钉文档ID")
        return source_id

    @staticmethod
    def _set_source_metadata(page: Page, entry: Dict[str, Any]) -> None:
        source_id = DingTalkRAGImporter._source_id(entry)
        page.source_type = "dingtalk"
        page.source_id = source_id
        page.source_path = str(entry.get("dingtalk_path") or "")
        page.source_space_id = str(entry.get("space_id") or "")
        page.source_url = str(
            entry.get("source_url")
            or f"https://alidocs.dingtalk.com/i/nodes/{source_id}"
        )
        page.source_file_hash = entry.get("source_file_hash") or None
        page.source_file_size = entry.get("source_file_size") or None
        page.source_mime_type = entry.get("source_mime_type") or None

    def get_or_create_notebook(self, name: str) -> Notebook:
        notebook_name = str(name or "钉钉知识库").strip() or "钉钉知识库"
        notebook = self.db.query(Notebook).filter(
            Notebook.name == notebook_name
        ).first()
        if notebook is None:
            notebook = Notebook(id=str(uuid.uuid4()), name=notebook_name)
            self.db.add(notebook)
            self.db.commit()
            self.db.refresh(notebook)
        return notebook

    async def _embed_markdown(self, markdown: str) -> List[tuple[str, List[float]]]:
        index_content = self._strip_frontmatter(markdown)
        chunks = self.embedding_service.split_text(index_content)
        if not chunks:
            raise ValueError("Markdown未生成有效RAG分块")

        embeddings = await self.embedding_service.encode_batch(chunks)
        if len(embeddings) != len(chunks) or any(not vector for vector in embeddings):
            raise RuntimeError("Embedding服务未返回完整分块向量")
        expected_dimensions = max(int(settings.embedding_dimensions), 1)
        invalid_dimensions = {
            len(vector) for vector in embeddings
            if len(vector) != expected_dimensions
        }
        if invalid_dimensions:
            raise RuntimeError(
                f"Embedding向量维度不正确，期望{expected_dimensions}，"
                f"实际{sorted(invalid_dimensions)}"
            )
        return list(zip(chunks, embeddings))

    async def import_entry(
        self,
        entry: Dict[str, Any],
        notebook: Notebook,
        force: bool = False,
    ) -> Dict[str, Any]:
        source_id = self._source_id(entry)
        markdown_path = self._markdown_path(entry)
        markdown_bytes = markdown_path.read_bytes()
        markdown_hash = hashlib.sha256(markdown_bytes).hexdigest()
        expected_hash = str(entry.get("markdown_hash") or "").strip()
        if expected_hash and markdown_hash != expected_hash:
            raise ValueError("Markdown文件哈希与同步清单不一致")
        try:
            markdown = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown文件不是有效UTF-8") from exc
        if not markdown.strip():
            raise ValueError("Markdown文件正文为空")
        display_content = self._strip_frontmatter(markdown)
        if not display_content:
            raise ValueError("Markdown去除同步元数据后正文为空")
        display_hash = hashlib.sha256(display_content.encode("utf-8")).hexdigest()

        page = self.db.query(Page).filter(
            Page.source_type == "dingtalk",
            Page.source_id == source_id,
        ).first()
        existing_chunks = (
            self.db.query(PageChunk).filter(PageChunk.page_id == page.id).count()
            if page is not None
            else 0
        )
        source_file_hash = str(entry.get("source_file_hash") or "").strip()
        source_unchanged = (
            not source_file_hash
            or (page is not None and page.source_file_hash == source_file_hash)
        )
        legacy_frontmatter_page = (
            not force
            and page is not None
            and markdown.startswith("---")
            and page.current_content_hash == markdown_hash
            and page.indexed_content_hash == markdown_hash
            and page.source_content_hash == markdown_hash
            and not page.index_dirty
            and source_unchanged
            and existing_chunks > 0
        )
        if legacy_frontmatter_page:
            page.notebook_id = notebook.id
            self._set_source_metadata(page, entry)
            page.content = display_content
            page.source_content = markdown
            page.source_content_hash = markdown_hash
            page.content_hash = display_hash
            page.indexed_content_hash = display_hash
            page.index_dirty = False
            page.last_synced_at = datetime.now()
            self.db.commit()
            return {
                "status": "skipped",
                "page_id": page.id,
                "chunks": existing_chunks,
            }

        if (
            not force
            and page is not None
            and page.source_content_hash == markdown_hash
            and page.current_content_hash == display_hash
            and page.indexed_content_hash == display_hash
            and not page.index_dirty
            and source_unchanged
            and existing_chunks > 0
        ):
            page.notebook_id = notebook.id
            self._set_source_metadata(page, entry)
            page.source_content = markdown
            page.source_content_hash = markdown_hash
            page.content_hash = display_hash
            page.last_synced_at = datetime.now()
            self.db.commit()
            return {
                "status": "skipped",
                "page_id": page.id,
                "chunks": existing_chunks,
            }

        embedded_chunks = await self._embed_markdown(display_content)
        title = str(entry.get("name") or "未命名").strip() or "未命名"
        try:
            if page is None:
                page = Page(id=str(uuid.uuid4()))
                self.db.add(page)
            page.notebook_id = notebook.id
            page.title = title
            page.content = display_content
            page.keywords = ",".join(
                EmbeddingService.extract_keywords(
                    title + " " + display_content,
                    20,
                )
            )
            self._set_source_metadata(page, entry)
            page.source_content = markdown
            page.source_content_hash = markdown_hash
            page.content_hash = display_hash
            page.indexed_content_hash = display_hash
            page.index_dirty = False
            page.last_synced_at = datetime.now()
            self.db.flush()
            await self.vector_store.add_page_chunks(page.id, embedded_chunks)
            self.db.commit()
            return {
                "status": "imported",
                "page_id": page.id,
                "chunks": len(embedded_chunks),
            }
        except Exception:
            self.db.rollback()
            raise

    async def import_manifest(
        self,
        notebook_name: str = "钉钉知识库",
        force: bool = False,
        document_ids: Optional[List[str]] = None,
        limit: int = 0,
        on_progress: Optional[
            Callable[[Dict[str, Any], int, int, str, int], None]
        ] = None,
    ) -> Dict[str, Any]:
        manifest = self.storage.read_manifest()
        selected_ids = {
            str(document_id).strip() for document_id in (document_ids or [])
            if str(document_id).strip()
        }
        entries = [
            entry for entry in manifest["documents"]
            if entry.get("source_status", "active") == "active"
            and entry.get("conversion_status") == "converted"
            and (
                not selected_ids
                or str(entry.get("document_id") or "") in selected_ids
            )
        ]
        if limit > 0:
            entries = entries[:limit]

        notebook = self.get_or_create_notebook(notebook_name)
        imported = 0
        skipped = 0
        chunk_count = 0
        failures = []
        for index, entry in enumerate(entries, 1):
            status = "imported"
            chunks = 0
            try:
                result = await self.import_entry(entry, notebook, force=force)
                status = result["status"]
                chunks = int(result["chunks"])
                if status == "skipped":
                    skipped += 1
                else:
                    imported += 1
                chunk_count += chunks
                self.storage.update_rag_status(
                    entry,
                    "imported",
                    rag_page_id=result["page_id"],
                    rag_chunk_count=chunks,
                    rag_notebook_id=notebook.id,
                    rag_notebook_name=notebook.name,
                    rag_write_result=(
                        "unchanged" if status == "skipped" else "written"
                    ),
                )
            except Exception as exc:
                self.db.rollback()
                status = "failed"
                try:
                    self.storage.update_rag_status(
                        entry,
                        "failed",
                        error=str(exc),
                    )
                except Exception:
                    pass
                failures.append({
                    "document_id": str(entry.get("document_id") or ""),
                    "name": str(entry.get("name") or "未命名"),
                    "error": str(exc),
                })
            if on_progress:
                on_progress(entry, index, len(entries), status, chunks)

        return {
            "notebook_id": notebook.id,
            "notebook_name": notebook.name,
            "total": len(entries),
            "imported": imported,
            "skipped": skipped,
            "failed": len(failures),
            "chunks": chunk_count,
            "failures": failures,
        }

    def prune_deleted(
        self,
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """显式清理已被完整快照判定删除的钉钉RAG页面及关联数据。"""
        manifest = self.storage.read_manifest()
        selected_ids = {
            str(document_id).strip() for document_id in (document_ids or [])
            if str(document_id).strip()
        }
        entries = [
            entry for entry in manifest["documents"]
            if entry.get("source_status") == "deleted"
            and entry.get("rag_status") != "deleted"
            and (
                not selected_ids
                or str(entry.get("document_id") or "") in selected_ids
            )
        ]
        deleted = 0
        not_found = 0
        failures = []
        for entry in entries:
            source_id = self._source_id(entry)
            try:
                page = self.db.query(Page).filter(
                    Page.source_type == "dingtalk",
                    Page.source_id == source_id,
                ).first()
                if page is None:
                    not_found += 1
                else:
                    self.db.query(PageChunk).filter(
                        PageChunk.page_id == page.id
                    ).delete(synchronize_session=False)
                    self.db.query(GraphEdge).filter(
                        (GraphEdge.source_id == page.id)
                        | (GraphEdge.target_id == page.id)
                    ).delete(synchronize_session=False)
                    self.db.delete(page)
                    self.db.commit()
                    deleted += 1
                self.storage.update_rag_status(
                    entry,
                    "deleted",
                    rag_write_result=("deleted" if page is not None else "not_found"),
                )
            except Exception as exc:
                self.db.rollback()
                try:
                    self.storage.update_rag_status(entry, "failed", error=str(exc))
                except Exception:
                    pass
                failures.append({
                    "document_id": source_id,
                    "name": str(entry.get("name") or "未命名"),
                    "error": str(exc),
                })
        return {
            "total": len(entries),
            "deleted": deleted,
            "not_found": not_found,
            "failed": len(failures),
            "failures": failures,
        }

    async def close(self) -> None:
        await self.embedding_service.close()
