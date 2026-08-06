import hashlib
import json
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.config import settings


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class DingTalkDocumentPaths:
    """一份钉钉文档在本地的原文件、Markdown和资源目录。"""

    raw_path: Path
    markdown_path: Path
    assets_dir: Path


class DingTalkLocalStorage:
    """负责钉钉文档本地目录初始化和安全路径映射。"""

    MANIFEST_VERSION = 3
    HISTORY_LIMIT = 100

    def __init__(self, root: str | Path | None = None):
        configured_root = root or settings.dingtalk_local_storage_dir
        self.root = Path(configured_root).expanduser().resolve()
        self.raw_root = self.root / "raw"
        self.markdown_root = self.root / "markdown"
        self.assets_root = self.root / "assets"
        self.manifest_path = self.root / "manifest.json"

    def ensure_directories(self) -> None:
        """创建本地存储骨架，并初始化空同步清单。"""
        for directory in (
            self.root,
            self.raw_root,
            self.markdown_root,
            self.assets_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        if not self.manifest_path.exists():
            now = self._timestamp()
            manifest = {
                "version": self.MANIFEST_VERSION,
                "created_at": now,
                "updated_at": now,
                "documents": [],
                "summary": self._empty_summary(),
            }
            self.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def read_manifest(self) -> Dict[str, Any]:
        """读取同步清单，并将旧版清单无损升级到当前结构。"""
        self.ensure_directories()
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取钉钉同步清单: {self.manifest_path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
            raise RuntimeError(f"钉钉同步清单格式不正确: {self.manifest_path}")
        changed = self._normalize_manifest(data)
        if changed:
            self._write_manifest(data)
        return data

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        self.ensure_directories()
        now = self._timestamp()
        manifest["version"] = self.MANIFEST_VERSION
        manifest.setdefault("created_at", now)
        manifest["updated_at"] = now
        self._refresh_summary(manifest)
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _empty_summary() -> Dict[str, Any]:
        return {
            "document_count": 0,
            "source_status_counts": {},
            "status_counts": {},
            "pipeline_status_counts": {},
            "download_status_counts": {},
            "conversion_status_counts": {},
            "rag_status_counts": {},
            "source_bytes": 0,
            "markdown_bytes": 0,
            "error_document_count": 0,
        }

    @staticmethod
    def _pipeline_status(entry: Dict[str, Any]) -> str:
        if entry.get("source_status") == "deleted":
            return "source_deleted"
        if entry.get("rag_status") == "stale":
            return "source_changed"
        if entry.get("rag_status") == "imported":
            return "imported"
        if entry.get("rag_status") == "failed":
            return "rag_failed"
        if entry.get("conversion_status") == "converted":
            return "converted"
        if entry.get("conversion_status") == "failed":
            return "conversion_failed"
        if entry.get("download_status") == "downloaded":
            return "downloaded"
        if entry.get("download_status") == "failed":
            return "download_failed"
        return "discovered"

    @classmethod
    def _refresh_summary(cls, manifest: Dict[str, Any]) -> None:
        documents = [
            entry for entry in manifest.get("documents", [])
            if isinstance(entry, dict)
        ]
        for entry in documents:
            entry["pipeline_status"] = cls._pipeline_status(entry)
        manifest["summary"] = {
            "document_count": len(documents),
            "source_status_counts": dict(Counter(
                str(entry.get("source_status") or "active")
                for entry in documents
            )),
            "status_counts": dict(Counter(
                str(entry.get("status") or "unknown") for entry in documents
            )),
            "pipeline_status_counts": dict(Counter(
                str(entry.get("pipeline_status") or "unknown")
                for entry in documents
            )),
            "download_status_counts": dict(Counter(
                str(entry.get("download_status") or "unknown")
                for entry in documents
            )),
            "conversion_status_counts": dict(Counter(
                str(entry.get("conversion_status") or "unknown")
                for entry in documents
            )),
            "rag_status_counts": dict(Counter(
                str(entry.get("rag_status") or "unknown")
                for entry in documents
            )),
            "source_bytes": sum(
                int(entry.get("source_file_size") or 0) for entry in documents
            ),
            "markdown_bytes": sum(
                int(entry.get("markdown_size") or 0) for entry in documents
            ),
            "error_document_count": sum(
                bool(
                    entry.get("download_error")
                    or entry.get("conversion_error")
                    or entry.get("rag_error")
                )
                for entry in documents
            ),
        }

    @classmethod
    def _append_history(
        cls,
        entry: Dict[str, Any],
        stage: str,
        status: str,
        at: str,
        error: str | None = None,
    ) -> None:
        history = entry.setdefault("status_history", [])
        if not isinstance(history, list):
            history = []
            entry["status_history"] = history
        event = {"at": at, "stage": stage, "status": status}
        if error:
            event["error"] = str(error)
        history.append(event)
        del history[:-cls.HISTORY_LIMIT]

    def _normalize_manifest(self, manifest: Dict[str, Any]) -> bool:
        """为旧清单补齐阶段字段，保留原有业务数据。"""
        try:
            existing_version = int(manifest.get("version") or 0)
        except (TypeError, ValueError):
            existing_version = 0
        changed = existing_version < self.MANIFEST_VERSION
        now = self._timestamp()
        previous_summary = manifest.get("summary")
        previous_pipeline_statuses = [
            entry.get("pipeline_status") if isinstance(entry, dict) else None
            for entry in manifest["documents"]
        ]
        if not manifest.get("created_at"):
            manifest["created_at"] = (
                manifest.get("last_inventory_at") or manifest.get("updated_at") or now
            )
            changed = True
        seen_ids = set()
        for entry in manifest["documents"]:
            if not isinstance(entry, dict):
                raise RuntimeError("钉钉同步清单包含非对象文档项")
            document_id = str(entry.get("document_id") or "").strip()
            if not document_id:
                raise RuntimeError("钉钉同步清单包含缺少文档ID的文档项")
            if document_id in seen_ids:
                raise RuntimeError(f"钉钉同步清单包含重复文档ID: {document_id}")
            seen_ids.add(document_id)

            legacy_status = str(entry.get("status") or "discovered")
            event_at = str(
                entry.get("updated_at") or entry.get("last_seen_at") or now
            )
            defaults = {
                "source_status": "active",
                "deleted_at": None,
                "deletion_reason": None,
                "restored_at": None,
                "source_versions": [],
                "path_history": [],
                "discovery_status": "discovered",
                "discovered_at": entry.get("last_seen_at") or event_at,
                "download_status": (
                    "downloaded" if legacy_status in {
                        "downloaded", "converted", "conversion_failed"
                    } else "failed" if legacy_status == "failed" else "pending"
                ),
                "download_attempts": (
                    1 if legacy_status in {
                        "downloaded", "converted", "conversion_failed", "failed"
                    } else 0
                ),
                "downloaded_at": (
                    event_at if legacy_status in {
                        "downloaded", "converted", "conversion_failed"
                    } else None
                ),
                "download_error": entry.get("error") if legacy_status == "failed" else None,
                "conversion_status": (
                    "converted" if legacy_status == "converted"
                    else "failed" if legacy_status == "conversion_failed"
                    else "pending"
                ),
                "conversion_attempts": (
                    1 if legacy_status in {"converted", "conversion_failed"} else 0
                ),
                "converted_at": event_at if legacy_status == "converted" else None,
                "conversion_error": (
                    entry.get("error") if legacy_status == "conversion_failed" else None
                ),
                "rag_status": (
                    "pending" if legacy_status == "converted"
                    else "blocked" if legacy_status == "conversion_failed"
                    else "not_ready"
                ),
                "rag_attempts": 0,
                "rag_imported_at": None,
                "rag_error": None,
                "status_history": [],
            }
            for key, value in defaults.items():
                if key not in entry:
                    entry[key] = value
                    changed = True
            if not isinstance(entry.get("status_history"), list):
                entry["status_history"] = []
                changed = True
            if not isinstance(entry.get("source_versions"), list):
                entry["source_versions"] = []
                changed = True
            if not isinstance(entry.get("path_history"), list):
                entry["path_history"] = []
                changed = True
            if not entry["status_history"]:
                self._append_history(
                    entry,
                    "migration",
                    legacy_status,
                    event_at,
                    entry.get("error"),
                )
                changed = True

        if manifest.get("version") != self.MANIFEST_VERSION:
            manifest["version"] = self.MANIFEST_VERSION
            changed = True
        self._refresh_summary(manifest)
        if manifest.get("summary") != previous_summary:
            changed = True
        if any(
            entry.get("pipeline_status") != previous
            for entry, previous in zip(
                manifest["documents"], previous_pipeline_statuses
            )
        ):
            changed = True
        return changed

    def _manifest_entry(self, document: Dict[str, Any]) -> Dict[str, Any]:
        paths = self.document_paths(document)
        source_id = str(document.get("id") or "").strip()
        if not source_id:
            raise ValueError("钉钉文档清单项缺少文档ID")
        return {
            "document_id": source_id,
            "name": str(document.get("title") or "未命名"),
            "extension": str(document.get("extension") or "").lower().lstrip("."),
            "node_type": str(document.get("node_type") or ""),
            "space_id": str(document.get("space_id") or ""),
            "space_name": str(document.get("space_name") or ""),
            "dingtalk_path": str(document.get("path") or ""),
            "source_url": str(
                document.get("source_url")
                or f"https://alidocs.dingtalk.com/i/nodes/{source_id}"
            ),
            "source_updated_at": str(document.get("updated_at") or ""),
            "reported_file_size": document.get("file_size") or None,
            "raw_path": paths.raw_path.relative_to(self.root).as_posix(),
            "markdown_path": paths.markdown_path.relative_to(self.root).as_posix(),
            "assets_path": paths.assets_dir.relative_to(self.root).as_posix(),
        }

    @staticmethod
    def _matches_current_sync_filter(entry: Dict[str, Any]) -> bool:
        """配置缩小同步类型范围时，不把仍在钉钉中的旧类型误判为删除。"""
        supported = {
            item.strip().lower().lstrip(".")
            for item in settings.dingtalk_supported_extensions.split(",")
            if item.strip()
        }
        extension = str(entry.get("extension") or "").lower().lstrip(".")
        node_type = str(entry.get("node_type") or "")
        is_wiki = settings.dingtalk_include_wiki and (
            extension in {"", "wiki", "mindmap", "note"}
            or node_type == "DOC"
        )
        return extension in supported or is_wiki

    def _relocate_local_paths(
        self,
        previous: Dict[str, Any],
        desired: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """文档重命名或移动时同步调整本地路径；目标冲突时保留两端文件。"""
        moved: List[Dict[str, str]] = []
        for field, root in (
            ("raw_path", self.raw_root),
            ("markdown_path", self.markdown_root),
            ("assets_path", self.assets_root),
        ):
            old_relative = str(previous.get(field) or "").strip()
            new_relative = str(desired.get(field) or "").strip()
            if not old_relative or not new_relative or old_relative == new_relative:
                continue
            old_path = (self.root / old_relative).resolve()
            new_path = (self.root / new_relative).resolve()
            self._assert_contained(old_path, root)
            self._assert_contained(new_path, root)
            if not old_path.exists():
                continue
            if new_path.exists():
                moved.append({
                    "field": field,
                    "from": old_relative,
                    "to": new_relative,
                    "result": "target_exists",
                })
                continue
            new_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_path, new_path)
            moved.append({
                "field": field,
                "from": old_relative,
                "to": new_relative,
                "result": "moved",
            })
        return moved

    def record_inventory(
        self,
        documents: List[Dict[str, Any]],
        *,
        complete_snapshot: bool = False,
        scope_space_ids: Iterable[str] | None = None,
    ) -> Dict[str, int]:
        """写入发现结果；仅完整且范围明确的快照可以把缺失文档标为软删除。"""
        manifest = self.read_manifest()
        existing = {
            str(item.get("document_id") or ""): item
            for item in manifest["documents"]
            if isinstance(item, dict)
        }
        seen_at = self._timestamp()
        seen_ids = set()
        counters = {"discovered": 0, "moved": 0, "restored": 0, "deleted": 0}
        for document in documents:
            entry = self._manifest_entry(document)
            seen_ids.add(entry["document_id"])
            previous = existing.get(entry["document_id"], {})
            is_new = not previous
            old_location = {
                key: previous.get(key)
                for key in (
                    "name", "space_id", "space_name", "dingtalk_path",
                    "raw_path", "markdown_path", "assets_path",
                )
            }
            was_deleted = previous.get("source_status") == "deleted"
            path_changed = bool(previous) and any(
                str(previous.get(key) or "") != str(entry.get(key) or "")
                for key in (
                    "name", "space_id", "space_name", "dingtalk_path",
                    "raw_path", "markdown_path", "assets_path",
                )
            )
            relocation = self._relocate_local_paths(previous, entry) if path_changed else []
            desired_location = {
                key: entry.get(key)
                for key in old_location
            }
            for result in relocation:
                if result.get("result") == "target_exists":
                    field = str(result.get("field") or "")
                    entry[field] = old_location.get(field)
            previous.update(entry)
            previous.setdefault("status", "discovered")
            previous.setdefault("error", None)
            previous.setdefault("discovery_status", "discovered")
            previous.setdefault("discovered_at", seen_at)
            previous.setdefault("download_status", "pending")
            previous.setdefault("download_attempts", 0)
            previous.setdefault("conversion_status", "pending")
            previous.setdefault("conversion_attempts", 0)
            previous.setdefault("rag_status", "not_ready")
            previous.setdefault("rag_attempts", 0)
            previous.setdefault("status_history", [])
            previous.setdefault("source_versions", [])
            previous.setdefault("path_history", [])
            previous["source_status"] = "active"
            previous["last_seen_at"] = seen_at
            if is_new:
                counters["discovered"] += 1
                self._append_history(
                    previous, "discovery", "discovered", seen_at
                )
            if path_changed:
                counters["moved"] += 1
                previous["path_history"].append({
                    "at": seen_at,
                    "from": old_location,
                    "to": desired_location,
                    "local_relocation": relocation,
                })
                del previous["path_history"][:-self.HISTORY_LIMIT]
                self._append_history(previous, "source", "moved", seen_at)
            if was_deleted:
                counters["restored"] += 1
                previous["restored_at"] = seen_at
                previous["deleted_at"] = None
                previous["deletion_reason"] = None
                if previous.get("rag_status") == "pending_delete":
                    previous["rag_status"] = (
                        previous.pop("rag_status_before_delete", None)
                        or ("pending" if previous.get("conversion_status") == "converted" else "not_ready")
                    )
                self._append_history(previous, "source", "restored", seen_at)
            existing[entry["document_id"]] = previous

        scope_ids = {
            str(space_id).strip() for space_id in (scope_space_ids or [])
            if str(space_id).strip()
        }
        if complete_snapshot and not scope_ids:
            raise ValueError("完整快照必须明确提供知识库范围，避免误判源端删除")
        if complete_snapshot:
            for document_id, previous in existing.items():
                if (
                    document_id in seen_ids
                    or str(previous.get("space_id") or "") not in scope_ids
                    or previous.get("source_status") == "deleted"
                    or not self._matches_current_sync_filter(previous)
                ):
                    continue
                previous["source_status"] = "deleted"
                previous["deleted_at"] = seen_at
                previous["deletion_reason"] = "missing_from_complete_snapshot"
                if previous.get("rag_status") == "imported":
                    previous["rag_status_before_delete"] = "imported"
                    previous["rag_status"] = "pending_delete"
                self._append_history(previous, "source", "deleted", seen_at)
                counters["deleted"] += 1

        manifest["version"] = self.MANIFEST_VERSION
        manifest["last_inventory_at"] = seen_at
        manifest["last_inventory"] = {
            "at": seen_at,
            "complete_snapshot": complete_snapshot,
            "scope_space_ids": sorted(scope_ids),
            "seen_document_count": len(seen_ids),
            **counters,
        }
        if complete_snapshot:
            manifest["last_complete_inventory"] = dict(manifest["last_inventory"])
        manifest["documents"] = sorted(
            existing.values(),
            key=lambda item: (
                str(item.get("space_name") or ""),
                str(item.get("dingtalk_path") or ""),
                str(item.get("document_id") or ""),
            ),
        )
        self._write_manifest(manifest)
        return counters

    def update_document_status(
        self,
        document: Dict[str, Any],
        status: str,
        error: str | None = None,
        **metadata: Any,
    ) -> None:
        """更新单个文档的下载状态和本地文件元数据。"""
        force_reconvert = bool(metadata.pop("force_reconvert", False))
        manifest = self.read_manifest()
        entry = self._manifest_entry(document)
        source_id = entry["document_id"]
        documents = [
            item for item in manifest["documents"]
            if str(item.get("document_id") or "") != source_id
        ]
        current = next(
            (
                item for item in manifest["documents"]
                if str(item.get("document_id") or "") == source_id
            ),
            {},
        )
        previous_status = str(current.get("status") or "discovered")
        previous_error = current.get("error")
        previous_download_status = current.get("download_status")
        previous_conversion_status = current.get("conversion_status")
        previous_rag_status = current.get("rag_status")
        previous_source_hash = str(current.get("source_file_hash") or "")
        previous_source_size = current.get("source_file_size")
        current.update(entry)
        previous_markdown_hash = current.get("markdown_hash")
        current.update(metadata)
        current["status"] = status
        current["error"] = error
        updated_at = self._timestamp()
        current["updated_at"] = updated_at

        stage = "pipeline"
        stage_status = status
        if status == "discovered":
            stage = "discovery"
            current["discovery_status"] = "discovered"
            current.setdefault("discovered_at", updated_at)
        elif status in {"downloaded", "failed"}:
            stage = "download"
            current["download_attempts"] = int(current.get("download_attempts") or 0) + 1
            current["download_status"] = "downloaded" if status == "downloaded" else "failed"
            current["download_error"] = None if status == "downloaded" else error
            if status == "downloaded":
                current["downloaded_at"] = updated_at
                new_source_hash = str(current.get("source_file_hash") or "")
                source_changed = bool(
                    previous_source_hash
                    and new_source_hash
                    and previous_source_hash != new_source_hash
                )
                source_unchanged = bool(
                    previous_source_hash
                    and previous_source_hash == new_source_hash
                    and (
                        previous_download_status == "downloaded"
                        or previous_conversion_status == "converted"
                    )
                    and not force_reconvert
                )
                if source_changed:
                    versions = current.setdefault("source_versions", [])
                    versions.append({
                        "replaced_at": updated_at,
                        "source_file_hash": previous_source_hash,
                        "source_file_size": previous_source_size,
                        "markdown_hash": previous_markdown_hash,
                        "rag_page_id": current.get("rag_page_id"),
                    })
                    del versions[:-self.HISTORY_LIMIT]
                    current["source_changed_at"] = updated_at
                    current["conversion_status"] = "pending"
                    current["conversion_error"] = None
                    current["rag_status"] = (
                        "stale" if current.get("rag_page_id") else "not_ready"
                    )
                    current["rag_error"] = None
                    stage_status = "content_changed"
                elif source_unchanged:
                    current["status"] = previous_status
                    current["error"] = previous_error
                    current["conversion_status"] = previous_conversion_status
                    current["rag_status"] = previous_rag_status
                    stage_status = "unchanged"
                else:
                    current["conversion_status"] = "pending"
                    current["rag_status"] = "not_ready"
            else:
                if previous_download_status == "downloaded":
                    current["status"] = previous_status
                    current["conversion_status"] = previous_conversion_status
                    current["rag_status"] = previous_rag_status
                else:
                    current["conversion_status"] = "blocked"
                    current["rag_status"] = "blocked"
        elif status in {"converted", "conversion_failed"}:
            stage = "conversion"
            current["conversion_attempts"] = int(
                current.get("conversion_attempts") or 0
            ) + 1
            current["conversion_status"] = (
                "converted" if status == "converted" else "failed"
            )
            current["conversion_error"] = (
                None if status == "converted" else error
            )
            if status == "converted":
                current["converted_at"] = updated_at
                new_hash = current.get("markdown_hash")
                if (
                    previous_markdown_hash != new_hash
                    or previous_rag_status == "stale"
                ):
                    current["rag_status"] = "pending"
                    current["rag_error"] = None
                    for key in (
                        "rag_page_id", "rag_chunk_count", "rag_notebook_id",
                        "rag_notebook_name", "rag_imported_markdown_hash",
                        "rag_write_result", "rag_imported_at",
                    ):
                        current[key] = None
            else:
                current["rag_status"] = (
                    "stale" if current.get("rag_page_id") else "blocked"
                )
        self._append_history(
            current, stage, stage_status, updated_at, error
        )
        documents.append(current)
        manifest["documents"] = sorted(
            documents,
            key=lambda item: (
                str(item.get("space_name") or ""),
                str(item.get("dingtalk_path") or ""),
                str(item.get("document_id") or ""),
            ),
        )
        self._write_manifest(manifest)

    @staticmethod
    def document_from_manifest(entry: Dict[str, Any]) -> Dict[str, Any]:
        """将清单项转换为状态更新所需的文档元数据。"""
        return {
            "id": entry.get("document_id") or "",
            "title": entry.get("name") or "未命名",
            "extension": entry.get("extension") or "",
            "node_type": entry.get("node_type") or "",
            "space_id": entry.get("space_id") or "",
            "space_name": entry.get("space_name") or "",
            "path": entry.get("dingtalk_path") or "",
            "source_url": entry.get("source_url") or "",
            "updated_at": entry.get("source_updated_at") or "",
            "file_size": entry.get("reported_file_size"),
        }

    def update_rag_status(
        self,
        entry: Dict[str, Any],
        status: str,
        error: str | None = None,
        **metadata: Any,
    ) -> None:
        """回写单文档RAG入库状态，不改变兼容用的本地转换状态。"""
        if status not in {
            "imported", "failed", "pending", "blocked", "stale", "deleted",
            "pending_delete",
        }:
            raise ValueError(f"不支持的RAG状态: {status}")
        manifest = self.read_manifest()
        document_id = str(entry.get("document_id") or "").strip()
        current = next(
            (
                item for item in manifest["documents"]
                if str(item.get("document_id") or "") == document_id
            ),
            None,
        )
        if current is None:
            raise KeyError(f"同步清单中不存在文档: {document_id}")

        updated_at = self._timestamp()
        current.update(metadata)
        current["rag_status"] = status
        current["rag_error"] = error
        current["rag_attempts"] = int(current.get("rag_attempts") or 0) + 1
        current["updated_at"] = updated_at
        if status == "imported":
            current["rag_imported_at"] = updated_at
            current["rag_imported_markdown_hash"] = current.get("markdown_hash")
        elif status == "deleted":
            current["rag_deleted_at"] = updated_at
            current["rag_page_id"] = None
            current["rag_chunk_count"] = 0
        self._append_history(current, "rag", status, updated_at, error)
        self._write_manifest(manifest)

    def audit_manifest(self, record: bool = False) -> Dict[str, Any]:
        """核验清单字段、本地文件和哈希，并可把审计摘要写回清单。"""
        manifest = self.read_manifest()
        errors: List[Dict[str, str]] = []
        warnings: List[Dict[str, str]] = []

        def add(collection, entry, message):
            collection.append({
                "document_id": str(entry.get("document_id") or ""),
                "name": str(entry.get("name") or "未命名"),
                "message": message,
            })

        for entry in manifest["documents"]:
            is_deleted = entry.get("source_status") == "deleted"
            if entry.get("download_status") == "downloaded":
                raw_path = (self.root / str(entry.get("raw_path") or "")).resolve()
                try:
                    self._assert_contained(raw_path, self.raw_root)
                except ValueError:
                    add(errors, entry, "原文件路径越界")
                else:
                    if not raw_path.is_file():
                        add(errors, entry, "原文件不存在")
                    else:
                        if raw_path.stat().st_size != int(entry.get("source_file_size") or -1):
                            add(errors, entry, "原文件大小与清单不一致")
                        expected = str(entry.get("source_file_hash") or "")
                        if not expected or self.file_sha256(raw_path) != expected:
                            add(errors, entry, "原文件哈希与清单不一致")

            if entry.get("conversion_status") == "converted":
                markdown_path = (
                    self.root / str(entry.get("markdown_path") or "")
                ).resolve()
                try:
                    self._assert_contained(markdown_path, self.markdown_root)
                except ValueError:
                    add(errors, entry, "Markdown路径越界")
                else:
                    if not markdown_path.is_file():
                        add(errors, entry, "Markdown文件不存在")
                    else:
                        if markdown_path.stat().st_size != int(entry.get("markdown_size") or -1):
                            add(errors, entry, "Markdown大小与清单不一致")
                        expected = str(entry.get("markdown_hash") or "")
                        if not expected or self.file_sha256(markdown_path) != expected:
                            add(errors, entry, "Markdown哈希与清单不一致")
                        try:
                            markdown_path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            add(errors, entry, "Markdown不是有效UTF-8")

            if entry.get("rag_status") == "imported":
                if not entry.get("rag_page_id"):
                    add(errors, entry, "RAG入库记录缺少页面ID")
                if int(entry.get("rag_chunk_count") or 0) <= 0:
                    add(errors, entry, "RAG入库记录缺少有效分块数")
                if entry.get("rag_imported_markdown_hash") != entry.get("markdown_hash"):
                    add(errors, entry, "RAG入库哈希不是当前Markdown哈希")
            elif entry.get("conversion_status") == "converted" and not is_deleted:
                add(warnings, entry, "Markdown已转换但尚未记录RAG入库成功")
            if is_deleted and entry.get("rag_status") == "pending_delete":
                add(warnings, entry, "源端文档已删除，RAG记录等待显式清理")

        result = {
            "audited_at": self._timestamp(),
            "valid": not errors,
            "document_count": len(manifest["documents"]),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "summary": manifest.get("summary") or self._empty_summary(),
        }
        if record:
            manifest["last_audit"] = {
                key: result[key]
                for key in (
                    "audited_at", "valid", "document_count",
                    "error_count", "warning_count",
                )
            }
            self._write_manifest(manifest)
        return result

    def persist_raw_file(
        self,
        document: Dict[str, Any],
        content: bytes,
        extension: str | None = None,
        mime_type: str | None = None,
    ) -> Dict[str, Any]:
        """通过同目录临时文件原子保存原文件，并返回校验元数据。"""
        if not content:
            raise ValueError("不能保存空的钉钉原文件")
        local_document = dict(document)
        if extension is not None:
            local_document["extension"] = extension
        paths = self.document_paths(local_document)
        paths.raw_path.parent.mkdir(parents=True, exist_ok=True)

        source_hash = hashlib.sha256(content).hexdigest()
        write_result = "written"
        if paths.raw_path.exists():
            existing_hash = self.file_sha256(paths.raw_path)
            if existing_hash == source_hash:
                write_result = "unchanged"

        if write_result == "written":
            temporary = paths.raw_path.with_name(
                f".{paths.raw_path.name}.{uuid.uuid4().hex}.part"
            )
            try:
                temporary.write_bytes(content)
                if temporary.stat().st_size != len(content):
                    raise OSError("钉钉原文件写入大小校验失败")
                os.replace(temporary, paths.raw_path)
            finally:
                temporary.unlink(missing_ok=True)

        metadata = {
            "local_raw_path": str(paths.raw_path),
            "raw_path": paths.raw_path.relative_to(self.root).as_posix(),
            "source_file_hash": source_hash,
            "source_file_size": len(content),
            "source_mime_type": mime_type or "application/octet-stream",
            "raw_write_result": write_result,
        }
        self.update_document_status(
            local_document,
            "downloaded",
            **{key: value for key, value in metadata.items() if key != "local_raw_path"},
        )
        return metadata

    @staticmethod
    def file_sha256(path: Path) -> str:
        """分块计算文件哈希，避免大文件一次性读入内存。"""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def raw_temporary_path(
        self,
        document: Dict[str, Any],
        extension: str | None = None,
    ) -> Path:
        """为流式下载创建位于目标目录内的唯一临时文件路径。"""
        local_document = dict(document)
        if extension is not None:
            local_document["extension"] = extension
        target = self.document_paths(local_document).raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        self._assert_contained(temporary, self.raw_root)
        return temporary

    def commit_raw_temporary_file(
        self,
        document: Dict[str, Any],
        temporary: Path,
        extension: str | None = None,
        mime_type: str | None = None,
        expected_hash: str | None = None,
        expected_size: int | None = None,
    ) -> Dict[str, Any]:
        """校验流式下载临时文件并原子提交为正式原文件。"""
        local_document = dict(document)
        if extension is not None:
            local_document["extension"] = extension
        paths = self.document_paths(local_document)
        temporary = temporary.resolve()
        self._assert_contained(temporary, self.raw_root)
        if not temporary.is_file():
            raise FileNotFoundError(f"钉钉下载临时文件不存在: {temporary}")

        actual_size = temporary.stat().st_size
        actual_hash = self.file_sha256(temporary)
        if actual_size <= 0:
            raise ValueError("不能提交空的钉钉原文件")
        if expected_size is not None and actual_size != expected_size:
            raise OSError("钉钉流式下载文件大小校验失败")
        if expected_hash and actual_hash != expected_hash:
            raise OSError("钉钉流式下载文件哈希校验失败")

        write_result = "written"
        if paths.raw_path.exists() and self.file_sha256(paths.raw_path) == actual_hash:
            temporary.unlink(missing_ok=True)
            write_result = "unchanged"
        else:
            os.replace(temporary, paths.raw_path)

        metadata = {
            "local_raw_path": str(paths.raw_path),
            "raw_path": paths.raw_path.relative_to(self.root).as_posix(),
            "source_file_hash": actual_hash,
            "source_file_size": actual_size,
            "source_mime_type": mime_type or "application/octet-stream",
            "raw_write_result": write_result,
        }
        self.update_document_status(
            local_document,
            "downloaded",
            **{key: value for key, value in metadata.items() if key != "local_raw_path"},
        )
        return metadata

    def persist_markdown_file(
        self,
        document: Dict[str, Any],
        markdown: str,
        conversion_metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """以UTF-8和原子替换方式保存转换后的Markdown文件。"""
        normalized = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            raise ValueError("不能保存空的Markdown文件")
        if not normalized.endswith("\n"):
            normalized += "\n"

        paths = self.document_paths(document)
        paths.markdown_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = normalized.encode("utf-8")
        markdown_hash = hashlib.sha256(encoded).hexdigest()
        write_result = "written"
        if (
            paths.markdown_path.exists()
            and self.file_sha256(paths.markdown_path) == markdown_hash
        ):
            write_result = "unchanged"
        else:
            temporary = paths.markdown_path.with_name(
                f".{paths.markdown_path.name}.{uuid.uuid4().hex}.part"
            )
            try:
                temporary.write_bytes(encoded)
                if temporary.stat().st_size != len(encoded):
                    raise OSError("Markdown文件写入大小校验失败")
                os.replace(temporary, paths.markdown_path)
            finally:
                temporary.unlink(missing_ok=True)

        metadata = {
            "local_markdown_path": str(paths.markdown_path),
            "markdown_path": paths.markdown_path.relative_to(self.root).as_posix(),
            "markdown_hash": markdown_hash,
            "markdown_size": len(encoded),
            "markdown_write_result": write_result,
        }
        self.update_document_status(
            document,
            "converted",
            **dict(conversion_metadata or {}),
            **{key: value for key, value in metadata.items() if key != "local_markdown_path"},
        )
        return metadata

    @staticmethod
    def sanitize_segment(value: Any, fallback: str = "未命名") -> str:
        """将钉钉目录名或文件名转换为Windows安全名称。"""
        text = str(value or "").strip()
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
        text = re.sub(r"\s+", " ", text).rstrip(" .")
        if not text or text in {".", ".."}:
            text = fallback

        stem = text.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            text = f"_{text}"
        return text[:120].rstrip(" .") or fallback

    @classmethod
    def _safe_parts(cls, value: Any) -> Iterable[str]:
        normalized = str(value or "").replace("\\", "/")
        for part in normalized.split("/"):
            if not part or part in {".", ".."}:
                continue
            yield cls.sanitize_segment(part)

    @staticmethod
    def _document_suffix(document_id: Any, source_path: Any) -> str:
        identity = str(document_id or source_path or "unknown")
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _with_extension(filename: str, extension: str) -> str:
        extension = extension.lower().lstrip(".")
        if not extension:
            return filename
        suffix = f".{extension}"
        if filename.lower().endswith(suffix):
            return filename
        return f"{filename}{suffix}"

    def document_paths(self, document: Dict[str, Any]) -> DingTalkDocumentPaths:
        """按知识库目录生成不会因同名文档互相覆盖的本地路径。"""
        workspace = self.sanitize_segment(
            document.get("space_name") or document.get("space_id"),
            "默认知识库",
        )
        source_parts = list(self._safe_parts(document.get("path")))
        title = self.sanitize_segment(
            document.get("title") or (source_parts[-1] if source_parts else "未命名"),
        )
        parent_parts = source_parts[:-1] if source_parts else []
        extension = str(document.get("extension") or "").lower().lstrip(".")

        if extension and title.lower().endswith(f".{extension}"):
            title = title[:-(len(extension) + 1)]
        suffix = self._document_suffix(document.get("id"), document.get("path"))
        stable_name = f"{title}__{suffix}"
        raw_name = self._with_extension(stable_name, extension or "wiki")

        relative_parent = Path(workspace, *parent_parts)
        raw_path = (self.raw_root / relative_parent / raw_name).resolve()
        markdown_path = (
            self.markdown_root / relative_parent / f"{stable_name}.md"
        ).resolve()
        assets_dir = (self.assets_root / relative_parent / stable_name).resolve()

        self._assert_contained(raw_path, self.raw_root)
        self._assert_contained(markdown_path, self.markdown_root)
        self._assert_contained(assets_dir, self.assets_root)
        return DingTalkDocumentPaths(raw_path, markdown_path, assets_dir)

    @staticmethod
    def _assert_contained(path: Path, expected_root: Path) -> None:
        try:
            path.relative_to(expected_root.resolve())
        except ValueError as exc:
            raise ValueError(f"本地文档路径超出允许目录: {path}") from exc
