"""DingTalk Connector 适配器（P10-BE-01，V3 计划 5.1）。

不重写已稳定的下载/转换/Manifest/删除检测/来源保护；作为适配层从
manifest 读取数据，实现 SourceConnector 契约。

- discover → 返回钉钉空间列表（简化：manifest 中的 space_name）
- iter_changes → 按 manifest documents 生成 SourceChange
- fetch_item → 从 manifest 读 markdown 内容生成 NormalizedSourceItem
- fetch_acl → 返回空间级 ACL（fail closed：无法解析时 resolve_failed=True）

不直接操作 Card/Wiki/图谱（契约要求）。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import AsyncIterator

from app.core.dingtalk_storage import DingTalkLocalStorage
from app.sources.schemas import (
    ConnectionTestResult,
    InputRepresentation,
    NormalizedSourceItem,
    SourceACL,
    SourceAttachment,
    SourceChange,
    SourcePayloadError,
    SourceScope,
)

logger = logging.getLogger(__name__)

import re
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256(value: str) -> bool:
    """严格 64 位小写 SHA-256（不自动 lower）。"""
    return bool(value) and bool(_SHA256_RE.match(value))


class DingTalkConnector:
    key = "dingtalk"
    name = "钉钉知识库"

    def __init__(self, config: dict, storage=None):
        self.config = config or {}
        # storage 可注入（测试用临时目录）；默认用 settings 目录
        self._storage = storage or DingTalkLocalStorage()

    # ---- SourceConnector 契约 ----

    async def test_connection(self) -> ConnectionTestResult:
        # 真实远程连接测试（凭证 → token → 可访问空间 → 本地 manifest 状态）
        from app.sources.dingtalk_pipeline import DingTalkRemoteSyncPipeline

        pipeline = DingTalkRemoteSyncPipeline(self.config, storage=self._storage)
        return await pipeline.test_remote_connection()

    async def discover(self) -> list[SourceScope]:
        # 优先读真实钉钉空间；网络/凭证不可用时回退到本地 manifest 已见空间
        from app.config import settings
        if settings.dingtalk_app_key and settings.dingtalk_app_secret:
            try:
                from app.core.dingtalk import DingTalkClient
                client = DingTalkClient()
                try:
                    workspaces = await client.list_workspaces()
                finally:
                    await client.close()
                return [
                    SourceScope(scope_id=str(ws.get("id") or ""), name=str(ws.get("name") or ""), kind="space")
                    for ws in workspaces if ws.get("id")
                ]
            except Exception as exc:
                logger.warning(f"discover 远程空间失败，回退本地 manifest：{exc}")

        manifest = self._storage.read_manifest()
        spaces: dict[str, str] = {}
        for doc in manifest.get("documents", []):
            space_id = str(doc.get("space_id") or "")
            space_name = str(doc.get("space_name") or "")
            if space_id and space_id not in spaces:
                spaces[space_id] = space_name
        return [
            SourceScope(scope_id=sid, name=name, kind="space")
            for sid, name in spaces.items()
        ]

    async def iter_changes(self, cursor: dict | None) -> AsyncIterator[SourceChange]:
        manifest = self._storage.read_manifest()
        for doc in manifest.get("documents", []):
            external_id = str(doc.get("document_id") or "").strip()
            if not external_id:
                continue
            deleted = str(doc.get("source_status") or "active") == "deleted"
            version = str(doc.get("markdown_hash") or doc.get("source_file_hash") or "")
            yield SourceChange(
                external_id=external_id,
                deleted=deleted,
                external_version=version,
            )

    async def fetch_item(self, external_id: str) -> NormalizedSourceItem:
        manifest = self._storage.read_manifest()
        doc = next(
            (d for d in manifest.get("documents", [])
             if str(d.get("document_id") or "") == external_id),
            None,
        )
        if doc is None:
            raise KeyError(f"manifest 中无此文档: {external_id}")

        deleted = str(doc.get("source_status") or "active") == "deleted"
        extension = str(doc.get("extension") or "text").lstrip(".")
        source_file_hash = str(doc.get("source_file_hash") or "").strip()
        markdown_hash = str(doc.get("markdown_hash") or "").strip()
        manifest_status = str(doc.get("conversion_status") or "").strip()

        # ---- 1. deleted：不读取/不校验任何路径 ----
        if deleted:
            return NormalizedSourceItem(
                connection_id=self.config.get("connection_id", ""),
                source_type="dingtalk",
                external_id=external_id,
                external_version=str(doc.get("source_updated_at") or ""),
                title=str(doc.get("name") or ""),
                content="",
                content_bytes=None,
                content_type=extension,
                content_hash="",
                # Phase 2.5：deleted 也需规范 hash（非法 → 空，不构造非法对象）
                original_source_hash=source_file_hash if _is_sha256(source_file_hash) else "",
                source_url=str(doc.get("source_url") or ""),
                source_path=str(doc.get("dingtalk_path") or ""),
                source_updated_at=str(doc.get("source_updated_at") or ""),
                deleted=True,
                acl_scope=self._acl_from_doc(doc),
                metadata_json={
                    "document_id": external_id,
                    "space_name": str(doc.get("space_name") or ""),
                    "manifest_conversion_status": manifest_status,
                },
                attachments=[],
            )

        markdown_path = str(doc.get("markdown_path") or "").strip()
        raw_path = str(doc.get("raw_path") or "").strip()

        # ---- 状态矩阵（Phase 2.3） ----
        # 区分显式 conversion_status 与旧 Manifest 推断（storage 补 pending 时标 inferred）
        inferred = bool(doc.get("conversion_status_inferred_from_legacy"))
        legacy_status = str(doc.get("legacy_status") or "").strip()

        _CONVERTED = {"converted"}
        _UNFINISHED = {"pending", "failed", "blocked", "not_ready", "downloaded",
                       "conversion_failed", "download_failed"}

        if manifest_status in _CONVERTED:
            # modern converted：必须 markdown_hash + 可验证 raw
            return self._load_converted(external_id, doc, markdown_path, markdown_hash,
                                        source_file_hash, raw_path, extension)
        if manifest_status in _UNFINISHED and not inferred:
            # 显式 pending/failed/blocked/...：一律忽略 markdown_path，用当前 raw。
            return self._load_unfinished(external_id, doc, raw_path, source_file_hash,
                                         extension, manifest_status)
        if inferred:
            # 旧 Manifest 推断：按保留 legacy_status 判断（converted → Markdown；否则真实 pending）
            if legacy_status == "converted":
                return self._load_converted(external_id, doc, markdown_path, markdown_hash,
                                            source_file_hash, raw_path, extension)
            # 无法证明是历史 converted → 一律用 raw（inferred-unconverted），绝不读 Markdown
            return self._load_inferred_unconverted(external_id, doc, raw_path,
                                                   source_file_hash, extension)
        # conversion_status 完全缺失且无 inferred（既非显式也非推断）→ 状态冲突
        raise SourcePayloadError(
            "MANIFEST_STATE_CONFLICT", stage="fetch", retryable=False,
            safe_message="Manifest 转换状态未知且非空",
            internal_detail=f"document_id={external_id} conversion_status={manifest_status!r}",
        )

    # ------------------------------------------------------------------
    # 状态矩阵：converted
    # ------------------------------------------------------------------

    def _load_converted(
        self, external_id: str, doc: dict, markdown_path: str, markdown_hash: str,
        source_file_hash: str, raw_path: str, extension: str,
    ) -> NormalizedSourceItem:
        """converted：必须存在安全 markdown_path；缺失/越界/不可读/Frontmatter 无效/hash 不符均 fail closed。
        即使 raw 存在，也不得因 Markdown 缺失而静默改走原始 Converter。"""
        from app.sources.schemas import SourcePayloadError

        if not markdown_path:
            raise SourcePayloadError(
                "MANIFEST_STATE_CONFLICT", stage="fetch", retryable=False,
                safe_message="已标记转换完成但缺少 Markdown 路径",
                internal_detail=f"document_id={external_id} status=converted markdown_path 缺失",
            )
        md_path = self._resolve_contained(markdown_path, self._storage.markdown_root)
        if md_path is None:
            raise SourcePayloadError(
                "SOURCE_PATH_UNSAFE", stage="fetch", retryable=False,
                safe_message="Markdown 路径不安全，已拒绝读取",
                internal_detail=f"document_id={external_id} markdown_path={markdown_path!r}",
            )
        if not md_path.is_file():
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=False,
                safe_message="已转换的 Markdown 文件缺失",
                internal_detail=f"document_id={external_id} md_path={markdown_path}",
            )
        # 文件只读一次：同一份 bytes 完成 hash 校验 + 严格解码（避免 TOCTOU）
        try:
            md_bytes = md_path.read_bytes()
        except OSError:
            raise SourcePayloadError(
                "SOURCE_FILE_UNREADABLE", stage="fetch", retryable=True,
                safe_message="Markdown 文件不可读",
                internal_detail=f"document_id={external_id}",
            )
        # Phase 2.3：converted 必须 markdown_hash（严格 64 位）+ 基于本次 md_bytes 校验
        if not markdown_hash:
            raise SourcePayloadError(
                "MANIFEST_STATE_CONFLICT", stage="fetch", retryable=False,
                safe_message="converted 但缺少 Markdown 哈希",
                internal_detail=f"document_id={external_id} status=converted markdown_hash 缺失",
            )
        if not _is_sha256(markdown_hash):
            raise SourcePayloadError(
                "MARKDOWN_HASH_INVALID", stage="fetch", retryable=False,
                safe_message="Manifest 中的 Markdown 哈希非法",
                internal_detail=f"document_id={external_id} markdown_hash={markdown_hash!r}",
            )
        if hashlib.sha256(md_bytes).hexdigest() != markdown_hash:
            raise SourcePayloadError(
                "MARKDOWN_HASH_MISMATCH", stage="fetch", retryable=False,
                safe_message="Markdown 文件哈希与 Manifest 不一致",
                internal_detail=f"document_id={external_id}",
            )
        content = self._decode_strict(md_bytes)
        if content is None:
            raise SourcePayloadError(
                "SOURCE_FILE_UNREADABLE", stage="fetch", retryable=False,
                safe_message="Markdown 内容无法解码",
                internal_detail=f"document_id={external_id}",
            )
        # 精确 Frontmatter 身份校验（严格历史 Markdown 识别）
        if not self._looks_like_legacy_markdown(content, doc):
            raise SourcePayloadError(
                "LEGACY_MARKDOWN_UNRECOGNIZED", stage="fetch", retryable=False,
                safe_message="已转换 Markdown 无法通过历史管道身份校验",
                internal_detail=f"document_id={external_id}",
            )
        # Phase 2.3：modern converted 必须可验证 raw_path/source_file_hash
        if not raw_path:
            raise SourcePayloadError(
                "MANIFEST_STATE_CONFLICT", stage="fetch", retryable=False,
                safe_message="converted 但缺少原始文件路径",
                internal_detail=f"document_id={external_id} status=converted raw_path 缺失",
            )
        if not source_file_hash:
            raise SourcePayloadError(
                "MANIFEST_STATE_CONFLICT", stage="fetch", retryable=False,
                safe_message="converted 但缺少原始文件哈希",
                internal_detail=f"document_id={external_id} status=converted source_file_hash 缺失",
            )
        raw = self._resolve_contained(raw_path, self._storage.raw_root)
        if raw is None:
            raise SourcePayloadError(
                "SOURCE_PATH_UNSAFE", stage="fetch", retryable=False,
                safe_message="原始文件路径不安全，已拒绝读取",
                internal_detail=f"document_id={external_id}",
            )
        if not raw.is_file():
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=False,
                safe_message="converted 的原始文件缺失",
                internal_detail=f"document_id={external_id}",
            )
        if not _is_sha256(source_file_hash):
            raise SourcePayloadError(
                "SOURCE_HASH_INVALID", stage="fetch", retryable=False,
                safe_message="Manifest 中的原始文件哈希非法",
                internal_detail=f"document_id={external_id}",
            )
        try:
            raw_bytes = raw.read_bytes()
        except OSError:
            raise SourcePayloadError(
                "SOURCE_FILE_UNREADABLE", stage="fetch", retryable=False,
                safe_message="converted 的原始文件不可读",
                internal_detail=f"document_id={external_id}",
            )
        if hashlib.sha256(raw_bytes).hexdigest() != source_file_hash:
            raise SourcePayloadError(
                "SOURCE_HASH_MISMATCH", stage="fetch", retryable=False,
                safe_message="原始文件哈希与 Manifest 不一致",
                internal_detail=f"document_id={external_id}",
            )
        return self._finalize(external_id, doc, content=content, content_bytes=None,
                              md_available=True, extension=extension,
                              manifest_status="converted", is_manifest_markdown=True)

    # ------------------------------------------------------------------
    # 状态矩阵：明确未完成（pending/failed/blocked/...）
    # ------------------------------------------------------------------

    def _load_unfinished(
        self, external_id: str, doc: dict, raw_path: str, source_file_hash: str,
        extension: str, manifest_status: str,
    ) -> NormalizedSourceItem:
        """pending/failed/blocked：一律忽略遗留 markdown_path，从安全 raw_path 读真实 bytes。

        Phase 2.3：显式 pending 永不使用 Markdown（即使有合法旧管道 Frontmatter）；
        只有 inferred legacy + legacy_status==converted 才走 Markdown。
        """
        from app.sources.schemas import SourcePayloadError

        markdown_path = str(doc.get("markdown_path") or "").strip()
        if not raw_path:
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=True,
                safe_message="未完成转换但缺少原始文件路径",
                internal_detail=f"document_id={external_id} status={manifest_status}",
            )
        raw = self._resolve_contained(raw_path, self._storage.raw_root)
        if raw is None:
            raise SourcePayloadError(
                "SOURCE_PATH_UNSAFE", stage="fetch", retryable=False,
                safe_message="原始文件路径不安全，已拒绝读取",
                internal_detail=f"document_id={external_id} raw_path={raw_path!r}",
            )
        if not raw.is_file():
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=True,
                safe_message="原始文件缺失",
                internal_detail=f"document_id={external_id}",
            )
        try:
            content_bytes = raw.read_bytes()
        except OSError:
            raise SourcePayloadError(
                "SOURCE_FILE_UNREADABLE", stage="fetch", retryable=True,
                safe_message="原始文件不可读",
                internal_detail=f"document_id={external_id}",
            )
        if source_file_hash:
            if len(source_file_hash) != 64 or not all(c in "0123456789abcdef" for c in source_file_hash):
                raise SourcePayloadError(
                    "SOURCE_HASH_INVALID", stage="fetch", retryable=False,
                    safe_message="Manifest 中的原始文件哈希非法",
                    internal_detail=f"document_id={external_id}",
                )
            if hashlib.sha256(content_bytes).hexdigest() != source_file_hash:
                raise SourcePayloadError(
                    "SOURCE_HASH_MISMATCH", stage="fetch", retryable=False,
                    safe_message="原始文件哈希与 Manifest 不一致",
                    internal_detail=f"document_id={external_id}",
                )
        return self._finalize(external_id, doc, content="", content_bytes=content_bytes,
                              md_available=False, extension=extension,
                              manifest_status=manifest_status, is_manifest_markdown=False)

    # ------------------------------------------------------------------
    # 状态矩阵：legacy（conversion_status 缺失）
    # ------------------------------------------------------------------

    def _load_inferred_unconverted(
        self, external_id: str, doc: dict, raw_path: str,
        source_file_hash: str, extension: str,
    ) -> NormalizedSourceItem:
        """inferred 但非 converted：一律使用 raw，绝不读取/返回 markdown_path。

        raw 缺失/越界/不可读/空/hash 非法/不符 → 精确 SourcePayloadError。
        """
        from app.sources.schemas import SourcePayloadError

        if not raw_path:
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=True,
                safe_message="未完成转换但缺少原始文件路径",
                internal_detail=f"document_id={external_id}",
            )
        raw = self._resolve_contained(raw_path, self._storage.raw_root)
        if raw is None:
            raise SourcePayloadError(
                "SOURCE_PATH_UNSAFE", stage="fetch", retryable=False,
                safe_message="原始文件路径不安全，已拒绝读取",
                internal_detail=f"document_id={external_id} raw_path={raw_path!r}",
            )
        if not raw.is_file():
            raise SourcePayloadError(
                "SOURCE_FILE_MISSING", stage="fetch", retryable=True,
                safe_message="原始文件缺失",
                internal_detail=f"document_id={external_id}",
            )
        try:
            content_bytes = raw.read_bytes()
        except OSError:
            raise SourcePayloadError(
                "SOURCE_FILE_UNREADABLE", stage="fetch", retryable=True,
                safe_message="原始文件不可读",
                internal_detail=f"document_id={external_id}",
            )
        if not content_bytes:
            raise SourcePayloadError(
                "SOURCE_PAYLOAD_EMPTY", stage="fetch", retryable=False,
                safe_message="原始文件为空",
                internal_detail=f"document_id={external_id}",
            )
        if source_file_hash:
            if not _is_sha256(source_file_hash):
                raise SourcePayloadError(
                    "SOURCE_HASH_INVALID", stage="fetch", retryable=False,
                    safe_message="Manifest 中的原始文件哈希非法",
                    internal_detail=f"document_id={external_id}",
                )
            if hashlib.sha256(content_bytes).hexdigest() != source_file_hash:
                raise SourcePayloadError(
                    "SOURCE_HASH_MISMATCH", stage="fetch", retryable=False,
                    safe_message="原始文件哈希与 Manifest 不一致",
                    internal_detail=f"document_id={external_id}",
                )
        return self._finalize(external_id, doc, content="", content_bytes=content_bytes,
                              md_available=False, extension=extension,
                              manifest_status="", is_manifest_markdown=False)

    # ------------------------------------------------------------------
    # 统一构造
    # ------------------------------------------------------------------

    def _finalize(
        self, external_id: str, doc: dict, *, content: str, content_bytes: bytes | None,
        md_available: bool, extension: str, manifest_status: str, is_manifest_markdown: bool,
    ) -> NormalizedSourceItem:
        """构造 NormalizedSourceItem（effective payload 与 hash 一致）。

        - input_representation：md_available（已转换 Markdown）→ PRECONVERTED_MARKDOWN；
          否则（raw 输入）→ ORIGINAL。
        - original_source_hash：raw 输入缺 Manifest hash 时自动计算 actual_raw_hash（不空置）。
        """
        from app.sources.schemas import InputRepresentation

        effective = content_bytes if content_bytes is not None else content.encode("utf-8")
        declared_hash = str(doc.get("source_file_hash") or "").strip()
        if md_available:
            input_repr = InputRepresentation.PRECONVERTED_MARKDOWN
            original_hash = declared_hash if _is_sha256(declared_hash) else ""
        else:
            input_repr = InputRepresentation.ORIGINAL
            # raw 输入：manifest 有合法 hash 用之；否则用实际 payload hash（不空置）
            original_hash = declared_hash if _is_sha256(declared_hash) else hashlib.sha256(effective).hexdigest()
        return NormalizedSourceItem(
            connection_id=self.config.get("connection_id", ""),
            source_type="dingtalk",
            external_id=external_id,
            external_version=str(doc.get("source_updated_at") or ""),
            title=str(doc.get("name") or ""),
            content=content,
            content_bytes=content_bytes,
            content_type=extension,
            content_hash=hashlib.sha256(effective).hexdigest(),
            original_source_hash=original_hash,
            input_representation=input_repr,
            source_url=str(doc.get("source_url") or ""),
            source_path=str(doc.get("dingtalk_path") or ""),
            source_updated_at=str(doc.get("source_updated_at") or ""),
            deleted=False,
            acl_scope=self._acl_from_doc(doc),
            metadata_json={
                "document_id": external_id,
                "space_name": str(doc.get("space_name") or ""),
                "rag_page_id": doc.get("rag_page_id"),
                "manifest_conversion_status": manifest_status,
                "manifest_converter": str(doc.get("converter") or ""),
                "manifest_pipeline_version": str(doc.get("conversion_pipeline_version") or ""),
                "manifest_markdown_hash": str(doc.get("markdown_hash") or ""),
                "manifest_is_markdown": is_manifest_markdown,
            },
            attachments=[],
        )

    def _decode_strict(self, data: bytes) -> str | None:
        """严格解码（UTF-8-sig → GB18030 → UTF-16），失败返回 None。"""
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return None

    def _resolve_contained(self, relative: str, root: Path) -> Path | None:
        """把 manifest 相对路径解析到 storage.root，并强制限制在 root 内。

        越界/不存在返回 None（禁止读取任意路径）。
        """
        try:
            p = (self._storage.root / relative).resolve()
            p.relative_to(root.resolve())
            return p
        except (ValueError, OSError):
            return None

    @staticmethod
    def _looks_like_legacy_markdown(content: str, doc: dict) -> bool:
        """严格识别历史钉钉 Markdown（Phase 2.2）。

        必须**同时**满足：
        1. source_type == "dingtalk"（Frontmatter 中）；
        2. dingtalk_node_id 与当前 manifest document_id 精确相等；
        3. 存在合法 conversion_pipeline_version（非空）；
        4. converter 字段存在且为非空字符串。

        禁止仅凭任意一个 Frontmatter 字段判断。
        """
        if not content:
            return False
        from app.core.source_conversion.frontmatter import parse_frontmatter_safe
        parsed = parse_frontmatter_safe(content)
        if not parsed.matched:
            return False
        meta = dict(parsed.metadata)

        source_type = str(meta.get("source_type") or "")
        node_id = str(meta.get("dingtalk_node_id") or "")
        pipeline = str(meta.get("conversion_pipeline_version") or "").strip()
        converter = str(meta.get("converter") or "").strip()
        document_id = str(doc.get("document_id") or "")

        return bool(
            source_type == "dingtalk"
            and node_id == document_id
            and bool(pipeline)
            and bool(converter)
        )

    @staticmethod
    def _acl_from_doc(doc: dict) -> dict:
        """构建明确 ACL 结构（scope/groups/resolve_failed/raw），不压平。"""
        space_id = str(doc.get("space_id") or "")
        return {
            "scope": space_id,
            "groups": [],
            "resolve_failed": not bool(space_id),
            "raw": {"space_id": space_id} if space_id else {},
        }

    async def fetch_attachments(self, external_id: str) -> list[SourceAttachment]:
        return []  # 附件走原管线，不在 manifest 层

    async def fetch_acl(self, external_id: str) -> SourceACL:
        manifest = self._storage.read_manifest()
        doc = next(
            (d for d in manifest.get("documents", [])
             if str(d.get("document_id") or "") == external_id),
            None,
        )
        if doc is None:
            return SourceACL(scope="", raw={}, resolve_failed=True)
        space_id = str(doc.get("space_id") or "")
        if not space_id:
            # 无法解析 ACL 必须 fail closed
            return SourceACL(scope="", raw={}, resolve_failed=True)
        return SourceACL(scope=space_id, raw={"space_id": space_id}, resolve_failed=False)


def register_dingtalk_connector() -> None:
    """注册钉钉 connector（显式调用，导入不自动注册）。"""
    from app.sources.registry import register
    register("dingtalk", lambda config: DingTalkConnector(config))


def is_legacy_sync_running() -> bool:
    """检查旧钉钉入口（DingTalkSyncState 文件锁）是否正在同步。

    供新入口在触发 SourceSyncRun 前检查，避免双重同步。
    """
    from app.core.dingtalk_sync_service import DingTalkSyncState
    from app.core.dingtalk_storage import DingTalkLocalStorage

    state = DingTalkSyncState(DingTalkLocalStorage().root / "sync-task.json")
    snapshot = state.snapshot()
    return bool(snapshot.get("running"))


def is_new_sync_running(db) -> bool:
    """检查新数据源入口是否有 running/queued 的钉钉同步任务。

    供旧入口在触发前检查，避免双重同步。
    """
    from sqlalchemy import text
    count = db.execute(text(
        "SELECT COUNT(*) FROM source_sync_runs r "
        "JOIN source_connections c ON c.id = r.connection_id "
        "WHERE c.connector_key = 'dingtalk' AND r.status IN ('queued', 'running')"
    )).scalar()
    return bool(count)
