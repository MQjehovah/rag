"""Phase 7A：Evidence 约束与事实绑定（不接数据库，纯内存 / 纯函数）。

不变量：
- Evidence 生命周期 active/stale/rejected；只有 active Evidence 可进入提取输入，
  stale/rejected 不得支撑任何事实字段；
- 事实字段必须经 ApiFieldBinding 绑定 active Evidence；空证据 / 引用到非 active
  或未知 evidence_id 的绑定无法通过契约（构造即失败）；
- content_hash 为纯确定性 sha256（同输入同输出）；
- 不保存 ORM / Session / callable / Secret；输入为 JSON-safe 记录映射。

增量重算（删除/缺失 Evidence）留到 Phase 7C，本模块不处理持久化。
"""
from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from app.core.wiki_skills.api_reference.schemas import (
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiEvidenceCoverageError,
    ApiEvidenceRef,
    ApiFieldBinding,
    EVIDENCE_STATUS_VALUES,
    USAGE_TYPE_VALUES,
    assert_json_safe,
    _thaw,
)


def content_hash(source_page_id: str, source_chunk_id: str | None, locator: Any) -> str:
    """稳定内容哈希：同一 (page, chunk, locator) 恒得同一 hash。

    locator 需 JSON-safe Mapping；内部用 sort_keys 保证与键插入顺序无关。
    """
    assert_json_safe(locator, "locator")
    payload = {
        "source_page_id": source_page_id,
        "source_chunk_id": source_chunk_id,
        "locator": _thaw(locator),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_evidence_ref(record: Mapping[str, Any]) -> ApiEvidenceRef:
    """单条 Evidence 记录（JSON-safe Mapping）→ ApiEvidenceRef。

    记录可省略 content_hash（此处自动计算），但 status 必须受控。
    """
    if not isinstance(record, Mapping):
        raise ValueError("evidence record must be a mapping")
    assert_json_safe(dict(record), "evidence record")
    record = dict(record)
    status = str(record.get("status", "active")).strip().lower()
    if status not in EVIDENCE_STATUS_VALUES:
        raise ValueError(f"evidence status must be one of {EVIDENCE_STATUS_VALUES}")
    ref = ApiEvidenceRef(
        evidence_id=str(record.get("evidence_id", "")),
        source_page_id=str(record.get("source_page_id", "")),
        source_chunk_id=record.get("source_chunk_id"),
        evidence_type=str(record.get("evidence_type", "openapi")),
        locator=record.get("locator") or {},
        content_hash=str(record.get("content_hash", "")),
        status=status,
    )
    if not ref.content_hash:
        object.__setattr__(
            ref, "content_hash",
            content_hash(ref.source_page_id, ref.source_chunk_id, ref.locator))
    return ref


def select_active(records: Iterable[Mapping[str, Any]]) -> tuple[ApiEvidenceRef, ...]:
    """只允许 active Evidence 进入提取输入；stale/rejected 被剔除。"""
    active = []
    for record in records:
        ref = build_evidence_ref(record)
        if ref.status == "active":
            active.append(ref)
    return tuple(active)


def build_registry(refs: Iterable[ApiEvidenceRef]) -> Mapping[str, ApiEvidenceRef]:
    """evidence_id → ApiEvidenceRef 冻结索引（id 唯一，非 active 不允许进入）。"""
    index: dict[str, ApiEvidenceRef] = {}
    for ref in refs:
        if ref.status != "active":
            raise ValueError(
                f"non-active evidence cannot enter registry: {ref.evidence_id}")
        if ref.evidence_id in index:
            raise ValueError(f"duplicate evidence_id: {ref.evidence_id}")
        index[ref.evidence_id] = ref
    return MappingProxyType(index)


def usable_evidence_ids(refs: Iterable[ApiEvidenceRef]) -> tuple[str, ...]:
    """active Evidence id 的有序去重集合。"""
    ids = sorted({r.evidence_id for r in refs if r.status == "active"})
    return tuple(ids)


def make_binding(
    field_path: str,
    evidence_ids: Iterable[str] | str | None,
    usage_type: str = "support",
    *,
    available: Iterable[str] | None = None,
) -> ApiFieldBinding:
    """构造事实字段绑定（fail closed）。

    规则：
    - usage_type 只允许 support/example/conflict；
    - evidence_ids 不能为空（无 Evidence 的内容不得成为事实字段）；
    - 提供 available（active evidence id 集合）时，任何引用非 active / 未知
      evidence_id 的绑定一律拒绝 —— stale/rejected 不得支撑事实。
    """
    if usage_type not in USAGE_TYPE_VALUES:
        raise ValueError(f"usage_type must be one of {USAGE_TYPE_VALUES}")
    if evidence_ids is None:
        raise ValueError("evidence_ids must not be empty: factual field requires evidence")
    if isinstance(evidence_ids, str):
        evidence_ids = (evidence_ids,)
    ids = tuple(evidence_ids)
    binding = ApiFieldBinding(field_path=field_path, evidence_ids=ids, usage_type=usage_type)
    if available is not None:
        allowed = set(available)
        unknown = sorted(set(binding.evidence_ids) - allowed)
        if unknown:
            raise ValueError(
                f"binding references non-active/unknown evidence {unknown} "
                f"for {field_path!r}: stale/rejected evidence cannot support facts")
    return binding


def merge_bindings(
    bindings: Iterable[ApiFieldBinding],
) -> tuple[ApiFieldBinding, ...]:
    """按 (field_path, usage_type) 合并同一事实的多来源证据（证据集合并去重）。"""
    merged: dict[tuple[str, str], set[str]] = {}
    for binding in bindings:
        key = (binding.field_path, binding.usage_type)
        merged.setdefault(key, set()).update(binding.evidence_ids)
    result = []
    for (field_path, usage_type), ids in sorted(merged.items()):
        result.append(ApiFieldBinding(
            field_path=field_path,
            evidence_ids=tuple(sorted(ids)),
            usage_type=usage_type,
        ))
    return tuple(result)


# ---------------------------------------------------------------------------
# Evidence 覆盖验证（纯函数，供 7B Validator 调用；不查询全局 Evidence）
# ---------------------------------------------------------------------------


def _document_section_path(section: str, item) -> str:
    """document 级 section 事实的稳定 field_path（与 merge 的 key 规约一致）。"""
    if isinstance(item, ApiErrorCode):
        key = f"{item.http_status}|{item.code}" if item.http_status else item.code
    else:
        key = item.name  # ApiAuthentication / ApiHeader / ApiDataModel
    return f"{section}.{key}"


def _endpoint_fact_paths(endpoint: ApiEndpoint) -> tuple[str, ...]:
    """Endpoint 内每个实际存在的事实字段路径（不要求空字段绑定）。"""
    paths = ["method", "path", "version_scope"]
    if endpoint.summary:
        paths.append("summary")
    if endpoint.description:
        paths.append("description")
    for param in endpoint.path_parameters:
        paths.append(f"path_parameters.{param.name}")
    for param in endpoint.query_parameters:
        paths.append(f"query_parameters.{param.name}")
    for param in endpoint.headers:
        paths.append(f"headers.{param.name}")
    if endpoint.request_body is not None:
        paths.append("request_body")
    for response in endpoint.responses:
        paths.append(f"responses.{response.status_code}")
    for error in endpoint.error_codes:
        paths.append(f"error_codes.{error.code}")
    for example in endpoint.examples:
        paths.append(f"examples.{example.title}")
    return tuple(paths)


def _binding_index(bindings) -> dict[str, ApiFieldBinding]:
    index = {}
    for binding in bindings:
        index.setdefault(binding.field_path, binding)
    return index


def evidence_coverage_issues(
    ir: ApiDocumentIR,
    available_evidence_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """结构化检查 ApiDocumentIR 的 Evidence 覆盖，返回确定性诊断列表（不抛错）。

    规则：
    - 所有实际存在的事实字段必须有对应 Binding（identity 也视为事实）；
    - Binding 引用的 evidence_id 必须位于 available active Evidence 集合
      （stale/unknown 引用 → 失败）；
    - conflict Binding 仍算有效覆盖，但必须引用 Evidence；
    - knowledge_gaps 不要求 Evidence；
    - 纯函数：不查询全局 Evidence / DB，不修改输入。
    """
    if not isinstance(ir, ApiDocumentIR):
        raise TypeError("ir must be ApiDocumentIR")
    available = set(available_evidence_ids)
    issues: list[str] = []

    def check_binding(binding: ApiFieldBinding, where: str) -> None:
        unknown = sorted(set(binding.evidence_ids) - available)
        if unknown:
            issues.append(
                f"{where}: binding {binding.field_path!r} references evidence "
                f"{unknown} not in available active set")

    for binding in ir.evidence_bindings:
        check_binding(binding, "document")

    # document 级事实字段（存在才要求绑定；无事实不要求）。
    if ir.overview and "overview" not in _binding_index(ir.evidence_bindings):
        issues.append("document: overview fact without binding")
    doc_map = _binding_index(ir.evidence_bindings)
    for auth in ir.authentication:
        path = _document_section_path("authentication", auth)
        if path not in doc_map:
            issues.append(f"document: authentication fact {path!r} without binding")
    for header in ir.common_headers:
        path = _document_section_path("common_headers", header)
        if path not in doc_map:
            issues.append(f"document: common_header fact {path!r} without binding")
    for model in ir.data_models:
        path = _document_section_path("data_models", model)
        if path not in doc_map:
            issues.append(f"document: data_model fact {path!r} without binding")
    for error in ir.common_errors:
        path = _document_section_path("common_errors", error)
        if path not in doc_map:
            issues.append(f"document: common_error fact {path!r} without binding")
    # version_notes：按 (endpoint_id, version_scope) 群要求至少一条绑定。
    note_identities = {(n.endpoint_id, n.version_scope) for n in ir.version_notes}
    for endpoint_id, scope in sorted(note_identities):
        path = (f"version_notes.{endpoint_id}.{scope}" if endpoint_id
                else f"version_notes.{scope}")
        if path not in doc_map:
            issues.append(f"document: version_note fact {path!r} without binding")

    # endpoint 级事实字段。
    for endpoint in ir.endpoints:
        where = f"endpoint {endpoint.endpoint_id}"
        ep_map = _binding_index(endpoint.evidence_bindings)
        for path in _endpoint_fact_paths(endpoint):
            if path not in ep_map:
                issues.append(f"{where}: fact {path!r} without binding")
        for binding in endpoint.evidence_bindings:
            check_binding(binding, where)

    return tuple(sorted(set(issues)))


def validate_evidence_coverage(
    ir: ApiDocumentIR,
    available_evidence_ids: Iterable[str] = (),
) -> None:
    """Evidence 覆盖验证门禁：存在任何缺失/非法引用即抛受控错误。

    通过（无诊断）时静默返回；失败不静默：抛 ApiEvidenceCoverageError，
    附带结构化 diagnostics，供 7B Validator 记录/阻断。
    """
    issues = evidence_coverage_issues(ir, available_evidence_ids)
    if issues:
        raise ApiEvidenceCoverageError(diagnostics=issues)
