"""Phase 7A：确定性解析器测试（OpenAPI JSON/YAML + Markdown hint）。

覆盖：JSON/YAML 解析；参数位置；request/response/status/error/example 有 Evidence；
本地 $ref；循环 $ref；外部 $ref 不下载；不支持的 method 不进 Endpoint；非法输入产生
安全诊断；无 Evidence 内容只能成为 knowledge gap；Markdown 只产 hint。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.wiki_skills.api_reference.evidence import (
    evidence_coverage_issues,
    validate_evidence_coverage,
)
from app.core.wiki_skills.api_reference.parser import (
    MAX_REF_DEPTH,
    ApiReferenceParseError,
    parse_markdown_hints,
    parse_openapi,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_reference"
V1_SPEC = (FIXTURES / "openapi_users_v1.json").read_text(encoding="utf-8")
V2_SPEC = (FIXTURES / "openapi_users_v2.yaml").read_text(encoding="utf-8")


def _evidence(*ids, section="all", status="active"):
    rows = []
    for i, eid in enumerate(ids):
        rows.append({
            "evidence_id": eid,
            "source_page_id": "page-users",
            "source_chunk_id": f"chunk-{i}",
            "status": status,
            "locator": {"section": section},
        })
    return rows


def _endpoint_by(doc, method, path):
    return next(e for e in doc.endpoints if e.method == method and e.path == path)


# ---------------------------------------------------------------------------
# OpenAPI JSON / YAML 解析
# ---------------------------------------------------------------------------


def test_parse_v1_json_endpoints_sorted_and_scoped():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    assert [e.endpoint_id for e in doc.endpoints] == [
        "DELETE /users/{userId} [v1]",
        "GET /users [v1]",
        "GET /users/{userId} [v1]",
        "POST /users [v1]",
    ]


def test_parse_yaml_v2():
    doc = parse_openapi(V2_SPEC, _evidence("ev2"), version_scope="v2", format="yaml")
    ids = [e.endpoint_id for e in doc.endpoints]
    assert "GET /users [v2]" in ids
    assert "PATCH /users/{userId} [v2]" in ids
    get = _endpoint_by(doc, "GET", "/users")
    assert "v2" in get.summary
    assert [p.name for p in get.query_parameters] == ["cursor"]


def test_param_locations_distinct():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    users = _endpoint_by(doc, "GET", "/users")
    assert all(p.location == "query" for p in users.query_parameters)
    assert {p.name for p in users.query_parameters} == {"page", "size"}
    one = _endpoint_by(doc, "GET", "/users/{userId}")
    assert [p.name for p in one.path_parameters] == ["userId"]
    assert one.path_parameters[0].location == "path"
    post = _endpoint_by(doc, "POST", "/users")
    assert [h.name for h in post.headers] == ["X-Request-Id"]
    assert post.headers[0].location == "header"


def test_request_response_status_error_example_have_evidence():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    one = _endpoint_by(doc, "GET", "/users/{userId}")
    assert one.request_body is None
    assert {r.status_code for r in one.responses} == {"200", "404", "500"}
    assert {ec.code for ec in one.error_codes} == {"404", "500"}
    paths = {b.field_path for b in one.evidence_bindings}
    assert "responses.200" in paths and "responses.404" in paths
    assert "error_codes.404" in paths
    assert all(b.evidence_ids == ("ev1",) for b in one.evidence_bindings)

    post = _endpoint_by(doc, "POST", "/users")
    assert post.request_body is not None
    assert post.request_body.required is True
    body_paths = {b.field_path for b in post.evidence_bindings}
    assert "request_body" in body_paths
    assert {ec.code for ec in post.error_codes} == {"400"}


def test_example_is_json_and_bound():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    users = _endpoint_by(doc, "GET", "/users")
    titles = [x.title for x in users.examples]
    assert "example-200-application_json" in titles
    example = next(x for x in users.examples
                   if x.title == "example-200-application_json")
    json.dumps(example.to_dict(), ensure_ascii=False)  # 内容必须可 JSON 序列化
    bound = [b for b in users.evidence_bindings
             if b.field_path == f"examples.{example.title}"]
    assert bound and bound[0].evidence_ids == ("ev1",)


def test_local_ref_data_models_parsed_and_no_resolution_error():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    assert {m.name for m in doc.data_models} == {"User", "Error"}
    assert not [g for g in doc.knowledge_gaps
                if g.gap_type in ("unresolvable_ref", "circular_ref",
                                  "external_ref")]


# ---------------------------------------------------------------------------
# Evidence 约束：无 Evidence 内容只能成为 knowledge gap
# ---------------------------------------------------------------------------


def test_missing_evidence_means_no_facts_only_gaps():
    doc = parse_openapi(V1_SPEC, [], version_scope="v1")
    assert doc.endpoints == ()
    assert doc.data_models == ()
    assert doc.authentication == ()
    assert doc.overview == ""
    types = {g.gap_type for g in doc.knowledge_gaps}
    assert "no_evidence" in types
    assert any("GET /users" in g.description for g in doc.knowledge_gaps)


def test_stale_and_rejected_evidence_cannot_support_facts():
    stale = [{"evidence_id": "ev-s", "source_page_id": "p", "status": "stale",
              "locator": {"section": "all"}}]
    doc = parse_openapi(V1_SPEC, stale, version_scope="v1")
    assert doc.endpoints == ()
    assert any(g.gap_type == "no_evidence" for g in doc.knowledge_gaps)


def test_evidence_requires_operation_region():
    # endpoint 证据只匹配同 method/path；其他 operation 仍为 no_evidence gap。
    rows = [{"evidence_id": "ev-list", "source_page_id": "p", "status": "active",
             "locator": {"section": "endpoints", "method": "GET",
                         "path": "/users"}}]
    doc = parse_openapi(V1_SPEC, rows, version_scope="v1")
    assert [e.path for e in doc.endpoints] == ["/users"]
    assert len(doc.knowledge_gaps) >= 3


# ---------------------------------------------------------------------------
# 非法/边界输入：安全诊断而非崩溃
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad,msg", [
    ('{"paths": {}}', "openapi"),
    ('{"openapi": "2.0", "paths": {}}', "version"),
    ('{"openapi": "3.1.0"}', "paths"),
    ('["not", "mapping"]', "root"),
])
def test_root_level_errors_raise(bad, msg):
    with pytest.raises(ApiReferenceParseError) as exc:
        parse_openapi(bad, format="json")
    assert msg in str(exc.value)


def test_size_limit_rejected():
    huge = '{"openapi": "3.0.0", "paths": {}}' + " " * (2_000_000)
    with pytest.raises(ApiReferenceParseError):
        parse_openapi(huge, max_bytes=1_000)


def test_circular_ref_detected():
    spec = {
        "openapi": "3.0.0",
        "paths": {},
        "components": {"schemas": {
            "Node": {"type": "object", "properties": {"next": {
                "$ref": "#/components/schemas/Node"}}},
        }},
    }
    rows = [{"evidence_id": "e1", "source_page_id": "p", "status": "active",
             "locator": {"section": "data_models", "name": "Node"}}]
    doc = parse_openapi(json.dumps(spec), rows, format="json")
    assert any(g.gap_type == "circular_ref" for g in doc.knowledge_gaps)


def test_external_ref_rejected_without_download():
    spec = {
        "openapi": "3.0.0",
        "paths": {},
        "components": {"schemas": {
            "Remote": {"type": "object",
                       "properties": {"x": {"$ref": "https://evil.example/x.json"}}},
        }},
    }
    rows = [{"evidence_id": "e1", "source_page_id": "p", "status": "active",
             "locator": {"section": "all"}}]
    doc = parse_openapi(json.dumps(spec), rows, format="json")
    assert any(g.gap_type == "external_ref" and "no download" in g.description
               for g in doc.knowledge_gaps)


def test_unsupported_method_does_not_create_endpoint():
    spec = {"openapi": "3.0.0", "paths": {"/x": {"FETCH": {"responses": {}}}}}
    doc = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    assert doc.endpoints == ()
    # 合法 method 不受影响。
    spec["paths"]["/y"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    doc2 = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    assert [e.path for e in doc2.endpoints] == ["/y"]


def test_invalid_status_code_and_param_location_diagnostics():
    spec = {
        "openapi": "3.0.0",
        "paths": {"/bad": {"get": {
            "parameters": [{"name": "q", "in": "body", "schema": {}}],
            "responses": {"nonsense": {"description": "x"}}},
        }},
    }
    doc = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    ep = doc.endpoints[0]
    assert ep.responses == ()
    types = {g.gap_type for g in doc.knowledge_gaps}
    assert "invalid_status" in types
    assert "unsupported_param_location" in types


def test_missing_responses_generates_gap():
    spec = {"openapi": "3.0.0",
            "paths": {"/x": {"post": {"summary": "create"}}}}
    doc = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    assert any(g.gap_type == "missing_responses" for g in doc.knowledge_gaps)
    assert doc.endpoints[0].responses == ()


def test_default_and_range_status_codes_accepted():
    spec = {"openapi": "3.0.0", "paths": {"/x": {"get": {
        "responses": {"default": {"description": "d"},
                      "2XX": {"description": "range"}}}}}}
    doc = parse_openapi(json.dumps(spec), _evidence("e1"), format="json")
    assert {r.status_code for r in doc.endpoints[0].responses} == {"2XX", "default"}


# ---------------------------------------------------------------------------
# Markdown 确定性 hint
# ---------------------------------------------------------------------------


def test_markdown_only_produces_deterministic_hints():
    text = (FIXTURES / "auth_notes.md").read_text(encoding="utf-8")
    doc = parse_markdown_hints(text)
    assert doc.endpoints == ()
    assert doc.data_models == ()
    assert doc.authentication == ()
    assert doc.overview == ""
    types = {g.gap_type for g in doc.knowledge_gaps}
    assert types <= {"markdown_endpoint_hint", "markdown_status_hint"}
    ep_hints = [g for g in doc.knowledge_gaps
                if g.gap_type == "markdown_endpoint_hint"]
    assert any("GET /users" in g.description for g in ep_hints)
    assert any("POST /users" in g.description for g in ep_hints)


def test_markdown_status_hints_and_no_type_inference():
    text = (FIXTURES / "errors.md").read_text(encoding="utf-8")
    doc = parse_markdown_hints(text)
    codes = [g for g in doc.knowledge_gaps
             if g.gap_type == "markdown_status_hint"]
    assert {g.description for g in codes}
    assert len(codes) >= 5
    # 不得猜参数类型 / required / Schema：不存在任何 Endpoint/参数事实。
    assert doc.endpoints == ()
    # hint 描述明确声明不做推断。
    assert all("not inferred" in g.description or "不推断" in g.description
               for g in codes)
    # 没有正文里的自然语言进入事实：markdown 说明无 endpoint 出现 → 无 hint。
    empty = parse_markdown_hints("令牌过期后需要重新登录。")
    assert empty.knowledge_gaps == ()


def test_parser_does_not_mutate_input_text():
    spec = {"openapi": "3.0.0", "paths": {"/users": {"get": {
        "responses": {"200": {"description": "ok"}}}}}}
    text = json.dumps(spec)
    before = text
    doc1 = parse_openapi(text, _evidence("e1"), format="json")
    doc2 = parse_openapi(text, _evidence("e1"), format="json")
    assert text == before
    assert doc1.to_dict() == doc2.to_dict()


# ---------------------------------------------------------------------------
# Phase 7A.1：parser 输出必须通过 Evidence 覆盖验证
# ---------------------------------------------------------------------------


def test_parser_output_passes_evidence_coverage():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    assert evidence_coverage_issues(doc, {"ev1"}) == ()
    validate_evidence_coverage(doc, {"ev1"})
    # 每个 endpoint 身份与方法/字段都被独立绑定。
    for ep in doc.endpoints:
        bound = {b.field_path for b in ep.evidence_bindings}
        assert {"method", "path", "version_scope"} <= bound
    # YAML 输入同样通过覆盖验证。
    yaml_doc = parse_openapi(V2_SPEC, _evidence("ev1"), version_scope="v2",
                             format="yaml")
    validate_evidence_coverage(yaml_doc, {"ev1"})


def test_parser_endpoint_binding_split_summary_and_description():
    doc = parse_openapi(V1_SPEC, _evidence("ev1"), version_scope="v1")
    users = _endpoint_by(doc, "GET", "/users")
    bound = {b.field_path for b in users.evidence_bindings}
    assert "summary" in bound and "description" in bound
    # 同一 Evidence 支撑多个 field_path。
    assert all(b.evidence_ids == ("ev1",) for b in users.evidence_bindings)


# ---------------------------------------------------------------------------
# Phase 7A.1：path-level 参数
# ---------------------------------------------------------------------------


def test_path_level_parameters_only():
    spec = {"openapi": "3.0.1",
            "paths": {"/orgs/{orgId}": {
                "parameters": [
                    {"name": "orgId", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "X-Tenant", "in": "header", "required": False,
                     "schema": {"type": "string"}},
                ],
                "get": {"responses": {"200": {"description": "ok"}}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert [p.name for p in ep.path_parameters] == ["orgId"]
    assert [p.name for p in ep.headers] == ["X-Tenant"]
    # path-level 参数仍带 Evidence binding。
    bound = {b.field_path for b in ep.evidence_bindings}
    assert "path_parameters.orgId" in bound and "headers.X-Tenant" in bound


def test_path_level_and_operation_level_complement():
    spec = {"openapi": "3.0.1",
            "paths": {"/items": {
                "parameters": [{"name": "page", "in": "query",
                                "schema": {"type": "integer"}}],
                "get": {"parameters": [{"name": "size", "in": "query",
                                        "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "ok"}}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert {p.name for p in ep.query_parameters} == {"page", "size"}


def test_operation_level_overrides_path_level():
    spec = {"openapi": "3.0.1",
            "paths": {"/items": {
                "parameters": [{"name": "q", "in": "query", "required": False,
                                "description": "path level",
                                "schema": {"type": "string"}}],
                "get": {"parameters": [{"name": "q", "in": "query",
                                        "required": True,
                                        "description": "op overrides",
                                        "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "ok"}}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert len(ep.query_parameters) == 1
    param = ep.query_parameters[0]
    assert param.description == "op overrides"
    assert param.required is True


def test_same_name_path_query_do_not_override_each_other():
    spec = {"openapi": "3.0.1",
            "paths": {"/things/{id}": {
                "get": {"parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "id", "in": "query", "required": False,
                     "schema": {"type": "integer"}},
                ], "responses": {"200": {"description": "ok"}}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert {p.location for p in ep.path_parameters} == {"path"}
    assert {p.location for p in ep.query_parameters} == {"query"}
    assert ep.path_parameters[0].name == "id"
    assert ep.query_parameters[0].name == "id"


def test_path_level_and_operation_level_local_refs():
    spec = {"openapi": "3.0.1",
            "paths": {"/items": {
                "parameters": [{"$ref": "#/components/parameters/TenantHeader"}],
                "get": {"parameters": [{"$ref": "#/components/parameters/PageParam"}],
                        "responses": {"200": {"description": "ok"}}},
            }},
            "components": {"parameters": {
                "TenantHeader": {"name": "X-Tenant", "in": "header",
                                 "schema": {"type": "string"}},
                "PageParam": {"name": "page", "in": "query",
                              "schema": {"type": "integer"}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert [p.name for p in ep.headers] == ["X-Tenant"]
    assert [p.name for p in ep.query_parameters] == ["page"]
    assert not [g for g in doc.knowledge_gaps
                if g.gap_type in ("unresolvable_ref", "circular_ref",
                                  "external_ref")]


# ---------------------------------------------------------------------------
# Phase 7A.1：有界链式本地 $ref
# ---------------------------------------------------------------------------


def test_two_level_parameter_ref_chain():
    spec = {"openapi": "3.0.1",
            "paths": {"/r": {"get": {
                "parameters": [{"$ref": "#/components/parameters/PageAlias"}],
                "responses": {"200": {"description": "ok"}}}},
            },
            "components": {"parameters": {
                "PageAlias": {"$ref": "#/components/parameters/Page"},
                "Page": {"name": "page", "in": "query",
                         "schema": {"type": "integer"}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert [(p.location, p.name) for p in ep.query_parameters] == [("query", "page")]
    assert doc.knowledge_gaps == ()


def test_two_level_response_ref_chain():
    spec = {"openapi": "3.0.1",
            "paths": {"/r": {"get": {"responses": {
                "404": {"$ref": "#/components/responses/NotFoundAlias"}}}},
            },
            "components": {"responses": {
                "NotFoundAlias": {"$ref": "#/components/responses/NotFound"},
                "NotFound": {"description": "missing"},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    ep = doc.endpoints[0]
    assert [r.status_code for r in ep.responses] == ["404"]
    assert not [g for g in doc.knowledge_gaps]


def test_chain_ref_alias_cycle_detected():
    spec = {"openapi": "3.0.1",
            "paths": {"/r": {"get": {
                "parameters": [{"$ref": "#/components/parameters/A"}],
                "responses": {"200": {"description": "ok"}}}},
            },
            "components": {"parameters": {
                "A": {"$ref": "#/components/parameters/B"},
                "B": {"$ref": "#/components/parameters/A"},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    assert any(g.gap_type == "circular_ref" for g in doc.knowledge_gaps)
    assert doc.endpoints[0].query_parameters == ()


def test_chain_ref_max_depth_rejected():
    params = {}
    for i in range(MAX_REF_DEPTH + 4):
        params[f"P{i}"] = {"$ref": f"#/components/parameters/P{i + 1}"}
    params[f"P{MAX_REF_DEPTH + 4}"] = {
        "name": "page", "in": "query", "schema": {"type": "integer"}}
    spec = {"openapi": "3.0.1",
            "paths": {"/r": {"get": {
                "parameters": [{"$ref": "#/components/parameters/P0"}],
                "responses": {"200": {"description": "ok"}}}},
            },
            "components": {"parameters": params}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    assert any(g.gap_type == "max_depth_ref" for g in doc.knowledge_gaps)
    assert doc.endpoints[0].query_parameters == ()


def test_chain_ref_missing_pointer_and_external_rejected():
    spec = {"openapi": "3.0.1",
            "paths": {"/r": {"get": {
                "parameters": [
                    {"$ref": "#/components/parameters/Nope"},
                    {"$ref": "https://evil.example/x.yaml#/components/parameters/P"},
                ],
                "responses": {"200": {"description": "ok"}}}},
            },
            "components": {"parameters": {}}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    types = {g.gap_type for g in doc.knowledge_gaps}
    assert "unresolvable_ref" in types
    assert "external_ref" in types
    assert doc.endpoints[0].query_parameters == ()


def test_schema_and_param_refs_share_chain_rules():
    spec = {"openapi": "3.0.0",
            "paths": {},
            "components": {"schemas": {
                "A": {"type": "object", "properties": {
                    "b": {"$ref": "#/components/schemas/B"}}},
                "B": {"type": "object", "properties": {
                    "a": {"$ref": "#/components/schemas/A"}}},
            }}}
    doc = parse_openapi(json.dumps(spec), _evidence("ev1"), format="json")
    # A/B 互相引用：链式规则下被识别为循环（而非无限递归崩溃）。
    assert any(g.gap_type == "circular_ref" for g in doc.knowledge_gaps)
