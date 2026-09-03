"""Phase 7B：确定性 Markdown 渲染测试（事实原样 / 空字段 / JSON 格式 / 不泄漏）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.wiki_skills.api_reference.blueprint import plan_document
from app.core.wiki_skills.api_reference.merge import merge_documents
from app.core.wiki_skills.api_reference.parser import parse_openapi
from app.core.wiki_skills.api_reference.renderer import (
    ApiRenderedSection,
    render_document,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_reference"
V1_SPEC = (FIXTURES / "openapi_users_v1.json").read_text(encoding="utf-8")


def _evidence(prefix="ev1"):
    return [{"evidence_id": prefix, "source_page_id": "p", "status": "active",
             "locator": {"section": "all"}}]


def _ir(scope="v1", prefix="ev1"):
    return parse_openapi(V1_SPEC, _evidence(prefix), version_scope=scope)


def _render(ir, labels=("OpenAPI 用户 v1",)):
    bp = plan_document(ir, labels)
    return bp, render_document(ir, bp)


def test_rendered_section_roundtrip():
    sec = ApiRenderedSection(section_key="overview", heading="概述",
                             content="说明", evidence_ids=("ev1",))
    restored = ApiRenderedSection.from_dict(json.loads(json.dumps(sec.to_dict())))
    assert restored.to_dict() == sec.to_dict()


def test_rendered_section_evidence_ids_validated():
    # 去重 + 稳定排序 + 拒绝空字符串/非字符串。
    sec = ApiRenderedSection(section_key="k", heading="h",
                             evidence_ids=("ev-b", "ev-a", "ev-b"))
    assert sec.evidence_ids == ("ev-a", "ev-b")
    with pytest.raises(ValueError):
        ApiRenderedSection(section_key="k", heading="h", evidence_ids=("",))
    with pytest.raises(ValueError):
        ApiRenderedSection(section_key="k", heading="h", evidence_ids=(1,))
    with pytest.raises(ValueError):
        ApiRenderedSection(section_key="k", heading="h", evidence_ids="ev1")
    with pytest.raises(ValueError):
        ApiRenderedSection(section_key="k", heading="h",
                           validation_status="weird")


def test_render_endpoint_keeps_method_path_and_params():
    bp, sections = _render(_ir())
    by_key = {s.section_key: s for s in sections}
    ep_sec = by_key["api_endpoint|get|/users|v1"]
    content = ep_sec.content
    assert "GET" in content and "/users" in content
    one = by_key["api_endpoint|get|/users/{userId}|v1"]
    assert "userId" in one.content
    assert "**路径参数**" in one.content


def test_render_queries_params_and_request_body():
    bp, sections = _render(_ir())
    by_key = {s.section_key: s for s in sections}
    list_sec = by_key["api_endpoint|get|/users|v1"]
    assert "page" in list_sec.content and "size" in list_sec.content
    post = by_key["api_endpoint|post|/users|v1"]
    assert "**请求体**" in post.content
    assert "application/json" in post.content
    assert "X-Request-Id" in post.content


def test_render_responses_and_error_codes_raw():
    bp, sections = _render(_ir())
    by_key = {s.section_key: s for s in sections}
    one = by_key["api_endpoint|get|/users/{userId}|v1"]
    for token in ("`404`", "`500`", "`200`"):
        assert token in one.content
    assert "**错误码**" in one.content
    # 缺失 description 的接口显示确定性占位（DELETE 无 description）。
    delete = by_key["api_endpoint|delete|/users/{userId}|v1"]
    assert "资料未提供" in delete.content


def test_render_example_json_valid_and_media():
    bp, sections = _render(_ir())
    by_key = {s.section_key: s for s in sections}
    content = by_key["api_endpoint|get|/users|v1"].content
    assert "```json" in content
    block = content.split("```json")[1].split("```")[0]
    parsed = json.loads(block)  # 必须是合法 JSON
    assert parsed[0]["name"]
    assert "application/json" in content or "example-" in content


def test_render_does_not_leak_evidence_ids_or_sensitive_fields():
    bp, sections = _render(_ir(), labels=("OpenAPI 用户 v1",))
    joined = "\n".join(s.content for s in sections)
    assert "ev1" not in joined
    assert "Authorization: " not in joined
    # 结构化 DTO 保留证据，正文不含。
    ep_sections = [s for s in sections if s.evidence_ids]
    assert ep_sections
    assert all(s.evidence_ids == ("ev1",) for s in ep_sections if s.evidence_ids)


def test_v1_v2_render_isolated_sections():
    merged = merge_documents([
        parse_openapi(V1_SPEC, _evidence("e1"), version_scope="v1"),
        parse_openapi(V1_SPEC, _evidence("e2"), version_scope="v2"),
    ])
    bp, sections = _render(merged)
    keys = {s.section_key for s in sections}
    assert "api_endpoint|get|/users|v1" in keys
    assert "api_endpoint|get|/users|v2" in keys


def test_render_missing_responses_shows_gap():
    spec = {"openapi": "3.0.0",
            "paths": {"/x": {"post": {"summary": "create"}}}}
    ir = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    bp, sections = _render(ir)
    gap_sec = next(s for s in sections if s.section_key == "knowledge_gaps")
    assert "missing_responses" in gap_sec.content
    ep = next(s for s in sections
              if s.heading.startswith("POST /x"))
    assert "资料未提供" in ep.content


def test_render_deterministic():
    ir = _ir()
    _, a = _render(ir)
    _, b = _render(ir)
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]


def test_rendered_section_validation_status_default_unknown():
    bp, sections = _render(_ir())
    assert all(s.validation_status == "unknown" for s in sections)
