"""Phase 7A：API Reference IR 数据契约测试（不可变 / JSON 往返 / 拒绝非安全值）。

覆盖：DTO JSON 往返对称；深层不可变；ORM/callable/bytes/NaN 等非 JSON-safe 拒绝；
active/stale/rejected 状态约束；factual binding 无 Evidence 无法通过契约。
"""
from __future__ import annotations

import json
import math
from types import MappingProxyType

import pytest

from app.core.wiki_skills.api_reference.schemas import (
    API_DOCUMENT_SCHEMA,
    ApiAuthentication,
    ApiDataModel,
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiEvidenceCoverageError,
    ApiEvidenceRef,
    ApiExample,
    ApiFieldBinding,
    ApiHeader,
    ApiKnowledgeGap,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
    ApiVersionNote,
)
from app.core.wiki_skills.api_reference import evidence as evidence_mod


def _binding(path="responses.200", ids=("ev-1", "ev-0"), usage="support"):
    return ApiFieldBinding(field_path=path, evidence_ids=ids, usage_type=usage)


def _user_param():
    return ApiParameter(name="userId", location="path", required=True,
                        description="用户 ID", schema={"type": "string"})


def _query_param():
    return ApiParameter(name="page", location="query", required=False,
                        schema={"type": "integer"})


def _response_ok():
    return ApiResponse(status_code="200", description="成功",
                       content={"application/json": {"type": "object"}})


def _response_404():
    return ApiResponse(status_code="404", description="不存在")


def _body():
    return ApiRequestBody(required=True,
                          content={"application/json": {"type": "object"}})


def _endpoint():
    return ApiEndpoint(
        method="get",
        path="users/{userId}//",
        version_scope="V1",
        summary="获取用户",
        path_parameters=(_user_param(),),
        query_parameters=(_query_param(),),
        request_body=None,
        responses=(_response_ok(), _response_404()),
        error_codes=(ApiErrorCode(code="404", description="不存在", http_status="404"),),
        examples=(ApiExample(title="example-200-json", content={"id": 1}),),
        evidence_bindings=(
            _binding("responses.200"), _binding("responses.404"),
            _binding("path_parameters.userId"), _binding("query_parameters.page"),
            _binding("error_codes.404"),
        ),
    )


def _document():
    return ApiDocumentIR(
        overview="用户管理 API",
        authentication=(ApiAuthentication(name="bearerAuth", kind="http", scheme="bearer"),),
        common_headers=(ApiHeader(name="X-Trace", required=False),),
        endpoints=(_endpoint(),),
        data_models=(ApiDataModel(name="User", schema={"type": "object"}),),
        common_errors=(ApiErrorCode(code="404", http_status="404", description="不存在"),),
        version_notes=(ApiVersionNote(version_scope="v1", note="首版"),),
        knowledge_gaps=(ApiKnowledgeGap(gap_type="no_evidence", description="缺证据"),),
        evidence_bindings=(
            ApiFieldBinding(field_path="overview", evidence_ids=("ev-doc",)),
            ApiFieldBinding(field_path="authentication.bearerAuth",
                            evidence_ids=("ev-doc",)),
        ),
    )


# ---------------------------------------------------------------------------
# JSON 往返 / 契约版本
# ---------------------------------------------------------------------------


def test_evidence_ref_json_roundtrip():
    ref = ApiEvidenceRef(evidence_id="e1", source_page_id="p1",
                         source_chunk_id="c1", locator={"section": "endpoints"})
    restored = ApiEvidenceRef.from_dict(json.loads(json.dumps(ref.to_dict())))
    assert restored.to_dict() == ref.to_dict()


def test_field_binding_json_roundtrip():
    b = ApiFieldBinding(field_path="responses.200",
                        evidence_ids=("ev-b", "ev-a"), usage_type="support")
    restored = ApiFieldBinding.from_dict(json.loads(json.dumps(b.to_dict())))
    assert restored.to_dict() == b.to_dict()
    assert restored.evidence_ids == ("ev-a", "ev-b")


def test_endpoint_json_roundtrip():
    ep = _endpoint()
    restored = ApiEndpoint.from_dict(json.loads(json.dumps(ep.to_dict())))
    assert restored.to_dict() == ep.to_dict()
    # 往返后 endpoint_id 仍由 method+path+scope 推导，不被任意字符串改写。
    assert restored.endpoint_id == "GET /users/{userId} [v1]"


