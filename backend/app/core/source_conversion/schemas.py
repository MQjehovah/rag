"""Phase 1.3 最终封板契约。

- 严格 Mapping 检查（顶层必须是 Mapping，不能是 list/tuple/str）；
- SourceACLView.groups 严格 list/tuple/set/frozenset 校验；
- SourceIdentity 必填（source_type/source_id），拒绝空白/类型错误；
- 所有 from_dict 严格反序列化（禁止 str() 掩盖类型错误）；
- ConverterOutput 真正的 to_dict/from_dict；
- ConverterMatch 严格校验（converter_key/specificity/priority/reason/evidence）；
- CanonicalNote 严格类型检查；
- freeze_mapping/require_mapping 公共辅助。
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SCHEMA_VERSIONS = {"canonical-note/v1"}


# ---------------------------------------------------------------------------
# 公共辅助：require_mapping / freeze_mapping
# ---------------------------------------------------------------------------


def require_mapping(value: Any, *, field_name: str) -> None:
    """顶层必须为 Mapping（dict 或其子类）；list/tuple/str/object → ValueError。"""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是 Mapping（dict），收到 {type(value).__name__}")


def _freeze_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("非有限浮点值 NaN/Infinity 被拒绝")
        return value
    raise ValueError(f"不允许的类型: {type(value).__name__}")


def freeze_mapping(value: Mapping, *, field_name: str) -> MappingProxyType:
    """冻结一个 Mapping 的顶层与嵌套值（JSON-safe，非字符串 key 拒绝）。

    只接受 Mapping；嵌套仍只允许 JSON-safe 类型。返回 MappingProxyType。
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是 Mapping，收到 {type(value).__name__}")
    result: dict = {}
    for key, val in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{field_name} 的 key 必须为 str，收到 {type(key).__name__}")
        result[key] = freeze_value(val)
    return MappingProxyType(result)


