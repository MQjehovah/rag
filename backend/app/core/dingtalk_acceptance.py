import hashlib
from collections import Counter
from typing import Any, Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.dingtalk_rag_importer import DingTalkRAGImporter
from app.models.database import Notebook, Page, PageChunk


class DingTalkAcceptanceVerifier:
    """核对钉钉manifest、本地文件与RAG数据库是否形成一致闭环。"""

    def __init__(self, db: Session, storage: DingTalkLocalStorage | None = None):
        self.db = db
        self.storage = storage or DingTalkLocalStorage()

    @staticmethod
    def _issue(entry: Dict[str, Any] | None, message: str) -> Dict[str, str]:
        entry = entry or {}
        return {
            "document_id": str(entry.get("document_id") or ""),
            "name": str(entry.get("name") or "未命名"),
            "message": message,
        }

    def verify(self) -> Dict[str, Any]:
        manifest = self.storage.read_manifest()
        file_audit = self.storage.audit_manifest()
        entries = [
            entry for entry in manifest["documents"]
            if entry.get("source_status", "active") == "active"
        ]
        by_id = {
            str(entry.get("document_id") or ""): entry
            for entry in entries
        }
        expected_imported = {
            document_id: entry
            for document_id, entry in by_id.items()
            if entry.get("rag_status") == "imported"
        }
        blocked = [
            self._issue(
                entry,
                str(entry.get("conversion_error") or "转换阶段阻塞"),
            )
            for entry in entries
            if entry.get("conversion_status") in {"failed", "blocked"}
        ]

        pages = self.db.query(Page).filter(Page.source_type == "dingtalk").all()
        pages_by_source = {
            str(page.source_id or ""): page
            for page in pages
        }
        chunk_counts = dict(
            self.db.query(PageChunk.page_id, func.count(PageChunk.id))
            .group_by(PageChunk.page_id)
            .all()
        )
        errors: List[Dict[str, str]] = list(file_audit["errors"])
        warnings: List[Dict[str, str]] = list(file_audit["warnings"])

        for document_id, entry in expected_imported.items():
            page = pages_by_source.get(document_id)
            if page is None:
                errors.append(self._issue(entry, "manifest记录已入库，但数据库页面不存在"))
                continue
            if page.source_file_hash != entry.get("source_file_hash"):
                errors.append(self._issue(entry, "数据库原文件哈希与manifest不一致"))
            if page.source_content_hash != entry.get("markdown_hash"):
                errors.append(self._issue(entry, "数据库Markdown哈希与manifest不一致"))
            markdown_path = self.storage.root / str(entry.get("markdown_path") or "")
            try:
                markdown = markdown_path.read_text(encoding="utf-8")
                display_content = DingTalkRAGImporter._strip_frontmatter(markdown)
                expected_display_hash = hashlib.sha256(
                    display_content.encode("utf-8")
                ).hexdigest()
            except (OSError, UnicodeDecodeError):
                expected_display_hash = ""
            if page.current_content_hash != expected_display_hash:
                errors.append(self._issue(entry, "数据库页面正文已偏离本地Markdown"))
            if page.index_status != "current":
                errors.append(self._issue(entry, f"数据库索引状态不是current: {page.index_status}"))
            actual_chunks = int(chunk_counts.get(page.id, 0))
            expected_chunks = int(entry.get("rag_chunk_count") or 0)
            if actual_chunks != expected_chunks:
                errors.append(self._issue(
                    entry,
                    f"RAG分块数不一致，manifest={expected_chunks}，数据库={actual_chunks}",
                ))
            if page.source_path != str(entry.get("dingtalk_path") or ""):
                errors.append(self._issue(entry, "数据库钉钉路径与manifest不一致"))

        for source_id, page in pages_by_source.items():
            entry = by_id.get(source_id)
            if entry is None:
                errors.append({
                    "document_id": source_id,
                    "name": page.title or "未命名",
                    "message": "数据库存在manifest活动清单之外的钉钉页面",
                })
            elif entry.get("rag_status") != "imported":
                warnings.append(self._issue(
                    entry,
                    f"数据库仍有页面，但manifest RAG状态为{entry.get('rag_status')}",
                ))

        orphan_chunks = (
            self.db.query(PageChunk)
            .outerjoin(Page, PageChunk.page_id == Page.id)
            .filter(Page.id.is_(None))
            .count()
        )
        if orphan_chunks:
            errors.append({
                "document_id": "",
                "name": "RAG分块",
                "message": f"数据库存在{orphan_chunks}个无页面的孤立分块",
            })

        notebook_ids = {page.notebook_id for page in pages if page.notebook_id}
        notebook_count = (
            self.db.query(Notebook).filter(Notebook.id.in_(notebook_ids)).count()
            if notebook_ids else 0
        )
        raw_count = sum(1 for path in self.storage.raw_root.rglob("*") if path.is_file())
        markdown_count = sum(
            1 for path in self.storage.markdown_root.rglob("*.md") if path.is_file()
        )
        pipeline_counts = dict(Counter(
            str(entry.get("pipeline_status") or "unknown") for entry in entries
        ))
        result = {
            "valid": not errors,
            "acceptance_status": (
                "passed_with_source_blockers" if not errors and blocked
                else "passed" if not errors
                else "failed"
            ),
            "manifest_version": manifest.get("version"),
            "active_documents": len(entries),
            "raw_files": raw_count,
            "markdown_files": markdown_count,
            "manifest_imported": len(expected_imported),
            "database_dingtalk_pages": len(pages),
            "database_dingtalk_chunks": sum(
                int(chunk_counts.get(page.id, 0)) for page in pages
            ),
            "database_notebooks": notebook_count,
            "pipeline_status_counts": pipeline_counts,
            "blocked_count": len(blocked),
            "blocked_documents": blocked,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
        }
        return result
