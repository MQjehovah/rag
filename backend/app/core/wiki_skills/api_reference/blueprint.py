"""Phase 7B：Section Blueprint（确定性规划，不由 LLM 决定 section_key）。

规则：
- 默认 Section 顺序由 ROLES 常量决定：
  1. overview 2. authentication 3. common_conventions
  4. endpoint:<METHOD>:<PATH>:<VERSION>（每 Endpoint 一个独立 Section）
  5. data_models 6. error_codes 7. version_notes 8. knowledge_gaps 9. sources；
- section_key 由稳定 identity 生成；同一输入恒得同一 Blueprint；
- 没有内容且非 required 的 Section 可省略；required Section 缺内容仍保留；
- Endpoint 按 endpoint_id 排序；v1/v2 是不同的 Section（scope 参与 section_key）；
- ApiBlueprint 是可序列化 frozen DTO，不含 ORM/正文/Prompt/Secret。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.core.wiki_skills.api_reference.identity import build_endpoint_section_key
from app.core.wiki_skills.api_reference.schemas import (
    ApiDocumentIR,
)

# Blueprint schema 版本（Phase 7B 契约）。
API_BLUEPRINT_SCHEMA = "api-blueprint/v1"

# Section 角色（固定顺序；endpoint 顺序按 endpoint_id 稳定排列）。
SECTION_ROLES = (
    "overview",
    "authentication",
    "common_conventions",
    "endpoint",
    "data_models",
    "error_codes",
    "version_notes",
    "knowledge_gaps",
    "sources",
)

# 默认显示标题（endpoint 用模板方法单独生成）。
ROLE_HEADINGS = {
    "overview": "概述",
    "authentication": "认证",
    "common_conventions": "通用约定",
    "data_models": "数据模型",
    "error_codes": "错误码",
    "version_notes": "版本说明",
    "knowledge_gaps": "知识缺口",
    "sources": "来源资料",
}

# required Section：缺内容时仍保留并显示 knowledge gap。
REQUIRED_SECTION_ROLES = ("overview", "sources")

# 默认 role → field_paths（endpoint 由具体 Endpoint 字段生成）。
ROLE_FIELD_PATHS = {
    "overview": ("overview",),
    "authentication": ("authentication",),
    "common_conventions": ("common_headers",),
    "data_models": ("data_models",),
    "error_codes": ("common_errors",),
    "version_notes": ("version_notes",),
    "knowledge_gaps": ("knowledge_gaps",),
    "sources": ("sources",),
}

_ENDPOINT_FIELD_PATHS = (
    "method", "path", "version_scope", "summary", "description",
    "path_parameters", "query_parameters", "headers", "request_body",
    "responses", "error_codes", "examples",
)


def _validate_enum(value: Any, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}")
    return value


def _validate_bool(value: Any, name: str) -> bool:
    # 必须真 bool；bool("false") 之类不能通过。
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a real bool")
    return value


def _validate_str_sequence(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be a sequence of strings")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must contain only non-empty strings")
        result.append(item.strip())
    return tuple(result)


@dataclass(frozen=True)
class ApiSectionSpec:
    """单个 Section 的规划（Blueprint 的最小单元）。"""

    section_key: str
    section_role: str
    heading: str
    required: bool = False
    version_label: str = ""
    endpoint_id: str = ""
    field_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.section_key, str) or not self.section_key.strip():
            raise ValueError("section_key required")
        object.__setattr__(self, "section_key", self.section_key.strip())
        object.__setattr__(self, "section_role", _validate_enum(
            self.section_role, SECTION_ROLES, "section_role"))
        if not isinstance(self.heading, str) or not self.heading.strip():
            raise ValueError("heading required")
        object.__setattr__(self, "heading", self.heading.strip())
        object.__setattr__(self, "required", _validate_bool(
            self.required, "required"))
        object.__setattr__(self, "version_label",
                           (self.version_label or "").strip())
        object.__setattr__(self, "endpoint_id", (self.endpoint_id or "").strip())
        object.__setattr__(self, "field_paths", _validate_str_sequence(
            self.field_paths, "field_paths"))

    def to_dict(self) -> dict:
        return {
            "section_key": self.section_key,
            "section_role": self.section_role,
            "heading": self.heading,
            "required": self.required,
            "version_label": self.version_label,
            "endpoint_id": self.endpoint_id,
            "field_paths": list(self.field_paths),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiSectionSpec":
        allowed = {"section_key", "section_role", "heading", "required",
                   "version_label", "endpoint_id", "field_paths"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            section_key=data.get("section_key", ""),
            section_role=data.get("section_role", ""),
            heading=data.get("heading", ""),
            required=data.get("required", False),
            version_label=data.get("version_label", ""),
            endpoint_id=data.get("endpoint_id", ""),
            field_paths=data.get("field_paths", ()),
        )


@dataclass(frozen=True)
class ApiBlueprint:
    """整份内容规划（schema_version + sections；sources 为显示标签清单）。

    sources 只保存来源显示标签（来自 ApiSourceDocument.label），不是内容、
    不是 Evidence ID、不是数据库 ID。
    """

    schema_version: str = API_BLUEPRINT_SCHEMA
    sections: tuple[ApiSectionSpec, ...] = ()
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != API_BLUEPRINT_SCHEMA:
            raise ValueError("schema_version must be api-blueprint/v1")
        if not isinstance(self.sections, (tuple, list)):
            raise ValueError("sections must be a sequence")
        keys = []
        seen = set()
        for spec in self.sections:
            if not isinstance(spec, ApiSectionSpec):
                raise ValueError("sections items must be ApiSectionSpec")
            if spec.section_key in seen:
                raise ValueError(f"duplicate section_key: {spec.section_key}")
            seen.add(spec.section_key)
            keys.append(spec.section_key)
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "sources", tuple(sorted(set(
            _validate_str_sequence(self.sources, "sources")))))

    def section_keys(self) -> tuple[str, ...]:
        return tuple(s.section_key for s in self.sections)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "sections": [s.to_dict() for s in self.sections],
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiBlueprint":
        allowed = {"schema_version", "sections", "sources"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            schema_version=data.get("schema_version", API_BLUEPRINT_SCHEMA),
            sections=tuple(ApiSectionSpec.from_dict(s)
                           for s in (data.get("sections") or [])),
            sources=tuple(data.get("sources") or ()),
        )


def _endpoint_heading(endpoint) -> str:
    scope_label = endpoint.version_scope or "unversioned"
    return f"{endpoint.method} {endpoint.path}（{scope_label}）"


def plan_document(
    ir: ApiDocumentIR,
    source_labels: Iterable[str] = (),
) -> ApiBlueprint:
    """根据 ApiDocumentIR 确定性生成 Blueprint（纯函数，不修改输入）。

    不含内容且非 required 的 Section 省略；overview/sources 为 required Section。
    Endpoint 每一条独立 Section，按 endpoint_id 排序；v1/v2 天然分离。
    """
    if not isinstance(ir, ApiDocumentIR):
        raise TypeError("ir must be ApiDocumentIR")
    specs: list[ApiSectionSpec] = []

    def add(section_key: str, role: str, heading: str, *,
            required: bool = False, version_label: str = "",
            endpoint_id: str = "", field_paths: tuple[str, ...] = ()) -> None:
        specs.append(ApiSectionSpec(
            section_key=section_key, section_role=role, heading=heading,
            required=required, version_label=version_label,
            endpoint_id=endpoint_id,
            field_paths=field_paths or ROLE_FIELD_PATHS.get(role, ())))

    # 1. overview（required：缺内容保留）。
    add("overview", "overview", ROLE_HEADINGS["overview"], required=True)
    # 2. authentication（有事实才出现）。
    if ir.authentication:
        add("authentication", "authentication", ROLE_HEADINGS["authentication"])
    # 3. common_conventions（common_headers 等通用约定）。
    if ir.common_headers:
        add("common_conventions", "common_conventions",
            ROLE_HEADINGS["common_conventions"])
    # 4. endpoint：每 Endpoint 一个独立 Section（按 endpoint_id 已排序）。
    for endpoint in ir.endpoints:
        add(
            build_endpoint_section_key(
                endpoint.method, endpoint.path, endpoint.version_scope),
            "endpoint",
            _endpoint_heading(endpoint),
            version_label=endpoint.version_scope,
            endpoint_id=endpoint.endpoint_id,
            field_paths=_ENDPOINT_FIELD_PATHS,
        )
    # 5. data_models。
    if ir.data_models:
        add("data_models", "data_models", ROLE_HEADINGS["data_models"])
    # 6. error_codes（文档级 common_errors）。
    if ir.common_errors:
        add("error_codes", "error_codes", ROLE_HEADINGS["error_codes"])
    # 7. version_notes。
    if ir.version_notes:
        add("version_notes", "version_notes", ROLE_HEADINGS["version_notes"])
    # 8. knowledge_gaps。
    if ir.knowledge_gaps:
        add("knowledge_gaps", "knowledge_gaps", ROLE_HEADINGS["knowledge_gaps"])
    # 9. sources（required：显示来源资料清单）。
    add("sources", "sources", ROLE_HEADINGS["sources"], required=True,
        field_paths=("sources",))

    labels = tuple(sorted({
        (str(s) or "").strip() for s in source_labels if str(s).strip()}))
    return ApiBlueprint(sections=tuple(specs), sources=labels)
