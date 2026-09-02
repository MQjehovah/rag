"""Legacy Adapter：NormalizedSourceItem → RawSourceItem → CanonicalNote（Phase 2）。

职责：
- 把现有 Connector 输出的 NormalizedSourceItem 映射到 core RawSourceItem；
- 通过 CanonicalNoteService 得到 CanonicalNote；
- ACL 规则：标准 ACL 写入 RawSourceItem.acl；原始 acl_scope 只写入
  source_metadata["raw_acl"]；缺失/异常 fail closed；不伪造 raw_acl；
- 格式判断确定性完成（content_type + 扩展名 + 文件名 + MIME），不使用 LLM。

注意：connector_key 决定 content_kind 的默认判定（GitLab 代码 vs 钉钉 Markdown）。
"""
from __future__ import annotations

import logging
from pathlib import PurePosixPath

from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    RawSourceItem,
    SourceACLView,
    SourceBytesPayload,
    SourceTextPayload,
    source_bytes_payload,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService as _Service
from app.sources.schemas import NormalizedSourceItem

logger = logging.getLogger(__name__)

# 明确 Markdown 扩展名
_MARKDOWN_EXT = {"md", "markdown", "mdown", "mkd", "mdx"}
# 明确文本扩展名
_TEXT_EXT = {"txt", "text", "log", "rst", "adoc", "asciidoc"}
# 明确代码扩展名
_CODE_EXT = {
    "py", "js", "ts", "jsx", "tsx", "java", "kt", "c", "cpp", "cc", "h", "hpp",
    "go", "rs", "rb", "php", "cs", "swift", "scala", "sh", "bash", "zsh",
    "sql", "html", "htm", "xml", "css", "scss", "less", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "dockerfile", "makefile",
}
# 明确 CSV/TSV
_CSV_EXT = {"csv", "tsv", "tab"}
# 明确 Office
_OFFICE_EXT = {"docx", "pptx", "xlsx"}
# 明确 PDF
_PDF_EXT = {"pdf"}


def _content_kind_from_ext(ext: str) -> ContentKind | None:
    """按扩展名确定性判定 content_kind（返回 None 表示未知/交给 Registry）。"""
    ext = (ext or "").lower().lstrip(".")
    if ext in _MARKDOWN_EXT:
        return ContentKind.MARKDOWN
    if ext in _CODE_EXT:
        return ContentKind.CODE
    if ext in _CSV_EXT:
        return ContentKind.CSV
    if ext in _OFFICE_EXT:
        return ContentKind.OFFICE
    if ext in _PDF_EXT:
        return ContentKind.PDF
    if ext in _TEXT_EXT:
        return ContentKind.TEXT
    return None


def _guess_content_kind(
    source_type: str,
    content_type: str,
    source_path: str,
    filename: str,
    mime_type: str,
) -> ContentKind:
    """确定性格式判定（不用 LLM）。

    优先级：content_type（Connector 显式语义）> 扩展名 > MIME > 默认。
    """
    ct = (content_type or "").lower().strip()
    ext = ""
    if source_path and "." in PurePosixPath(source_path.replace("\\", "/")).name:
        ext = PurePosixPath(source_path.replace("\\", "/")).name.rsplit(".", 1)[-1]
    elif filename and "." in filename.replace("\\", "/").rsplit("/", 1)[-1]:
        ext = filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1]

    # content_type 显式语义（Connector 自己声明）
    if ct in ("md", "markdown", "wiki"):
        return ContentKind.MARKDOWN
    if ct == "code":
        return ContentKind.CODE
    if ct in ("csv", "tsv"):
        return ContentKind.CSV
    if ct in ("pdf", "docx", "pptx", "xlsx", "office"):
        return ContentKind.OFFICE if ct in ("docx", "pptx", "xlsx", "office") else ContentKind.PDF
    if ct in ("text", "plain", "txt"):
        return ContentKind.TEXT

    # 扩展名判定
    by_ext = _content_kind_from_ext(ext)
    if by_ext is not None:
        return by_ext

    # MIME 判定
    mime = (mime_type or "").lower()
    if mime == "application/pdf":
        return ContentKind.PDF
    if mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return ContentKind.OFFICE
    if mime in ("text/csv", "text/tab-separated-values"):
        return ContentKind.CSV
    if mime.startswith("text/") or mime in ("application/json", "application/javascript", "application/x-yaml"):
        return ContentKind.TEXT

    # 未知：GitLab 代码仓库内容可能是代码，但无证据 → UNKNOWN（交给 Registry / TEXT_PAYLOAD）
    return ContentKind.UNKNOWN


def _build_payload(item: NormalizedSourceItem, content_kind: ContentKind) -> SourceBytesPayload | SourceTextPayload:
    """构造 payload：真实载荷，不用 b" " 或占位伪造。

    - content_bytes 存在 → 始终用 bytes（PDF/Office/CSV/Text/Code 都交给 Converter 处理真实字节）；
    - content_bytes 为 None → 用文本 content；
    - 空载荷 → 抛 ValueError（不允许伪造）。
    """
    if item.content_bytes is not None:
        if not item.content_bytes:
            raise ValueError("content_bytes 为空，禁止占位伪造载荷")
        return source_bytes_payload(item.content_bytes)
    content = item.content or ""
    if not content.strip():
        raise ValueError("content 为空，禁止占位伪造载荷")
    return source_text_payload(content)


