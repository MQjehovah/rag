"""Phase 7A：API Reference 知识 IR 不可变数据契约。

全部公开 DTO 满足：
- frozen dataclass；构造期深度冻结（list→tuple、dict→MappingProxyType）；
- JSON-safe（拒绝 NaN/Infinity/bytes/datetime/callable/ORM/自定义对象）；
- to_dict/from_dict 对称，JSON 往返不丢字段；
- 集合顺序确定，同语义输入产出相同序列化；
- 不保存 ORM / Session / callable / Secret / 模型对象；
- 事实字段通过 ApiFieldBinding（field_path → evidence_ids）统一追溯，
  不为每个字符串设计包装类。

ApiEvidenceRef/Evidence 关系在 evidence.py 层维护（active 才可进入提取输入）。
本模块只声明 DTO、受限枚举与基础校验，不依赖其它 Phase 7A 模块。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

# API Document IR schema 版本（Phase 7A 契约）。
API_DOCUMENT_SCHEMA = "api-document-ir/v1"

# Evidence 生命周期状态。
EVIDENCE_STATUS_VALUES = ("active", "stale", "rejected")
# ApiFieldBinding.usage_type 受限枚举。
USAGE_TYPE_VALUES = ("support", "example", "conflict")
# ApiParameter.location / 参数位置受限枚举。
PARAM_LOCATION_VALUES = ("path", "query", "header", "cookie")
# ApiKnowledgeGap.gap_type 受控取值（知识缺口不得作为事实字段）。
GAP_TYPE_VALUES = (
    "missing_responses", "invalid_status", "unsupported_method", "external_ref",
    "circular_ref", "max_depth_ref", "unresolvable_ref", "no_evidence",
    "markdown_endpoint_hint", "markdown_status_hint", "ambiguous_input",
    "conflicting_facts", "unsupported_param_location",
)

# 文本长度上限。
NAME_MAX = 128
DESCRIPTION_MAX = 1024

# field_path 合法字符控制（相对所属容器，不含控制字符/空白）。
_FIELD_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-{}[\]:/ ]+$")


class EvidenceStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    REJECTED = "rejected"


class UsageType(str, Enum):
    SUPPORT = "support"
    EXAMPLE = "example"
    CONFLICT = "conflict"


class ParamLocation(str, Enum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class ApiEvidenceCoverageError(ValueError):
    """Evidence 覆盖验证失败（受控错误，供 7B Validator 调用方捕获）。

    携带结构化诊断：每个违反项为一行可读消息（稳定、确定性排序）。
    """

    def __init__(self, diagnostics=(), message: str = "") -> None:
        super().__init__(message or "evidence coverage validation failed")
        self.diagnostics = tuple(diagnostics)

    def __str__(self) -> str:
        if not self.diagnostics:
            return super().__str__()
        return f"{super().__str__()}: " + " | ".join(self.diagnostics)


def _freeze(value: Any) -> Any:
    """深度冻结：list→tuple、dict→MappingProxyType（递归）。"""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    return value


def _thaw(value: Any) -> Any:
    """深度解冻：tuple→list、MappingProxyType→dict（递归），恢复 JSON-safe 结构。"""
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _is_json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def assert_json_safe(value: Any, field_name: str) -> None:
    """严格 JSON-safe 校验（allow_nan=False：拒绝 NaN/Infinity 与 bytes 等）。"""
    if value is None:
        return
    if isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError(f"{field_name}: NaN/Infinity not allowed")
        return
    if isinstance(value, (bytes, datetime, date)):
        raise ValueError(f"{field_name}: bytes/datetime not allowed")
    if callable(value):
        raise ValueError(f"{field_name}: callable not allowed")
    if hasattr(value, "_sa_instance_state") or hasattr(value, "__table__"):
        raise ValueError(f"{field_name}: ORM object not allowed")
    if isinstance(value, (list, tuple, dict, Mapping)):
        if not _is_json_safe(_thaw(value)):
            raise ValueError(f"{field_name}: not JSON-safe")
        return
    raise ValueError(f"{field_name}: unsupported type {type(value).__name__}")


def _validate_plain_text(value: Any, field_name: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be str")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} required")
    if "\x00" in text:
        raise ValueError(f"{field_name}: control chars not allowed")
    if len(text) > max_len:
        raise ValueError(f"{field_name} too long")
    return text


def _validate_optional_text(value: Any, field_name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be str")
    text = value.strip()
    if "\x00" in text:
        raise ValueError(f"{field_name}: control chars not allowed")
    return text[:max_len]


def _validate_frozen_mapping(value: Any, field_name: str) -> MappingProxyType:
    """校验并深度冻结 JSON-safe Mapping。"""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    assert_json_safe(value, field_name)
    return _freeze(dict(value))  # type: ignore[return-value]


def _normalize_evidence_ids(values: Any) -> tuple[str, ...]:
    """evidence_ids 去重、稳定排序、非空校验。"""
    if not isinstance(values, (tuple, list)):
        raise ValueError("evidence_ids must be a sequence")
    seen = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("evidence_ids must be non-empty strings")
        if v not in seen:
            seen.append(v.strip())
    if not seen:
        raise ValueError("evidence_ids must not be empty: factual binding requires evidence")
    return tuple(sorted(seen))


def _normalize_binding_field_path(value: Any) -> str:
    path = _validate_plain_text(value, "field_path", 512)
    if not _FIELD_PATH_RE.match(path):
        raise ValueError("field_path contains unsupported characters")
    return path


def _from_dict(cls, data: Mapping[str, Any], allowed: set[str], kwargs: dict[str, Any]):
    """from_dict 公共入口：未知字段拒绝 + 构造。"""
    if not isinstance(data, Mapping):
        raise ValueError("from_dict expects a mapping")
    unknown = set(data.keys()) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Evidence / Binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiEvidenceRef:
    """一条可用 Evidence 的不可变引用（不保存 ORM/正文/Secret）。

    evidence_id/source_page_id 非空；locator 深层不可变且 JSON-safe；
    只有 status=active 的 Evidence 可进入提取输入（evidence.py 强制）。
    """

    evidence_id: str
    source_page_id: str
    source_chunk_id: str | None = None
    evidence_type: str = "openapi"
    locator: Mapping = field(default_factory=lambda: MappingProxyType({}))
    content_hash: str = ""
    status: str = "active"

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _validate_plain_text(
            self.evidence_id, "evidence_id", 128))
        object.__setattr__(self, "source_page_id", _validate_plain_text(
            self.source_page_id, "source_page_id", 128))
        if self.source_chunk_id is not None:
            if not isinstance(self.source_chunk_id, str) or not self.source_chunk_id.strip():
                raise ValueError("source_chunk_id must be a non-empty string or None")
            object.__setattr__(self, "source_chunk_id", self.source_chunk_id.strip())
        object.__setattr__(self, "evidence_type", _validate_plain_text(
            self.evidence_type, "evidence_type", 64))
        object.__setattr__(self, "locator", _validate_frozen_mapping(self.locator, "locator"))
        if not isinstance(self.content_hash, str):
            raise ValueError("content_hash must be str")
        object.__setattr__(self, "content_hash", self.content_hash.strip())
        if self.status not in EVIDENCE_STATUS_VALUES:
            raise ValueError(f"status must be one of {EVIDENCE_STATUS_VALUES}")

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "source_page_id": self.source_page_id,
            "source_chunk_id": self.source_chunk_id,
            "evidence_type": self.evidence_type,
            "locator": _thaw(self.locator),
            "content_hash": self.content_hash,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiEvidenceRef":
        allowed = {
            "evidence_id", "source_page_id", "source_chunk_id", "evidence_type",
            "locator", "content_hash", "status",
        }
        return _from_dict(cls, data, allowed, {
            "evidence_id": data.get("evidence_id", ""),
            "source_page_id": data.get("source_page_id", ""),
            "source_chunk_id": data.get("source_chunk_id"),
            "evidence_type": data.get("evidence_type", "openapi"),
            "locator": data.get("locator") or {},
            "content_hash": data.get("content_hash", ""),
            "status": data.get("status", "active"),
        })


@dataclass(frozen=True)
class ApiFieldBinding:
    """IR 字段与 Evidence 的关系（只表达关系，本阶段不落数据库）。

    field_path 非空；evidence_ids 去重、稳定排序、不可为空；
    usage_type 为受限枚举（support/example/conflict）。
    没有 Evidence 的事实字段无法通过契约（构造即失败）。
    """

    field_path: str
    evidence_ids: tuple[str, ...] = ()
    usage_type: str = "support"

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_path", _normalize_binding_field_path(self.field_path))
        object.__setattr__(self, "evidence_ids", _normalize_evidence_ids(self.evidence_ids))
        if self.usage_type not in USAGE_TYPE_VALUES:
            raise ValueError(f"usage_type must be one of {USAGE_TYPE_VALUES}")
        object.__setattr__(self, "usage_type", self.usage_type)

    def to_dict(self) -> dict:
        return {
            "field_path": self.field_path,
            "evidence_ids": list(self.evidence_ids),
            "usage_type": self.usage_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiFieldBinding":
        allowed = {"field_path", "evidence_ids", "usage_type"}
        return _from_dict(cls, data, allowed, {
            "field_path": data.get("field_path", ""),
            "evidence_ids": tuple(data.get("evidence_ids") or ()),
            "usage_type": data.get("usage_type", "support"),
        })


# ---------------------------------------------------------------------------
# API IR 子对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiAuthentication:
    """认证方案（OpenAPI securitySchemes 结构化字段，非 LLM 推测）。"""

    name: str
    description: str = ""
    kind: str = "http"
    scheme: str = ""
    location: str = ""
    param_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_plain_text(self.name, "name", NAME_MAX))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "kind", _validate_optional_text(self.kind, "kind", 64))
        object.__setattr__(self, "scheme", _validate_optional_text(self.scheme, "scheme", 64))
        object.__setattr__(self, "location", _validate_optional_text(self.location, "location", 32))
        object.__setattr__(self, "param_name", _validate_optional_text(
            self.param_name, "param_name", NAME_MAX))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "description": self.description, "kind": self.kind,
            "scheme": self.scheme, "location": self.location, "param_name": self.param_name,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiAuthentication":
        allowed = {"name", "description", "kind", "scheme", "location", "param_name"}
        return _from_dict(cls, data, allowed, {
            "name": data.get("name", ""), "description": data.get("description", ""),
            "kind": data.get("kind", "http"), "scheme": data.get("scheme", ""),
            "location": data.get("location", ""), "param_name": data.get("param_name", ""),
        })


@dataclass(frozen=True)
class ApiHeader:
    """Header 定义（OpenAPI header 结构化字段）。"""

    name: str
    description: str = ""
    required: bool = False
    schema: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_plain_text(self.name, "name", NAME_MAX))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "schema", _validate_frozen_mapping(self.schema, "schema"))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "description": self.description,
            "required": self.required, "schema": _thaw(self.schema),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiHeader":
        allowed = {"name", "description", "required", "schema"}
        return _from_dict(cls, data, allowed, {
            "name": data.get("name", ""), "description": data.get("description", ""),
            "required": bool(data.get("required", False)), "schema": data.get("schema") or {},
        })


@dataclass(frozen=True)
class ApiParameter:
    """Operation 参数（位置受限枚举 + 结构化 schema，不改写参数名）。"""

    name: str
    location: str = "query"
    required: bool = False
    description: str = ""
    schema: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_plain_text(self.name, "name", NAME_MAX))
        if self.location not in PARAM_LOCATION_VALUES:
            raise ValueError(f"location must be one of {PARAM_LOCATION_VALUES}")
        object.__setattr__(self, "location", self.location)
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "schema", _validate_frozen_mapping(self.schema, "schema"))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "location": self.location, "required": self.required,
            "description": self.description, "schema": _thaw(self.schema),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiParameter":
        allowed = {"name", "location", "required", "description", "schema"}
        return _from_dict(cls, data, allowed, {
            "name": data.get("name", ""), "location": data.get("location", "query"),
            "required": bool(data.get("required", False)),
            "description": data.get("description", ""), "schema": data.get("schema") or {},
        })


@dataclass(frozen=True)
class ApiRequestBody:
    """请求体（content 为受限 JSON-safe Mapping：媒体类型 → schema）。"""

    description: str = ""
    required: bool = False
    content: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "content", _validate_frozen_mapping(self.content, "content"))

    def to_dict(self) -> dict:
        return {
            "description": self.description, "required": self.required,
            "content": _thaw(self.content),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiRequestBody":
        allowed = {"description", "required", "content"}
        return _from_dict(cls, data, allowed, {
            "description": data.get("description", ""),
            "required": bool(data.get("required", False)),
            "content": data.get("content") or {},
        })


@dataclass(frozen=True)
class ApiResponse:
    """状态码级响应定义（状态码不可改写，非法码进入安全诊断）。"""

    status_code: str
    description: str = ""
    headers: Mapping = field(default_factory=lambda: MappingProxyType({}))
    content: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_code", _validate_plain_text(
            self.status_code, "status_code", 8))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "headers", _validate_frozen_mapping(self.headers, "headers"))
        object.__setattr__(self, "content", _validate_frozen_mapping(self.content, "content"))

    def to_dict(self) -> dict:
        return {
            "status_code": self.status_code, "description": self.description,
            "headers": _thaw(self.headers), "content": _thaw(self.content),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiResponse":
        allowed = {"status_code", "description", "headers", "content"}
        return _from_dict(cls, data, allowed, {
            "status_code": data.get("status_code", ""),
            "description": data.get("description", ""),
            "headers": data.get("headers") or {},
            "content": data.get("content") or {},
        })


@dataclass(frozen=True)
class ApiErrorCode:
    """确定性错误码（OpenAPI >=400 响应/显式错误码；Markdown 候选只进 gap）。"""

    code: str
    description: str = ""
    http_status: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _validate_plain_text(self.code, "code", 128))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "http_status", _validate_optional_text(
            self.http_status, "http_status", 8))

    def to_dict(self) -> dict:
        return {"code": self.code, "description": self.description,
                "http_status": self.http_status}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiErrorCode":
        allowed = {"code", "description", "http_status"}
        return _from_dict(cls, data, allowed, {
            "code": data.get("code", ""), "description": data.get("description", ""),
            "http_status": data.get("http_status", ""),
        })


@dataclass(frozen=True)
class ApiExample:
    """示例（声明为 JSON 的内容必须可 JSON 序列化）。"""

    title: str
    description: str = ""
    media_type: str = ""
    content: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _validate_plain_text(self.title, "title", NAME_MAX))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "media_type", _validate_optional_text(
            self.media_type, "media_type", 64))
        if self.content is not None:
            assert_json_safe(self.content, "content")
            object.__setattr__(self, "content", _freeze(self.content))

    def to_dict(self) -> dict:
        return {"title": self.title, "description": self.description,
                "media_type": self.media_type, "content": _thaw(self.content)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiExample":
        allowed = {"title", "description", "media_type", "content"}
        return _from_dict(cls, data, allowed, {
            "title": data.get("title", ""), "description": data.get("description", ""),
            "media_type": data.get("media_type", ""), "content": data.get("content"),
        })


@dataclass(frozen=True)
class ApiDataModel:
    """数据模型（components/schemas，schema 为受限 JSON-safe Mapping）。"""

    name: str
    description: str = ""
    schema: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_plain_text(self.name, "name", NAME_MAX))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "schema", _validate_frozen_mapping(self.schema, "schema"))

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "schema": _thaw(self.schema)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiDataModel":
        allowed = {"name", "description", "schema"}
        return _from_dict(cls, data, allowed, {
            "name": data.get("name", ""), "description": data.get("description", ""),
            "schema": data.get("schema") or {},
        })


@dataclass(frozen=True)
class ApiVersionNote:
    """版本说明（v1/v2 隔离；scope 记录所针对的 endpoint/全局范围）。"""

    version_scope: str
    note: str
    endpoint_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "version_scope", _validate_optional_text(
            self.version_scope, "version_scope", 64))
        object.__setattr__(self, "note", _validate_plain_text(self.note, "note", DESCRIPTION_MAX))
        object.__setattr__(self, "endpoint_id", _validate_optional_text(
            self.endpoint_id, "endpoint_id", 512))

    def to_dict(self) -> dict:
        return {"version_scope": self.version_scope, "note": self.note,
                "endpoint_id": self.endpoint_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiVersionNote":
        allowed = {"version_scope", "note", "endpoint_id"}
        return _from_dict(cls, data, allowed, {
            "version_scope": data.get("version_scope", ""),
            "note": data.get("note", ""), "endpoint_id": data.get("endpoint_id", ""),
        })


@dataclass(frozen=True)
class ApiKnowledgeGap:
    """无 Evidence / 未确定内容的受控记录，永不进入事实字段。"""

    gap_type: str
    description: str
    endpoint_id: str = ""

    def __post_init__(self) -> None:
        if self.gap_type not in GAP_TYPE_VALUES:
            raise ValueError(f"gap_type must be one of {GAP_TYPE_VALUES}")
        object.__setattr__(self, "gap_type", self.gap_type)
        object.__setattr__(self, "description", _validate_plain_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "endpoint_id", _validate_optional_text(
            self.endpoint_id, "endpoint_id", 512))

    def to_dict(self) -> dict:
        return {"gap_type": self.gap_type, "description": self.description,
                "endpoint_id": self.endpoint_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiKnowledgeGap":
        allowed = {"gap_type", "description", "endpoint_id"}
        return _from_dict(cls, data, allowed, {
            "gap_type": data.get("gap_type", ""), "description": data.get("description", ""),
            "endpoint_id": data.get("endpoint_id", ""),
        })


# ---------------------------------------------------------------------------
# 顶层容器
# ---------------------------------------------------------------------------

_LOCATION_BY_GROUP = {
    "path_parameters": "path",
    "query_parameters": "query",
    "headers": "header",
}


def _validate_parameter_group(
    params: Any, group: str, field_name: str,
) -> tuple[ApiParameter, ...]:
    """参数组容器校验：location 必须与分组一致，组内 name 唯一。"""
    if params is None:
        return ()
    if not isinstance(params, (tuple, list)):
        raise ValueError(f"{field_name} must be a sequence")
    expected = _LOCATION_BY_GROUP[group]
    result = []
    seen = set()
    for item in params:
        if not isinstance(item, ApiParameter):
            raise ValueError(f"{field_name} items must be ApiParameter")
        if item.location != expected:
            raise ValueError(
                f"{field_name} item {item.name!r} must have location={expected!r}, "
                f"got {item.location!r}")
        if item.name in seen:
            raise ValueError(f"{field_name} duplicate parameter name: {item.name}")
        seen.add(item.name)
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class ApiEndpoint:
    """单个 Endpoint IR。

    method/path/version_scope 必须与 endpoint_id 一致（构造期用 identity 重算比对，
    杜绝伪造 / LLM 改写 method/path/参数名/状态码）。参数名与大小写保持不变。
    """

    method: str
    path: str
    version_scope: str = "unversioned"
    endpoint_id: str = ""
    summary: str = ""
    description: str = ""
    path_parameters: tuple[ApiParameter, ...] = ()
    query_parameters: tuple[ApiParameter, ...] = ()
    headers: tuple[ApiParameter, ...] = ()
    request_body: ApiRequestBody | None = None
    responses: tuple[ApiResponse, ...] = ()
    error_codes: tuple[ApiErrorCode, ...] = ()
    examples: tuple[ApiExample, ...] = ()
    evidence_bindings: tuple[ApiFieldBinding, ...] = ()

    def __post_init__(self) -> None:
        from app.core.wiki_skills.api_reference.identity import (
            build_endpoint_id,
            normalize_api_path,
            normalize_http_method,
            normalize_version_scope,
        )

        method = normalize_http_method(self.method)
        path = normalize_api_path(self.path)
        scope = normalize_version_scope(self.version_scope)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "version_scope", scope)
        canonical_id = build_endpoint_id(method, path, scope)
        if self.endpoint_id and self.endpoint_id != canonical_id:
            raise ValueError(
                f"endpoint_id mismatch: got {self.endpoint_id!r}, "
                f"expected {canonical_id!r} (endpoint_id derives only from "
                f"method+path+version_scope)")
        object.__setattr__(self, "endpoint_id", canonical_id)
        object.__setattr__(self, "summary", _validate_optional_text(
            self.summary, "summary", DESCRIPTION_MAX))
        object.__setattr__(self, "description", _validate_optional_text(
            self.description, "description", DESCRIPTION_MAX))
        object.__setattr__(self, "path_parameters", _validate_parameter_group(
            self.path_parameters, "path_parameters", "path_parameters"))
        object.__setattr__(self, "query_parameters", _validate_parameter_group(
            self.query_parameters, "query_parameters", "query_parameters"))
        object.__setattr__(self, "headers", _validate_parameter_group(
            self.headers, "headers", "headers"))
        if self.request_body is not None and not isinstance(self.request_body, ApiRequestBody):
            raise ValueError("request_body must be ApiRequestBody or None")
        object.__setattr__(self, "request_body", self.request_body)
        object.__setattr__(self, "responses", self._validate_unique_list(
            self.responses, "responses", lambda r: r.status_code))
        object.__setattr__(self, "error_codes", self._validate_unique_list(
            self.error_codes, "error_codes", lambda e: e.code))
        object.__setattr__(self, "examples", self._validate_unique_list(
            self.examples, "examples", lambda e: e.title))
        object.__setattr__(self, "evidence_bindings", self._validate_unique_list(
            self.evidence_bindings, "evidence_bindings",
            lambda b: (b.field_path, tuple(b.evidence_ids), b.usage_type)))

    @staticmethod
    def _validate_unique_list(items, field_name, key_fn):
        if items is None:
            return ()
        if not isinstance(items, (tuple, list)):
            raise ValueError(f"{field_name} must be a sequence")
        result = []
        seen = set()
        for item in items:
            key = key_fn(item)
            if key in seen:
                raise ValueError(f"{field_name} duplicate entry: {key}")
            seen.add(key)
            result.append(item)
        return tuple(result)

    def to_dict(self) -> dict:
        return {
            "method": self.method, "path": self.path, "version_scope": self.version_scope,
            "endpoint_id": self.endpoint_id, "summary": self.summary,
            "description": self.description,
            "path_parameters": [p.to_dict() for p in self.path_parameters],
            "query_parameters": [p.to_dict() for p in self.query_parameters],
            "headers": [h.to_dict() for h in self.headers],
            "request_body": self.request_body.to_dict() if self.request_body else None,
            "responses": [r.to_dict() for r in self.responses],
            "error_codes": [e.to_dict() for e in self.error_codes],
            "examples": [e.to_dict() for e in self.examples],
            "evidence_bindings": [b.to_dict() for b in self.evidence_bindings],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiEndpoint":
        allowed = {
            "method", "path", "version_scope", "endpoint_id", "summary", "description",
            "path_parameters", "query_parameters", "headers", "request_body",
            "responses", "error_codes", "examples", "evidence_bindings",
        }
        return _from_dict(cls, data, allowed, {
            "method": data.get("method", ""), "path": data.get("path", ""),
            "version_scope": data.get("version_scope", "unversioned"),
            "endpoint_id": data.get("endpoint_id", ""), "summary": data.get("summary", ""),
            "description": data.get("description", ""),
            "path_parameters": tuple(ApiParameter.from_dict(p)
                                     for p in (data.get("path_parameters") or [])),
            "query_parameters": tuple(ApiParameter.from_dict(p)
                                      for p in (data.get("query_parameters") or [])),
            "headers": tuple(ApiParameter.from_dict(h) for h in (data.get("headers") or [])),
            "request_body": (ApiRequestBody.from_dict(data["request_body"])
                             if data.get("request_body") else None),
            "responses": tuple(ApiResponse.from_dict(r)
                               for r in (data.get("responses") or [])),
            "error_codes": tuple(ApiErrorCode.from_dict(e)
                                 for e in (data.get("error_codes") or [])),
            "examples": tuple(ApiExample.from_dict(e) for e in (data.get("examples") or [])),
            "evidence_bindings": tuple(ApiFieldBinding.from_dict(b)
                                       for b in (data.get("evidence_bindings") or [])),
        })


@dataclass(frozen=True)
class ApiDocumentIR:
    """整份 API 参考文档 IR（api-document-ir/v1）。

    document 级事实（overview/authentication/common_headers/data_models/
    common_errors）绑定记录在 evidence_bindings；endpoint 事实绑定在其
    evidence_bindings。无 Evidence 内容只能进入 knowledge_gaps。
    """

    schema_version: str = API_DOCUMENT_SCHEMA
    overview: str = ""
    authentication: tuple[ApiAuthentication, ...] = ()
    common_headers: tuple[ApiHeader, ...] = ()
    endpoints: tuple[ApiEndpoint, ...] = ()
    data_models: tuple[ApiDataModel, ...] = ()
    common_errors: tuple[ApiErrorCode, ...] = ()
    version_notes: tuple[ApiVersionNote, ...] = ()
    knowledge_gaps: tuple[ApiKnowledgeGap, ...] = ()
    evidence_bindings: tuple[ApiFieldBinding, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != API_DOCUMENT_SCHEMA:
            raise ValueError("schema_version must be api-document-ir/v1")
        object.__setattr__(self, "overview", _validate_optional_text(
            self.overview, "overview", DESCRIPTION_MAX))
        object.__setattr__(self, "authentication", self._check_items(
            self.authentication, "authentication", lambda a: a.name))
        object.__setattr__(self, "common_headers", self._check_items(
            self.common_headers, "common_headers", lambda h: h.name))
        endpoints = self._check_items(self.endpoints, "endpoints", lambda e: e.endpoint_id)
        object.__setattr__(self, "endpoints", tuple(
            sorted(endpoints, key=lambda e: e.endpoint_id)))
        object.__setattr__(self, "data_models", self._check_items(
            self.data_models, "data_models", lambda m: m.name))
        object.__setattr__(self, "common_errors", self._check_items(
            self.common_errors, "common_errors",
            lambda c: (c.code, c.http_status)))
        object.__setattr__(self, "version_notes", self._check_items(
            self.version_notes, "version_notes",
            lambda v: (v.endpoint_id, v.version_scope, v.note)))
        object.__setattr__(self, "knowledge_gaps", self._check_items(
            self.knowledge_gaps, "knowledge_gaps",
            lambda g: (g.endpoint_id, g.gap_type, g.description)))
        object.__setattr__(self, "evidence_bindings", self._check_items(
            self.evidence_bindings, "evidence_bindings",
            lambda b: (b.field_path, tuple(b.evidence_ids), b.usage_type)))

    @staticmethod
    def _check_items(items, field_name, key_fn):
        if items is None:
            return ()
        if not isinstance(items, (tuple, list)):
            raise ValueError(f"{field_name} must be a sequence")
        result = []
        seen = set()
        for item in items:
            key = key_fn(item)
            if key in seen:
                raise ValueError(f"{field_name} duplicate entry: {key}")
            seen.add(key)
            result.append(item)
        return tuple(result)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "overview": self.overview,
            "authentication": [a.to_dict() for a in self.authentication],
            "common_headers": [h.to_dict() for h in self.common_headers],
            "endpoints": [e.to_dict() for e in self.endpoints],
            "data_models": [m.to_dict() for m in self.data_models],
            "common_errors": [e.to_dict() for e in self.common_errors],
            "version_notes": [n.to_dict() for n in self.version_notes],
            "knowledge_gaps": [g.to_dict() for g in self.knowledge_gaps],
            "evidence_bindings": [b.to_dict() for b in self.evidence_bindings],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiDocumentIR":
        allowed = {
            "schema_version", "overview", "authentication", "common_headers",
            "endpoints", "data_models", "common_errors", "version_notes",
            "knowledge_gaps", "evidence_bindings",
        }
        return _from_dict(cls, data, allowed, {
            "schema_version": data.get("schema_version", API_DOCUMENT_SCHEMA),
            "overview": data.get("overview", ""),
            "authentication": tuple(ApiAuthentication.from_dict(a)
                                    for a in (data.get("authentication") or [])),
            "common_headers": tuple(ApiHeader.from_dict(h)
                                    for h in (data.get("common_headers") or [])),
            "endpoints": tuple(ApiEndpoint.from_dict(e) for e in (data.get("endpoints") or [])),
            "data_models": tuple(ApiDataModel.from_dict(m)
                                 for m in (data.get("data_models") or [])),
            "common_errors": tuple(ApiErrorCode.from_dict(e)
                                   for e in (data.get("common_errors") or [])),
            "version_notes": tuple(ApiVersionNote.from_dict(n)
                                   for n in (data.get("version_notes") or [])),
            "knowledge_gaps": tuple(ApiKnowledgeGap.from_dict(g)
                                    for g in (data.get("knowledge_gaps") or [])),
            "evidence_bindings": tuple(ApiFieldBinding.from_dict(b)
                                       for b in (data.get("evidence_bindings") or [])),
        })