def freeze_value(value: Any) -> Any:
    """递归冻结（JSON-safe）。用于嵌套值；顶层容器校验用 require_mapping/freeze_mapping。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("非有限浮点值 NaN/Infinity 被拒绝")
        return value
    if isinstance(value, Mapping):
        return freeze_mapping(value, field_name="嵌套 mapping")
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(v) for v in value)
    if isinstance(value, (set, frozenset)):
        raise ValueError("set/frozenset 不允许作为普通 metadata（避免无序序列化）")
    raise ValueError(f"不允许的类型: {type(value).__name__}")


def thaw_value(value: Any) -> Any:
    """递归解冻为普通 JSON-compatible 数据。"""
    if isinstance(value, Mapping):
        return {k: thaw_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_value(v) for v in value]
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 str，收到 {type(value).__name__}")
    return value


def _require_str_nonempty(value: Any, field_name: str) -> str:
    s = _require_str(value, field_name)
    if not s.strip():
        raise ValueError(f"{field_name} 不能为空")
    return s


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("base64 载荷无效") from exc


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class ContentKind(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"
    CODE = "code"
    CSV = "csv"
    OFFICE = "office"
    PDF = "pdf"
    UNKNOWN = "unknown"


class ConversionStatus(str, Enum):
    CONVERTED = "converted"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# 原始载荷
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceTextPayload:
    """文本载荷。kind 固定为 text。"""

    text: str = ""
    kind: str = field(default="text", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("SourceTextPayload.text 必须是 str")

    def to_dict(self) -> dict:
        return {"kind": "text", "text": self.text}

    @classmethod
    def from_dict(cls, data: Any) -> "SourceTextPayload":
        require_mapping(data, field_name="SourceTextPayload")
        if data.get("kind") != "text":
            raise ValueError(f"未知 payload kind: {data.get('kind')!r}")
        text = data.get("text", "")
        if not isinstance(text, str):
            raise ValueError("SourceTextPayload.text 必须是 str（禁止用 str() 掩盖类型）")
        return cls(text=text)


@dataclass(frozen=True)
class SourceBytesPayload:
    """二进制载荷。kind 固定为 bytes。"""

    bytes: bytes = b""
    kind: str = field(default="bytes", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bytes, bytes):
            raise ValueError("SourceBytesPayload.bytes 必须是 bytes")

    def to_dict(self) -> dict:
        return {"kind": "bytes", "bytes": _b64(self.bytes)}

    @classmethod
    def from_dict(cls, data: Any) -> "SourceBytesPayload":
        require_mapping(data, field_name="SourceBytesPayload")
        if data.get("kind") != "bytes":
            raise ValueError(f"未知 payload kind: {data.get('kind')!r}")
        raw = data.get("bytes", "")
        if not isinstance(raw, str):
            raise ValueError("SourceBytesPayload.bytes 必须是 base64 str")
        return cls(bytes=_unb64(raw))


SourcePayload = SourceTextPayload | SourceBytesPayload


def source_text_payload(text: str) -> SourceTextPayload:
    return SourceTextPayload(text=text)


def source_bytes_payload(data: bytes) -> SourceBytesPayload:
    return SourceBytesPayload(bytes=data)


def canonical_source_hash(payload: SourcePayload) -> str:
    if isinstance(payload, SourceBytesPayload):
        return _hash_bytes(payload.bytes)
    if isinstance(payload, SourceTextPayload):
        return _hash_bytes(payload.text.encode("utf-8"))
    raise ValueError("payload 必须是 text 或 bytes")


# ---------------------------------------------------------------------------
# SourceIdentity（必填）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceIdentity:
    """来源身份。source_type/source_id 必填（strip 后非空）。"""

    source_type: str
    source_id: str
    source_url: str = ""
    source_path: str = ""
    external_version: str = ""

    def __post_init__(self) -> None:
        stype = _require_str_nonempty(self.source_type, "source_type")
        sid = _require_str_nonempty(self.source_id, "source_id")
        for field_name, value in (("source_url", self.source_url),
                                  ("source_path", self.source_path),
                                  ("external_version", self.external_version)):
            _require_str(value, field_name)
        object.__setattr__(self, "source_type", stype.strip())
        object.__setattr__(self, "source_id", sid.strip())

    @property
    def external_id(self) -> str:
        return self.source_id

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_path": self.source_path,
            "external_version": self.external_version,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SourceIdentity":
        require_mapping(data, field_name="SourceIdentity")
        return cls(
            source_type=_require_str_nonempty(data.get("source_type"), "source_type"),
            source_id=_require_str_nonempty(data.get("source_id"), "source_id"),
            source_url=_require_str(data.get("source_url", ""), "source_url"),
            source_path=_require_str(data.get("source_path", ""), "source_path"),
            external_version=_require_str(data.get("external_version", ""), "external_version"),
        )


# ---------------------------------------------------------------------------
# SourceACLView（fail closed + 严格 groups）
# ---------------------------------------------------------------------------


def _normalize_groups(groups: Any) -> frozenset[str]:
    """统一 groups 规范化：只接受 list/tuple/set/frozenset；每项非空字符串；strip+去重。"""
    if not isinstance(groups, (list, tuple, set, frozenset)):
        raise ValueError(f"ACL.groups 必须是 list/tuple/set/frozenset，收到 {type(groups).__name__}")
    normalized: set[str] = set()
    for g in groups:
        if not isinstance(g, str):
            raise ValueError(f"ACL.group 必须是 str，收到 {type(g).__name__}")
        stripped = g.strip()
        if stripped:
            normalized.add(stripped)
    return frozenset(normalized)


@dataclass(frozen=True)
class SourceACLView:
    """标准 ACL（只读）。默认 fail closed；groups 严格规范化。"""

    scope: str = ""
    groups: frozenset[str] = field(default_factory=frozenset)
    resolve_failed: bool = True

    def __post_init__(self) -> None:
        # resolve_failed 必须严格 bool，禁止 bool(value) 隐式转换
        if not isinstance(self.resolve_failed, bool):
            raise ValueError(f"ACL.resolve_failed 必须是 bool，收到 {type(self.resolve_failed).__name__}")
        scope = _require_str(self.scope, "scope").strip()
        groups = _normalize_groups(self.groups)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "groups", groups)
        if not self.resolve_failed:
            if not scope and not groups:
                raise ValueError("resolve_failed=False 但 scope 与 groups 均为空（fail closed）")

    @property
    def is_resolved(self) -> bool:
        return not self.resolve_failed

    @property
    def is_fail_closed(self) -> bool:
        return self.resolve_failed

    @classmethod
    def from_source_acl(cls, acl) -> "SourceACLView":
        """从旧 SourceACL（dict/Mapping 或带 scope/groups/resolve_failed 的对象）构建。

        安全规则：外部 ACL 的 scope/groups/resolve_failed 任一类型非法时，
        返回 fail-closed SourceACLView（不得变 resolved，不得泄露为可见权限）。
        """
        if acl is None:
            return cls()
        try:
            if isinstance(acl, Mapping):
                scope_raw = acl.get("scope", "")
                groups_raw = acl.get("groups", ())
                rf_raw = acl.get("resolve_failed", True)
            else:
                scope_raw = getattr(acl, "scope", "")
                groups_raw = getattr(acl, "groups", ())
                rf_raw = getattr(acl, "resolve_failed", True)
            if not isinstance(scope_raw, str):
                return cls()  # fail closed
            if not isinstance(rf_raw, bool):
                return cls()  # fail closed
            try:
                groups = _normalize_groups(groups_raw)
            except ValueError:
                return cls()  # fail closed
            scope = scope_raw.strip()
            if not rf_raw and not scope and not groups:
                return cls()  # fail closed
            return cls(scope=scope, groups=groups, resolve_failed=rf_raw)
        except Exception:  # noqa: BLE001
            return cls()  # 任何异常 → fail closed

    @classmethod
    def from_dict(cls, data: Any) -> "SourceACLView":
        require_mapping(data, field_name="SourceACLView")
        scope_raw = data.get("scope", "")
        if not isinstance(scope_raw, str):
            raise ValueError("ACL.scope 必须是 str")
        groups_raw = data.get("groups", ())
        groups = _normalize_groups(groups_raw)
        resolve_failed = data.get("resolve_failed", True)
        if not isinstance(resolve_failed, bool):
            raise ValueError("ACL.resolve_failed 必须是 bool")
        if not resolve_failed and not scope_raw.strip() and not groups:
            resolve_failed = True  # 伪造 → 强制 fail closed
        return cls(scope=scope_raw.strip(), groups=groups, resolve_failed=resolve_failed)

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "groups": sorted(self.groups),
            "resolve_failed": self.resolve_failed,
        }


# ---------------------------------------------------------------------------
# RawSourceItem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawSourceItem:
    source_type: str
    external_id: str
    payload: SourcePayload
    filename: str = ""
    extension: str = ""
    mime_type: str = ""
    content_kind: ContentKind = ContentKind.UNKNOWN
    title: str = ""
    source_metadata: dict = field(default_factory=dict, compare=False, repr=False)
    acl: SourceACLView = field(default_factory=SourceACLView)
    source_hash: str = field(default="", init=False)
    source_updated_at: str = ""
    source_url: str = ""
    source_path: str = ""
    external_version: str = ""

    def __post_init__(self) -> None:
        stype = _require_str_nonempty(self.source_type, "source_type").strip()
        eid = _require_str_nonempty(self.external_id, "external_id").strip()
        object.__setattr__(self, "source_type", stype)
        object.__setattr__(self, "external_id", eid)
        # 载荷互斥 + 非空
        is_text = isinstance(self.payload, SourceTextPayload)
        is_bytes = isinstance(self.payload, SourceBytesPayload)
        if is_text == is_bytes:
            raise ValueError("payload 必须是 text 或 bytes 其中之一（互斥）")
        if is_text and not self.payload.text:
            raise ValueError("payload 内容为空：text 为空")
        if is_bytes and not self.payload.bytes:
            raise ValueError("payload 内容为空：bytes 为空")
        # content_kind 严格：允许 ContentKind 或合法字符串枚举值；其余类型 → ValueError
        raw_kind = self.content_kind
        if isinstance(raw_kind, str):
            try:
                raw_kind = ContentKind(raw_kind)
            except ValueError:
                raise ValueError(f"非法 content_kind: {raw_kind!r}")
        elif not isinstance(raw_kind, ContentKind):
            raise ValueError(f"content_kind 必须是 ContentKind 或合法字符串，收到 {type(raw_kind).__name__}")
        object.__setattr__(self, "content_kind", raw_kind)
        # 先校验 filename 类型，再对 filename 调用 replace/rsplit
        filename = _require_str(self.filename, "filename")
        extension = _require_str(self.extension, "extension")
        mime_type = _require_str(self.mime_type, "mime_type")
        ext = extension.lower().lstrip(".")
        if not ext and filename:
            base = filename.replace("\\", "/").rsplit("/", 1)[-1]
            if "." in base:
                ext = base.rsplit(".", 1)[-1].lower()
        object.__setattr__(self, "extension", ext)
        object.__setattr__(self, "mime_type", mime_type.lower().split(";")[0].strip())
        # source_metadata 顶层必须是 Mapping（严格）
        require_mapping(self.source_metadata, field_name="RawSourceItem.source_metadata")
        object.__setattr__(self, "source_metadata", freeze_mapping(self.source_metadata, field_name="RawSourceItem.source_metadata"))
        # acl 必须是 SourceACLView
        if self.acl is None:
            object.__setattr__(self, "acl", SourceACLView())
        elif not isinstance(self.acl, SourceACLView):
            raise ValueError("RawSourceItem.acl 必须是 SourceACLView")
        # 其它 str 字段
        for f in ("title", "source_updated_at", "source_url", "source_path", "external_version"):
            _require_str(getattr(self, f), f)
        # source_hash
        raw_hash = _hash_bytes(self.payload.bytes) if is_bytes else _hash_bytes(self.payload.text.encode("utf-8"))
        object.__setattr__(self, "source_hash", raw_hash)

    @property
    def raw_bytes(self) -> bytes:
        if isinstance(self.payload, SourceBytesPayload):
            return self.payload.bytes
        return self.payload.text.encode("utf-8")

    @property
    def native_text(self) -> str:
        if isinstance(self.payload, SourceTextPayload):
            return self.payload.text
        return ""

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "external_id": self.external_id,
            "payload": self.payload.to_dict(),
            "filename": self.filename,
            "extension": self.extension,
            "mime_type": self.mime_type,
            "content_kind": self.content_kind.value,
            "title": self.title,
            "source_metadata": thaw_value(self.source_metadata),
            "acl": self.acl.to_dict(),
            "source_hash": self.source_hash,
            "source_updated_at": self.source_updated_at,
            "source_url": self.source_url,
            "source_path": self.source_path,
            "external_version": self.external_version,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RawSourceItem":
        require_mapping(data, field_name="RawSourceItem")
        payload_data = data.get("payload")
        if not isinstance(payload_data, Mapping):
            raise ValueError("RawSourceItem.payload 必须是 Mapping")
        kind = payload_data.get("kind")
        if kind == "bytes":
            payload = SourceBytesPayload.from_dict(payload_data)
        elif kind == "text":
            payload = SourceTextPayload.from_dict(payload_data)
        else:
            raise ValueError(f"未知 payload kind: {kind!r}")
        source_metadata_raw = data.get("source_metadata")
        if not isinstance(source_metadata_raw, Mapping):
            raise ValueError("RawSourceItem.source_metadata 必须是 Mapping")
        # acl：缺失 → 默认 fail-closed；显式 None 或非法类型（list/str）→ ValueError
        if "acl" not in data:
            acl = SourceACLView()  # 缺失 → 默认 fail closed
        else:
            acl_raw = data["acl"]
            if acl_raw is None:
                raise ValueError("RawSourceItem.acl 显式 None 被拒绝")
            if not isinstance(acl_raw, Mapping):
                raise ValueError(f"RawSourceItem.acl 必须是 Mapping，收到 {type(acl_raw).__name__}")
            acl = SourceACLView.from_dict(acl_raw)
        item = cls(
            source_type=_require_str_nonempty(data.get("source_type"), "source_type"),
            external_id=_require_str_nonempty(data.get("external_id"), "external_id"),
            payload=payload,
            filename=_require_str(data.get("filename", ""), "filename"),
            extension=_require_str(data.get("extension", ""), "extension"),
            mime_type=_require_str(data.get("mime_type", ""), "mime_type"),
            content_kind=ContentKind(_require_str(data.get("content_kind", ContentKind.UNKNOWN.value), "content_kind")),
            title=_require_str(data.get("title", ""), "title"),
            source_metadata=dict(source_metadata_raw),
            acl=acl,
            source_updated_at=_require_str(data.get("source_updated_at", ""), "source_updated_at"),
            source_url=_require_str(data.get("source_url", ""), "source_url"),
            source_path=_require_str(data.get("source_path", ""), "source_path"),
            external_version=_require_str(data.get("external_version", ""), "external_version"),
        )
        persisted = data.get("source_hash", "")
        if isinstance(persisted, str) and persisted:
            if persisted != item.source_hash:
                raise ValueError("source_hash 与原始 payload 不匹配，拒绝恢复")
        elif persisted:
            raise ValueError("source_hash 必须是 str")
        return item


# ---------------------------------------------------------------------------
# ConversionDiagnostic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionDiagnostic:
    code: str
    severity: DiagnosticSeverity = DiagnosticSeverity.WARNING
    message: str = ""
    detail: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        code = _require_str_nonempty(self.code, "code")
        object.__setattr__(self, "code", code.strip())
        # severity 严格：允许 DiagnosticSeverity 或合法字符串枚举值；其余类型 → ValueError
        if isinstance(self.severity, str):
            try:
                object.__setattr__(self, "severity", DiagnosticSeverity(self.severity))
            except ValueError:
                raise ValueError(f"非法 severity: {self.severity!r}")
        elif not isinstance(self.severity, DiagnosticSeverity):
            raise ValueError(f"severity 必须是 DiagnosticSeverity 或合法字符串，收到 {type(self.severity).__name__}")
        message = _require_str(self.message, "message")
        object.__setattr__(self, "message", message)
        require_mapping(self.detail, field_name="ConversionDiagnostic.detail")
        object.__setattr__(self, "detail", freeze_mapping(self.detail, field_name="ConversionDiagnostic.detail"))

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "detail": thaw_value(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConversionDiagnostic":
        require_mapping(data, field_name="ConversionDiagnostic")
        detail_raw = data.get("detail")
        if not isinstance(detail_raw, Mapping):
            raise ValueError("ConversionDiagnostic.detail 必须是 Mapping")
        return cls(
            code=_require_str_nonempty(data.get("code"), "code"),
            severity=DiagnosticSeverity(_require_str(data.get("severity", DiagnosticSeverity.WARNING.value), "severity")),
            message=_require_str(data.get("message", ""), "message"),
            detail=dict(detail_raw),
        )

    @property
    def is_error(self) -> bool:
        return self.severity == DiagnosticSeverity.ERROR

    @property
    def is_warning(self) -> bool:
        return self.severity == DiagnosticSeverity.WARNING


# ---------------------------------------------------------------------------
# ConverterOutput（真正的 to_dict/from_dict）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConverterOutput:
    """Converter 唯一输出。含真实 to_dict/from_dict。"""

    body: str = ""
    status_signal: ConversionStatus | None = None
    diagnostics: tuple = ()
    conversion_metadata: dict = field(default_factory=dict, compare=False, repr=False)
    content_kind: ContentKind = ContentKind.UNKNOWN
    suggested_title: str | None = None
    source_metadata_extra: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        body = _require_str(self.body, "body")
        if body and not body.strip():
            body = ""  # whitespace-only 视为空
        object.__setattr__(self, "body", body)
        if self.diagnostics is None:
            raise ValueError("ConverterOutput.diagnostics 不能为 None（须为可迭代对象）")
        diags = tuple(self.diagnostics)
        for d in diags:
            if not isinstance(d, ConversionDiagnostic):
                raise ValueError("ConverterOutput.diagnostics 必须是 ConversionDiagnostic 对象")
        object.__setattr__(self, "diagnostics", diags)
        if self.status_signal is not None:
            if not isinstance(self.status_signal, ConversionStatus):
                try:
                    object.__setattr__(self, "status_signal", ConversionStatus(self.status_signal))
                except ValueError:
                    raise ValueError(f"非法 status_signal: {self.status_signal!r}")
            if self.status_signal == ConversionStatus.BLOCKED:
                raise ValueError("ConverterOutput 不允许返回 blocked")
        if not isinstance(self.content_kind, ContentKind):
            try:
                object.__setattr__(self, "content_kind", ContentKind(self.content_kind))
            except ValueError:
                raise ValueError(f"非法 content_kind: {self.content_kind!r}")
        if self.suggested_title is not None and not isinstance(self.suggested_title, str):
            raise ValueError("ConverterOutput.suggested_title 必须是 str 或 None")
        require_mapping(self.conversion_metadata, field_name="ConverterOutput.conversion_metadata")
        require_mapping(self.source_metadata_extra, field_name="ConverterOutput.source_metadata_extra")
        object.__setattr__(self, "conversion_metadata", freeze_mapping(self.conversion_metadata, field_name="ConverterOutput.conversion_metadata"))
        object.__setattr__(self, "source_metadata_extra", freeze_mapping(self.source_metadata_extra, field_name="ConverterOutput.source_metadata_extra"))

    def to_dict(self) -> dict:
        return {
            "body": self.body,
            "status_signal": self.status_signal.value if self.status_signal else None,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "conversion_metadata": thaw_value(self.conversion_metadata),
            "content_kind": self.content_kind.value,
            "suggested_title": self.suggested_title,
            "source_metadata_extra": thaw_value(self.source_metadata_extra),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConverterOutput":
        require_mapping(data, field_name="ConverterOutput")
        signal_raw = data.get("status_signal")
        if signal_raw is not None and not isinstance(signal_raw, str):
            raise ValueError("ConverterOutput.status_signal 必须是 str 或 None")
        if signal_raw is not None:
            try:
                signal = ConversionStatus(signal_raw)
            except ValueError:
                raise ValueError(f"非法 status_signal: {signal_raw!r}")
            if signal == ConversionStatus.BLOCKED:
                raise ValueError("ConverterOutput 不允许恢复为 blocked")
        else:
            signal = None
        diag_raw = data.get("diagnostics", [])
        if not isinstance(diag_raw, (list, tuple)):
            raise ValueError("ConverterOutput.diagnostics 必须是 list/tuple")
        diags = tuple(ConversionDiagnostic.from_dict(d) for d in diag_raw)
        conv_meta_raw = data.get("conversion_metadata")
        if not isinstance(conv_meta_raw, Mapping):
            raise ValueError("ConverterOutput.conversion_metadata 必须是 Mapping")
        extra_raw = data.get("source_metadata_extra")
        if not isinstance(extra_raw, Mapping):
            raise ValueError("ConverterOutput.source_metadata_extra 必须是 Mapping")
        kind_raw = data.get("content_kind", ContentKind.UNKNOWN.value)
        if not isinstance(kind_raw, str):
            raise ValueError("ConverterOutput.content_kind 必须是 str")
        try:
            kind = ContentKind(kind_raw)
        except ValueError:
            raise ValueError(f"非法 content_kind: {kind_raw!r}")
        return cls(
            body=_require_str(data.get("body", ""), "body"),
            status_signal=signal,
            diagnostics=diags,
            conversion_metadata=dict(conv_meta_raw),
            content_kind=kind,
            suggested_title=data.get("suggested_title"),
            source_metadata_extra=dict(extra_raw),
        )


# ---------------------------------------------------------------------------
# CanonicalNote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalNote:
    body: str
    title: str = ""
    schema_version: str = "canonical-note/v1"
    identity: SourceIdentity = None  # type: ignore[assignment]
    source_metadata: dict = field(default_factory=dict, compare=False, repr=False)
    acl: SourceACLView = field(default_factory=SourceACLView)
    converter_key: str = ""
    converter_version: str = ""
    conversion_status: ConversionStatus = ConversionStatus.CONVERTED
    diagnostics: tuple = field(default_factory=tuple)
    conversion_metadata: dict = field(default_factory=dict, compare=False, repr=False)
    source_hash: str = ""
    body_hash: str = field(default="", init=False)
    content_kind: ContentKind = ContentKind.UNKNOWN

    def __post_init__(self) -> None:
        # identity 必填（不使用空 default_factory）
        if self.identity is None:
            raise ValueError("CanonicalNote.identity 必填")
        if not isinstance(self.identity, SourceIdentity):
            raise ValueError("CanonicalNote.identity 必须是 SourceIdentity")
        body = _require_str(self.body, "body")
        object.__setattr__(self, "body", body)
        for f in ("title", "schema_version", "converter_key", "converter_version", "source_hash"):
            _require_str(getattr(self, f), f)
        if self.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version: {self.schema_version!r}")
        # converter_key/version 规范化：strip 后存储；纯空白拒绝
        if self.conversion_status in (ConversionStatus.CONVERTED, ConversionStatus.PARTIAL, ConversionStatus.FAILED):
            if not self.converter_key.strip():
                raise ValueError("converter_key 在 converted/partial/failed 状态必须 strip 后非空")
            if not self.converter_version.strip():
                raise ValueError("converter_version 在 converted/partial/failed 状态必须 strip 后非空")
            object.__setattr__(self, "converter_key", self.converter_key.strip())
            object.__setattr__(self, "converter_version", self.converter_version.strip())
        # conversion_status 严格
        if isinstance(self.conversion_status, str):
            try:
                object.__setattr__(self, "conversion_status", ConversionStatus(self.conversion_status))
            except ValueError:
                raise ValueError(f"非法 conversion_status: {self.conversion_status!r}")
        elif not isinstance(self.conversion_status, ConversionStatus):
            raise ValueError(f"conversion_status 必须是 ConversionStatus 或合法字符串，收到 {type(self.conversion_status).__name__}")
        # content_kind 严格（含 failed/blocked 状态）
        if isinstance(self.content_kind, str):
            try:
                object.__setattr__(self, "content_kind", ContentKind(self.content_kind))
            except ValueError:
                raise ValueError(f"非法 content_kind: {self.content_kind!r}")
        elif not isinstance(self.content_kind, ContentKind):
            raise ValueError(f"content_kind 必须是 ContentKind 或合法字符串，收到 {type(self.content_kind).__name__}")
        if self.diagnostics is None:
            raise ValueError("CanonicalNote.diagnostics 不能为 None（须为可迭代对象）")
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        for d in self.diagnostics:
            if not isinstance(d, ConversionDiagnostic):
                raise ValueError("CanonicalNote.diagnostics 必须是 ConversionDiagnostic 对象")
        require_mapping(self.source_metadata, field_name="CanonicalNote.source_metadata")
        require_mapping(self.conversion_metadata, field_name="CanonicalNote.conversion_metadata")
        object.__setattr__(self, "source_metadata", freeze_mapping(self.source_metadata, field_name="CanonicalNote.source_metadata"))
        object.__setattr__(self, "conversion_metadata", freeze_mapping(self.conversion_metadata, field_name="CanonicalNote.conversion_metadata"))
        if not isinstance(self.acl, SourceACLView):
            raise ValueError("CanonicalNote.acl 必须是 SourceACLView")
        object.__setattr__(self, "body_hash", _hash_bytes(self.body.encode("utf-8")))
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        body = self.body.strip()
        status = self.conversion_status
        has_error = any(d.is_error for d in self.diagnostics)
        has_warning = any(d.is_warning for d in self.diagnostics)

        if status == ConversionStatus.CONVERTED:
            if not body:
                raise ValueError("converted 但正文为空")
            if has_error or has_warning:
                raise ValueError("converted 但存在 warning/error diagnostic")
        elif status == ConversionStatus.PARTIAL:
            if not body:
                raise ValueError("partial 但正文为空")
        elif status == ConversionStatus.FAILED:
            if body:
                raise ValueError("failed 但存在正文")
            if not has_error:
                raise ValueError("failed 但无 error diagnostic")
        elif status == ConversionStatus.BLOCKED:
            if body:
                raise ValueError("blocked 但存在正文")
            if not has_error:
                raise ValueError("blocked 但无说明阻断原因的 error diagnostic")
        else:
            raise ValueError(f"非法 conversion_status: {status!r}")

        if status in (ConversionStatus.CONVERTED, ConversionStatus.PARTIAL, ConversionStatus.FAILED):
            if not self.converter_key or not self.converter_version:
                raise ValueError("converted/partial/failed 必须携带 converter_key 与 converter_version")

        if not _SHA256_RE.match(self.source_hash or ""):
            raise ValueError("source_hash 必须是 64 位十六进制 SHA-256")

        if status in (ConversionStatus.CONVERTED, ConversionStatus.PARTIAL) and self.content_kind == ContentKind.UNKNOWN:
            raise ValueError("converted/partial 的 content_kind 不得为 unknown")

    @property
    def source_type(self) -> str:
        return self.identity.source_type

    @property
    def source_id(self) -> str:
        return self.identity.source_id

    @property
    def warnings(self) -> tuple[dict, ...]:
        return tuple(
            d.to_dict()
            for d in self.diagnostics
            if d.severity in (DiagnosticSeverity.WARNING, DiagnosticSeverity.ERROR)
        )

    def to_dict(self) -> dict:
        return {
            "body": self.body,
            "title": self.title,
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "source_metadata": thaw_value(self.source_metadata),
            "acl": self.acl.to_dict(),
            "converter_key": self.converter_key,
            "converter_version": self.converter_version,
            "conversion_status": self.conversion_status.value,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "conversion_metadata": thaw_value(self.conversion_metadata),
            "source_hash": self.source_hash,
            "body_hash": self.body_hash,
            "content_kind": self.content_kind.value,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CanonicalNote":
        require_mapping(data, field_name="CanonicalNote")
        body_raw = data.get("body", "")
        if not isinstance(body_raw, str):
            raise ValueError("CanonicalNote.body 必须是 str（禁止 str() 把 list/dict 转字符串）")
        schema = data.get("schema_version", "canonical-note/v1")
        if not isinstance(schema, str):
            raise ValueError("CanonicalNote.schema_version 必须是 str")
        if schema not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version: {schema!r}")
        identity_raw = data.get("identity")
        if not isinstance(identity_raw, Mapping):
            raise ValueError("CanonicalNote.identity 必须是 Mapping")
        source_meta_raw = data.get("source_metadata")
        if not isinstance(source_meta_raw, Mapping):
            raise ValueError("CanonicalNote.source_metadata 必须是 Mapping")
        conv_meta_raw = data.get("conversion_metadata")
        if not isinstance(conv_meta_raw, Mapping):
            raise ValueError("CanonicalNote.conversion_metadata 必须是 Mapping")
        status_raw = data.get("conversion_status", ConversionStatus.CONVERTED.value)
        if not isinstance(status_raw, str):
            raise ValueError("CanonicalNote.conversion_status 必须是 str")
        try:
            status = ConversionStatus(status_raw)
        except ValueError:
            raise ValueError(f"非法 conversion_status: {status_raw!r}")
        kind_raw = data.get("content_kind", ContentKind.UNKNOWN.value)
        if not isinstance(kind_raw, str):
            raise ValueError("CanonicalNote.content_kind 必须是 str")
        try:
            kind = ContentKind(kind_raw)
        except ValueError:
            raise ValueError(f"非法 content_kind: {kind_raw!r}")
        source_hash = data.get("source_hash", "")
        if not isinstance(source_hash, str):
            raise ValueError("CanonicalNote.source_hash 必须是 str")
        # acl：缺失 → 默认 fail-closed；显式 None 或非法类型（list/str）→ ValueError
        if "acl" not in data:
            acl = SourceACLView()
        else:
            acl_raw = data["acl"]
            if acl_raw is None:
                raise ValueError("CanonicalNote.acl 显式 None 被拒绝")
            if not isinstance(acl_raw, Mapping):
                raise ValueError(f"CanonicalNote.acl 必须是 Mapping，收到 {type(acl_raw).__name__}")
            acl = SourceACLView.from_dict(acl_raw)
        # diagnostics：必须 list/tuple（JSON 边界只接受 list）；""/{} → ValueError
        diag_raw = data.get("diagnostics", [])
        if not isinstance(diag_raw, (list, tuple)):
            raise ValueError(f"CanonicalNote.diagnostics 必须是 list/tuple，收到 {type(diag_raw).__name__}")
        note = cls(
            body=body_raw,
            title=_require_str(data.get("title", ""), "title"),
            schema_version=schema,
            identity=SourceIdentity.from_dict(identity_raw),
            source_metadata=dict(source_meta_raw),
            acl=acl,
            converter_key=_require_str(data.get("converter_key", ""), "converter_key"),
            converter_version=_require_str(data.get("converter_version", ""), "converter_version"),
            conversion_status=status,
            diagnostics=tuple(ConversionDiagnostic.from_dict(d) for d in diag_raw),
            conversion_metadata=dict(conv_meta_raw),
            source_hash=source_hash,
            content_kind=kind,
        )
        persisted_body = data.get("body_hash", "")
        if isinstance(persisted_body, str) and persisted_body:
            if persisted_body != note.body_hash:
                raise ValueError("body_hash 与 body 不匹配，拒绝恢复")
        elif persisted_body:
            raise ValueError("body_hash 必须是 str")
        return note
