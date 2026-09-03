"""Phase 7B：Validator 测试（结构化报告；拦截无证据 / 虚构 / 非法参数 / 泄漏）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.wiki_skills.api_reference.blueprint import (
    ApiBlueprint,
    ApiSectionSpec,
    plan_document,
)
from app.core.wiki_skills.api_reference.parser import parse_openapi
from app.core.wiki_skills.api_reference.renderer import (
    ApiRenderedSection,
    render_document,
)
from app.core.wiki_skills.api_reference.schemas import (
    ApiDocumentIR,
    ApiEndpoint,
    ApiParameter,
    ApiResponse,
)
from app.core.wiki_skills.api_reference.validator import (
    ApiValidationIssue,
    ApiValidationReport,
    validate_compile,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_reference"
V1_SPEC = (FIXTURES / "openapi_users_v1.json").read_text(encoding="utf-8")


def _evidence(prefix="ev1"):
    return [{"evidence_id": prefix, "source_page_id": "p", "status": "active",
             "locator": {"section": "all"}}]


def _ir(prefix="ev1", scope="v1"):
    return parse_openapi(V1_SPEC, _evidence(prefix), version_scope=scope)


def _passing():
    ir = _ir()
    bp = plan_document(ir, ["OpenAPI 用户 v1"])
    sections = render_document(ir, bp)
    return ir, bp, sections


def _codes(report):
    return [i.code for i in report.issues]


def test_validation_report_roundtrip():
    issue = ApiValidationIssue(code="EVIDENCE_COVERAGE", section_key="s",
                               field_path="f", message="m")
    report = ApiValidationReport(status="fail", issues=(issue,))
    restored = ApiValidationReport.from_dict(
        json.loads(json.dumps(report.to_dict())))
    assert restored.to_dict() == report.to_dict()
    assert issue.to_dict()["severity"] == "error"


def test_valid_report_requires_fail_on_errors():
    issue = ApiValidationIssue(code="X", message="m")
    with pytest.raises(ValueError):
        ApiValidationReport(status="pass", issues=(issue,))
    with pytest.raises(ValueError):
        ApiValidationReport(status="fail", issues=())


def test_valid_compile_passes():
    ir, bp, sections = _passing()
    report = validate_compile(ir, bp, sections, {"ev1"})
    assert report.status == "pass"
    assert report.issues == ()


def test_validator_blocks_missing_evidence():
    from app.core.wiki_skills.api_reference.schemas import ApiFieldBinding
    ep = ApiEndpoint(
        method="GET", path="/x", summary="无证据事实",
        evidence_bindings=(ApiFieldBinding(field_path="summary",
                                           evidence_ids=("evX",)),))
    ir = ApiDocumentIR(endpoints=(ep,))
    bp = plan_document(ir)
    sections = render_document(ir, bp)
    report = validate_compile(ir, bp, sections, {"ev-other"})
    assert report.status == "fail"
    assert "EVIDENCE_COVERAGE" in _codes(report)


def test_validator_blocks_path_param_violations():
    from app.core.wiki_skills.api_reference.schemas import ApiFieldBinding
    ep = ApiEndpoint(
        method="GET", path="/users/{id}",
        path_parameters=(ApiParameter(name="missingInTemplate",
                                      location="path", required=False),),
        query_parameters=(ApiParameter(name="id", location="query"),),
        responses=(ApiResponse(status_code="bad-code"),),
        evidence_bindings=(
            ApiFieldBinding(field_path="method", evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="path", evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="version_scope", evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="responses.bad-code",
                            evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="query_parameters.id",
                            evidence_ids=("ev1",)),
            ApiFieldBinding(field_path="path_parameters.missingInTemplate",
                            evidence_ids=("ev1",)),
        ),
    )
    ir = ApiDocumentIR(endpoints=(ep,))
    bp = plan_document(ir)
    sections = render_document(ir, bp)
    report = validate_compile(ir, bp, sections, {"ev1"})
    assert report.status == "fail"
    codes = _codes(report)
    assert "PATH_PARAM_MISMATCH" in codes          # 声明参数不在模板 / {id} 未声明
    assert "PATH_PARAM_NOT_REQUIRED" in codes       # path 参数必须 required
    assert "INVALID_RESPONSE_STATUS" in codes       # 非法状态码


def test_validator_blocks_rendered_fact_mismatch_and_leak():
    ir, bp, sections = _passing()
    # 篡改 endpoint section：抹掉 method/path 提及并注入 IR 不存在的状态码，
    # 以及把 Evidence ID 打进正文。
    mutated = []
    for s in sections:
        if s.section_key == "api_endpoint|get|/users|v1":
            content = s.content.replace("GET", "").replace("`200`", "`999`")
            content += "\nev1 ev-secret\n"
            s = ApiRenderedSection(section_key=s.section_key,
                                   heading=s.heading, content=content,
                                   evidence_ids=s.evidence_ids)
        mutated.append(s)
    report = validate_compile(ir, bp, tuple(mutated), {"ev1"})
    assert report.status == "fail"
    codes = _codes(report)
    assert "RENDERED_FACT_MISMATCH" in codes
    assert "RENDERED_EVIDENCE_LEAK" in codes


def test_validator_blocks_section_mismatch():
    ir, bp, sections = _passing()
    report = validate_compile(ir, bp, tuple(reversed(sections)), {"ev1"})
    assert report.status == "fail"
    assert "SECTION_MISMATCH" in _codes(report)


def test_validator_blocks_required_section_and_unstable_blueprint():
    ir, bp, sections = _passing()
    # 去掉 overview required section。
    specs = [s for s in bp.sections if s.section_role != "overview"]
    broken_bp = ApiBlueprint(sections=tuple(specs), sources=bp.sources)
    report = validate_compile(ir, broken_bp, sections, {"ev1"})
    assert "REQUIRED_SECTION_MISSING" in _codes(report)
    # 乱序 Blueprint → 不稳定。
    shuffled = ApiBlueprint(sections=tuple(reversed(bp.sections)),
                            sources=bp.sources)
    report2 = validate_compile(ir, shuffled, tuple(reversed(sections)), {"ev1"})
    assert "UNSTABLE_BLUEPRINT" in _codes(report2)


def test_validator_v1_v2_not_mixed_within_section():
    from app.core.wiki_skills.api_reference.merge import merge_documents
    merged = merge_documents([
        parse_openapi(V1_SPEC, _evidence("e1"), version_scope="v1"),
        parse_openapi(V1_SPEC, _evidence("e2"), version_scope="v2"),
    ])
    bp = plan_document(merged)
    sections = render_document(merged, bp)
    report = validate_compile(merged, bp, sections, {"e1", "e2"})
    assert report.status == "pass"
    # 每个 endpoint Section 版本标签单一，不混写。
    for s in sections:
        heading = s.heading
        assert "（v1v2）" not in heading