def test_document_json_roundtrip():
    doc = _document()
    restored = ApiDocumentIR.from_dict(json.loads(json.dumps(doc.to_dict())))
    assert restored.to_dict() == doc.to_dict()
    assert restored.schema_version == API_DOCUMENT_SCHEMA


def test_document_roundtrip_keeps_all_sections():
    doc = _document()
    raw = json.loads(json.dumps(doc.to_dict()))
    assert set(raw) == {"schema_version", "overview", "authentication",
                        "common_headers", "endpoints", "data_models",
                        "common_errors", "version_notes", "knowledge_gaps",
                        "evidence_bindings"}
    assert len(raw["endpoints"]) == 1


# ---------------------------------------------------------------------------
# 深层不可变
# ---------------------------------------------------------------------------


def test_deep_immutability():
    doc = _document()
    with pytest.raises(AttributeError):
        doc.overview = "x"
    schema = doc.data_models[0].schema
    assert isinstance(schema, MappingProxyType)
    with pytest.raises(TypeError):
        schema["type"] = "string"
    # 嵌套 Mapping/list 都被冻结：无 append / 不可赋值。
    assert not hasattr(doc.authentication, "append")
    body = _body()
    assert isinstance(body.content, MappingProxyType)
    with pytest.raises(TypeError):
        body.content["application/json"] = {}


# ---------------------------------------------------------------------------
# 非 JSON-safe 内容拒绝
# ---------------------------------------------------------------------------


def test_callable_locator_rejected():
    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="p",
                       locator={"fn": lambda x: x})


def test_orm_and_bytes_rejected():
    class FakeORM:
        _sa_instance_state = object()

    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="p",
                       locator={"orm": FakeORM()})
    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="p",
                       locator={"raw": b"bytes"})


def test_nan_rejected_in_mapping_and_example():
    with pytest.raises(ValueError):
        ApiRequestBody(content={"application/json": {"x": float("nan")}})
    with pytest.raises(ValueError):
        ApiExample(title="bad", content=float("inf"))
    with pytest.raises(ValueError):
        ApiDataModel(name="m", schema={"x": float("nan")})


def test_datetime_rejected_in_locator():
    import datetime

    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="p",
                       locator={"when": datetime.datetime.now()})


# ---------------------------------------------------------------------------
# Evidence 契约约束
# ---------------------------------------------------------------------------


def test_evidence_required_ids():
    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="", source_page_id="p")
    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="   ")


def test_evidence_status_restricted():
    ApiEvidenceRef(evidence_id="e", source_page_id="p", status="active")
    ApiEvidenceRef(evidence_id="e", source_page_id="p", status="stale")
    with pytest.raises(ValueError):
        ApiEvidenceRef(evidence_id="e", source_page_id="p", status="unknown")


def test_factual_binding_without_evidence_rejected():
    # 契约：没有 Evidence 的事实字段不能通过；构造即失败。
    with pytest.raises(ValueError):
        ApiFieldBinding(field_path="summary", evidence_ids=())
    with pytest.raises(ValueError):
        ApiFieldBinding(field_path="summary", evidence_ids=[])


def test_binding_evidence_ids_dedup_sorted():
    b = ApiFieldBinding(field_path="summary",
                        evidence_ids=("ev-b", "ev-a", "ev-b"))
    assert b.evidence_ids == ("ev-a", "ev-b")


def test_binding_usage_type_restricted():
    for usage in ("support", "example", "conflict"):
        ApiFieldBinding(field_path="x", evidence_ids=("ev",), usage_type=usage)
    with pytest.raises(ValueError):
        ApiFieldBinding(field_path="x", evidence_ids=("ev",), usage_type="guess")
    with pytest.raises(ValueError):
        ApiFieldBinding(field_path="x", evidence_ids=("ev",), usage_type="")


def test_binding_field_path_required():
    with pytest.raises(ValueError):
        ApiFieldBinding(field_path="", evidence_ids=("ev",))


# ---------------------------------------------------------------------------
# Endpoint / Document 结构约束
# ---------------------------------------------------------------------------


def test_param_location_restricted():
    with pytest.raises(ValueError):
        ApiParameter(name="x", location="body")
    ApiParameter(name="x", location="cookie")


def test_param_group_location_mismatch_rejected():
    # path_parameters 组内参数 location 必须为 path，防止位置被改写。
    with pytest.raises(ValueError):
        ApiEndpoint(method="GET", path="/x",
                    path_parameters=(ApiParameter(name="q", location="query"),))


def test_endpoint_id_cannot_be_forged():
    with pytest.raises(ValueError):
        ApiEndpoint(method="GET", path="/users", version_scope="v1",
                    endpoint_id="GET /hacked [v9]")


