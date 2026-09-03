"""Phase 7B：Validator（结构化 ApiValidationReport，不抛内部异常文本）。

检查范围：
- Evidence coverage 完整（validate_evidence_coverage）；
- HTTP method/path/endpoint_id 一致、endpoint_id 唯一、method+path+version 不重复；
- path 参数名与 `{parameter}` 对应、path 参数必须 required、location 合法；
- response status 合法；JSON 示例可序列化；
- Blueprint section_key 唯一且稳定、required Section 存在；
- RenderedSection 与 Blueprint 一一对应；
- RenderedSection 不引用 IR 中不存在的端点/状态码；不泄漏 Evidence ID；
- v1/v2 Section 隔离（同一 method+path 不同 scope 是不同 section，不混写）。

校验失败以 ApiValidationReport 结构化返回；错误 → status='fail'，
不抛内部异常文本。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.core.wiki_skills.api_reference.blueprint import (
    REQUIRED_SECTION_ROLES,
    ApiBlueprint,
    ApiSectionSpec,
    plan_document,
)
from app.core.wiki_skills.api_reference.evidence import evidence_coverage_issues
from app.core.wiki_skills.api_reference.identity import (
    build_endpoint_id,
    build_endpoint_section_key,
)
from app.core.wiki_skills.api_reference.schemas import (
    ApiDocumentIR,
    ApiEndpoint,
)

VALIDATION_REPORT_SCHEMA = "api-validation/v1"
ISSUE_SEVERITIES = ("error", "warning")

_PATH_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_STATUS_RE = re.compile(r"^(?:default|[1-5][0-9]{2}|[1-5]XX)$")
_NUMERIC_STATUS_RE = re.compile(r"`([1-5][0-9]{2})`")


@dataclass(frozen=True)
class ApiValidationIssue:
    """单条校验问题（结构化，可安全序列化）。"""

    code: str
    severity: str = "error"
    section_key: str = ""
    field_path: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code required")
        object.__setattr__(self, "code", self.code.strip())
        if self.severity not in ISSUE_SEVERITIES:
            raise ValueError(f"severity must be one of {ISSUE_SEVERITIES}")
        object.__setattr__(self, "section_key", self.section_key.strip())
        object.__setattr__(self, "field_path", self.field_path.strip())
        if not isinstance(self.message, str):
            raise ValueError("message must be str")
        object.__setattr__(self, "message", self.message.strip())

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "section_key": self.section_key, "field_path": self.field_path,
                "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiValidationIssue":
        allowed = {"code", "severity", "section_key", "field_path", "message"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            code=data.get("code", ""),
            severity=data.get("severity", "error"),
            section_key=data.get("section_key", ""),
            field_path=data.get("field_path", ""),
            message=data.get("message", ""),
        )


@dataclass(frozen=True)
class ApiValidationReport:
    """整份校验报告（pass/fail + issues）。"""

    schema_version: str = VALIDATION_REPORT_SCHEMA
    status: str = "pass"
    issues: tuple[ApiValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATION_REPORT_SCHEMA:
            raise ValueError("schema_version must be api-validation/v1")
        if self.status not in ("pass", "fail"):
            raise ValueError("status must be pass or fail")
        object.__setattr__(self, "issues", tuple(self.issues))
        errors = [i for i in self.issues if i.severity == "error"]
        if self.status == "pass" and errors:
            raise ValueError("report status must be fail when error issues exist")
        if self.status == "fail" and not errors:
            raise ValueError("report status fail requires at least one error issue")

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "status": self.status,
                "issues": [i.to_dict() for i in self.issues]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiValidationReport":
        allowed = {"schema_version", "status", "issues"}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            schema_version=data.get("schema_version", VALIDATION_REPORT_SCHEMA),
            status=data.get("status", "pass"),
            issues=tuple(ApiValidationIssue.from_dict(i)
                         for i in (data.get("issues") or [])),
        )


# ---------------------------------------------------------------------------
# 校验实现
# ---------------------------------------------------------------------------


def _valid_status(code: Any) -> bool:
    return isinstance(code, str) and bool(_STATUS_RE.match(code.strip()))


def validate_compile(
    ir: ApiDocumentIR,
    blueprint: ApiBlueprint,
    sections: Sequence[Any],
    available_active_ids: Iterable[str] = (),
) -> ApiValidationReport:
    """IR + Blueprint + RenderedSections 的整体校验（纯函数）。"""
    if not isinstance(ir, ApiDocumentIR):
        raise TypeError("ir must be ApiDocumentIR")
    if not isinstance(blueprint, ApiBlueprint):
        raise TypeError("blueprint must be ApiBlueprint")
    available = set(available_active_ids)
    issues: list[ApiValidationIssue] = []
    endpoint_by_key: dict[str, ApiEndpoint] = {}

    # --- IR 级：coverage / identity / 重复 / 参数 / 状态 / 示例 ---
    for coverage in evidence_coverage_issues(ir, available):
        issues.append(ApiValidationIssue(
            code="EVIDENCE_COVERAGE", field_path="document",
            message=coverage))

    seen_ids: dict[str, str] = {}
    seen_triple: dict[tuple[str, str, str], str] = {}
    for ep in ir.endpoints:
        canonical_id = build_endpoint_id(
            ep.method, ep.path, ep.version_scope)
        if ep.endpoint_id != canonical_id:
            issues.append(ApiValidationIssue(
                code="ENDPOINT_ID_MISMATCH", section_key=ep.endpoint_id,
                message=f"endpoint_id {ep.endpoint_id!r} != canonical "
                        f"{canonical_id!r}"))
        triple = (ep.method, ep.path, ep.version_scope)
        if ep.endpoint_id in seen_ids:
            issues.append(ApiValidationIssue(
                code="DUPLICATE_ENDPOINT", section_key=ep.endpoint_id,
                message=f"duplicate endpoint_id {ep.endpoint_id!r}"))
        else:
            seen_ids[ep.endpoint_id] = triple
        if triple in seen_triple and seen_triple[triple] != ep.endpoint_id:
            issues.append(ApiValidationIssue(
                code="ENDPOINT_REPEATED", section_key=ep.endpoint_id,
                message=f"method+path+version repeated: {triple}"))
        else:
            seen_triple[triple] = ep.endpoint_id

        braces = set(_PATH_BRACE_RE.findall(ep.path))
        path_declared = set()
        for group_name in ("path_parameters", "query_parameters", "headers"):
            for param in getattr(ep, group_name):
                if group_name == "path_parameters":
                    if param.name not in braces:
                        issues.append(ApiValidationIssue(
                            code="PATH_PARAM_MISMATCH",
                            section_key=ep.endpoint_id,
                            field_path=f"{group_name}.{param.name}",
                            message=f"path parameter {param.name!r} not in "
                                    f"path template {ep.path!r}"))
                    if not param.required:
                        issues.append(ApiValidationIssue(
                            code="PATH_PARAM_NOT_REQUIRED",
                            section_key=ep.endpoint_id,
                            field_path=f"{group_name}.{param.name}",
                            message=f"path parameter {param.name!r} must be "
                                    f"required"))
                    path_declared.add(param.name)
        # 路径模板中的 {param} 必须在 path_parameters 中声明。
        for name in sorted(braces):
            if name not in path_declared:
                issues.append(ApiValidationIssue(
                    code="PATH_PARAM_MISMATCH", section_key=ep.endpoint_id,
                    field_path=f"path_parameters.{name}",
                    message=f"path template {ep.path!r} references undeclared "
                            f"parameter {name!r}"))
        # 身份与 section key 对应关系。
        endpoint_by_key[build_endpoint_section_key(
            ep.method, ep.path, ep.version_scope)] = ep

        for response in ep.responses:
            if not _valid_status(response.status_code):
                issues.append(ApiValidationIssue(
                    code="INVALID_RESPONSE_STATUS", section_key=ep.endpoint_id,
                    field_path=f"responses.{response.status_code}",
                    message=f"invalid response status "
                            f"{response.status_code!r}"))
        for example in ep.examples:
            try:
                json.dumps(example.to_dict(), ensure_ascii=False,
                           allow_nan=False)
            except (TypeError, ValueError):
                issues.append(ApiValidationIssue(
                    code="INVALID_EXAMPLE_JSON", section_key=ep.endpoint_id,
                    field_path=f"examples.{example.title}",
                    message=f"example {example.title!r} not JSON-serializable"))

    # 校验对伪造的 method/path 进 IR 的兜底：identity 三重即身份，已在上面校验。

    # --- Blueprint 级 ---
    bp_keys = blueprint.section_keys()
    if len(set(bp_keys)) != len(bp_keys):
        issues.append(ApiValidationIssue(
            code="BLUEPRINT_DUPLICATE_SECTION_KEY",
            message="blueprint contains duplicate section_key"))
    for required_role in REQUIRED_SECTION_ROLES:
        if not any(s.section_role == required_role for s in blueprint.sections):
            issues.append(ApiValidationIssue(
                code="REQUIRED_SECTION_MISSING",
                message=f"required section role {required_role!r} missing"))
    # 稳定：用相同输入重新规划应与给定 Blueprint 一致。
    recomputed = plan_document(ir, blueprint.sources).section_keys()
    if recomputed != bp_keys:
        issues.append(ApiValidationIssue(
            code="UNSTABLE_BLUEPRINT",
            message="blueprint not reproducible from the same IR"))

    # --- Rendered 级 ---
    rendered_keys = [getattr(s, "section_key", None) for s in sections]
    if len(rendered_keys) != len(bp_keys) or rendered_keys != list(bp_keys):
        issues.append(ApiValidationIssue(
            code="SECTION_MISMATCH",
            message="rendered sections must match blueprint one-to-one "
                    "in order"))

    section_by_key = {s.section_key: s for s in sections}
    for spec in blueprint.sections:
        rendered = section_by_key.get(spec.section_key)
        if rendered is None:
            issues.append(ApiValidationIssue(
                code="SECTION_MISMATCH", section_key=spec.section_key,
                message=f"section {spec.section_key!r} not rendered"))
            continue
        content = getattr(rendered, "content", "")
        if spec.section_role == "endpoint":
            ep = endpoint_by_key.get(spec.section_key)
            if ep is None:
                issues.append(ApiValidationIssue(
                    code="RENDERED_SECTION_NO_IR", section_key=spec.section_key,
                    message="endpoint section has no matching IR endpoint"))
                continue
            if content and (ep.method not in content or ep.path not in content):
                issues.append(ApiValidationIssue(
                    code="RENDERED_FACT_MISMATCH", section_key=spec.section_key,
                    message=f"rendered endpoint section does not mention "
                            f"{ep.method} {ep.path}"))
            allowed_numeric = set()
            for r in ep.responses:
                if r.status_code.isdigit():
                    allowed_numeric.add(r.status_code)
            for e in ep.error_codes:
                if e.http_status.isdigit():
                    allowed_numeric.add(e.http_status)
                if e.code.isdigit():
                    allowed_numeric.add(e.code)
            for token in _NUMERIC_STATUS_RE.findall(content):
                if token not in allowed_numeric:
                    issues.append(ApiValidationIssue(
                        code="RENDERED_FACT_MISMATCH", section_key=spec.section_key,
                        message=f"rendered content references status {token} "
                                f"not present in IR endpoint"))
        # Evidence ID 不得出现在渲染文本（内部标识只在结构化 DTO）。
        if available and content:
            leaked = sorted(eid for eid in available if eid in content)
            if leaked:
                issues.append(ApiValidationIssue(
                    code="RENDERED_EVIDENCE_LEAK", section_key=spec.section_key,
                    message=f"rendered content contains evidence ids {leaked}"))

    issues.sort(key=lambda i: (i.code, i.section_key, i.field_path,
                               i.message))
    errors = [i for i in issues if i.severity == "error"]
    return ApiValidationReport(
        status="fail" if errors else "pass", issues=tuple(issues))
