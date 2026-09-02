"""钉钉远程同步流水线（P10-BE-04，接入统一数据源 Worker）。

职责：把「远程清单扫描 → 原文件下载 → Markdown 转换」这条已稳定的
钉钉流水线，封装成可由 source-worker 在后台任务中直接调用的可复用组件。

- 复用 `app/core/dingtalk.py` 的 DingTalkClient（远程通信 + 下载 + 重试）
- 复用 `app/core/dingtalk_converter.py` 的 DingTalkMarkdownConverter（转换）
- 复用 `app/core/dingtalk_storage.py` 的 DingTalkLocalStorage（manifest/本地文件）
- 不复制新的下载/转换代码；不在本模块创建 threading.Thread
- 支持进度回调与取消检查（由调用方注入 callable）

同步模式语义：
- incremental：完整扫描但非完整快照（不软删除），只下载新增/变化/未完成的文档
- backfill：补齐 manifest 中 download/conversion 未完成的文档
- full_reconcile：完整快照（complete_snapshot=True），把缺失文档标记为软删除

输出统一结果（见 run() 返回 dict），供 executor 更新任务进度与远程游标。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.sources.schemas import ConnectionTestResult

logger = logging.getLogger(__name__)

# 进度回调：stage、已处理、总数、消息
ProgressCallback = Callable[[str, int, int, str], Awaitable[None]]
# 取消检查回调：返回 True 表示应停止
CancelCallback = Callable[[], Awaitable[bool]]


def map_sync_mode(mode: str) -> Dict[str, Any]:
    """把同步模式字符串映射为流水线参数。

    - complete_snapshot：是否把未扫描到的文档标记为软删除（仅 full_reconcile）
    - rescan_all：是否重新下载全部 active 文档（仅 full_reconcile 需要全量核对）
    """
    normalized = (mode or "incremental").strip()
    if normalized == "full_reconcile":
        return {"complete_snapshot": True, "rescan_all": True}
    if normalized == "backfill":
        return {"complete_snapshot": False, "rescan_all": False}
    return {"complete_snapshot": False, "rescan_all": False}  # incremental


def build_retry_scope(manifest: Dict[str, Any]) -> List[str]:
    """从 manifest 中找出下载/转换失败或未完成的文档 id（最小重试范围）。"""
    return [
        str(entry.get("document_id") or "")
        for entry in manifest.get("documents", [])
        if entry.get("source_status", "active") == "active"
        and (
            entry.get("download_status") != "downloaded"
            or entry.get("conversion_status") != "converted"
        )
    ]


class DingTalkRemoteSyncPipeline:
    """钉钉远程同步流水线。client/converter/storage 可注入（测试用）。"""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        storage=None,
        client=None,
        converter=None,
    ):
        self.config = config or {}
        self._client = client
        self._storage = storage
        self._converter = converter

    async def _get_client(self):
        if self._client is None:
            from app.core.dingtalk import DingTalkClient
            self._client = DingTalkClient()
        return self._client

    async def _get_storage(self):
        if self._storage is None:
            from app.core.dingtalk_storage import DingTalkLocalStorage
            self._storage = DingTalkLocalStorage()
        return self._storage

    async def _get_converter(self, storage):
        if self._converter is None:
            from app.core.dingtalk_converter import DingTalkMarkdownConverter
            self._converter = DingTalkMarkdownConverter(storage)
        return self._converter

    async def _maybe_cancel(self, check_cancelled: Optional[CancelCallback]) -> bool:
        if check_cancelled is None:
            return False
        return bool(await check_cancelled())

    async def test_remote_connection(self) -> ConnectionTestResult:
        """真实远程连接测试：凭证 → token → 可访问空间 → 本地状态。"""
        from app.config import settings

        if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
            return ConnectionTestResult(
                ok=False, message="钉钉凭证未配置（DINGTALK_APP_KEY / DINGTALK_APP_SECRET）",
                error_code="CREDENTIALS_MISSING",
            )
        try:
            client = await self._get_client()
            spaces = await client.list_workspaces()
        except Exception as exc:
            return ConnectionTestResult(
                ok=False, message=f"钉钉远程连接失败：{exc}",
                error_code="REMOTE_CONNECT_FAILED",
            )
        if not spaces:
            return ConnectionTestResult(
                ok=False, message="钉钉返回空工作区列表（可能凭证无权限）",
                error_code="NO_ACCESSIBLE_WORKSPACE",
            )

        # 本地 manifest 状态
        try:
            storage = await self._get_storage()
            manifest = storage.read_manifest()
            local_docs = len(manifest.get("documents", []))
            local_note = f"本地清单含 {local_docs} 篇"
        except Exception as exc:
            local_note = f"本地清单不可读：{exc}"

        names = ", ".join(str(s.get("name") or s.get("id") or "") for s in spaces[:3])
        return ConnectionTestResult(
            ok=True,
            message=f"远程可访问 {len(spaces)} 个工作区（{names}…）；{local_note}",
        )

    async def _list_remote_docs(self, client, space_ids: List[str], on_progress) -> List[Dict[str, Any]]:
        """扫描远程文档清单；space_ids 为空时使用配置范围。"""
        if not space_ids:
            return await client.list_all_docs(None, on_progress=on_progress)

        docs: List[Dict[str, Any]] = []
        for sid in space_ids:
            batch = await client.list_all_docs(sid, on_progress=on_progress)
            docs.extend(batch)
        return docs

    async def run(
        self,
        *,
        mode: str,
        space_ids: Optional[List[str]] = None,
        on_progress: Optional[ProgressCallback] = None,
        check_cancelled: Optional[CancelCallback] = None,
    ) -> Dict[str, Any]:
        """执行远程同步流水线，返回统一结果。

        返回：
        scanned/new/modified/unchanged/deleted/failed/download_failed/
        conversion_failed/manifest_path/new_cursor/retryable_errors
        """
        params = map_sync_mode(mode)
        storage = await self._get_storage()
        storage.ensure_directories()
        client = await self._get_client()
        converter = await self._get_converter(storage)

        # 1. 远程扫描（先记录旧清单，用于判断新增/变化）
        old_manifest = storage.read_manifest()
        old_updated = {
            str(e.get("document_id") or ""): str(e.get("source_updated_at") or "")
            for e in old_manifest.get("documents", [])
        }

        async def _on_scan(_doc, count):
            if on_progress:
                await on_progress("remote", count, 0, f"扫描钉钉文档，已发现 {count} 份")

        documents = await self._list_remote_docs(client, space_ids or [], _on_scan)
        if await self._maybe_cancel(check_cancelled):
            raise RuntimeError("同步已在远程扫描阶段取消")

        new_ids = {str(d.get("id") or "") for d in documents if str(d.get("id") or "") not in old_updated}
        changed_ids = {
            str(d.get("id") or "")
            for d in documents
            if str(d.get("id") or "") in old_updated
            and str(d.get("updated_at") or "") != old_updated[str(d.get("id") or "")]
        }

        # 2. 写入清单（full_reconcile 时完整快照，缺失文档标软删除）
        scope_ids = [str(s) for s in (space_ids or [])]
        inventory = storage.record_inventory(
            documents,
            complete_snapshot=bool(params["complete_snapshot"]),
            scope_space_ids=scope_ids or None,
        )

        # 3. 确定需要下载/转换的文档
        manifest = storage.read_manifest()
        download_candidates: List[Dict[str, Any]] = []
        for entry in manifest.get("documents", []):
            if entry.get("source_status", "active") != "active":
                continue
            doc_id = str(entry.get("document_id") or "")
            needs_download = (
                params["rescan_all"]
                or doc_id in new_ids
                or doc_id in changed_ids
                or entry.get("download_status") != "downloaded"
            )
            if needs_download:
                download_candidates.append(storage.document_from_manifest(entry))

        # 4. 下载原文件（download_selected_raw_files 内部幂等，未变化自动跳过）
        downloaded: List[Dict[str, Any]] = []
        if download_candidates:
            async def _on_download(_doc, index):
                if on_progress:
                    await on_progress(
                        "download", index, len(download_candidates),
                        f"下载原文件 {index}/{len(download_candidates)}",
                    )
            downloaded = await client.download_selected_raw_files(
                download_candidates, on_progress=_on_download
            )
            if await self._maybe_cancel(check_cancelled):
                raise RuntimeError("同步已在下载阶段取消")

        downloaded_ids = [str(d.get("id") or "") for d in downloaded]

        # 5. 转换 Markdown（转换未完成的 downloaded 文档）
        conversion = {"total": 0, "converted": 0, "failed": 0, "failures": []}
        if downloaded_ids:
            if on_progress:
                await on_progress("convert", 0, len(downloaded_ids), "正在转换 Markdown")
            conversion = converter.convert_manifest(document_ids=downloaded_ids)
            if on_progress:
                await on_progress("convert", conversion["converted"], conversion["total"],
                                  f"转换 Markdown 完成 {conversion['converted']}/{conversion['total']}")

        # 6. 汇总
        manifest = storage.read_manifest()
        active_entries = [
            e for e in manifest.get("documents", [])
            if e.get("source_status", "active") == "active"
        ]
        download_failed = sum(
            1 for e in active_entries if e.get("download_status") == "failed"
        )
        conversion_failed = sum(
            1 for e in active_entries
            if e.get("download_status") == "downloaded"
            and e.get("conversion_status") == "failed"
        )
        failed = download_failed + conversion_failed
        new_count = int(inventory.get("discovered") or 0)
        deleted_count = int(inventory.get("deleted") or 0)
        modified = len(changed_ids) - new_count  # changed 且非新增
        unchanged = max(
            len(active_entries) - new_count - modified - download_failed - conversion_failed,
            0,
        )

        new_cursor = {
            "last_inventory_at": manifest.get("last_inventory_at"),
            "workspace_ids": sorted(scope_ids),
            "mode": mode,
            "document_count": len(manifest.get("documents", [])),
        }
        retryable_errors = [
            {"document_id": str(e.get("document_id") or ""), "name": str(e.get("name") or ""),
             "error": str(e.get("download_error") or e.get("conversion_error") or "")}
            for e in manifest.get("documents", [])
            if e.get("source_status", "active") == "active"
            and (e.get("download_status") == "failed" or e.get("conversion_status") == "failed")
        ]

        return {
            "scanned": len(documents),
            "new": new_count,
            "modified": max(modified, 0),
            "unchanged": unchanged,
            "deleted": deleted_count,
            "failed": failed,
            "download_failed": download_failed,
            "conversion_failed": conversion_failed,
            "manifest_path": str(storage.manifest_path),
            "new_cursor": new_cursor,
            "retryable_errors": retryable_errors,
        }
