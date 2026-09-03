"""Phase 7A：API Reference 知识 IR 与确定性解析基础包。

独立基础包，位于 wiki_skills 之下但**不是 builtin Skill**：Phase 6 Loader
只扫描 `wiki_skills/builtin/` 下带 skill.yaml 的直接子目录，本目录不会被
自动加载，也未进入生产 Registry。本阶段不接入生产 Pipeline。

模块职责：
- schemas：不可变数据契约（frozen + 深层不可变 + JSON-safe，to_dict/from_dict）；
- identity：Endpoint 确定性身份（method/path/version_scope → endpoint_id/key）；
- evidence：Evidence active/stale/rejected 约束与事实绑定（fail closed）；
- parser：OpenAPI JSON/YAML 与 Markdown 的确定性解析（不调 LLM）；
- merge：多 ApiDocumentIR 按 Endpoint identity 确定性合并（纯函数）。
"""
from app.core.wiki_skills.api_reference.schemas import (
    API_DOCUMENT_SCHEMA,
    EVIDENCE_STATUS_VALUES,
    GAP_TYPE_VALUES,
    PARAM_LOCATION_VALUES,
    USAGE_TYPE_VALUES,
    ApiAuthentication,
    ApiDataModel,
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiEvidenceRef,
    ApiExample,
    ApiFieldBinding,
    ApiHeader,
    ApiKnowledgeGap,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
    ApiVersionNote,
    EvidenceStatus,
    ParamLocation,
    UsageType,
)
from app.core.wiki_skills.api_reference import evidence as evidence_mod
from app.core.wiki_skills.api_reference import identity as identity_mod

__all__ = [
    "API_DOCUMENT_SCHEMA",
    "EVIDENCE_STATUS_VALUES",
    "GAP_TYPE_VALUES",
    "PARAM_LOCATION_VALUES",
    "USAGE_TYPE_VALUES",
    "ApiAuthentication",
    "ApiDataModel",
    "ApiDocumentIR",
    "ApiEndpoint",
    "ApiErrorCode",
    "ApiEvidenceRef",
    "ApiExample",
    "ApiFieldBinding",
    "ApiHeader",
    "ApiKnowledgeGap",
    "ApiParameter",
    "ApiRequestBody",
    "ApiResponse",
    "ApiVersionNote",
    "EvidenceStatus",
    "ParamLocation",
    "UsageType",
]
