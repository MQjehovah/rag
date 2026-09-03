"""Phase 7B：Runtime 与内存编译链测试（Fake LLM / 顺序无关 / 失败不发布）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.wiki_skills.api_reference.blueprint import plan_document
from app.core.wiki_skills.api_reference.compiler import (
    ApiCompileResult,
    ApiSourceDocument,
    compile_api_reference,
    extract_source,
)
from app.core.wiki_skills.api_reference.evidence import build_evidence_ref
from app.core.wiki_skills.api_reference.merge import merge_documents
from app.core.wiki_skills.api_reference.renderer import render_document
from app.core.wiki_skills.api_reference.runtime import ApiReferenceRuntime
from app.core.wiki_skills.api_reference.schemas import ApiDocumentIR
from app.core.wiki_skills.api_reference.validator import validate_compile

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_reference"
V1_SPEC = (FIXTURES / "openapi_users_v1.json").read_text(encoding="utf-8")
V2_SPEC = (FIXTURES / "openapi_users_v2.yaml").read_text(encoding="utf-8")
MD_AUTH = (FIXTURES / "auth_notes.md").read_text(encoding="utf-8")


def _openapi_source(page, spec, scope="v1", prefix="ev-openapi", label=None):
    return ApiSourceDocument(
        source_page_id=page, format="openapi_json", content=spec,
        version_scope=scope, label=label or f"来源-{page}",
        evidence=tuple([{"evidence_id": prefix, "source_page_id": page,
                         "status": "active", "locator": {"section": "all"}}]))


def _md_source(page, content, prefix="ev-md"):
    return ApiSourceDocument(
        source_page_id=page, format="markdown", content=content,
        label=f"Markdown-{page}",
        evidence=tuple([{"evidence_id": prefix, "source_page_id": page,
                         "status": "active",
                         "locator": {"section": "endpoints"}}]))


def _fake_llm_factory(payload: str):
    def runner(prompt: str) -> str:
        return payload
    return runner


# ---------------------------------------------------------------------------
# 多文件 / 混合来源
# ---------------------------------------------------------------------------


def test_multi_openapi_merge_compile():
    src1 = _openapi_source("p1", V1_SPEC, scope="v1", prefix="e1", label="v1 文件")
    src2 = ApiSourceDocument(
        source_page_id="p2", format="openapi_yaml", content=V2_SPEC,
        version_scope="v2", label="v2 文件",
        evidence=tuple([{"evidence_id": "e2", "source_page_id": "p2",
                         "status": "active", "locator": {"section": "all"}}]))
    res = compile_api_reference([src1, src2])
    assert res.validation_report.status == "pass"
    assert res.is_publishable
    assert len(res.ir.endpoints) >= 6  # v1 4 条 + v2 2 条
    # v1/v2 各自独立 Section。
    keys = {s.section_key for s in res.blueprint.sections}
    assert "api_endpoint|get|/users|v1" in keys
    assert "api_endpoint|get|/users|v2" in keys


def test_openapi_plus_markdown_no_llm_does_not_guess_facts():
    md_src = _md_source("p-md", MD_AUTH)
    # 无 llm_runner：Markdown 只保留 hints，不新增 Endpoint 事实。
    res = compile_api_reference([_openapi_source("p1", V1_SPEC), md_src])
    assert res.validation_report.status == "pass"
    assert res.is_publishable
    assert len(res.ir.endpoints) == 4
    # Markdown hint 只是 knowledge_gaps，不产生新的 method/path 事实。
    assert {e.method for e in res.ir.endpoints} == \
        {"GET", "POST", "DELETE"}


def test_markdown_without_llm_only_gaps():
    md_src = _md_source("p-md", MD_AUTH)
    res = compile_api_reference([md_src])
    assert res.ir.endpoints == ()
    assert res.validation_report.status == "fail"
    assert not res.is_publishable
    assert all(g.gap_type.startswith("markdown_")
               for g in res.ir.knowledge_gaps)


def test_fake_llm_valid_extraction():
    md = ("# 接口\n\nGET /items 返回项目。\nHTTP 200 成功。\n"
          "错误码：FORBIDDEN；HTTP 403 表示无权。\n")
    payload = json.dumps([
        {"method": "GET", "path": "/items", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["200", "403"],
         "error_codes": ["FORBIDDEN"]},
    ])
    src = _md_source("p-md", md)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.validation_report.status == "pass"
    assert res.is_publishable
    assert [(e.method, e.path, e.version_scope)
            for e in res.ir.endpoints] == [("GET", "/items", "unversioned")]
    assert res.model_usage["llm_calls"] == 1


def test_fake_llm_unknown_evidence_rejected():
    md = "# 接口\n\nGET /items 返回项目。\nHTTP 200 成功。\n"
    payload = json.dumps([
        {"method": "GET", "path": "/items", "evidence_id": "ev-unknown",
         "version_scope": "unversioned", "status_codes": ["200"],
         "error_codes": []},
    ])
    src = _md_source("p-md", md)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir.endpoints == ()
    assert any(g.gap_type == "ambiguous_input" for g in res.ir.knowledge_gaps)


def test_fake_llm_fabricated_method_status_rejected():
    md = "# 接口\n\nGET /items 返回项目。\nHTTP 200 成功。\n"
    payload = json.dumps([
        {"method": "DELETE", "path": "/admin", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["999"]},
    ])
    src = _md_source("p-md", md)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir.endpoints == ()


def test_fake_llm_extra_field_and_bad_types_rejected():
    md = "# 接口\n\nGET /items 返回项目。\nHTTP 200 成功。\n"
    payload = json.dumps([
        {"method": "GET", "path": "/items", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["200"],
         "fabricated_field": True},
        {"method": 42, "path": "/items", "evidence_id": "ev-md"},
    ])
    src = _md_source("p-md", md)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir.endpoints == ()
    assert any(g.gap_type == "ambiguous_input"
               for g in res.ir.knowledge_gaps)


def test_single_markdown_failure_does_not_lose_openapi():
    # 一个损坏的 OpenAPI JSON 来源不影响其它文件。
    broken = ApiSourceDocument(source_page_id="p-bad", format="openapi_json",
                               content="{not json", label="坏文件",
                               evidence=())
    src = _openapi_source("p1", V1_SPEC, prefix="e1")
    res = compile_api_reference([broken, src])
    assert len(res.ir.endpoints) == 4
    assert res.diagnostics == ("SOURCE_EXTRACTION_FAILED",)


# ---------------------------------------------------------------------------
# 顺序无关 / 失败不发布
# ---------------------------------------------------------------------------


def test_compile_input_order_reversal_identical():
    src1 = _openapi_source("p1", V1_SPEC, scope="v1", prefix="e1", label="A")
    src2 = _openapi_source("p2", V1_SPEC, scope="v2", prefix="e2", label="B")
    md_src = _md_source("p3", MD_AUTH, prefix="e3")
    a = compile_api_reference([src1, src2, md_src])
    b = compile_api_reference([md_src, src2, src1])
    assert a.to_dict() == b.to_dict()


def test_compile_empty_inputs_not_publishable():
    res = compile_api_reference([])
    assert res.validation_report.status == "fail"
    assert not res.is_publishable
    assert "NO_FACTUAL_CONTENT" in [i.code for i in res.validation_report.issues]


def test_compile_failure_produces_no_publishable_result():
    md_src = _md_source("p-md", "只有自然语言叙述，没有明确接口。")
    res = compile_api_reference([md_src])
    assert res.validation_report.status == "fail"
    assert not res.is_publishable


def test_model_usage_counts_llm_calls():
    md1 = "# 一\n\nGET /a 返回。\nHTTP 200 成功。\n"
    md2 = "# 二\n\nPOST /b 创建。\nHTTP 201 成功。\n"
    payload1 = json.dumps([{"method": "GET", "path": "/a",
                            "evidence_id": "e1", "version_scope": "unversioned",
                            "status_codes": ["200"]}])
    payload2 = json.dumps([{"method": "POST", "path": "/b",
                            "evidence_id": "e2", "version_scope": "unversioned",
                            "status_codes": ["201"]}])
    s1 = ApiSourceDocument(source_page_id="m1", format="markdown", content=md1,
                           evidence=tuple([{"evidence_id": "e1",
                                            "source_page_id": "m1",
                                            "status": "active"}]))
    s2 = ApiSourceDocument(source_page_id="m2", format="markdown", content=md2,
                           evidence=tuple([{"evidence_id": "e2",
                                            "source_page_id": "m2",
                                            "status": "active"}]))

    class CountingRunner:
        def __init__(self, payloads):
            self.payloads = payloads
            self.calls = 0

        def __call__(self, prompt):
            self.calls += 1
            return self.payloads[min(self.calls - 1, len(self.payloads) - 1)]

    runner = CountingRunner([payload1, payload2])
    res = compile_api_reference([s1, s2], llm_runner=runner)
    assert res.model_usage["llm_calls"] == 2
    assert runner.calls == 2
    assert res.model_usage["estimated_input_tokens"] > 0


# ---------------------------------------------------------------------------
# Runtime 四方法与纯函数一致
# ---------------------------------------------------------------------------


def _ir_from_v1():
    src = _openapi_source("p1", V1_SPEC, prefix="ev1")
    ir = extract_source(src)[0]
    return ir


def test_runtime_methods_match_pure_functions():
    rt = ApiReferenceRuntime()
    ir = _ir_from_v1()
    assert rt.key == "api_reference"
    assert rt.version == "1"

    ctx = None
    assert rt.plan(ctx, ir).to_dict() == plan_document(ir).to_dict()
    bp = plan_document(ir)
    assert rt.render(ctx, ir, bp) == render_document(ir, bp)
    sections = render_document(ir, bp)
    report = rt.validate(ctx, ir, bp, sections, ["ev1"])
    assert report.status == "pass"


def test_runtime_extract_matches_extract_source():
    rt = ApiReferenceRuntime()
    src = _openapi_source("p1", V1_SPEC, prefix="ev1")
    direct = extract_source(src)[0]
    via_runtime = rt.extract(None, source=src)
    assert via_runtime is not None
    assert via_runtime.to_dict() == direct.to_dict()


def test_compile_result_roundtrip():
    src = _openapi_source("p1", V1_SPEC, prefix="ev1")
    res = compile_api_reference([src])
    restored = ApiCompileResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert restored.to_dict() == res.to_dict()
    assert restored.ir == res.ir or restored.ir.to_dict() == res.ir.to_dict()


# ---------------------------------------------------------------------------
# Phase 7B.1 反例：发布语义 / LLM 原子校验 / 错误码核验
# ---------------------------------------------------------------------------


def _md_ev_source(page="p-md", prefix="ev-md"):
    return _md_source(page, "", prefix=prefix)


def test_valid_fake_llm_result_is_publishable():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n错误码：MISSING；HTTP 404 不存在。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["200"],
                           "error_codes": ["MISSING"]}])
    src = _md_ev_source()
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=src.evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.validation_report.status == "pass"
    assert res.is_publishable is True


def test_successful_llm_summary_is_non_blocking():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["200"],
                           "error_codes": []}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.notes and any("LLM_EXTRACTED_CANDIDATES:1" in n for n in res.notes)
    assert res.diagnostics == ()
    assert res.validation_report.status == "pass"
    assert res.is_publishable is True


def test_missing_version_scope_rejects_whole_candidate():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "status_codes": ["200"],
                           "error_codes": []}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir.endpoints == ()
    assert any("field set mismatch" in g.description or
               "version_scope" in g.description
               for g in res.ir.knowledge_gaps)


def test_status_codes_int_rejects_candidate_without_crashing_source():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": [200],
                           "error_codes": []}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir is not None  # Source 未因 TypeError 返回 None
    assert res.ir.endpoints == ()
    assert res.model_usage["llm_calls"] == 1


def test_mixed_valid_invalid_candidates_preserve_valid():
    md = "# 接口\n\nGET /a 返回。\nPOST /b 创建。\nHTTP 200 成功。\nHTTP 201 成功。\n"
    payload = json.dumps([
        {"method": "GET", "path": "/a", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["200"],
         "error_codes": []},
        {"method": "POST", "path": "/b", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": [201],
         "error_codes": []},
    ])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert [e.path for e in res.ir.endpoints] == ["/a"]


def test_symbolic_error_code_in_text_accepted():
    md = "# 接口\n\nGET /a 返回。\nHTTP 404 不存在。\n错误码：USER_NOT_FOUND（HTTP 404）。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["404"],
                           "error_codes": ["USER_NOT_FOUND"]}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    ep = res.ir.endpoints[0]
    codes = {e.code for e in ep.error_codes}
    assert "USER_NOT_FOUND" in codes
    mapping = {e.code: e.http_status for e in ep.error_codes}
    assert mapping["USER_NOT_FOUND"] == "404"


def test_fabricated_symbolic_error_code_rejected():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["200"],
                           "error_codes": ["MADE_UP_ERROR"]}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    assert res.ir.endpoints == ()


def test_error_code_without_explicit_http_does_not_guess():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n错误码：GONE。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["200"],
                           "error_codes": ["GONE"]}])
    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content=md, label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=_fake_llm_factory(payload))
    ep = res.ir.endpoints[0]
    code = next(e for e in ep.error_codes if e.code == "GONE")
    assert code.http_status == ""


def test_raw_exception_path_token_does_not_enter_result():
    secret = "SECRET_ABC123"
    path_leak = "C:\\Users\\topsecret\\notes.md"
    marker = "TOKEN_XYZ_LEAK"

    def bad_runner(prompt):
        raise RuntimeError(f"{secret} crashed at {path_leak} {marker}")

    src = ApiSourceDocument(source_page_id="p-md", format="markdown",
                            content="只有叙述，无接口。", label="md",
                            evidence=_md_ev_source().evidence)
    res = compile_api_reference([src], llm_runner=bad_runner)
    blob = json.dumps(res.to_dict(), ensure_ascii=False)
    assert secret not in blob
    assert path_leak not in blob
    assert marker not in blob
    # 只出现固定安全 code。
    assert any("LLM_RUNNER_FAILED" == g.description or
               "LLM_RUNNER_FAILED" in g.description
               for g in res.ir.knowledge_gaps)


# ---------------------------------------------------------------------------
# Phase 7B.1 反例：DTO 边界
# ---------------------------------------------------------------------------


def test_source_evidence_deeply_immutable():
    record = {"evidence_id": "e1", "source_page_id": "p9",
              "status": "active",
              "locator": {"section": "endpoints",
                          "nested": {"k": ["a", "b"]}}}
    src = ApiSourceDocument(source_page_id="p9", format="markdown",
                            content="x", evidence=(record,))
    # 修改原始 dict 不影响对象。
    record["locator"]["section"] = "mutated"
    record["locator"]["nested"]["k"].append("c")
    record["evidence_id"] = "mutated-id"
    frozen = src.evidence[0]
    assert frozen["evidence_id"] == "e1"
    assert frozen["locator"]["section"] == "endpoints"
    assert list(frozen["locator"]["nested"]["k"]) == ["a", "b"]
    # 内部引用不可变：无法赋值 / append。
    with pytest.raises(TypeError):
        frozen["locator"]["section"] = "x"
    with pytest.raises(TypeError):
        frozen["locator"]["nested"]["k"][0] = "z"
    # to_dict 返回普通 JSON-safe 副本且可 roundtrip。
    d = src.to_dict()
    assert isinstance(d["evidence"][0], dict)
    assert json.dumps(d, ensure_ascii=False)
    restored = ApiSourceDocument.from_dict(json.loads(json.dumps(d)))
    assert restored.to_dict() == d


def test_source_evidence_page_mismatch_rejected():
    with pytest.raises(ValueError):
        ApiSourceDocument(
            source_page_id="p1", format="markdown", content="x",
            evidence=({"evidence_id": "e1", "source_page_id": "p2",
                       "status": "active"},))
    data = {"source_page_id": "p1", "format": "markdown", "content": "x",
            "evidence": [{"evidence_id": "e1", "source_page_id": "OTHER",
                          "status": "active"}]}
    with pytest.raises(ValueError):
        ApiSourceDocument.from_dict(data)
    # 缺 source_page_id 同样拒绝。
    with pytest.raises(ValueError):
        ApiSourceDocument(
            source_page_id="p1", format="markdown", content="x",
            evidence=({"evidence_id": "e1", "status": "active"},))


def test_compile_result_rejects_invalid_sections_diagnostics_and_usage():
    with pytest.raises(ValueError):
        ApiCompileResult(sections=({"not": "section"},))
    with pytest.raises(ValueError):
        ApiCompileResult(diagnostics=(1,))
    with pytest.raises(ValueError):
        ApiCompileResult(model_usage={"llm_calls": 1})
    with pytest.raises(ValueError):
        ApiCompileResult(model_usage={"llm_calls": 0, "llm_calls_extra": 1,
                                      "estimated_input_tokens": 0,
                                      "estimated_output_tokens": 0})


def test_compile_result_model_usage_bool_rejected():
    with pytest.raises(ValueError):
        ApiCompileResult(model_usage={
            "llm_calls": True, "estimated_input_tokens": 0,
            "estimated_output_tokens": 0})


def test_compile_result_model_usage_deep_immutable():
    res = ApiCompileResult()
    with pytest.raises(TypeError):
        res.model_usage["llm_calls"] = 1


# ---------------------------------------------------------------------------
# Phase 7B.2 反例：Source 级硬失败阻断发布 / notes 固定摘要
# ---------------------------------------------------------------------------


def test_valid_openapi_with_invalid_markdown_json_not_publishable():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n"
    src_openapi = _openapi_source("p1", V1_SPEC, prefix="e1")
    src_md = ApiSourceDocument(source_page_id="p-md", format="markdown",
                               content=md, label="md",
                               evidence=_md_ev_source().evidence)
    res = compile_api_reference([src_openapi, src_md],
                                llm_runner=lambda prompt: "{bad json")
    # 有效 OpenAPI IR 保留。
    assert len(res.ir.endpoints) == 4
    assert res.validation_report.status == "pass"
    # 但 Source 级 LLM 硬失败阻断发布。
    assert res.is_publishable is False
    assert res.diagnostics == ("LLM_OUTPUT_INVALID",)


def test_valid_openapi_with_llm_runner_exception_not_publishable():
    src_openapi = _openapi_source("p1", V1_SPEC, prefix="e1")
    src_md = ApiSourceDocument(source_page_id="p-md", format="markdown",
                               content="只有叙述。", label="md",
                               evidence=_md_ev_source().evidence)

    def boom(prompt):
        raise RuntimeError("boom secret")

    res = compile_api_reference([src_openapi, src_md], llm_runner=boom)
    assert len(res.ir.endpoints) == 4
    assert res.is_publishable is False
    assert res.diagnostics == ("LLM_RUNNER_FAILED",)


def test_successful_markdown_without_label_serialization_has_no_page_id():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n错误码：GONE。\n"
    payload = json.dumps([{"method": "GET", "path": "/a",
                           "evidence_id": "ev-md",
                           "version_scope": "unversioned",
                           "status_codes": ["200"],
                           "error_codes": ["GONE"]}])
    # 不提供 label（evidence source_page_id 必须与资料一致）。
    src_md = ApiSourceDocument(
        source_page_id="p-page-xyz", format="markdown", content=md,
        evidence=tuple([{"evidence_id": "ev-md", "source_page_id": "p-page-xyz",
                         "status": "active",
                         "locator": {"section": "endpoints"}}]))
    res = compile_api_reference([src_md],
                                llm_runner=_fake_llm_factory(payload))
    assert res.validation_report.status == "pass"
    assert res.is_publishable is True
    blob = json.dumps(res.to_dict(), ensure_ascii=False)
    assert "p-page-xyz" not in blob


def test_candidate_invalid_plus_valid_preserves_valid_without_source_diag():
    md = "# 接口\n\nGET /a 返回。\nHTTP 200 成功。\n错误码：GONE。\n"
    payload = json.dumps([
        {"method": "GET", "path": "/a", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["200"],
         "error_codes": ["GONE"]},
        {"method": "GET", "path": "/fabricated", "evidence_id": "ev-md",
         "version_scope": "unversioned", "status_codes": ["200"],
         "error_codes": ["MADE_UP"]},
    ])
    src_md = ApiSourceDocument(source_page_id="p-md", format="markdown",
                               content=md, label="md",
                               evidence=_md_ev_source().evidence)
    res = compile_api_reference([src_md],
                                llm_runner=_fake_llm_factory(payload))
    # 合法候选保留；非法候选只作为 knowledge gap。
    assert [e.path for e in res.ir.endpoints] == ["/a"]
    assert res.diagnostics == ()
    assert res.validation_report.status == "pass"
    assert res.is_publishable is True
    assert any(g.description.startswith("LLM candidate[1]")
               for g in res.ir.knowledge_gaps)