def test_endpoint_normalizes_and_computes_id():
    ep = ApiEndpoint(method=" get ", path="users//x/", version_scope="V1")
    assert ep.method == "GET"
    assert ep.path == "/users/x"
    assert ep.version_scope == "v1"
    assert ep.endpoint_id == "GET /users/x [v1]"


def test_endpoint_duplicate_items_rejected():
    with pytest.raises(ValueError):
        ApiEndpoint(method="GET", path="/x",
                    responses=(_response_ok(), _response_ok()))
    with pytest.raises(ValueError):
        ApiEndpoint(method="GET", path="/x",
                    query_parameters=(_query_param(), _query_param()))


def test_document_sorted_endpoints_and_rejects_duplicate_id():
    a = ApiEndpoint(method="GET", path="/b")
    b = ApiEndpoint(method="GET", path="/a")
    doc = ApiDocumentIR(endpoints=(a, b))
    assert [e.path for e in doc.endpoints] == ["/a", "/b"]
    with pytest.raises(ValueError):
        ApiDocumentIR(endpoints=(a, a))


def test_from_dict_rejects_unknown_fields_and_invalid_gap_type():
    with pytest.raises(ValueError):
        ApiDocumentIR.from_dict({"schema_version": API_DOCUMENT_SCHEMA, "junk": 1})
    with pytest.raises(ValueError):
        ApiKnowledgeGap(gap_type="guessed", description="x")
    with pytest.raises(ValueError):
        ApiEndpoint.from_dict({"method": "GET", "path": "/x", "nope": True})


def test_unknown_evidence_status_and_empty_binding_rejected_at_from_dict():
    with pytest.raises(ValueError):
        ApiFieldBinding.from_dict({"field_path": "summary", "evidence_ids": []})


# ---------------------------------------------------------------------------
# evidence 行为约束（active 过滤 / 稳定哈希 / 未知证据绑定拒绝）
# ---------------------------------------------------------------------------


def _rec(eid, status="active", locator=None):
    return {"evidence_id": eid, "source_page_id": "p1",
            "source_chunk_id": "c1", "status": status,
            "locator": locator or {"section": "endpoints",
                                   "method": "GET", "path": "/users"}}


def test_select_active_only_active_enters_extraction_input():
    records = [_rec("ev-active"), _rec("ev-stale", status="stale"),
               _rec("ev-rej", status="rejected")]
    active = evidence_mod.select_active(records)
    assert {r.evidence_id for r in active} == {"ev-active"}
    assert all(r.status == "active" for r in active)
    # 非 active 的 Evidence 无法进入 registry（registry 拒绝非 active ref）。
    stale_ref = evidence_mod.build_evidence_ref(_rec("ev-stale", status="stale"))
    with pytest.raises(ValueError):
        evidence_mod.build_registry([stale_ref])


def test_content_hash_stable_and_input_sensitive():
    loc = {"section": "endpoints", "method": "GET", "path": "/users"}
    h1 = evidence_mod.content_hash("p1", "c1", loc)
    h2 = evidence_mod.content_hash("p1", "c1", dict(loc))
    assert h1 == h2
    assert len(h1) == 64
    assert h1 != evidence_mod.content_hash("p2", "c1", loc)


def test_binding_rejects_unknown_or_non_active_evidence():
    available = ("ev-active",)
    b = evidence_mod.make_binding("summary", "ev-active", available=available)
    assert b.evidence_ids == ("ev-active",)
    with pytest.raises(ValueError):
        evidence_mod.make_binding("summary", "ev-stale", available=available)
    with pytest.raises(ValueError):
        evidence_mod.make_binding("summary", "ev-unknown", available=available)
    with pytest.raises(ValueError):
        evidence_mod.make_binding("summary", [], available=available)
    with pytest.raises(ValueError):
        evidence_mod.make_binding("summary", "ev-active", usage_type="made-up",
                                  available=available)


# ---------------------------------------------------------------------------
# Phase 7A.1：Evidence 覆盖验证（纯函数验证器）
# ---------------------------------------------------------------------------


def _covered_endpoint(*bindings, summary="s", description="d", bind_summary=True,
                      bind_description=True):
    return ApiEndpoint(
        method="GET", path="/x", version_scope="v1", summary=summary,
        description=description, evidence_bindings=tuple(bindings))


