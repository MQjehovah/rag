import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.dingtalk import DingTalkClient
from app.core.dingtalk_converter import DingTalkMarkdownConverter
from app.core.dingtalk_rag_importer import DingTalkRAGImporter
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.models.database import get_engine, get_session, init_db


class DingTalkSyncState:
    """保存单进程内最近一次钉钉同步任务状态，并原子阻止重复启动。"""

    ERROR_LIMIT = 50

    def __init__(self, state_path: str | Path | None = None):
        self._lock = threading.Lock()
        self.state_path = Path(state_path).resolve() if state_path else None
        self._state = self._idle_state()
        self._load()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _idle_state() -> Dict[str, Any]:
        return {
            "task_id": None,
            "running": False,
            "mode": None,
            "stage": "idle",
            "progress": "尚未执行同步",
            "percent": 0,
            "stage_processed": 0,
            "stage_total": 0,
            "total": 0,
            "found": 0,
            "downloaded": 0,
            "download_failed": 0,
            "converted": 0,
            "conversion_failed": 0,
            "imported": 0,
            "skipped": 0,
            "rag_failed": 0,
            "chunks": 0,
            "errors": 0,
            "error_details": [],
            "inventory": {
                "discovered": 0,
                "moved": 0,
                "restored": 0,
                "deleted": 0,
            },
            "notebook_id": None,
            "notebook_name": None,
            "space_id": None,
            "manifest_summary": None,
            "started_at": None,
            "finished_at": None,
            "last_sync": "",
            "recoverable": False,
            "retry_count": 0,
            "parent_task_id": None,
            "interrupted_at": None,
            "retry_plan": {
                "download": 0,
                "conversion": 0,
                "rag": 0,
                "restart": False,
            },
            "_request": {},
        }

    def _persist_locked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("任务状态根节点不是对象")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            backup = self.state_path.with_name(
                f"{self.state_path.name}.invalid-{uuid.uuid4().hex}.bak"
            )
            os.replace(self.state_path, backup)
            self._state.update({
                "stage": "failed",
                "progress": f"任务状态文件损坏，已保留备份：{exc}",
                "errors": 1,
                "error_details": [{
                    "stage": "recovery",
                    "document_id": "",
                    "name": "同步任务状态",
                    "error": str(exc),
                }],
                "finished_at": self._now(),
            })
            self._persist_locked()
            return

        idle = self._idle_state()
        idle.update(loaded)
        idle["inventory"] = {
            **self._idle_state()["inventory"],
            **(loaded.get("inventory") or {}),
        }
        idle["retry_plan"] = {
            **self._idle_state()["retry_plan"],
            **(loaded.get("retry_plan") or {}),
        }
        idle["error_details"] = list(loaded.get("error_details") or [])
        idle["_request"] = dict(loaded.get("_request") or {})
        self._state = idle
        if self._state.get("running"):
            interrupted_at = self._now()
            previous_stage = str(self._state.get("stage") or "unknown")
            self._state.update({
                "running": False,
                "stage": "interrupted",
                "progress": (
                    f"后端进程在{previous_stage}阶段退出，"
                    "可根据检查点重试未完成内容"
                ),
                "recoverable": True,
                "interrupted_at": interrupted_at,
                "finished_at": interrupted_at,
            })
            self._state.setdefault("error_details", []).append({
                "stage": "recovery",
                "document_id": "",
                "name": "同步任务",
                "error": f"进程在{previous_stage}阶段中断",
            })
            self._state["error_details"] = self._state["error_details"][-self.ERROR_LIMIT:]
            self._persist_locked()

    def begin(
        self,
        *,
        mode: str,
        notebook_name: str,
        space_id: Optional[str],
        total: int = 0,
        request: Optional[Dict[str, Any]] = None,
        retry_count: int = 0,
        parent_task_id: Optional[str] = None,
        retry_plan: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("同步正在进行中")
            task_id = str(uuid.uuid4())
            self._state = self._idle_state()
            self._state.update({
                "task_id": task_id,
                "running": True,
                "mode": mode,
                "stage": "queued",
                "progress": "同步任务已创建，等待执行",
                "total": max(int(total), 0),
                "notebook_name": notebook_name,
                "space_id": space_id,
                "started_at": self._now(),
                "retry_count": max(int(retry_count), 0),
                "parent_task_id": parent_task_id,
                "retry_plan": {
                    **self._idle_state()["retry_plan"],
                    **(retry_plan or {}),
                },
                "_request": dict(request or {}),
            })
            self._persist_locked()
            return self._public_snapshot_locked()

    def update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            self._persist_locked()

    def update_request(self, **values: Any) -> None:
        with self._lock:
            request = dict(self._state.get("_request") or {})
            request.update(values)
            self._state["_request"] = request
            self._persist_locked()

    def add_errors(self, failures: List[Dict[str, Any]]) -> None:
        normalized = [
            {
                "stage": str(item.get("stage") or "unknown"),
                "document_id": str(item.get("document_id") or ""),
                "name": str(item.get("name") or "未命名"),
                "error": str(item.get("error") or "未知错误"),
            }
            for item in failures
        ]
        with self._lock:
            details = list(self._state.get("error_details") or [])
            details.extend(normalized)
            self._state["error_details"] = details[-self.ERROR_LIMIT:]
            self._persist_locked()

    def finish(
        self,
        *,
        success: bool,
        progress: str,
        recoverable: Optional[bool] = None,
        **values: Any,
    ) -> None:
        now = self._now()
        with self._lock:
            self._state.update(values)
            self._state.update({
                "running": False,
                "stage": "completed" if success else "failed",
                "progress": progress,
                "percent": 100 if success else self._state.get("percent", 0),
                "finished_at": now,
                "last_sync": now,
            })
            if recoverable is None:
                self._state["recoverable"] = bool(
                    not success or int(self._state.get("errors") or 0) > 0
                )
            else:
                self._state["recoverable"] = bool(recoverable)
            self._persist_locked()

    def _public_snapshot_locked(self) -> Dict[str, Any]:
        result = {
            key: value for key, value in self._state.items()
            if key != "_request"
        }
        result["inventory"] = dict(self._state.get("inventory") or {})
        result["retry_plan"] = dict(self._state.get("retry_plan") or {})
        result["error_details"] = list(
            self._state.get("error_details") or []
        )
        return result

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._public_snapshot_locked()

    def request_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            request = dict(self._state.get("_request") or {})
            request["selected_docs"] = [
                dict(document)
                for document in request.get("selected_docs") or []
            ]
            return request


class DingTalkSyncService:
    """执行“清单、原文件、Markdown、RAG”统一同步流水线。"""

    def __init__(self, state: DingTalkSyncState):
        self.state = state

    @staticmethod
    def _document_ids(documents: List[Dict[str, Any]]) -> List[str]:
        return [
            str(document.get("id") or "").strip()
            for document in documents
            if str(document.get("id") or "").strip()
        ]

    @staticmethod
    def _stage_percent(start: int, end: int, processed: int, total: int) -> int:
        if total <= 0:
            return start
        ratio = min(max(processed / total, 0), 1)
        return round(start + (end - start) * ratio)

    def _download_failures(
        self,
        storage: DingTalkLocalStorage,
        selected_ids: List[str],
    ) -> List[Dict[str, str]]:
        selected = set(selected_ids)
        return [
            {
                "stage": "download",
                "document_id": str(entry.get("document_id") or ""),
                "name": str(entry.get("name") or "未命名"),
                "error": str(entry.get("download_error") or "原文件下载失败"),
            }
            for entry in storage.read_manifest()["documents"]
            if str(entry.get("document_id") or "") in selected
            and entry.get("download_status") == "failed"
        ]

    @staticmethod
    def _unique_ids(*groups: List[str]) -> List[str]:
        result: List[str] = []
        seen = set()
        for group in groups:
            for document_id in group:
                normalized = str(document_id or "").strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    result.append(normalized)
        return result

    @staticmethod
    def _classify_retry_entries(
        entries: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        plan = {"download": [], "conversion": [], "rag": []}
        for entry in entries:
            if entry.get("source_status", "active") != "active":
                continue
            if entry.get("download_status") != "downloaded":
                plan["download"].append(entry)
            elif entry.get("conversion_status") != "converted":
                plan["conversion"].append(entry)
            else:
                if entry.get("rag_status") != "imported":
                    plan["rag"].append(entry)
        return plan

    def build_retry_spec(self) -> Dict[str, Any]:
        """依据持久化任务请求和manifest阶段状态生成最小断点重试计划。"""
        status = self.state.snapshot()
        if status.get("running"):
            raise RuntimeError("同步正在进行中")
        if not status.get("recoverable"):
            raise ValueError("最近一次同步没有可重试内容")

        request = self.state.request_snapshot()
        original_mode = str(
            request.get("original_mode") or status.get("mode") or "selected"
        )
        selected_docs = list(request.get("selected_docs") or [])
        selected_ids = {
            str(document.get("id") or "").strip()
            for document in selected_docs
            if str(document.get("id") or "").strip()
        }
        restart_without_scope = bool(
            original_mode == "full"
            and not selected_docs
            and status.get("stage") in {"failed", "interrupted"}
        )
        storage = DingTalkLocalStorage()
        manifest = storage.read_manifest()
        entries = [
            entry for entry in manifest["documents"]
            if selected_ids
            and str(entry.get("document_id") or "") in selected_ids
        ]
        classified = self._classify_retry_entries(entries)
        counts = {
            stage: len(stage_entries)
            for stage, stage_entries in classified.items()
        }
        has_stage_work = any(counts.values())
        restart = restart_without_scope or bool(
            not has_stage_work
            and original_mode in {"full", "selected"}
            and status.get("stage") in {"failed", "interrupted"}
        )
        if not has_stage_work and not restart:
            raise ValueError("manifest中没有符合条件的失败或未完成文档")

        return {
            "restart": restart,
            "original_mode": original_mode,
            "notebook_name": str(
                request.get("notebook_name")
                or status.get("notebook_name")
                or "钉钉知识库"
            ),
            "space_id": request.get("space_id") or status.get("space_id"),
            "selected_docs": selected_docs,
            "download_documents": [
                storage.document_from_manifest(entry)
                for entry in classified["download"]
            ],
            "conversion_ids": [
                str(entry.get("document_id") or "")
                for entry in classified["conversion"]
            ],
            "rag_ids": [
                str(entry.get("document_id") or "")
                for entry in classified["rag"]
            ],
            "all_document_ids": self._unique_ids(
                [
                    str(entry.get("document_id") or "")
                    for entry in classified["download"]
                ],
                [
                    str(entry.get("document_id") or "")
                    for entry in classified["conversion"]
                ],
                [
                    str(entry.get("document_id") or "")
                    for entry in classified["rag"]
                ],
            ),
            "counts": counts,
        }

    async def run(
        self,
        *,
        mode: str,
        notebook_name: str,
        space_id: Optional[str] = None,
        selected_docs: Optional[List[Dict[str, Any]]] = None,
        retry_spec: Optional[Dict[str, Any]] = None,
    ) -> None:
        client: DingTalkClient | None = None
        importer: DingTalkRAGImporter | None = None
        db = None
        engine = None
        try:
            storage = DingTalkLocalStorage()
            storage.ensure_directories()
            converter = DingTalkMarkdownConverter(storage)
            stage_retry = bool(
                mode == "retry"
                and retry_spec
                and not retry_spec.get("restart")
            )
            effective_mode = (
                str(retry_spec.get("original_mode") or "full")
                if mode == "retry" and retry_spec and retry_spec.get("restart")
                else mode
            )
            if effective_mode in {"full", "selected"} or (
                stage_retry and retry_spec and retry_spec.get("download_documents")
            ):
                client = DingTalkClient()

            planned_conversion_ids: List[str] = []
            planned_rag_ids: List[str] = []
            if stage_retry:
                documents = list(retry_spec.get("download_documents") or [])
                document_ids = list(retry_spec.get("all_document_ids") or [])
                planned_conversion_ids = list(
                    retry_spec.get("conversion_ids") or []
                )
                planned_rag_ids = list(retry_spec.get("rag_ids") or [])
                self.state.update(
                    found=len(document_ids),
                    total=len(document_ids),
                    retry_plan=dict(retry_spec.get("counts") or {}),
                    progress=(
                        "已从manifest生成断点重试计划："
                        f"下载 {len(documents)}，"
                        f"转换 {len(planned_conversion_ids)}，"
                        f"RAG {len(planned_rag_ids)}"
                    ),
                )
            elif effective_mode == "full":
                self.state.update(
                    stage="inventory",
                    progress="正在递归读取钉钉知识库清单",
                    percent=1,
                    stage_processed=0,
                    stage_total=0,
                )

                async def on_inventory(_document, count):
                    self.state.update(
                        found=count,
                        stage_processed=count,
                        progress=f"正在读取钉钉文档清单，已发现 {count} 份",
                        percent=3,
                    )

                documents = await client.list_all_docs(
                    space_id,
                    on_progress=on_inventory,
                )
                inventory = storage.record_inventory(
                    documents,
                    complete_snapshot=bool(client._last_inventory_complete),
                    scope_space_ids=client._last_inventory_scope_ids,
                )
                self.state.update(
                    found=len(documents),
                    total=len(documents),
                    inventory=inventory,
                    percent=5,
                )
                self.state.update_request(selected_docs=documents)
            elif effective_mode == "selected":
                documents = list(selected_docs or [])
                storage.record_inventory(documents)
                self.state.update(found=len(documents), total=len(documents))
            else:
                raise ValueError(f"不支持的钉钉同步模式: {effective_mode}")

            if not stage_retry:
                document_ids = self._document_ids(documents)
            download_ids = self._document_ids(documents)
            if len(download_ids) != len(set(download_ids)):
                raise ValueError("同步文档列表包含重复的钉钉文档ID")

            self.state.update(
                stage="download",
                progress=f"开始下载原文件，共 {len(documents)} 份",
                stage_processed=0,
                stage_total=len(documents),
                percent=5,
            )

            async def on_download(_document, index):
                self.state.update(
                    stage_processed=index,
                    progress=f"正在下载原文件 {index}/{len(documents)}",
                    percent=self._stage_percent(5, 35, index, len(documents)),
                )

            downloaded = await client.download_selected_raw_files(
                documents,
                on_progress=on_download,
            ) if documents and client is not None else []
            downloaded_ids = self._document_ids(downloaded)
            download_failed = max(len(documents) - len(downloaded), 0)
            self.state.update(
                downloaded=len(downloaded),
                download_failed=download_failed,
            )
            download_failures = self._download_failures(storage, download_ids)
            self.state.add_errors(download_failures)

            conversion_ids = self._unique_ids(
                downloaded_ids,
                planned_conversion_ids,
            )
            rag_ids = self._unique_ids(
                downloaded_ids,
                planned_conversion_ids,
                planned_rag_ids,
            )

            self.state.update(
                stage="conversion",
                progress="正在把本地原文件转换为Markdown",
                stage_processed=0,
                stage_total=len(conversion_ids),
                percent=35,
            )

            def on_conversion(_entry, index, total, status):
                self.state.update(
                    stage_processed=index,
                    stage_total=total,
                    progress=f"正在转换Markdown {index}/{total}",
                    percent=self._stage_percent(35, 65, index, total),
                )

            conversion = converter.convert_manifest(
                document_ids=conversion_ids,
                on_progress=on_conversion,
            ) if conversion_ids else {
                "total": 0,
                "converted": 0,
                "failed": 0,
                "failures": [],
            }
            self.state.update(
                converted=conversion["converted"],
                conversion_failed=conversion["failed"],
            )
            self.state.add_errors([
                {**failure, "stage": "conversion"}
                for failure in conversion["failures"]
            ])

            rag_result = {
                "notebook_id": None,
                "total": 0,
                "imported": 0,
                "skipped": 0,
                "failed": 0,
                "chunks": 0,
                "failures": [],
            }
            if rag_ids:
                engine = get_engine(settings.database_url)
                init_db(engine)
                db = get_session(engine)
                importer = DingTalkRAGImporter(db, storage=storage)
                self.state.update(
                    stage="rag",
                    progress="正在生成Embedding并写入RAG知识库",
                    stage_processed=0,
                    stage_total=len(rag_ids),
                    percent=65,
                )

                def on_rag(_entry, index, total, status, chunks):
                    self.state.update(
                        stage_processed=index,
                        stage_total=total,
                        progress=(
                            f"正在写入RAG {index}/{total}，"
                            f"当前文档 {chunks} 个分块"
                        ),
                        percent=self._stage_percent(65, 99, index, total),
                    )

                rag_result = await importer.import_manifest(
                    notebook_name=notebook_name,
                    document_ids=rag_ids,
                    on_progress=on_rag,
                )
                self.state.add_errors([
                    {**failure, "stage": "rag"}
                    for failure in rag_result["failures"]
                ])

            errors = (
                download_failed
                + int(conversion["failed"])
                + int(rag_result["failed"])
            )
            manifest = storage.read_manifest()
            target_ids = set(document_ids)
            remaining_entries = [
                entry for entry in manifest["documents"]
                if str(entry.get("document_id") or "") in target_ids
            ]
            remaining = self._classify_retry_entries(remaining_entries)
            remaining_counts = {
                stage: len(entries)
                for stage, entries in remaining.items()
            }
            progress = (
                f"同步完成：下载 {len(downloaded)}，转换 {conversion['converted']}，"
                f"写入 {rag_result['imported']}，未变化 {rag_result['skipped']}，"
                f"失败 {errors}"
            )
            self.state.finish(
                success=True,
                progress=progress,
                recoverable=any(remaining_counts.values()),
                imported=rag_result["imported"],
                skipped=rag_result["skipped"],
                rag_failed=rag_result["failed"],
                chunks=rag_result["chunks"],
                errors=errors,
                notebook_id=rag_result.get("notebook_id"),
                manifest_summary=manifest.get("summary"),
                retry_plan=remaining_counts,
                stage_processed=rag_result["total"],
                stage_total=rag_result["total"],
            )
        except Exception as exc:
            self.state.add_errors([{
                "stage": "pipeline",
                "document_id": "",
                "name": "同步任务",
                "error": str(exc),
            }])
            snapshot = self.state.snapshot()
            self.state.finish(
                success=False,
                progress=f"同步失败：{exc}",
                errors=max(int(snapshot.get("errors") or 0), 1),
            )
        finally:
            if importer is not None:
                await importer.close()
            if db is not None:
                db.close()
            if engine is not None:
                engine.dispose()
            if client is not None:
                await client.close()
