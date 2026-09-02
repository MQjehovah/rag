"""数据源标准化模型（P9-BE-02，V3 计划 4.4）。

Connector 只负责外部通信与标准化，不直接操作 Card/Wiki/图谱。
所有字段为 plain data（dataclass），便于序列化与测试。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256_str(value: str) -> bool:
    """严格 64 位小写 SHA-256（不自动 lower）。"""
    return bool(value) and bool(_SHA256_RE.match(value))


class InputRepresentation(str, Enum):
    """载荷语义（Phase 2.3）：决定 source_content_hash 的权威来源。

    - ORIGINAL：payload 就是原始资料（GitLab 原文、钉钉 raw 等）
    - PRECONVERTED_MARKDOWN：payload 是上游已生成的 Markdown（钉钉已转换）
    """

    ORIGINAL = "original"
    PRECONVERTED_MARKDOWN = "preconverted_markdown"


class SourcePayloadError(RuntimeError):
    """结构化 Connector/Payload 错误（Phase 2.2）。

    - stage：fetch / convert
    - error_code：稳定机器码（如 SOURCE_PATH_UNSAFE）
    - safe_message：面向用户的安全消息（不含绝对路径/底层 Exception 文本）
    - retryable：是否可重试
    - internal_detail：仅用于服务器日志，不进 error_message
    """

    _ALLOWED_STAGES = frozenset({"fetch", "convert"})
    _CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

    def __init__(
        self,
        error_code: str,
        *,
        stage: str = "fetch",
        safe_message: str = "",
        retryable: bool = True,
        internal_detail: str = "",
    ):
        # Phase 2.4/2.5：严格契约校验
        if not isinstance(error_code, str) or not self._CODE_RE.match(error_code):
            raise ValueError(f"SourcePayloadError.error_code 必须匹配 ^[A-Z][A-Z0-9_]*$: {error_code!r}")
        if stage not in self._ALLOWED_STAGES:
            raise ValueError(f"SourcePayloadError.stage 非法: {stage!r}（允许 fetch/convert）")
        if not isinstance(safe_message, str) or not safe_message.strip():
            raise ValueError("SourcePayloadError.safe_message 必须是非空字符串")
        if not isinstance(retryable, bool):
            raise ValueError("SourcePayloadError.retryable 必须是 bool")
        if not isinstance(internal_detail, str):
            raise ValueError("SourcePayloadError.internal_detail 必须是字符串")
        super().__init__(error_code)
        self.stage = stage
        self.error_code = error_code
        self.safe_message = safe_message.strip()
        self.retryable = retryable
        self.internal_detail = internal_detail


@dataclass
class SourceAttachment:
    """附件（如图片、二进制）。"""
    external_id: str
    name: str = ""
    mime_type: str = ""
    content: bytes = b""
    url: str = ""


@dataclass
class SourceACL:
    """ACL 范围。resolve_failed=True 表示无法解析，必须 fail closed。"""
    scope: str = ""           # 内部 acl_scope 标识（如 group/notebook id）
    raw: dict = field(default_factory=dict)
    resolve_failed: bool = False


@dataclass
class NormalizedSourceItem:
    """标准化后的来源条目（V3 计划 4.4；Phase 2.1 扩展）。

    载荷不变量（Phase 2.1）：
    - 非 deleted 条目必须提供 content（文本）或 content_bytes（二进制）中的一种；
    - 不允许通过 b" "、空格、占位字符串伪造有效载荷；
    - 同时存在 text 与 bytes 时以 content_bytes 为准（显式选择规则）；
    - content_hash 必须根据实际选中的载荷计算。
    """
    connection_id: str
    source_type: str
    external_id: str           # 连接内稳定唯一
    external_version: str = ""
    title: str = ""
    content: str = ""
    content_bytes: bytes | None = None   # Phase 2.1：二进制原始内容（PDF/Office 等）
    content_type: str = "text"
    content_hash: str = ""
    original_source_hash: str = ""       # Phase 2.1：原始来源文件/文本 hash（不冒充 markdown）
    # Phase 2.3：载荷语义（决定 source_content_hash 权威来源；不用 source_type 猜测）
    input_representation: InputRepresentation = InputRepresentation.ORIGINAL
    source_url: str = ""
    source_path: str = ""
    source_updated_at: str = ""
    deleted: bool = False
    acl_scope: dict = field(default_factory=dict)
    metadata_json: dict = field(default_factory=dict)
    attachments: list[SourceAttachment] = field(default_factory=list)

    def __post_init__(self) -> None:
        """对象级边界校验（Phase 2.5）：非法对象在构造时结构化失败。"""
        # Phase 2.5：input_representation 构造函数只接受 Enum 实例（字符串必须在 from_dict 恢复）
        if not isinstance(self.input_representation, InputRepresentation):
            raise ValueError(
                f"input_representation 必须是 InputRepresentation 实例，收到 {type(self.input_representation).__name__}"
            )
        # identity 字段：非空、strip 后规范（deleted 同样要求）
        for f in ("connection_id", "source_type", "external_id"):
            val = getattr(self, f)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"NormalizedSourceItem.{f} 必须是非空字符串")
            if val != val.strip():
                raise ValueError(f"NormalizedSourceItem.{f} 必须已是 strip 后的规范值（无首尾空白）")
        # content_type 必须字符串
        if not isinstance(self.content_type, str):
            raise ValueError("NormalizedSourceItem.content_type 必须是 str")
        # 容器类型
        from collections.abc import Mapping as _Mapping
        for f in ("acl_scope", "metadata_json"):
            if not isinstance(getattr(self, f), _Mapping):
                raise ValueError(f"NormalizedSourceItem.{f} 必须是 Mapping")
        if not isinstance(self.attachments, (list, tuple)):
            raise ValueError("NormalizedSourceItem.attachments 必须是 list/tuple")
        if not all(isinstance(a, SourceAttachment) for a in self.attachments):
            raise ValueError("NormalizedSourceItem.attachments 元素必须是 SourceAttachment")
        # 载荷类型
        if not isinstance(self.content, str):
            raise ValueError("NormalizedSourceItem.content 必须是 str")
        if self.content_bytes is not None and not isinstance(self.content_bytes, bytes):
            raise ValueError("NormalizedSourceItem.content_bytes 必须是 bytes 或 None")
        # input_representation 语义
        if self.input_representation == InputRepresentation.PRECONVERTED_MARKDOWN:
            if self.content_bytes is not None:
                raise ValueError("PRECONVERTED_MARKDOWN 不允许二进制载荷")
            if not self.content.strip():
                raise ValueError("PRECONVERTED_MARKDOWN 必须使用非空文本 content")
        # 校验 identity / type / 已提供 hash（deleted 也执行，不得提前 return）
        if self.content_hash:
            if not _is_sha256_str(self.content_hash):
                raise ValueError("content_hash 必须是严格 64 位小写 SHA-256")
            if self.content_hash != hashlib.sha256(self.effective_content).hexdigest():
                raise ValueError("content_hash 与 effective payload 不一致")
        if self.original_source_hash and not _is_sha256_str(self.original_source_hash):
            raise ValueError("original_source_hash 必须是严格 64 位小写 SHA-256")
        # deleted：不要求 payload，但以上校验已执行
        if self.deleted:
            return
        # 非 deleted：payload 必须有效（互斥 / 非空）
        has_text = bool(self.content.strip())
        has_bytes = bool(self.content_bytes)
        if has_text and has_bytes:
            raise ValueError("NormalizedSourceItem 不允许 text 与 bytes 同时非空")
        if not has_text and not has_bytes:
            raise ValueError("NormalizedSourceItem 非 deleted 必须有有效 payload")
        # ORIGINAL：original_source_hash 为空时自动按 effective payload 计算
        if self.input_representation == InputRepresentation.ORIGINAL and not self.original_source_hash:
            object.__setattr__(self, "original_source_hash", hashlib.sha256(self.effective_content).hexdigest())

    @property
    def effective_content(self) -> bytes:
        """实际选中的载荷：content_bytes 优先；否则 content 编码 UTF-8。"""
        if self.content_bytes is not None:
            return self.content_bytes
        return (self.content or "").encode("utf-8")

    @classmethod
    def from_dict(cls, data: dict) -> "NormalizedSourceItem":
        """JSON/字典反序列化：显式从字符串恢复 InputRepresentation（不经过构造函数静默归一化）。"""
        from collections.abc import Mapping as _Mapping
        if not isinstance(data, _Mapping):
            raise ValueError("NormalizedSourceItem.from_dict 输入必须是 Mapping")
        repr_raw = data.get("input_representation", InputRepresentation.ORIGINAL.value)
        if isinstance(repr_raw, str):
            try:
                repr_enum = InputRepresentation(repr_raw)
            except ValueError:
                raise ValueError(f"非法 input_representation: {repr_raw!r}")
        elif isinstance(repr_raw, InputRepresentation):
            repr_enum = repr_raw
        else:
            raise ValueError(f"input_representation 类型非法: {type(repr_raw).__name__}")
        content_bytes_raw = data.get("content_bytes")
        if content_bytes_raw is not None:
            if isinstance(content_bytes_raw, str):
                import base64
                content_bytes = base64.b64decode(content_bytes_raw, validate=True)
            else:
                content_bytes = content_bytes_raw
        else:
            content_bytes = None
        return cls(
            connection_id=data["connection_id"],
            source_type=data["source_type"],
            external_id=data["external_id"],
            external_version=data.get("external_version", ""),
            title=data.get("title", ""),
            content=data.get("content", ""),
            content_bytes=content_bytes,
            content_type=data.get("content_type", "text"),
            content_hash=data.get("content_hash", ""),
            original_source_hash=data.get("original_source_hash", ""),
            input_representation=repr_enum,
            source_url=data.get("source_url", ""),
            source_path=data.get("source_path", ""),
            source_updated_at=data.get("source_updated_at", ""),
            deleted=bool(data.get("deleted", False)),
            acl_scope=data.get("acl_scope", {}),
            metadata_json=data.get("metadata_json", {}),
            attachments=data.get("attachments", []),
        )

    @property
    def has_valid_payload(self) -> bool:
        """非 deleted 条目必须有真实载荷（拒绝空/占位）。"""
        if self.deleted:
            return True
        if self.content_bytes is not None:
            return len(self.content_bytes) > 0
        return bool((self.content or "").strip())


@dataclass
class SourceChange:
    """增量变更事件。"""
    external_id: str
    deleted: bool = False
    external_version: str = ""


@dataclass
class SourceScope:
    """Connector 暴露的发现范围（如项目/空间/目录）。"""
    scope_id: str
    name: str = ""
    kind: str = ""


@dataclass
class ConnectionTestResult:
    """连接测试结果。"""
    ok: bool
    message: str = ""
    error_code: str = ""