def _standard_acl_from_acl_scope(acl_scope: dict) -> SourceACLView:
    """从 NormalizedSourceItem.acl_scope 提取标准 ACL（Phase 2.1 明确结构）。

    acl_scope 结构：{"scope": str, "groups": [...], "resolve_failed": bool, "raw": {...}}。
    规则：
    - resolve_failed 必须严格 bool；缺失/类型错 → fail closed；
    - 无 scope 或非 str → fail closed；
    - raw 不参与标准 ACL（raw 中的 scope 不得覆盖标准 scope）；
    - 非法 groups → fail closed。
    """
    if not isinstance(acl_scope, dict):
        return SourceACLView()  # fail closed
    scope = acl_scope.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        return SourceACLView()  # fail closed
    resolve_failed = acl_scope.get("resolve_failed", True)
    if not isinstance(resolve_failed, bool):
        return SourceACLView()  # fail closed（类型错误）
    groups_raw = acl_scope.get("groups")
    groups_set = frozenset()
    if groups_raw is not None:
        if not isinstance(groups_raw, (list, tuple, set, frozenset)):
            return SourceACLView()  # fail closed
        if not all(isinstance(g, str) and g.strip() for g in groups_raw):
            return SourceACLView()  # fail closed
        groups_set = frozenset(g.strip() for g in groups_raw)
    # resolve_failed=False 但无 scope 且无 groups → fail closed（由 SourceACLView 兜底）
    return SourceACLView(scope=scope.strip(), groups=groups_set, resolve_failed=resolve_failed)


def to_raw_source_item(item: NormalizedSourceItem, *, connector_key: str) -> RawSourceItem:
    """把 NormalizedSourceItem 映射为 core RawSourceItem。

    Phase 2.4：输入语义由 item.input_representation 唯一决定（不靠 connector_key）：
    - PRECONVERTED_MARKDOWN → 一律 Markdown 透传（绝不进入 PDF/Office/CSV Converter）；
    - ORIGINAL → 按 content_kind/MIME/扩展名/文件头选择 Converter。
    connector_key 仅用于来源身份/格式补充，不决定"原始 or 预转换"。
    """
    from app.sources.schemas import InputRepresentation

    if item.input_representation == InputRepresentation.PRECONVERTED_MARKDOWN:
        content_kind = ContentKind.MARKDOWN
    else:
        # ORIGINAL：确定性格式判定
        content_kind = _guess_content_kind(
            item.source_type,
            item.content_type,
            item.source_path,
            item.title or item.external_id,
            "",  # NormalizedSourceItem 无独立 mime 字段
        )
    # GitLab wiki 特殊处理：wiki 若无明确 Markdown 证据 → TEXT
    if connector_key == "gitlab" and item.content_type == "wiki" and content_kind == ContentKind.MARKDOWN:
        # wiki 内容通常是 Markdown，但保守判定依赖正文——由 Converter 决定
        pass

    # 构造 payload（真实载荷；空/占位 → ValueError）
    payload = _build_payload(item, content_kind)

    # 标准 ACL（严格保留 resolve_failed）
    acl = _standard_acl_from_acl_scope(item.acl_scope)

    # source_metadata：原始元数据 + 原始 ACL（raw_acl 仅 Connector 实际提供时写）
    source_metadata: dict = {}
    if item.metadata_json:
        source_metadata = dict(item.metadata_json)
    raw_acl = (item.acl_scope or {}).get("raw") if isinstance(item.acl_scope, dict) else None
    if isinstance(raw_acl, dict) and raw_acl:
        source_metadata["raw_acl"] = dict(raw_acl)
    # 保留 original_source_hash（Connector 提供时）
    if item.original_source_hash:
        source_metadata["original_source_hash"] = item.original_source_hash

    filename = ""
    if item.source_path:
        filename = item.source_path.replace("\\", "/").rsplit("/", 1)[-1]
    elif item.title:
        filename = item.title

    return RawSourceItem(
        source_type=item.source_type,
        external_id=item.external_id,
        payload=payload,
        filename=filename,
        extension=filename.rsplit(".", 1)[-1].lower() if "." in filename else "",
        content_kind=content_kind,
        title=item.title or "",
        source_metadata=source_metadata,
        acl=acl,
        source_updated_at=item.source_updated_at,
        source_url=item.source_url,
        source_path=item.source_path,
        external_version=item.external_version,
    )


def convert_normalized(
    item: NormalizedSourceItem,
    *,
    connector_key: str,
    service: _Service | None = None,
) -> CanonicalNote:
    """统一入口：NormalizedSourceItem → CanonicalNote。

    service 可注入（测试）；默认用共享 CanonicalNoteService。
    """
    from app.core.source_conversion import CanonicalNoteService

    service = service or CanonicalNoteService()
    raw = to_raw_source_item(item, connector_key=connector_key)
    return service.convert(raw)
