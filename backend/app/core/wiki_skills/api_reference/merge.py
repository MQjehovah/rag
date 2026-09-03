"""Phase 7A：多 ApiDocumentIR 合并（纯函数，Evidence 保持）。

规则：
- Endpoint 按 endpoint_id 合并；同 method/path 但 v1/v2 不合并（endpoint_id
  含 version_scope，天然隔离）；
- 互补字段合并，并合并对应 Evidence binding（同一事实多来源证据取并集）；
- 完全相同事实去重；冲突事实不得静默覆盖：保留 usage_type=conflict binding
  （引用全部相关 evidence_id）+ 生成 conflicting_facts knowledge gap；
  不自行判断哪个来源正确——展示值只取「规范序首个来源」，且该事实带冲突标记；
- authentication/common_headers/data_models/common_errors 按稳定 key 合并，
  不会复制出无来源事实；
- 输入顺序变化不得改变最终序列化结果（按文档规范序列化排序再合并）；
- 不修改输入 IR（全部构造新对象，输入只读）。

删除/缺失 Evidence 的增量重算留到 Phase 7C，本模块只保证纯函数合并。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.core.wiki_skills.api_reference.evidence import merge_bindings
from app.core.wiki_skills.api_reference.schemas import (
    ApiAuthentication,
    ApiDataModel,
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiExample,
    ApiFieldBinding,
    ApiHeader,
    ApiKnowledgeGap,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
)


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _doc_sort_key(doc: ApiDocumentIR) -> str:
    return _canon(doc.to_dict())


def _binding_ids(owner, field_path: str) -> tuple[str, ...]:
    """owner.evidence_bindings 中匹配 field_path 且 usage=support 的 ids。"""
    for binding in getattr(owner, "evidence_bindings", ()):
        if binding.field_path == field_path and binding.usage_type == "support":
            return binding.evidence_ids
    return ()


def _conflict_gap(eid: str, field_path: str, candidates: Sequence[str]
                  ) -> ApiKnowledgeGap:
    distinct = []
    for v in candidates:
        if v not in distinct:
            distinct.append(v)
    return ApiKnowledgeGap(
        gap_type="conflicting_facts",
        description=f"conflicting facts for {eid or '(document)'} field "
                    f"{field_path!r}: candidates {distinct} "
                    f"(no source judged correct)",
        endpoint_id=eid,
    )


# ---------------------------------------------------------------------------
# 通用 keyed 合并（参数/响应/错误码/示例及 doc 级 section 共用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    item: Any
    evidence_ids: tuple[str, ...] = ()
    conflict: bool = False
    field_path: str = ""


def _merge_occurrences(
    occurrences: Sequence[tuple[int, Any]],
    key_of,
    field_path_of,
    owner_getter,
    scope_id: str,
    gaps: list[ApiKnowledgeGap],
) -> list[_Outcome]:
    """把 (doc_index, item) 贡献合并为 outcome 列表。

    - 同 key 内容相同 → 单条 support 记录，证据取各来源并集；
    - 同 key 内容不同 → 冲突：保留 conflict binding（证据并集）+ knowledge gap，
      展示值取规范序最小 doc_index 的来源；不判断谁正确。
    """
    by_key: dict[str, list[tuple[int, Any]]] = {}
    for idx, item in occurrences:
        by_key.setdefault(key_of(item), []).append((idx, item))

    outcomes: list[_Outcome] = []
    for key in sorted(by_key):
        group = sorted(by_key[key], key=lambda pair: pair[0])
        first_item = group[0][1]
        first_canon = _canon(first_item.to_dict())
        same = all(_canon(item.to_dict()) == first_canon for _, item in group)
        field_path = field_path_of(first_item, key)
        ids = tuple(sorted({
            eid
            for idx, _item in group
            for eid in _binding_ids(owner_getter(idx), field_path)
        }))
        if same:
            outcomes.append(_Outcome(
                item=first_item, evidence_ids=ids, conflict=False,
                field_path=field_path))
        else:
            outcomes.append(_Outcome(
                item=first_item, evidence_ids=ids, conflict=True,
                field_path=field_path))
            gaps.append(_conflict_gap(
                scope_id, field_path,
                [_canon(item.to_dict()) for _, item in group]))
    return outcomes


def _item_bindings(
    outcomes: Iterable[_Outcome], conflict_map: dict[str, ApiFieldBinding],
) -> tuple[ApiFieldBinding, ...]:
    """由 outcome 还原绑定：冲突字段用 conflict usage，其余用 support。"""
    result = []
    for out in outcomes:
        if out.field_path in conflict_map:
            continue
        if out.evidence_ids:
            result.append(ApiFieldBinding(
                field_path=out.field_path, evidence_ids=out.evidence_ids,
                usage_type="support"))
    for binding in conflict_map.values():
        result.append(binding)
    return tuple(sorted(result, key=lambda b: (b.field_path, b.usage_type,
                                               tuple(b.evidence_ids))))


def _make_conflict_bindings(outcomes: Iterable[_Outcome]
                            ) -> dict[str, ApiFieldBinding]:
    return {
        out.field_path: ApiFieldBinding(
            field_path=out.field_path, evidence_ids=out.evidence_ids,
            usage_type="conflict")
        for out in outcomes if out.conflict and out.evidence_ids
    }


# ---------------------------------------------------------------------------
# Endpoint 合并
# ---------------------------------------------------------------------------


def _param_key(p: ApiParameter) -> str:
    # 参数身份 = (location, name)：query 与 header 同名不算同参数。
    return f"{p.location}|{p.name}"


def _param_path(p: ApiParameter, key: str) -> str:
    group = {
        "path": "path_parameters", "query": "query_parameters",
        "header": "headers",
    }.get(p.location, "query_parameters")
    return f"{group}.{p.name}"


def _merge_endpoint_group(endpoints: Sequence[ApiEndpoint], gaps: list[ApiKnowledgeGap],
                          ) -> ApiEndpoint:
    """合并同一 endpoint_id 的一组 source endpoint（互补 + 冲突显式化）。"""
    ordered = sorted(enumerate(endpoints), key=lambda pair: _canon(pair[1].to_dict()))
    sources = [ep for _, ep in ordered]
    eid = sources[0].endpoint_id
    method = sources[0].method
    path = sources[0].path
    version_scope = sources[0].version_scope

    def _owner(idx: int) -> Any:
        return sources[idx]

    # 自由标量事实：summary / description / request_body。
    scalar_outcomes: dict[str, _Outcome] = {}

    def _merge_scalar(field_path: str, values: list[tuple[int, str]]) -> None:
        present = [(idx, v) for idx, v in values if v]
        if not present:
            return
        present_sorted = sorted(present, key=lambda pair: pair[0])
        canonical = present_sorted[0][1]
        same = all(v == canonical for _, v in present)
        ids = tuple(sorted({
            eid_
            for idx, _v in present
            for eid_ in _binding_ids(_owner(idx), field_path)
        }))
        if same:
            scalar_outcomes[field_path] = _Outcome(
                item=canonical, evidence_ids=ids, conflict=False,
                field_path=field_path)
        else:
            scalar_outcomes[field_path] = _Outcome(
                item=canonical, evidence_ids=ids, conflict=True,
                field_path=field_path)
            gaps.append(_conflict_gap(
                eid, field_path, [v for _, v in present]))

    _merge_scalar("summary", [(i, ep.summary) for i, ep in enumerate(sources)])
    _merge_scalar("description", [(i, ep.description) for i, ep in enumerate(sources)])

    # request_body：None 表示该来源无请求体；有内容才贡献。
    rb_occurrences = [(i, ep.request_body) for i, ep in enumerate(sources)
                      if ep.request_body is not None]
    rb_by_content: dict[str, list[tuple[int, ApiRequestBody]]] = {}
    for idx, rb in rb_occurrences:
        rb_by_content.setdefault(_canon(rb.to_dict()), []).append((idx, rb))
    rb_outcomes: list[_Outcome] = []
    for canonical in sorted(rb_by_content):
        group = sorted(rb_by_content[canonical], key=lambda pair: pair[0])
        first_idx, first_rb = group[0]
        field_path = "request_body"
        ids = tuple(sorted({
            eid_ for idx, _rb in group
            for eid_ in _binding_ids(_owner(idx), field_path)
        }))
        conflict = len(rb_by_content) > 1
        rb_outcomes.append(_Outcome(
            item=first_rb, evidence_ids=ids, conflict=conflict,
            field_path=field_path))
    if len(rb_by_content) > 1:
        gaps.append(_conflict_gap(
            eid, "request_body",
            [c for c in sorted(rb_by_content)]))

    # 分组参数 / 响应 / 错误码 / 示例。
    category_occurrences: dict[str, list[tuple[int, Any]]] = {}

    def _add(key: str, idx: int, item: Any) -> None:
        category_occurrences.setdefault(key, []).append((idx, item))

    for idx, ep in enumerate(sources):
        for p in ep.path_parameters:
            _add("params", idx, p)
        for p in ep.query_parameters:
            _add("params", idx, p)
        for p in ep.headers:
            _add("params", idx, p)
        for r in ep.responses:
            _add("responses", idx, r)
        for e in ep.error_codes:
            _add("error_codes", idx, e)
        for ex in ep.examples:
            _add("examples", idx, ex)

    outcomes: list[_Outcome] = []
    outcomes += list(scalar_outcomes.values())
    outcomes += rb_outcomes
    for kind in ("params", "responses", "error_codes", "examples"):
        occ = category_occurrences.get(kind, [])
        if not occ:
            continue
        if kind == "params":
            outcomes += _merge_occurrences(
                occ, _param_key, _param_path, _owner, eid, gaps)
        elif kind == "responses":
            outcomes += _merge_occurrences(
                occ, lambda r: r.status_code,
                lambda r, k: f"responses.{k}", _owner, eid, gaps)
        elif kind == "error_codes":
            outcomes += _merge_occurrences(
                occ, lambda e: e.code,
                lambda e, k: f"error_codes.{k}", _owner, eid, gaps)
        else:
            outcomes += _merge_occurrences(
                occ, lambda x: x.title,
                lambda x, k: f"examples.{k}", _owner, eid, gaps)

    conflict_map = _make_conflict_bindings(outcomes)
    bindings = _item_bindings(outcomes, conflict_map)

    # 身份 Binding 字段级精度：method/path/version_scope 各自合并本字段来源
    # Binding 的 Evidence 并集；绝不把三类 Evidence 合并后复制给三个字段。
    # 某字段无 Evidence 时保持缺失（不加 binding），让 coverage gate 失败，
    # 不得借用其它字段的 Evidence 静默补齐。
    identity_bindings = []
    for fp in ("method", "path", "version_scope"):
        fp_ids = tuple(sorted({
            eid_
            for idx, _ep in enumerate(sources)
            for eid_ in _binding_ids(_owner(idx), fp)
        }))
        if fp_ids:
            identity_bindings.append(
                ApiFieldBinding(field_path=fp, evidence_ids=fp_ids))
    if identity_bindings:
        bindings = tuple(sorted(
            tuple(bindings) + tuple(identity_bindings),
            key=lambda b: (b.field_path, b.usage_type, tuple(b.evidence_ids))))

    # 组装最终 endpoint（参数按 location 分组回去）。
    final_params = [o.item for o in outcomes if isinstance(o.item, ApiParameter)]
    path_params = tuple(sorted((p for p in final_params if p.location == "path"),
                               key=lambda p: p.name))
    query_params = tuple(sorted((p for p in final_params if p.location == "query"),
                                key=lambda p: p.name))
    header_params = tuple(sorted((p for p in final_params if p.location == "header"),
                                 key=lambda p: p.name))
    responses = tuple(sorted((o.item for o in outcomes
                              if isinstance(o.item, ApiResponse)),
                             key=lambda r: r.status_code))
    error_codes = tuple(sorted((o.item for o in outcomes
                                if isinstance(o.item, ApiErrorCode)),
                               key=lambda e: (e.http_status, e.code)))
    examples = tuple(sorted((o.item for o in outcomes
                             if isinstance(o.item, ApiExample)),
                            key=lambda x: x.title))
    request_body = next((o.item for o in outcomes
                         if isinstance(o.item, ApiRequestBody)), None)

    return ApiEndpoint(
        method=method, path=path, version_scope=version_scope,
        summary=next((o.item for o in outcomes if o.field_path == "summary"), ""),
        description=next((o.item for o in outcomes
                          if o.field_path == "description"), ""),
        path_parameters=path_params,
        query_parameters=query_params,
        headers=header_params,
        request_body=request_body,
        responses=responses,
        error_codes=error_codes,
        examples=examples,
        evidence_bindings=bindings,
    )


# ---------------------------------------------------------------------------
# Document 级合并
# ---------------------------------------------------------------------------


def _merge_doc_section(docs: Sequence[ApiDocumentIR], section: str,
                       gaps: list[ApiKnowledgeGap]) -> tuple[list[Any],
                                                             list[ApiFieldBinding]]:
    """合并 doc 级 keyed section（authentication/common_headers/data_models/
    common_errors），key 来自对象的稳定 name/code。

    只返回「有来源」的项；同名但内容不同的项保留冲突标记，不覆盖。
    """
    def key_of(item: Any) -> str:
        if isinstance(item, (ApiAuthentication, ApiHeader, ApiDataModel)):
            return item.name
        if isinstance(item, ApiErrorCode):
            return f"{item.http_status}|{item.code}" if item.http_status else item.code
        return _canon(item.to_dict())

    field_base = section
    occurrences: list[tuple[int, Any]] = []
    for doc_idx, doc in enumerate(docs):
        items = getattr(doc, section, ())
        for item in items:
            occurrences.append((doc_idx, item))

    if not occurrences:
        return [], []

    by_key: dict[str, list[tuple[int, Any]]] = {}
    for idx, item in occurrences:
        by_key.setdefault(key_of(item), []).append((idx, item))

    merged_items: list[Any] = []
    bindings: list[ApiFieldBinding] = []
    for key in sorted(by_key):
        group = sorted(by_key[key], key=lambda pair: pair[0])
        first_item = group[0][1]
        first_canon = _canon(first_item.to_dict())
        same = all(_canon(item.to_dict()) == first_canon for _, item in group)
        field_path = f"{field_base}.{key}"
        ids = tuple(sorted({
            eid
            for idx, _item in group
            for eid in _binding_ids(docs[idx], field_path)
        }))
        if not same:
            if ids:
                bindings.append(ApiFieldBinding(
                    field_path=field_path, evidence_ids=ids, usage_type="conflict"))
            gaps.append(_conflict_gap(
                "", field_path, [_canon(item.to_dict()) for _, item in group]))
        elif ids:
            bindings.append(ApiFieldBinding(
                field_path=field_path, evidence_ids=ids, usage_type="support"))
        merged_items.append(first_item)
    return merged_items, bindings


def _merge_scalar_overview(docs: Sequence[ApiDocumentIR],
                           gaps: list[ApiKnowledgeGap]
                           ) -> tuple[str, ApiFieldBinding | None]:
    """overview 自由文本：内容相同合并证据；不同则 conflict + gap。"""
    present = [(i, doc.overview) for i, doc in enumerate(docs) if doc.overview]
    if not present:
        return "", None
    present_sorted = sorted(present, key=lambda pair: pair[0])
    canonical = present_sorted[0][1]
    ids = tuple(sorted({
        eid for idx, _v in present
        for eid in _binding_ids(docs[idx], "overview")
    }))
    same = all(v == canonical for _, v in present)
    if not same:
        gaps.append(_conflict_gap(
            "", "overview", [v for _, v in present]))
        return canonical, ApiFieldBinding(
            field_path="overview", evidence_ids=ids, usage_type="conflict")
    if ids:
        return canonical, ApiFieldBinding(
            field_path="overview", evidence_ids=ids, usage_type="support")
    return canonical, None


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def merge_documents(docs: Sequence[ApiDocumentIR]) -> ApiDocumentIR:
    """把多个 ApiDocumentIR 确定性合并为一个（纯函数；不修改输入）。"""
    if not docs:
        return ApiDocumentIR()
    ordered = sorted(docs, key=_doc_sort_key)
    gaps: list[ApiKnowledgeGap] = []

    # Endpoint 按 id 分组合并。
    grouped: dict[str, list[ApiEndpoint]] = {}
    for doc in ordered:
        for endpoint in doc.endpoints:
            grouped.setdefault(endpoint.endpoint_id, []).append(endpoint)
    endpoints = tuple(
        sorted(
            (_merge_endpoint_group(group, gaps) for group in grouped.values()),
            key=lambda e: e.endpoint_id,
        ))

    # Document 级 section。
    auth_items = common_headers = data_models = common_errors = ()
    auth_bindings = header_bindings = model_bindings = error_bindings = ()
    auth_items, auth_bindings = _merge_doc_section(ordered, "authentication", gaps)
    common_headers, header_bindings = _merge_doc_section(
        ordered, "common_headers", gaps)
    data_models, model_bindings = _merge_doc_section(ordered, "data_models", gaps)
    common_errors, error_bindings = _merge_doc_section(ordered, "common_errors", gaps)

    overview, overview_binding = _merge_scalar_overview(ordered, gaps)

    # version_notes：等值去重 union（不凭空生成）。
    version_notes = []
    for doc in ordered:
        for note in doc.version_notes:
            canon = _canon(note.to_dict())
            if not any(_canon(n.to_dict()) == canon for n in version_notes):
                version_notes.append(note)

    # knowledge_gaps：全部来源（含合并期生成的 conflict 诊断）去重合并。
    knowledge_gaps = []
    for doc in ordered:
        for gap in doc.knowledge_gaps:
            canon = _canon(gap.to_dict())
            if not any(_canon(g.to_dict()) == canon for g in knowledge_gaps):
                knowledge_gaps.append(gap)
    for gap in gaps:
        canon = _canon(gap.to_dict())
        if not any(_canon(g.to_dict()) == canon for g in knowledge_gaps):
            knowledge_gaps.append(gap)

    doc_bindings = list(auth_bindings) + list(header_bindings) \
        + list(model_bindings) + list(error_bindings)
    if overview_binding is not None:
        doc_bindings.append(overview_binding)

    # 额外的 doc 级 support binding（同一路径无冲突时合并 union）。
    extra = {}
    for doc in ordered:
        for binding in doc.evidence_bindings:
            if binding.field_path.startswith(("authentication.", "common_headers.",
                                              "data_models.", "common_errors.",
                                              "overview")):
                continue
            extra.setdefault(binding.field_path, set()).update(binding.evidence_ids)
    for field_path in sorted(extra):
        doc_bindings.append(ApiFieldBinding(
            field_path=field_path,
            evidence_ids=tuple(sorted(extra[field_path])),
            usage_type="support"))

    doc_bindings = merge_bindings(tuple(doc_bindings))
    return ApiDocumentIR(
        overview=overview,
        authentication=tuple(auth_items),
        common_headers=tuple(common_headers),
        endpoints=endpoints,
        data_models=tuple(data_models),
        common_errors=tuple(common_errors),
        version_notes=tuple(version_notes),
        knowledge_gaps=tuple(knowledge_gaps),
        evidence_bindings=tuple(doc_bindings),
    )
