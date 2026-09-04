# -*- coding: utf-8 -*-
"""Phase 9B 第二批 Agent A：OpenAPI 顶层 `x-wiki-version-scope` 版本接线。

契约 phase9b/CONTRACT-version-config.md §1/§2 验收（真实 Pipeline 方式：
alembic head 临时库 + v3 executor + 真实发布 + DB 级断言，不允许只 HTTP 200）。

覆盖（按 §2 八条）：
1. 同 method/path 双版本隔离：p-api 声明 v1、p-hidden 声明 v2，同一路径
   `/shared`（同 method/path 不同声明 scope 各自成节）。断言两套 Section key
   （`…|v1` / `…|v2`）、内容与 Evidence Binding 各自正确隔离；v1 页 Evidence
   不支撑 v2 section，反之亦然。
2. 修改 v1 参数/摘要后重编：v2 语义与字段/证据不串改（content/structure/
   binding 全等），v1 变化；旧 current Revision 原样保留。
3. 无声明 → unversioned（真实链断言 `…|unversioned`，heading 含 unversioned）。
4. 非法声明（null/非字符串/空白，JSON+YAML）：parser/compile 受控失败
   （固定 issue code）；真实链上「p-hidden 非法 + p-api 合法」也零发布：旧
   current_revision 不变、无新发布、dirty 保持、无 Manifest。
5. 内部 version_scope 参数 × 文档声明：空参数用声明；相同（规范化后）接受；
   不同受控失败（VERSION_SCOPE_CONFLICT）。
6. 大小写/空白规范化沿用 identity.normalize_version_scope（` V1 `→`v1`）。
7. 只改版本声明（Page 内容变化）但在发布窗口不同步 Evidence hash → 过不了
   `_reverify_api_publish`：run failed（EVIDENCE_STALE）、零新 Revision、dirty
   保持、无 Manifest（EVIDENCE_STALE/PAGE_STALE 或等价，零发布）。
8. 说明：本文件双版本隔离用的是**同一路径 `/shared`**（声明驱动），`/v1`、
   `/v2` 不同路径不算版本隔离证据（§2.8）。

== 版本→Section/Evidence 真实链路（实现后的接线）==
- db_adapter 仍恒置 `ApiSourceDocument.version_scope=""`（不改）；Page 全文
  经 compile→parser.parse_openapi 在**顶层解析后一次读取**
  `x-wiki-version-scope`，经 identity.normalize_version_scope 规范化，作用于该
  文档全部 Endpoint → section_key/endpoint_id 含该 scope，merge 按 endpoint_id
  隔离，Evidence Binding 各自沿用本来源证据（v1 资料不支撑 v2 section）。
- 文档声明变更 = 来源内容变化：Page.content_hash / Evidence source_doc_hash
  不同步时，发布前 `_reverify_api_publish`（wiki_skilled_default_v3）拦截 →
  EVIDENCE_STALE/PAGE_STALE，零发布。

门禁：cd backend；
python -m pytest tests/test_phase9b_version_scope.py \
  tests/test_api_reference_parser.py tests/test_api_reference_identity.py \
  tests/test_api_reference_db_adapter.py tests/test_api_reference_merge.py \
  tests/test_api_reference_runtime.py tests/test_api_reference_schemas.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod  # noqa: E402
from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_skills.api_reference.compiler import (  # noqa: E402
    ApiSourceDocument,
    compile_api_reference,
    extract_source,
)
from app.core.wiki_skills.api_reference.parser import (  # noqa: E402
    ApiVersionScopeError,
    VERSION_SCOPE_FIELD,
    VERSION_SCOPE_ISSUE_CONFLICT,
    VERSION_SCOPE_ISSUE_INVALID,
    parse_openapi,
)
from app.core.wiki_skills.api_reference.identity import (  # noqa: E402
    build_endpoint_section_key,
)
from app.core.wiki_pipeline import executor  # noqa: E402
from app.core.wiki_pipeline import registry as pregs  # noqa: E402
from app.core.wiki_pipeline import worker as wiki_worker  # noqa: E402
from app.core.wiki_pipeline.pipelines import wiki_default as wd  # noqa: E402
from app.core.wiki_pipeline.pipelines.wiki_default import (  # noqa: E402
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    register_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (  # noqa: E402
    register_default_pipeline_v2,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (  # noqa: E402
    register_default_pipeline_v3,
)
from app.core.wiki_skills import registry as sreg  # noqa: E402
from app.core.wiki_skills import service as skill_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    EvidenceItem,
    KnowledgeCompileArtifact as CompileArtifact,
    KnowledgeCompileRun as CompileRun,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    init_db,
)
from phase9a import bootstrap_db as bdb  # noqa: E402
from phase9a import fixtures  # noqa: E402

_P44_REVISION = "a9b8c7d6e5f4"

WIKI_API = fixtures.WIKI_API_ID            # w-eng-api（api_reference）
PAGE_API = fixtures.PAGE_API_ID            # p-api
PAGE_HIDDEN = fixtures.PAGE_HIDDEN_ID      # p-hidden
EV_API = fixtures.EVIDENCE_API_ID          # ev-api
EV_HIDDEN = fixtures.EVIDENCE_HIDDEN_ID    # ev-hidden

_TIMEOUT = 120.0

# 版本化共享接口（同一 method/path，两份文档声明不同 scope）的稳定 section key。
_K_SHARED_V1 = "api_endpoint|get|/shared|v1"
_K_SHARED_V2 = "api_endpoint|get|/shared|v2"


# ---------------------------------------------------------------------------
# 确定性 OpenAPI 文档构造（同一 /shared 路径；声明驱动版本）
# ---------------------------------------------------------------------------

_OVERVIEW = (
    "Phase9B 共享接口。上位机系统承载该接口，支持连接调试器完成版本化调用。"
    "返回 200 成功，401 认证失败，404 不存在。"
)


def _responses() -> dict:
    return {
        "200": {
            "description": "成功",
            "content": {
                "application/json": {"schema": {"type": "object"}},
            },
        },
        "401": {"description": "未认证"},
        "404": {"description": "不存在"},
    }


def shared_doc(scope_value) -> dict:
    """构造 /shared 单端点 OpenAPI 文档；scope_value 即 x-wiki-version-scope 值。

    允许传入 None（模拟 null 声明）；非字符串/number 等由调用方直接改 dict。
    """
    return {
        "openapi": "3.0.1",
        VERSION_SCOPE_FIELD: scope_value,
        "info": {"title": "共享接口文档", "version": "1.0",
                 "description": _OVERVIEW},
        "paths": {"/shared": {"get": {
            "summary": "查询共享资源（v0）",
            "description": "查询共享资源。",
            "parameters": [
                {"name": "limit", "in": "query", "required": False,
                 "description": "每页条数", "schema": {"type": "integer",
                                                      "default": 10}},
            ],
            "responses": _responses(),
        }}},
    }


def shared_doc_v1(*, enhanced: bool = False) -> dict:
    """v1 资料：/shared + limit/offset；enhanced 追加 sort 并改摘要（重编场景）。"""
    spec = shared_doc("v1")
    op = spec["paths"]["/shared"]["get"]
    op["summary"] = "查询共享资源（v1 增强版）" if enhanced \
        else "查询共享资源（v1）"
    op["description"] = ("v1 查询共享资源，分页返回。" if not enhanced
                         else "v1 查询共享资源，分页返回，支持排序。")
    params = [
        {"name": "limit", "in": "query", "required": False,
         "description": "每页条数", "schema": {"type": "integer",
                                              "default": 10}},
        {"name": "offset", "in": "query", "required": False,
         "description": "偏移量", "schema": {"type": "integer",
                                            "default": 0}},
    ]
    if enhanced:
        params.append({"name": "sort", "in": "query", "required": False,
                       "description": "排序字段",
                       "schema": {"type": "string"}})
    op["parameters"] = params
    return spec


def shared_doc_v2() -> dict:
    """v2 资料：/shared + limit（语义/字段与 v1 不同，但路径与 method 相同）。"""
    spec = shared_doc("v2")
    op = spec["paths"]["/shared"]["get"]
    op["summary"] = "查询共享资源（v2）"
    op["description"] = "v2 查询共享资源，limit 分页。"
    return spec


def json_text(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False)


def yaml_text(spec: dict) -> str:
    import yaml

    return yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)


def _evidence_record(eid: str, page: str, section: str = "all") -> dict:
    return {"evidence_id": eid, "source_page_id": page, "status": "active",
            "locator": {"section": section}}


# ---------------------------------------------------------------------------
# §1 版本契约的 parser / compile 级测试（缺省兼容 + 非法 + 边界）
# ---------------------------------------------------------------------------


def _spec_no_scope() -> dict:
    spec = shared_doc("v2")  # 覆盖 v2 后删除扩展键 → 无声明
    spec.pop(VERSION_SCOPE_FIELD, None)
    return spec


def _parser(scope_value="v2", *, param=None, extra_paths: tuple = ()):
    spec = shared_doc(scope_value)
    for extra in extra_paths:
        spec["paths"][extra] = {"get": {"summary": "s",
                                        "responses": {"200": {"description": "ok"}}}}
    return spec


def test_parser_missing_or_blank_param_and_no_declaration_is_unversioned():
    for param in (None, ""):
        doc = parse_openapi(
            json_text(_spec_no_scope()), [_evidence_record("e1", "p")],
            version_scope=param, format="json")
        assert doc.endpoints
        assert {e.version_scope for e in doc.endpoints} == {"unversioned"}


def test_parser_declared_scope_applies_to_all_endpoints_once():
    spec = _parser("v2", extra_paths=("/other",))
    doc = parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                        format="json")
    assert {e.version_scope for e in doc.endpoints} == {"v2"}
    assert all(" [v2]" in e.endpoint_id for e in doc.endpoints)
    assert all(build_endpoint_section_key(e.method, e.path, e.version_scope)
               .endswith("|v2") for e in doc.endpoints)


def test_parser_empty_param_uses_document_declaration():
    spec = _parser("v3")
    for param in (None, "", "   "):
        doc = parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                            version_scope=param, format="json")
        assert {e.version_scope for e in doc.endpoints} == {"v3"}


def test_parser_param_and_declaration_same_normalized_accepted():
    # 文档声明 " V1 " 与内部参数 "v1"：规范化后相同 → 接受。
    spec = shared_doc(" V1 ")
    doc = parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                        version_scope="v1", format="json")
    assert {e.version_scope for e in doc.endpoints} == {"v1"}


def test_parser_param_and_declaration_conflict_fails_closed():
    spec = shared_doc("v1")
    with pytest.raises(ApiVersionScopeError) as exc:
        parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                      version_scope="v2", format="json")
    assert exc.value.issue_code == VERSION_SCOPE_ISSUE_CONFLICT
    # 即使内容/证据都合法，也不产出任何 IR（来源级阻断）。
    with pytest.raises(ApiVersionScopeError):
        parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                      version_scope="V2", format="json")


def test_parser_declared_scope_case_whitespace_normalized_like_identity():
    for raw, want in ((" V1 ", "v1"), ("v2", "v2"), (" V3 ", "v3")):
        spec = shared_doc(raw)
        doc = parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                            format="json")
        assert {e.version_scope for e in doc.endpoints} == {want}


@pytest.mark.parametrize("bad_value", [None, 42, True, [], {}, "   ", "",
                                       "v1 beta", "a" * 65])
def test_parser_invalid_json_declaration_raises(bad_value):
    spec = shared_doc("v1")
    spec[VERSION_SCOPE_FIELD] = bad_value
    with pytest.raises(ApiVersionScopeError) as exc:
        parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                      format="json")
    assert exc.value.issue_code == VERSION_SCOPE_ISSUE_INVALID


@pytest.mark.parametrize("bad_value", [None, 42, [], "   ", "", "v1 beta",
                                       "a" * 65])
def test_parser_invalid_yaml_declaration_raises(bad_value):
    spec = shared_doc("v1")
    spec[VERSION_SCOPE_FIELD] = bad_value
    with pytest.raises(ApiVersionScopeError) as exc:
        parse_openapi(yaml_text(spec), [_evidence_record("e1", "p")],
                      format="yaml")
    assert exc.value.issue_code == VERSION_SCOPE_ISSUE_INVALID


def test_parser_does_not_infer_version_from_paths_title_or_info_version():
    # 路径 /v1/users、info.version 等一律不推断；无扩展字段 → unversioned。
    spec = {
        "openapi": "3.0.1",
        "info": {"title": "v2 API", "version": "2.0",
                 "description": _OVERVIEW},
        "paths": {"/v1/users": {"get": {
            "summary": "s", "responses": {"200": {"description": "ok"}}}}},
    }
    doc = parse_openapi(json_text(spec), [_evidence_record("e1", "p")],
                        format="json")
    ep = doc.endpoints[0]
    assert ep.path == "/v1/users"
    assert ep.version_scope == "unversioned"


# ---------------------------------------------------------------------------
# §1.2 compile 级（ApiSourceDocument 内部参数 × 文档声明）
# ---------------------------------------------------------------------------


def _api_source(page, content, scope="", *, fmt="openapi_json", eid="e1"):
    return ApiSourceDocument(
        source_page_id=page, format=fmt, content=content,
        version_scope=scope, label=f"来源-{page}",
        evidence=(_evidence_record(eid, page),))


def test_compile_empty_param_uses_document_declaration():
    src = _api_source("p1", json_text(shared_doc("v2")), scope="")
    res = compile_api_reference([src])
    assert res.is_publishable, res.to_dict()
    assert {e.version_scope for e in res.ir.endpoints} == {"v2"}
    assert "api_endpoint|get|/shared|v2" in res.blueprint.section_keys()


def test_compile_yaml_declared_scope_roundtrip_passes():
    src = _api_source("p1", yaml_text(shared_doc("v3")), scope="",
                      fmt="openapi_yaml")
    res = compile_api_reference([src])
    assert res.is_publishable, res.to_dict()
    assert {e.version_scope for e in res.ir.endpoints} == {"v3"}


def test_compile_param_and_declaration_same_normalized_accepted():
    src = _api_source("p1", json_text(shared_doc(" V2 ")), scope="v2")
    res = compile_api_reference([src])
    assert res.is_publishable, res.to_dict()
    assert {e.version_scope for e in res.ir.endpoints} == {"v2"}


def test_compile_param_and_declaration_conflict_blocked():
    src = _api_source("p1", json_text(shared_doc("v1")), scope="v2")
    res = compile_api_reference([src])
    assert not res.is_publishable
    assert res.diagnostics == (VERSION_SCOPE_ISSUE_CONFLICT,)
    assert res.ir.endpoints == ()


def test_compile_same_path_dual_declared_scope_evidence_isolated():
    """同 method/path、不同声明 scope → 各自成节、Evidence 各用各的资料。"""
    src1 = _api_source("p-api", json_text(shared_doc_v1()), scope="",
                       eid="ev-api")
    src2 = _api_source("p-hidden", json_text(shared_doc_v2()), scope="",
                       eid="ev-hidden")
    res = compile_api_reference([src1, src2])
    assert res.is_publishable, res.to_dict()
    keys = res.blueprint.section_keys()
    assert _K_SHARED_V1 in keys and _K_SHARED_V2 in keys
    eps = {e.version_scope: e for e in res.ir.endpoints}
    assert set(eps) == {"v1", "v2"}
    b_v1 = {b.field_path: b.evidence_ids
            for b in eps["v1"].evidence_bindings}
    b_v2 = {b.field_path: b.evidence_ids
            for b in eps["v2"].evidence_bindings}
    assert b_v1["version_scope"] == ("ev-api",)
    assert b_v2["version_scope"] == ("ev-hidden",)
    # v1 事实全部由 ev-api 支撑；v2 全部由 ev-hidden 支撑；不串借。
    assert all(ids == ("ev-api",) for ids in b_v1.values())
    assert all(ids == ("ev-hidden",) for ids in b_v2.values())


@pytest.mark.parametrize("fmt,make", [
    ("json", json_text),
    ("yaml", yaml_text),
])
def test_compile_invalid_declaration_blocks_even_other_source_valid(fmt, make):
    """坏来源不可被静默丢弃后照常发布：即使另一来源合法，整次编译不可发布。"""
    good = _api_source("p-good", json_text(shared_doc("v1")), eid="ev-good")
    spec = shared_doc("v2")
    spec[VERSION_SCOPE_FIELD] = None  # null 声明
    bad = _api_source("p-bad", make(spec),
                      fmt="openapi_yaml" if fmt == "yaml" else "openapi_json",
                      eid="ev-bad")
    res = compile_api_reference([good, bad])
    assert res.ir.endpoints  # 合法来源的 IR 仍在
    assert not res.is_publishable
    assert res.diagnostics == (VERSION_SCOPE_ISSUE_INVALID,)


def test_extract_source_invalid_declaration_returns_issue_not_crash():
    spec = shared_doc("v1")
    spec[VERSION_SCOPE_FIELD] = "   "
    src = _api_source("p1", json_text(spec))
    ir, diags = extract_source(src)
    assert ir is None
    assert diags == (VERSION_SCOPE_ISSUE_INVALID,)


# ---------------------------------------------------------------------------
# 真实 Pipeline（alembic head 临时库 + v3 executor）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_phase9b_registries():
    wiki_worker.stop_worker()
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()
    app.dependency_overrides.clear()
    yield
    wiki_worker.stop_worker()
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()
    app.dependency_overrides.clear()


def _wait_for(fn, description: str, timeout: float = _TIMEOUT,
              interval: float = 0.2):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"等待超时: {description} (last={last!r})")


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


class Env:
    """单测试隔离环境（alembic head 临时库 + phase9a seed + 登录）。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = Path(tmp_path)
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "phase9b_vs.db"
        self.db_url = _sqlite_url(self.db_path)
        self.record_file = self.tmp / "records" / "calls.jsonl"
        self.fault_flag = self.tmp / "records" / "fault.flag"
        self._monkeypatch = monkeypatch

        envmap = dict(os.environ)
        envmap["DATABASE_URL"] = self.db_url
        envmap["PYTHONIOENCODING"] = "utf-8"
        heads = bdb.alembic_single_head(bdb._BACKEND, envmap)
        assert [h for h in heads.split("|") if h] == [_P44_REVISION], heads
        bdb.alembic_upgrade_head(bdb._BACKEND, envmap)

        self.engine = create_engine(
            self.db_url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA busy_timeout=8000")

        init_db(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        monkeypatch.setattr(settings, "database_url", self.db_url)
        monkeypatch.setattr(settings, "wiki_pipeline_active_version", "3")
        monkeypatch.setattr(settings, "wiki_topic_enabled", True)
        monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
        monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
        monkeypatch.setattr(deps, "_engine", self.engine)

        skill_service.register_builtin_skills()
        register_default_pipeline()
        register_default_pipeline_v2()
        register_default_pipeline_v3()
        pregs.set_active_version("wiki.default", "3")

        self.db = self.Session()
        fixtures.apply_seed(self.db)
        self.db.commit()

        self.recorder = fixtures.Recorder(self.record_file, self.fault_flag,
                                          block_timeout=60.0)
        executor.configure_external_runners(
            llm_runner=self.recorder.llm_runner(),
            graph_runner=self.recorder.graph_runner(wd._default_graph_runner),
        )

        self.c = TestClient(app)
        self.admin = self._login(fixtures.ADMIN_USERNAME)

    def _login(self, username: str) -> dict:
        r = self.c.post("/api/auth/login", json={
            "username": username, "password": fixtures.PHASE9A_PASSWORD})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        assert token
        return {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        app.dependency_overrides.clear()
        try:
            self.db.close()
        finally:
            self.engine.dispose()

    # ---------- 查询 helper ----------
    def run_for_wiki(self, wid: str) -> CompileRun:
        self.db.expire_all()
        run = (self.db.query(CompileRun)
               .filter(CompileRun.wiki_page_id == wid)
               .order_by(CompileRun.created_at.desc(), CompileRun.id.desc())
               .first())
        assert run is not None, f"no run for wiki {wid}"
        return run

    def drive_run(self, run_id: str) -> CompileRun:
        self.db.expire_all()
        executor.execute_run(self.db, run_id)
        self.db.expire_all()
        run = self.db.get(CompileRun, run_id)
        assert run is not None
        return run

    def wiki(self, wid: str) -> WikiPage:
        self.db.expire_all()
        return self.db.get(WikiPage, wid)

    def revisions(self, wid: str) -> list[WikiRevision]:
        self.db.expire_all()
        return (self.db.query(WikiRevision)
                .filter(WikiRevision.wiki_page_id == wid)
                .order_by(WikiRevision.created_at, WikiRevision.id).all())

    def current_sections(self, wid: str) -> list[WikiSection]:
        w = self.wiki(wid)
        assert w and w.current_revision_id
        return self.sections_of(w.current_revision_id)

    def sections_of(self, revision_id: str) -> list[WikiSection]:
        self.db.expire_all()
        return (self.db.query(WikiSection)
                .filter(WikiSection.revision_id == revision_id)
                .order_by(WikiSection.order_index, WikiSection.id).all())

    def sections_by_key(self, revision_id: str) -> dict[str, WikiSection]:
        out: dict[str, WikiSection] = {}
        for s in self.sections_of(revision_id):
            if s.section_key:
                out[s.section_key] = s
        return out

    def binding_key_set(self, section_id: str) -> set[tuple[str, str, str]]:
        rows = (self.db.query(WikiSectionEvidenceBinding)
                .filter(WikiSectionEvidenceBinding.section_id == section_id)
                .all())
        return {(b.field_path, b.usage_type, b.evidence_id) for b in rows}

    def manifest_count(self, run_id: str) -> int:
        return (self.db.query(CompileArtifact)
                .filter(CompileArtifact.run_id == run_id,
                        CompileArtifact.artifact_type
                        == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST)
                .count())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


# ---------------------------------------------------------------------------
# 触发与数据变更 helper（真实入口）
# ---------------------------------------------------------------------------


def only_dirty(env: Env, keep_ids) -> None:
    keep = set(keep_ids)
    for row in env.db.query(WikiPage).all():
        row.dirty = row.id in keep
    env.db.commit()


def refresh_and_drive(env: Env, wid: str) -> CompileRun:
    r = env.c.post("/api/wiki/refresh-page-dirty", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    run = env.run_for_wiki(wid)
    return env.drive_run(run.id)


def assert_run_succeeded(env: Env, wid: str) -> CompileRun:
    run = refresh_and_drive(env, wid)
    assert run.status == "succeeded", (run.safe_error_code,
                                       run.safe_error_message)
    w = env.wiki(wid)
    assert w.dirty is False
    assert w.status == "published"
    assert w.current_revision_id is not None
    return run


def set_page_openapi(env: Env, page_id: str, evidence_ids, spec: dict) -> None:
    """改写来源 Page 的 OpenAPI 内容并同步该页 Evidence 的 hash（工作流等价）。"""
    text = json_text(spec)
    page = env.db.get(Page, page_id)
    assert page is not None
    page.content = text
    page.content_hash = fixtures.sha256_hex(text)
    for eid in evidence_ids:
        ev = env.db.get(EvidenceItem, eid)
        assert ev is not None
        ev.content = text
        ev.content_hash = fixtures.sha256_hex(text)
        ev.source_doc_hash = page.content_hash
    env.db.commit()
    env.db.expire_all()


def mark_dirty(env: Env, wid: str) -> None:
    w = env.wiki(wid)
    assert w is not None
    w.dirty = True
    env.db.commit()
    env.db.expire_all()


# ---------------------------------------------------------------------------
# §2.1 + §2.2 + §2.8：同 method/path 双版本隔离；改 v1 不动 v2（真实链）
# ---------------------------------------------------------------------------


def test_real_chain_same_path_dual_version_isolated_and_v1_change_leaves_v2(
        env: Env):
    only_dirty(env, [WIKI_API])

    # 同一路径 /shared：p-api 声明 v1、p-hidden 声明 v2（非 /v1 /v2 路径差异）。
    set_page_openapi(env, PAGE_API, [EV_API], shared_doc_v1())
    set_page_openapi(env, PAGE_HIDDEN, [EV_HIDDEN], shared_doc_v2())
    run1 = assert_run_succeeded(env, WIKI_API)
    rev1 = env.wiki(WIKI_API).current_revision_id
    secs1 = env.sections_by_key(rev1)
    assert _K_SHARED_V1 in secs1, secs1.keys()
    assert _K_SHARED_V2 in secs1, secs1.keys()
    s1_v1, s1_v2 = secs1[_K_SHARED_V1], secs1[_K_SHARED_V2]
    assert s1_v1.validation_status == "pass"
    assert s1_v2.validation_status == "pass"
    for sec in (s1_v1, s1_v2):
        struct = json.loads(sec.structure_json or "{}")
        assert (struct.get("display") or {}).get("endpoint", {}).get("path") \
            == "/shared"

    b1_v1 = env.binding_key_set(s1_v1.id)
    b1_v2 = env.binding_key_set(s1_v2.id)
    assert b1_v1 and b1_v2
    # v1 页 Evidence 不支撑 v2 section，反之亦然。
    assert {ev for _fp, _ut, ev in b1_v1} == {EV_API}
    assert {ev for _fp, _ut, ev in b1_v2} == {EV_HIDDEN}
    snap_v2_before = (s1_v2.content, s1_v2.structure_json)

    # 只修改 v1 资料（摘要 + 新增 sort 查询参数）；v2 资料不动。
    set_page_openapi(env, PAGE_API, [EV_API], shared_doc_v1(enhanced=True))
    mark_dirty(env, WIKI_API)
    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "succeeded", (run2.safe_error_code,
                                        run2.safe_error_message)
    rev2 = env.wiki(WIKI_API).current_revision_id
    assert rev2 != rev1
    secs2 = env.sections_by_key(rev2)
    assert _K_SHARED_V1 in secs2 and _K_SHARED_V2 in secs2

    # v2 语义/字段/证据不串改：content、structure、binding 全等。
    s2_v2 = secs2[_K_SHARED_V2]
    assert (s2_v2.content, s2_v2.structure_json) == snap_v2_before
    assert env.binding_key_set(s2_v2.id) == b1_v2

    # v1 变化：content 变了、新增 sort 参数的 binding。
    s2_v1 = secs2[_K_SHARED_V1]
    assert s2_v1.content != s1_v1.content
    assert "增强" in s2_v1.content or "增强" in s2_v1.structure_json
    b2_v1 = env.binding_key_set(s2_v1.id)
    assert b1_v1 < b2_v1
    assert {ev for _fp, _ut, ev in b2_v1} == {EV_API}
    assert any(fp.startswith("query_parameters.sort") for fp, _ut, _ev in b2_v1)

    # 旧 revision 原样保留；无串改。
    old_secs = env.sections_by_key(rev1)
    assert (old_secs[_K_SHARED_V2].content,
            old_secs[_K_SHARED_V2].structure_json) == snap_v2_before
    assert env.db.get(WikiRevision, rev1).status == "published"
    assert env.manifest_count(run1.id) == 1
    assert env.manifest_count(run2.id) == 1


# ---------------------------------------------------------------------------
# §2.3：无声明 → unversioned（真实链）
# ---------------------------------------------------------------------------


def test_real_chain_no_declaration_defaults_to_unversioned(env: Env):
    only_dirty(env, [WIKI_API])
    # 保留 seed 原始 fixture：不含 x-wiki-version-scope。
    run = assert_run_succeeded(env, WIKI_API)
    secs = env.sections_by_key(env.wiki(WIKI_API).current_revision_id)
    ep_keys = [k for k in secs if k.startswith("api_endpoint|")]
    assert ep_keys, "expected endpoint sections"
    assert "api_endpoint|get|/v1/users|unversioned" in secs
    assert "api_endpoint|get|/v2/users|unversioned" in secs
    # 全部 endpoint section 都归一到 unversioned（不推断 /v1 /v2 路径）。
    assert {k.rsplit("|", 1)[-1] for k in ep_keys} == {"unversioned"}
    v1_sec = secs["api_endpoint|get|/v1/users|unversioned"]
    assert v1_sec.validation_status == "pass"
    assert "unversioned" in (v1_sec.heading or "")
    assert env.manifest_count(run.id) == 1


# ---------------------------------------------------------------------------
# §2.4：非法声明（JSON null）真实链来源级阻断：零发布、旧 current 不变
# ---------------------------------------------------------------------------


def test_real_chain_invalid_declaration_source_level_block_zero_publish(
        env: Env):
    only_dirty(env, [WIKI_API])
    # 先发布一版合法双版本。
    set_page_openapi(env, PAGE_API, [EV_API], shared_doc_v1())
    set_page_openapi(env, PAGE_HIDDEN, [EV_HIDDEN], shared_doc_v2())
    run1 = assert_run_succeeded(env, WIKI_API)
    rev1 = env.wiki(WIKI_API).current_revision_id
    n_revs_1 = len(env.revisions(WIKI_API))

    # p-hidden 改坏：x-wiki-version-scope=null（JSON）；p-api 仍合法。
    bad = shared_doc("v2")
    bad[VERSION_SCOPE_FIELD] = None
    set_page_openapi(env, PAGE_HIDDEN, [EV_HIDDEN], bad)
    mark_dirty(env, WIKI_API)
    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "failed", (run2.safe_error_code,
                                     run2.safe_error_message)
    assert run2.safe_error_code == "VALIDATION_FAILED"

    # 旧 current_revision 不变、无新发布、dirty 保持、无 Manifest。
    w = env.wiki(WIKI_API)
    assert w.current_revision_id == rev1
    assert w.dirty is True
    assert w.status == "published"
    assert len(env.revisions(WIKI_API)) == n_revs_1
    assert env.manifest_count(run2.id) == 0
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() >= 2


# ---------------------------------------------------------------------------
# §2.7：只改声明（Page 内容变化）但发布窗口证据 hash 不同步 → EVIDENCE_STALE
# ---------------------------------------------------------------------------


def test_real_chain_declaration_change_without_evidence_sync_fails_reverify(
        env: Env, monkeypatch):
    only_dirty(env, [WIKI_API])
    set_page_openapi(env, PAGE_API, [EV_API], shared_doc_v1())
    set_page_openapi(env, PAGE_HIDDEN, [EV_HIDDEN], shared_doc_v2())
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.wiki(WIKI_API).current_revision_id
    n_revs = len(env.revisions(WIKI_API))
    n_secs_r1 = env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count()

    # “只改版本声明”：v1 资料仅把声明从 v1 改为 v3（其它字段与 /shared 内容不变），
    # 并同步 Evidence hash（否则 db_adapter 就先拦下，到不了发布前重验）。
    spec_v3 = shared_doc_v1()
    spec_v3[VERSION_SCOPE_FIELD] = "v3"
    set_page_openapi(env, PAGE_API, [EV_API], spec_v3)
    mark_dirty(env, WIKI_API)

    # 调度（真实 HTTP 入口）产生 queued run。
    r_pre = env.c.post("/api/wiki/refresh-page-dirty", headers=env.admin)
    assert r_pre.status_code == 200, r_pre.text
    run3 = env.run_for_wiki(WIKI_API)
    assert run3.status == "queued"
    run3_id = run3.id

    # 在 publish_by_skill 入口放一道门（同步点，委托真实 publish 实现）。
    entered = threading.Event()
    release = threading.Event()
    orig_publish = v3mod._stage_publish_v3

    def _gated_publish(db, run, stage_row, ctx):
        entered.set()
        if not release.wait(_TIMEOUT):
            raise RuntimeError("release timeout in gated publish")
        return orig_publish(db, run, stage_row, ctx)

    monkeypatch.setattr(v3mod, "_stage_publish_v3", _gated_publish)
    pregs.replace_for_test(v3mod._candidate_pipeline_v3())

    errors: list[BaseException] = []

    def _bg() -> None:
        sess = env.Session()
        try:
            executor.execute_run(sess, run3_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    assert entered.wait(_TIMEOUT), "publish stage not reached"

    # 竞态窗口内：另一写入者把 ev-api 证据 hash 改回“未同步”状态（只改声明、
    # 不同步证据 hash 的等价破坏）。
    ev = env.db.get(EvidenceItem, EV_API)
    assert ev is not None
    ev.content_hash = "b" * 64
    ev.source_doc_hash = "c" * 64
    env.db.commit()
    env.db.expire_all()

    release.set()
    t.join(timeout=_TIMEOUT)
    assert not t.is_alive(), "gated worker thread did not finish"
    assert not errors, errors

    env.db.expire_all()
    r3 = env.db.get(CompileRun, run3_id)
    assert r3.status == "failed", r3.status
    assert r3.safe_error_code == "EVIDENCE_STALE", \
        (r3.safe_error_code, r3.safe_error_message)

    # 零新 Revision；当前仍 rev1；历史 sections 完整；dirty 保持。
    w = env.wiki(WIKI_API)
    assert w.current_revision_id == rev1
    assert w.dirty is True
    assert len(env.revisions(WIKI_API)) == n_revs
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() == n_secs_r1
    assert env.manifest_count(run3_id) == 0
