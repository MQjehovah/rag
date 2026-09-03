"""Phase 7A：多 ApiDocumentIR 合并测试（纯函数；Evidence 保持；顺序无关）。"""
from __future__ import annotations

import json

from app.core.wiki_skills.api_reference.merge import merge_documents
from app.core.wiki_skills.api_reference.parser import parse_openapi


def _spec(method="get", path="/users", summary="列表",
          responses=None, params=None, components_schemas=None):
    responses = responses or {"200": {"description": "ok"}}
    return {
        "openapi": "3.0.1",
        "paths": {path: {method: {
            "summary": summary,
            "parameters": params or [],
            "responses": responses,
        }}},
        "components": {"schemas": components_schemas or {}},
    }


def _rows(*ids):
    return [{"evidence_id": eid, "source_page_id": "p", "status": "active",
             "locator": {"section": "all"}} for eid in ids]


def _parse(spec, ids, scope=None):
    return parse_openapi(json.dumps(spec), _rows(*ids), format="json",
                         version_scope=scope)


# ---------------------------------------------------------------------------
# 互补合并
# ---------------------------------------------------------------------------


def test_merge_complementary_responses_union_and_evidence_kept():
    doc_a = _parse(_spec(responses={"200": {"description": "ok-a"}}), ["evA"])
    doc_b = _parse(_spec(responses={
        "404": {"description": "missing-b"},
        "500": {"description": "err-b"},
    }), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    assert len(merged.endpoints) == 1
    ep = merged.endpoints[0]
    assert {r.status_code for r in ep.responses} == {"200", "404", "500"}
    binding = next(b for b in ep.evidence_bindings if b.field_path == "responses.200")
    assert binding.usage_type == "support"
    assert binding.evidence_ids == ("evA",)
    binding500 = next(b for b in ep.evidence_bindings
                      if b.field_path == "responses.500")
    assert binding500.evidence_ids == ("evB",)


def test_merge_same_fact_evidence_union():
    doc_a = _parse(_spec(summary="完全相同"), ["evA"])
    doc_b = _parse(_spec(summary="完全相同"), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    ep = merged.endpoints[0]
    binding = next(b for b in ep.evidence_bindings if b.field_path == "summary")
    assert binding.usage_type == "support"
    assert binding.evidence_ids == ("evA", "evB")
    assert ep.summary == "完全相同"


# ---------------------------------------------------------------------------
# 冲突：不静默覆盖
# ---------------------------------------------------------------------------


def test_merge_conflict_not_silently_overwritten():
    doc_a = _parse(_spec(summary="版本A的语义"), ["evA"])
    doc_b = _parse(_spec(summary="版本B的语义"), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    ep = merged.endpoints[0]
    # 展示值取规范序首个来源（确定性选择），事实本身被标记为冲突。
    assert ep.summary in ("版本A的语义", "版本B的语义")
    binding = next(b for b in ep.evidence_bindings if b.field_path == "summary")
    assert binding.usage_type == "conflict"
    assert binding.evidence_ids == ("evA", "evB")
    # 冲突进入 knowledge gap，保留冲突记录，不判断谁正确。
    conflict_gaps = [g for g in merged.knowledge_gaps
                     if g.gap_type == "conflicting_facts"]
    assert conflict_gaps
    assert any("版本A的语义" in g.description and "版本B的语义" in g.description
               for g in conflict_gaps)


def test_merge_v1_v2_not_merged_by_scope():
    doc_v1 = _parse(_spec(method="get", path="/users", summary="v1 列表"),
                    ["ev1"], scope="v1")
    doc_v2 = _parse(_spec(method="get", path="/users", summary="v2 列表"),
                    ["ev2"], scope="v2")
    merged = merge_documents([doc_v1, doc_v2])
    assert len(merged.endpoints) == 2
    assert {e.version_scope for e in merged.endpoints} == {"v1", "v2"}


def test_merge_different_methods_kept_separate():
    doc_a = _parse(_spec(method="get", path="/users"), ["evA"])
    doc_b = _parse(_spec(method="post", path="/users"), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    assert {e.method for e in merged.endpoints} == {"GET", "POST"}


# ---------------------------------------------------------------------------
# 顺序无关 / 幂等 / 输入不变
# ---------------------------------------------------------------------------


def test_merge_order_reversal_identical_output():
    doc_a = _parse(_spec(responses={"200": {"description": "a"},
                                    "404": {"description": "a404"}}), ["evA"])
    doc_b = _parse(_spec(responses={"500": {"description": "b"}}), ["evB"])
    out = merge_documents([doc_a, doc_b])
    rev = merge_documents([doc_b, doc_a])
    assert out.to_dict() == rev.to_dict()


def test_merge_does_not_modify_inputs():
    doc_a = _parse(_spec(summary="A"), ["evA"])
    doc_b = _parse(_spec(summary="B"), ["evB"])
    before_a = json.dumps(doc_a.to_dict(), ensure_ascii=False, sort_keys=True)
    before_b = json.dumps(doc_b.to_dict(), ensure_ascii=False, sort_keys=True)
    merge_documents([doc_a, doc_b])
    assert json.dumps(doc_a.to_dict(), ensure_ascii=False, sort_keys=True) == before_a
    assert json.dumps(doc_b.to_dict(), ensure_ascii=False, sort_keys=True) == before_b


def test_merge_idempotent_duplicate_docs():
    doc = _parse(_spec(summary="一次"), ["evA"])
    once = merge_documents([doc])
    twice = merge_documents([doc, doc])
    assert once.to_dict() == twice.to_dict()
    assert not [g for g in twice.knowledge_gaps
                if g.gap_type == "conflicting_facts"]


# ---------------------------------------------------------------------------
# Document 级事实 / gaps
# ---------------------------------------------------------------------------


def test_merge_doc_auth_data_models_union_without_fabrication():
    spec_a = dict(_spec(summary="a"))
    spec_a["components"]["schemas"] = {"UserA": {"type": "object"}}
    spec_a["components"]["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer"}}
    spec_b = dict(_spec(summary="b"))
    spec_b["components"]["schemas"] = {"UserB": {"type": "object"}}
    doc_a = _parse(spec_a, ["evA"])
    doc_b = _parse(spec_b, ["evB"])
    merged = merge_documents([doc_a, doc_b])
    assert {m.name for m in merged.data_models} == {"UserA", "UserB"}
    assert {a.name for a in merged.authentication} == {"bearerAuth"}
    # authentication 事实来自源文档，不凭空复制。
    assert len(merged.authentication) == 1


def test_merge_doc_level_conflict_surface():
    from app.core.wiki_skills.api_reference.schemas import ApiDocumentIR, \
        ApiFieldBinding
    # 直接构造两个不同 overview 的文档。
    d1 = ApiDocumentIR(
        overview="说明甲",
        evidence_bindings=(ApiFieldBinding(field_path="overview",
                                           evidence_ids=("evA",)),))
    d2 = ApiDocumentIR(
        overview="说明乙",
        evidence_bindings=(ApiFieldBinding(field_path="overview",
                                           evidence_ids=("evB",)),))
    merged = merge_documents([d1, d2])
    assert any(g.gap_type == "conflicting_facts"
               and "overview" in g.description for g in merged.knowledge_gaps)
    conflict = [b for b in merged.evidence_bindings
                if b.field_path == "overview"]
    assert conflict and conflict[0].usage_type == "conflict"


def test_merge_knowledge_gaps_deduped_union():
    spec_no_resp = {"openapi": "3.0.0",
                    "paths": {"/x": {"post": {"summary": "s"}}}}
    doc_a = parse_openapi(json.dumps(spec_no_resp), _rows("evA"), format="json")
    doc_b = parse_openapi(json.dumps(spec_no_resp), _rows("evB"), format="json")
    merged = merge_documents([doc_a, doc_b])
    missing = [g for g in merged.knowledge_gaps
               if g.gap_type == "missing_responses"]
    assert len(missing) == 1


# ---------------------------------------------------------------------------
# Phase 7B 第 0 步：merge 输出闭合 Evidence coverage
# ---------------------------------------------------------------------------


def test_merge_output_passes_evidence_coverage():
    from app.core.wiki_skills.api_reference.evidence import \
        validate_evidence_coverage
    doc_a = _parse(_spec(responses={"200": {"description": "ok-a"}}), ["evA"])
    doc_b = _parse(_spec(responses={"404": {"description": "missing-b"}}), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    validate_evidence_coverage(merged, {"evA", "evB"})  # 不抛即通过


def test_merge_identity_bindings_evidence_union():
    doc_a = _parse(_spec(summary="相同"), ["evA"])
    doc_b = _parse(_spec(summary="相同"), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    ep = merged.endpoints[0]
    for fp in ("method", "path", "version_scope"):
        binding = next(b for b in ep.evidence_bindings if b.field_path == fp)
        assert binding.usage_type == "support"
        assert binding.evidence_ids == ("evA", "evB")


def test_merge_does_not_fabricate_missing_identity_bindings():
    from app.core.wiki_skills.api_reference.evidence import \
        evidence_coverage_issues
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
    )
    # 输入 Endpoint 完全没有身份 Binding（缺陷输入）：merge 不得伪造补齐。
    broken = ApiDocumentIR(endpoints=(
        ApiEndpoint(method="GET", path="/x", summary="无绑定"),))
    merged = merge_documents([broken])
    assert evidence_coverage_issues(merged, {"ev1"})


def test_merge_conflict_output_still_passes_coverage():
    from app.core.wiki_skills.api_reference.evidence import \
        validate_evidence_coverage
    doc_a = _parse(_spec(summary="语义A"), ["evA"])
    doc_b = _parse(_spec(summary="语义B"), ["evB"])
    merged = merge_documents([doc_a, doc_b])
    assert any(g.gap_type == "conflicting_facts"
               for g in merged.knowledge_gaps)
    validate_evidence_coverage(merged, {"evA", "evB"})


def test_merge_identity_evidence_field_specific():
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiFieldBinding,
    )
    em, ep, ev = "ev-method", "ev-path", "ev-scope"
    ep_obj = ApiEndpoint(
        method="GET", path="/x", version_scope="v1",
        evidence_bindings=(
            ApiFieldBinding(field_path="method", evidence_ids=(em,)),
            ApiFieldBinding(field_path="path", evidence_ids=(ep,)),
            ApiFieldBinding(field_path="version_scope", evidence_ids=(ev,)),
        ))
    merged = merge_documents([
        ApiDocumentIR(endpoints=(ep_obj,)),
        ApiDocumentIR(endpoints=(ep_obj,)),
    ])
    out = merged.endpoints[0]
    by_path = {b.field_path: b.evidence_ids for b in out.evidence_bindings}
    assert by_path["method"] == (em,)
    assert by_path["path"] == (ep,)
    assert by_path["version_scope"] == (ev,)


def test_merge_missing_path_evidence_not_borrowed():
    from app.core.wiki_skills.api_reference.evidence import \
        evidence_coverage_issues
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiFieldBinding,
    )
    # 输入 path 无 Evidence（模拟缺失）：merge 不得借用 method 的 Evidence。
    broken = ApiDocumentIR(endpoints=(ApiEndpoint(
        method="GET", path="/x", version_scope="v1",
        evidence_bindings=(
            ApiFieldBinding(field_path="method", evidence_ids=("em",)),
            ApiFieldBinding(field_path="version_scope", evidence_ids=("ev",)),
        )),))
    merged = merge_documents([broken])
    out = merged.endpoints[0]
    by_path = {b.field_path: b.evidence_ids for b in out.evidence_bindings}
    assert "path" not in by_path           # 缺失保持缺失，不借用 method
    assert by_path["method"] == ("em",)    # method 只合并来源 method
    issues = evidence_coverage_issues(merged, {"em", "ev"})
    assert any("path" in i for i in issues)  # coverage gate 失败
