"""Phase 7B：Blueprint 规划测试（确定性 / section_key / 顺序 / v1v2 隔离）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.wiki_skills.api_reference.blueprint import (
    REQUIRED_SECTION_ROLES,
    ApiBlueprint,
    ApiSectionSpec,
    plan_document,
)
from app.core.wiki_skills.api_reference.identity import (
    build_endpoint_id,
    build_endpoint_section_key,
)
from app.core.wiki_skills.api_reference.parser import parse_openapi
from app.core.wiki_skills.api_reference.schemas import ApiDocumentIR
from app.core.wiki_skills.api_reference.merge import merge_documents

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_reference"
V1_SPEC = (FIXTURES / "openapi_users_v1.json").read_text(encoding="utf-8")


def _evidence(prefix="ev"):
    return [{"evidence_id": prefix, "source_page_id": "p", "status": "active",
             "locator": {"section": "all"}}]


def _ir_v1():
    return parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")


def test_section_spec_roundtrip():
    spec = ApiSectionSpec(section_key="overview", section_role="overview",
                          heading="概述", required=True,
                          field_paths=("overview",))
    restored = ApiSectionSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert restored.to_dict() == spec.to_dict()


def test_blueprint_roundtrip():
    bp = plan_document(_ir_v1(), ["来源 A"])
    restored = ApiBlueprint.from_dict(json.loads(json.dumps(bp.to_dict())))
    assert restored.to_dict() == bp.to_dict()
    assert restored.schema_version == "api-blueprint/v1"


def test_blueprint_roles_order_for_rich_ir():
    ir = _ir_v1()
    bp = plan_document(ir, ["来源 A"])
    roles = [s.section_role for s in bp.sections]
    # 固定顺序约束：overview → endpoint → data_models → sources。
    assert roles[0] == "overview"
    assert roles[-1] == "sources"
    endpoints = [s for s in bp.sections if s.section_role == "endpoint"]
    assert [e.endpoint_id for e in ir.endpoints] == \
        [s.endpoint_id for s in endpoints]
    assert roles.count("endpoint") == len(ir.endpoints)
    assert "authentication" in roles
    assert "data_models" in roles


def test_each_endpoint_own_section_with_stable_key():
    bp = plan_document(_ir_v1())
    keys = [s.section_key for s in bp.sections
            if s.section_role == "endpoint"]
    assert len(set(keys)) == len(keys)
    # Endpoint Section 按 endpoint_id 排序（与 IR 一致）。
    expected = [build_endpoint_section_key(ep.method, ep.path, ep.version_scope)
                for ep in _ir_v1().endpoints]
    assert keys == expected
    ep_spec = next(s for s in bp.sections if s.endpoint_id
                   == build_endpoint_id("GET", "/users/{userId}", "v1"))
    assert ep_spec.version_label == "v1"
    assert "path_parameters" in ep_spec.field_paths


def test_v1_v2_are_distinct_sections():
    ir = merge_documents([
        parse_openapi(V1_SPEC, _evidence("e1"), version_scope="v1"),
        parse_openapi(V1_SPEC, _evidence("e2"), version_scope="v2"),
    ])
    bp = plan_document(ir)
    keys = [s.section_key for s in bp.sections
            if s.section_role == "endpoint"]
    assert build_endpoint_section_key("GET", "/users", "v1") in keys
    assert build_endpoint_section_key("GET", "/users", "v2") in keys
    assert len(keys) == len(ir.endpoints)


def test_required_sections_present_even_empty_ir():
    bp = plan_document(ApiDocumentIR())
    keys = bp.section_keys()
    assert keys == ("overview", "sources")
    for spec in bp.sections:
        assert spec.required is True
    assert set(REQUIRED_SECTION_ROLES) == {"overview", "sources"}


def test_contentless_optional_sections_omitted():
    bp = plan_document(ApiDocumentIR(overview="有概述",
                                     evidence_bindings=()))
    roles = {s.section_role for s in bp.sections}
    assert "authentication" not in roles
    assert "data_models" not in roles
    assert "endpoint" not in roles
    assert "overview" in roles


def test_plan_is_deterministic():
    ir = _ir_v1()
    assert plan_document(ir, ["B", "A"]).to_dict() == \
        plan_document(ir, ["B", "A"]).to_dict()
    # sources 标签顺序与输入顺序无关（排序去重）。
    assert plan_document(ir, ["B", "A"]).sources == \
        plan_document(ir, ["A", "B"]).sources


def test_blueprint_rejects_duplicate_and_unknown():
    with pytest.raises(ValueError):
        ApiBlueprint(sections=(
            ApiSectionSpec(section_key="k", section_role="overview",
                           heading="h", required=True),
            ApiSectionSpec(section_key="k", section_role="sources",
                           heading="s", required=True),
        ))
    with pytest.raises(ValueError):
        ApiBlueprint.from_dict({"schema_version": "api-blueprint/v1",
                                "sections": [], "junk": 1})
    with pytest.raises(ValueError):
        ApiSectionSpec(section_key="k", section_role="mystery", heading="h")


def test_section_spec_required_must_be_real_bool():
    with pytest.raises(ValueError):
        ApiSectionSpec(section_key="k", section_role="overview",
                       heading="h", required="false")
    with pytest.raises(ValueError):
        ApiSectionSpec(section_key="k", section_role="overview",
                       heading="h", required=1)
    with pytest.raises(ValueError):
        ApiSectionSpec.from_dict({"section_key": "k", "section_role": "overview",
                                  "heading": "h", "required": "false"})
    spec = ApiSectionSpec(section_key="k", section_role="overview",
                          heading="h", required=True)
    assert spec.required is True


def test_section_spec_field_paths_must_be_str_sequence():
    with pytest.raises(ValueError):
        ApiSectionSpec(section_key="k", section_role="overview",
                       heading="h", field_paths="overview")
    with pytest.raises(ValueError):
        ApiSectionSpec(section_key="k", section_role="overview",
                       heading="h", field_paths=("overview", 1))


def test_blueprint_sources_normalized_sorted_dedup():
    bp = ApiBlueprint(sections=(), sources=("b", "a", "b"))
    assert bp.sources == ("a", "b")
    with pytest.raises(ValueError):
        ApiBlueprint(sections=(), sources="a")
    with pytest.raises(ValueError):
        ApiBlueprint(sections=(), sources=(1,))
    with pytest.raises(ValueError):
        ApiBlueprint(sections=(), sources=("",))
    restored = ApiBlueprint.from_dict(
        json.loads(json.dumps(ApiBlueprint(sections=(), sources=("b", "a")).to_dict())))
    assert restored.sources == ("a", "b")
    assert restored.to_dict()["sources"] == ["a", "b"]
