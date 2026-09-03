"""Phase 7B：确定性 Markdown 渲染（模板渲染，不让 LLM 改写事实）。

规则：
- 参数名 / method / path / 状态码 / 错误码原样来自 IR，不改写、不猜测；
- 空字段不生成虚假内容；确实缺失的关键信息显示“资料未提供”；
- JSON 示例/模型 schema 输出合法 JSON（缩进稳定）；
- 每个 RenderedSection 带 evidence_ids（结构化 DTO）；
- Markdown 正文不输出内部 Evidence ID / 数据库 ID / ACL / Prompt / Secret
  （Evidence ID 只保留在结构化字段，供 7C 持久化）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.core.wiki_skills.api_reference.blueprint import ApiBlueprint
from app.core.wiki_skills.api_reference.identity import build_endpoint_section_key
from app.core.wiki_skills.api_reference.schemas import (
    ApiAuthentication,
    ApiDataModel,
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiExample,
    ApiHeader,
    ApiResponse,
)

# 缺失信息的确定性占位（渲染层唯一允许的“补充”文案）。
_NOT_PROVIDED = "资料未提供"

# RenderedSection.validation_status 初始值（compile 后由校验结果回填）。
STATUS_UNKNOWN = "unknown"


def _json_block(value: Any, default: str = "{}") -> str:
    """把 JSON-safe 内容格式化为缩进 JSON 文本；无法序列化返回占位。"""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        return default


def _desc(description: str) -> str:
    return description if description else _NOT_PROVIDED


def _scope_label(scope: str) -> str:
    return scope if scope else "unversioned"


@dataclass(frozen=True)
class ApiRenderedSection:
    """一个已渲染 Section（content 为确定性 Markdown 文本）。

    evidence_ids 为该 Section 用到的事实证据（结构化保留，不写入正文）。
    """

    section_key: str
    heading: str
    content: str = ""
    evidence_ids: tuple[str, ...] = ()
    validation_status: str = STATUS_UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.section_key, str) or not self.section_key.strip():
            raise ValueError("section_key required")
        object.__setattr__(self, "section_key", self.section_key.strip())
        if not isinstance(self.heading, str) or not self.heading.strip():
            raise ValueError("heading required")
        object.__setattr__(self, "heading", self.heading.strip())
        if not isinstance(self.content, str):
            raise ValueError("content must be str")
        object.__setattr__(self, "content", self.content.strip())
        if not isinstance(self.evidence_ids, (tuple, list)):
            raise ValueError("evidence_ids must be a string sequence")
        ids = []
        for eid in self.evidence_ids:
            if not isinstance(eid, str) or not eid.strip():
                raise ValueError("evidence_ids must be non-empty strings")
            if eid not in ids:
                ids.append(eid)
        object.__setattr__(self, "evidence_ids", tuple(sorted(ids)))
        if self.validation_status not in (STATUS_UNKNOWN, "pass", "fail"):
            raise ValueError("invalid validation_status")
        object.__setattr__(self, "validation_status", self.validation_status)

    def to_dict(self) -> dict:
        return {
            "section_key": self.section_key, "heading": self.heading,
            "content": self.content, "evidence_ids": list(self.evidence_ids),
            "validation_status": self.validation_status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiRenderedSection":
        allowed = {"section_key", "heading", "content", "evidence_ids",
                   "validation_status"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            section_key=data.get("section_key", ""),
            heading=data.get("heading", ""),
            content=data.get("content", ""),
            evidence_ids=tuple(data.get("evidence_ids") or ()),
            validation_status=data.get("validation_status", STATUS_UNKNOWN),
        )


# ---------------------------------------------------------------------------
# 各 role 的确定性内容生成
# ---------------------------------------------------------------------------


def _overview_content(ir: ApiDocumentIR) -> str:
    return ir.overview if ir.overview else _NOT_PROVIDED


def _authentication_content(ir: ApiDocumentIR) -> str:
    lines = []
    for auth in ir.authentication:
        kind = auth.kind or "?"
        scheme = f"（scheme: {auth.scheme}）" if auth.scheme else ""
        where = ""
        if auth.location:
            where = f" 位置={auth.location}"
        if auth.param_name:
            where += f" 参数={auth.param_name}"
        head = f"- {auth.name}：{_desc(auth.description)} 类型={kind}{scheme}{where}"
        lines.append(head.rstrip())
    return "\n".join(lines) if lines else _NOT_PROVIDED


def _conventions_content(ir: ApiDocumentIR) -> str:
    lines = []
    for header in ir.common_headers:
        req = "必填" if header.required else "选填"
        lines.append(f"- {header.name}（{req}）：{_desc(header.description)}")
    if not lines:
        return _NOT_PROVIDED
    return "\n".join(lines)


def _params_block(params: Sequence[Any], label: str) -> list[str]:
    lines = [f"**{label}**"]
    if not params:
        return []
    for param in params:
        req = "必填" if param.required else "选填"
        lines.append(f"- `{param.name}`（{req}）：{_desc(param.description)}")
    return lines


def _responses_block(responses: Sequence[ApiResponse]) -> list[str]:
    lines = ["**响应**"]
    if not responses:
        return []
    for response in responses:
        media = ", ".join(sorted(response.content)) if response.content else ""
        detail = _desc(response.description)
        if media:
            detail = f"{detail} 媒体类型：{media}"
        lines.append(f"- `{response.status_code}`：{detail}")
    return lines


def _error_block(error_codes: Sequence[ApiErrorCode]) -> list[str]:
    if not error_codes:
        return []
    lines = ["**错误码**"]
    for code in error_codes:
        status = f"HTTP {code.http_status}" if code.http_status else ""
        text = f"{code.code}：{_desc(code.description)}"
        if status:
            text += f"（{status}）"
        lines.append(f"- {text}")
    return lines


def _examples_block(examples: Sequence[ApiExample]) -> list[str]:
    if not examples:
        return []
    lines = ["**示例**"]
    for example in examples:
        title = example.title
        if example.media_type:
            title += f"（{example.media_type}）"
        lines.append(f"- {title}")
        content = example.to_dict().get("content")
        lines.append("```json")
        lines.append(_json_block(content))
        lines.append("```")
    return lines


def _endpoint_content(ep: ApiEndpoint, ir: ApiDocumentIR) -> str:
    lines = [
        f"**请求方法**：{ep.method}",
        f"**请求路径**：{ep.path}",
        f"**版本范围**：{_scope_label(ep.version_scope)}",
        "",
        f"**摘要**：{ep.summary if ep.summary else _NOT_PROVIDED}",
        f"**描述**：{_desc(ep.description)}",
    ]
    lines += _params_block(ep.path_parameters, "路径参数")
    lines += _params_block(ep.query_parameters, "查询参数")
    lines += _params_block(ep.headers, "请求头")

    if ep.request_body is not None:
        req = "必填" if ep.request_body.required else "选填"
        lines.append(f"**请求体**（{req}）：{_desc(ep.request_body.description)}")
        if ep.request_body.content:
            media = ", ".join(sorted(ep.request_body.content))
            lines.append(f"- 媒体类型：{media}")
    lines += _responses_block(ep.responses)
    lines += _error_block(ep.error_codes)
    lines += _examples_block(ep.examples)

    ep_gaps = [g for g in ir.knowledge_gaps
               if g.endpoint_id == ep.endpoint_id]
    if ep_gaps:
        lines.append("**知识缺口**")
        for gap in ep_gaps:
            lines.append(f"- [{gap.gap_type}] {gap.description}")
    return "\n".join(lines)


def _models_content(ir: ApiDocumentIR) -> str:
    blocks = []
    for model in ir.data_models:
        blocks.append(f"### {model.name}")
        blocks.append(_desc(model.description))
        schema_text = _json_block(model.to_dict().get("schema"))
        if schema_text != "{}":
            blocks.append("```json")
            blocks.append(schema_text)
            blocks.append("```")
    return "\n\n".join(blocks) if blocks else _NOT_PROVIDED


def _doc_errors_content(ir: ApiDocumentIR) -> str:
    lines = []
    for code in ir.common_errors:
        status = f"HTTP {code.http_status}" if code.http_status else ""
        text = f"- {code.code}：{_desc(code.description)}"
        if status:
            text += f"（{status}）"
        lines.append(text)
    return "\n".join(lines) if lines else _NOT_PROVIDED


def _version_notes_content(ir: ApiDocumentIR) -> str:
    lines = []
    for note in ir.version_notes:
        scope = note.version_scope or "unversioned"
        target = f"（{note.endpoint_id}）" if note.endpoint_id else ""
        lines.append(f"- 版本 {scope}{target}：{note.note}")
    return "\n".join(lines) if lines else _NOT_PROVIDED


def _gap_content(ir: ApiDocumentIR) -> str:
    lines = []
    for gap in ir.knowledge_gaps:
        target = f"（{gap.endpoint_id}）" if gap.endpoint_id else ""
        lines.append(f"- [{gap.gap_type}]{target} {gap.description}")
    return "\n".join(lines) if lines else _NOT_PROVIDED


def _sources_content(blueprint: ApiBlueprint) -> str:
    labels = list(blueprint.sources)
    if not labels:
        return _NOT_PROVIDED
    return "\n".join(f"- {label}" for label in labels)


# ---------------------------------------------------------------------------
# evidence_ids 汇总
# ---------------------------------------------------------------------------


def _doc_ids(ir: ApiDocumentIR, prefix: str) -> tuple[str, ...]:
    ids = sorted({
        eid
        for binding in ir.evidence_bindings
        if binding.field_path.startswith(prefix)
        for eid in binding.evidence_ids
    })
    return tuple(ids)


def _endpoint_ids(ep: ApiEndpoint) -> tuple[str, ...]:
    ids = sorted({eid for b in ep.evidence_bindings for eid in b.evidence_ids})
    return tuple(ids)


# ---------------------------------------------------------------------------
# 公共渲染入口
# ---------------------------------------------------------------------------


def render_document(
    ir: ApiDocumentIR,
    blueprint: ApiBlueprint,
) -> tuple[ApiRenderedSection, ...]:
    """按 Blueprint 顺序确定性渲染 ApiDocumentIR（纯函数，不修改输入）。"""
    endpoints_by_key = {
        build_endpoint_section_key(ep.method, ep.path, ep.version_scope): ep
        for ep in ir.endpoints
    }
    section_by_key = {s.section_key: s for s in blueprint.sections}
    rendered: list[ApiRenderedSection] = []
    for spec in blueprint.sections:
        role = spec.section_role
        content = _NOT_PROVIDED
        evidence: tuple[str, ...] = ()
        if role == "overview":
            content = _overview_content(ir)
            evidence = _doc_ids(ir, "overview")
        elif role == "authentication":
            content = _authentication_content(ir)
            evidence = _doc_ids(ir, "authentication.")
        elif role == "common_conventions":
            content = _conventions_content(ir)
            evidence = _doc_ids(ir, "common_headers.")
        elif role == "endpoint":
            ep = endpoints_by_key.get(spec.section_key)
            if ep is not None:
                content = _endpoint_content(ep, ir)
                evidence = _endpoint_ids(ep)
        elif role == "data_models":
            content = _models_content(ir)
            evidence = _doc_ids(ir, "data_models.")
        elif role == "error_codes":
            content = _doc_errors_content(ir)
            evidence = _doc_ids(ir, "common_errors.")
        elif role == "version_notes":
            content = _version_notes_content(ir)
            evidence = _doc_ids(ir, "version_notes.")
        elif role == "knowledge_gaps":
            content = _gap_content(ir)
            evidence = ()
        elif role == "sources":
            content = _sources_content(blueprint)
            evidence = ()
        rendered.append(ApiRenderedSection(
            section_key=spec.section_key, heading=spec.heading,
            content=content, evidence_ids=evidence))
    return tuple(rendered)