def test_coverage_endpoint_without_binding_fails():
    doc = ApiDocumentIR(endpoints=(_covered_endpoint(),))
    issues = evidence_mod.evidence_coverage_issues(doc, {"ev1"})
    assert issues
    assert any("method" in i and "without binding" in i for i in issues)
    assert any("path" in i for i in issues)
    assert any("version_scope" in i for i in issues)
    with pytest.raises(ApiEvidenceCoverageError) as exc:
        evidence_mod.validate_evidence_coverage(doc, {"ev1"})
    assert exc.value.diagnostics


def test_coverage_missing_identity_binding_fails():
    ep = ApiEndpoint(
        method="GET", path="/x", version_scope="v1", summary="s",
        evidence_bindings=(ApiFieldBinding(field_path="summary",
                                           evidence_ids=("ev1",)),))
    issues = evidence_mod.evidence_coverage_issues(ApiDocumentIR(endpoints=(ep,)), {"ev1"})
    assert any("method" in i for i in issues)
    assert any("version_scope" in i for i in issues)


def test_coverage_summary_bound_description_unbound_fails():
    ep = _covered_endpoint(ApiFieldBinding(field_path="method",
                                           evidence_ids=("ev1",)),
                           ApiFieldBinding(field_path="path",
                                           evidence_ids=("ev1",)),
                           ApiFieldBinding(field_path="version_scope",
                                           evidence_ids=("ev1",)),
                           ApiFieldBinding(field_path="summary",
                                           evidence_ids=("ev1",)))
    issues = evidence_mod.evidence_coverage_issues(
        ApiDocumentIR(endpoints=(ep,)), {"ev1"})
    assert any("description" in i for i in issues)
    assert not any("summary" in i for i in issues)


def test_coverage_unknown_and_stale_evidence_binding_fails():
    ids = ("ev-active",)
    full = tuple(ApiFieldBinding(field_path=fp, evidence_ids=ids)
                 for fp in ("method", "path", "version_scope", "summary"))
    doc = ApiDocumentIR(endpoints=(_covered_endpoint(
        *full, ApiFieldBinding(field_path="description",
                               evidence_ids=("ev-unknown",))),))
    issues = evidence_mod.evidence_coverage_issues(doc, ids)
    assert any("ev-unknown" in i for i in issues)
    # available 集合外的 active 记录同样失败（模拟 stale/未知）。
    issues2 = evidence_mod.evidence_coverage_issues(doc, {"ev-other"})
    assert any("not in available" in i for i in issues2)


def test_coverage_knowledge_gap_without_evidence_allowed():
    doc = ApiDocumentIR(
        knowledge_gaps=(ApiKnowledgeGap(gap_type="no_evidence",
                                        description="缺证据"),))
    assert evidence_mod.evidence_coverage_issues(doc, ()) == ()
    evidence_mod.validate_evidence_coverage(doc, ())  # 不抛


def test_coverage_conflict_binding_with_evidence_allowed():
    ep = _covered_endpoint(
        ApiFieldBinding(field_path="method", evidence_ids=("ev1",)),
        ApiFieldBinding(field_path="path", evidence_ids=("ev1",)),
        ApiFieldBinding(field_path="version_scope", evidence_ids=("ev1",)),
        ApiFieldBinding(field_path="summary", evidence_ids=("ev1",),
                        usage_type="conflict"),
        description="")
    assert evidence_mod.evidence_coverage_issues(
        ApiDocumentIR(endpoints=(ep,)), {"ev1"}) == ()


def test_coverage_document_level_facts_require_binding():
    doc = ApiDocumentIR(
        overview="说明",
        authentication=(ApiAuthentication(name="bearerAuth", kind="http"),),
        data_models=(ApiDataModel(name="User", schema={"type": "object"}),),
    )
    issues = evidence_mod.evidence_coverage_issues(doc, {"ev1"})
    assert any("overview" in i for i in issues)
    assert any("authentication" in i for i in issues)
    assert any("data_model" in i for i in issues)
    ok = ApiDocumentIR(
        overview="说明",
        authentication=(ApiAuthentication(name="bearerAuth", kind="http"),),
        data_models=(ApiDataModel(name="User", schema={"type": "object"}),),
        evidence_bindings=(
            ApiFieldBinding(field_path="overview", evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="authentication.bearerAuth",
                            evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="data_models.User",
                            evidence_ids=("ev1",)),
        ),
    )
    assert evidence_mod.evidence_coverage_issues(ok, {"ev1"}) == ()


def test_coverage_empty_document_passes():
    assert evidence_mod.evidence_coverage_issues(ApiDocumentIR(), ()) == ()
    evidence_mod.validate_evidence_coverage(ApiDocumentIR(), ())
