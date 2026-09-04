"""Phase 8B：API Reference Section display DTO —— 后端投影 + 只读 API 增量返回。

覆盖（对应任务清单 1-12）：
1. 合法 endpoint display 精确且顺序稳定（参数顺序 query/header 稳定；两次调用相等）；
2. 同 Wiki 两个 endpoint（/users v1 与 /users v2）各自返回各自 display，不串用；
3. 单目标与 batch 共用同一 _derive_persist_sections/投影函数（含依据断言）；
4. 当前与历史 revision 各自返回自己的结构（admin ?revision_id= 断言不同）；
5. missing/null/坏 JSON/未知 schema_version/未知 role → 不 500，role None 或已知、display None；
6. 伪造内部字段（prompt/evidence/secret 等）经 sanitize 丢弃，响应体不含这些键；
7. 正文 hash 不匹配 → display None；
8. 人工编辑后不展示旧表格：lifecycle 复制出的新 revision 该 section structure_json
   为 NULL、响应 role/display 为 null、content 保持人工内容；
9. protected/manual 复制内容不变：structure_json 随行保留但 display 仍 null，content 原样；
10. 无权限 Wiki/Revision 仍被拒（普通用户对不可见 wiki 404、非 published revision 404）；
11. 投影超限不产生展示（build 返回 None / 读侧 DISPLAY_MAX_BYTES 边界降级 null）；
12. 发布失败不残留：事务内 _persist_api_revision 后 rollback，无 Section 行残留
    （display 随既有发布事务写入、无独立 commit）。

使用 tmp 文件 SQLite + TestClient + dependency override（对齐 test_wiki_v4_api）。
"""
from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.main import app
from app.models.database import (
    EvidenceItem,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    init_db,
)

ADMIN = {"id": "u-admin", "username": "admin", "groups": ["__local_admin__"]}


def _hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# IR / display 构造 helpers
# ---------------------------------------------------------------------------


def _make_ir(scope: str = "v1", *, with_conflict=True, deep_content=None) -> tuple:
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiErrorCode,
        ApiExample,
        ApiFieldBinding,
        ApiKnowledgeGap,
        ApiParameter,
        ApiResponse,
        ApiVersionNote,
    )

    ev = ("ev-1",)
    responses = [
        ApiResponse(status_code="200", description="成功返回",
                    content={"application/json": {"type": "object"}}),
        ApiResponse(status_code="404", description="不存在", content={}),
    ]
    bindings = [
        ApiFieldBinding(field_path="responses.200", evidence_ids=ev),
        ApiFieldBinding(field_path="responses.404", evidence_ids=ev,
                        usage_type="conflict" if with_conflict else "support"),
    ]
    examples = [ApiExample(
        title="用户示例", description="示例描述", media_type="application/json",
        content=deep_content if deep_content is not None else {"id": 1, "ok": True},
    )]
    ep = ApiEndpoint(
        method="GET", path="/users", version_scope=scope,
        summary="用户列表", description=f"返回{scope}用户列表。",
        query_parameters=(ApiParameter(name="page", location="query",
                                       required=False, description="页码"),
                          ApiParameter(name="page_size", location="query",
                                       required=True, description="每页条数")),
        headers=(ApiParameter(name="X-Request-Id", location="header",
                              required=False, description="请求ID"),),
        responses=tuple(responses),
        error_codes=(ApiErrorCode(code="E_PERM", description="无权限",
                                  http_status="403"),),
        examples=tuple(examples),
        evidence_bindings=tuple(bindings),
    )
    notes = (ApiVersionNote(version_scope=scope,
                            note=f"新增 {scope} 版本说明", endpoint_id=ep.endpoint_id),)
    gaps = (ApiKnowledgeGap(gap_type="no_evidence", description="无响应示例证据",
                            endpoint_id=ep.endpoint_id),)
    ir = ApiDocumentIR(
        endpoints=(ep,),
        version_notes=notes,
        knowledge_gaps=gaps,
    )
    return ir, ep


def _built(scope: str = "v1", *, content: str | None = None, **opts) -> SimpleNamespace:
    """构造一个可直接落库的 endpoint section 事实来源。

    返回对象含 spec/ir/content/structure_json/display/content_hash/section_key。
    structure_json 模拟 _write_auto_section 的 sort_keys 序列化。
    """
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display

    ir, ep = _make_ir(scope, **opts)
    bp = plan_document(ir)
    spec = next(s for s in bp.sections if s.section_role == "endpoint")
    content = content if content is not None else (
        f"# GET /users（{scope}）\n\n返回{scope}用户列表。")
    content_hash = _hash(content)
    display = build_section_display(ir, spec, content_hash)
    structure = spec.to_dict()
    if display is not None:
        structure["display"] = display
    return SimpleNamespace(
        ir=ir, ep=ep, spec=spec, content=content, content_hash=content_hash,
        display=display, structure_json=json.dumps(
            structure, ensure_ascii=False, sort_keys=True),
        section_key=spec.section_key,
    )


def _row(b, sec_id, *, structure_json=None, content=None, hash_=None,
         origin="auto", merge="auto", locked=False, validation="pass",
         order_index=0, heading=None, section_key=None) -> dict:
    return {
        "id": sec_id, "order_index": order_index,
        "content": content if content is not None else getattr(b, "content", ""),
        "content_hash": hash_ if hash_ is not None else getattr(b, "content_hash", None),
        "structure_json": structure_json if structure_json is not None else (
            getattr(b, "structure_json", None)),
        "content_origin": origin, "merge_policy": merge, "locked": locked,
        "validation_status": validation,
        "heading": heading, "section_key": section_key,
    }


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'wiki_display.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "wiki_topic_enabled", True)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    client = TestClient(app)
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _override(user: dict):
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: user


def _engine_of(api_env):
    return api_env[1]


def _seed_wiki(engine, wid="w1", *, acl_scope=None, status="published") -> None:
    db = sessionmaker(bind=engine)()
    db.add(WikiPage(id=wid, title="主题", summary="", acl_scope=acl_scope,
                    status=status, dirty=False))
    db.commit()
    db.close()


def _seed_revision(engine, wid, rev_id, rows, *, current=True,
                   status="published", title="主题") -> None:
    db = sessionmaker(bind=engine)()
    db.add(WikiRevision(id=rev_id, wiki_page_id=wid, title=title, status=status,
                        edit_type="auto"))
    db.flush()
    for row in rows:
        db.add(WikiSection(
            id=row["id"], revision_id=rev_id, section_type="facts",
            heading=row.get("heading"), content=row.get("content") or "",
            order_index=row.get("order_index", 0),
            locked=bool(row.get("locked", False)),
            content_origin=row.get("content_origin", "auto"),
            merge_policy=row.get("merge_policy", "auto"),
            section_key=row.get("section_key"),
            skill_key="api_reference", skill_version="1",
            content_hash=row.get("content_hash"),
            validation_status=row.get("validation_status", "pass"),
            structure_json=row.get("structure_json"),
        ))
    db.flush()
    if current:
        page = db.get(WikiPage, wid)
        page.current_revision_id = rev_id
    db.commit()
    db.close()


def _get_sections(engine, revision_id):
    db = sessionmaker(bind=engine)()
    rows = db.query(WikiSection).filter(
        WikiSection.revision_id == revision_id
    ).order_by(WikiSection.order_index).all()
    db.close()
    return rows


def _seed_page_and_evidence(db, pid="p1"):
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0",
                 "description": "GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {
            "summary": "用户列表", "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)
    db.add(Page(id=pid, title="用户 API 文档", content=spec,
                content_hash=_hash(spec)))
    db.flush()
    db.add(EvidenceItem(
        id="ev1", source_page_id=pid, status="active", content="x",
        locator_json=json.dumps({"section": "all"}),
        content_hash="f" * 64, source_doc_hash=_hash(spec)))
    db.flush()
    return spec


# ---------------------------------------------------------------------------
# 1. 合法 endpoint display：精确 + 顺序稳定
# ---------------------------------------------------------------------------


def test_endpoint_display_exact_stable_and_parameter_order(api_env):
    client, engine = api_env
    b = _built("v1")
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-ep", section_key=b.section_key),
    ])
    _override(ADMIN)
    r1 = client.get("/api/wiki/w1")
    assert r1.status_code == 200
    body1 = r1.json()
    sec = body1["sections"][0]
    assert sec["section_role"] == "endpoint"
    d = sec["display"]
    assert d is not None
    # 与 compile 侧 build_section_display 精确一致（sanitize 是白名单无损重建）。
    assert d == b.display
    assert d["schema_version"] == "api-section-display/v2"
    assert d["content_hash"] == _hash(b.content)
    assert d["section_role"] == "endpoint"
    assert d["version_scope"] == "v1"
    assert d["endpoint"] == {
        "method": "GET", "path": "/users",
        "summary": "用户列表", "description": "返回v1用户列表。",
    }
    # 参数顺序保持 IR 分组平铺顺序（本 IR 无 path 参数，query 在前 header 在后）。
    assert [p["location"] for p in d["parameters"]] == ["query", "query", "header"]
    assert [p["name"] for p in d["parameters"]] == ["page", "page_size", "X-Request-Id"]
    assert d["parameters"][0]["required"] is False
    assert d["parameters"][1]["required"] is True
    # 响应分区保留每个媒体类型名称；schema_status 只做保守判定。
    assert {resp["status_code"] for resp in d["responses"]} == {"200", "404"}
    by_code = {resp["status_code"]: resp for resp in d["responses"]}
    assert by_code["200"]["media_types"] == [
        {"media_type": "application/json", "schema_status": "present"}]
    assert by_code["404"]["media_types"] == []
    # 错误码：http_status 原样（业务码不并入）。
    assert d["error_codes"] == [{"code": "E_PERM", "description": "无权限",
                                 "http_status": "403"}]
    assert d["version_notes"] == [{"version_scope": "v1", "note": "新增 v1 版本说明"}]
    assert d["knowledge_gaps"] == [{"gap_type": "no_evidence",
                                    "description": "无响应示例证据"}]
    assert d["conflicts"] == [{"field_path": "responses.404"}]
    # 确定性：两次调用返回完全一致。
    r2 = client.get("/api/wiki/w1")
    assert r2.status_code == 200
    assert r2.json() == body1


def test_path_query_header_parameter_order_and_request_body(api_env):
    """带 path 参数的 endpoint：参数顺序严格 path→query→header；request_body 形态。"""
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiParameter,
        ApiRequestBody,
        ApiResponse,
    )
    client, engine = api_env
    ep = ApiEndpoint(
        method="POST", path="/users/{user_id}", version_scope="v2",
        summary="更新用户", description="更新指定用户。",
        path_parameters=(ApiParameter(name="user_id", location="path",
                                      required=True, description="用户ID"),),
        query_parameters=(ApiParameter(name="page", location="query",
                                       required=False, description="页码"),),
        headers=(ApiParameter(name="X-Request-Id", location="header",
                              required=False, description="请求ID"),),
        request_body=ApiRequestBody(
            description="用户对象", required=True,
            content={"application/json": {"type": "object"},
                     "text/plain": {}},
        ),
        responses=(ApiResponse(status_code="200", description="成功",
                               content={"application/json": {"schema": {}}}),),
    )
    ir = ApiDocumentIR(endpoints=(ep,))
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    content = "# POST /users/{user_id} v2"
    ch = _hash(content)
    display = build_section_display(ir, spec, ch)
    assert display is not None
    assert [p["location"] for p in display["parameters"]] == ["path", "query", "header"]
    assert [p["name"] for p in display["parameters"]] == ["user_id", "page", "X-Request-Id"]
    assert display["parameters"][0]["required"] is True
    # request_body 形态（媒体类型逐项保留 + 保守 schema_status）。
    assert display["request_body"] == {
        "required": True, "description": "用户对象",
        "media_types": [
            {"media_type": "application/json", "schema_status": "present"},
            {"media_type": "text/plain", "schema_status": "unspecified"},
        ],
    }

    # 经 DB + API 往返仍一致。
    _seed_wiki(engine)
    structure = spec.to_dict()
    structure["display"] = display
    structure_json = json.dumps(structure, ensure_ascii=False, sort_keys=True)
    _seed_revision(engine, "w1", "rev1", [
        _row(SimpleNamespace(content=content, content_hash=ch,
                             structure_json=structure_json),
             "sec-pb", structure_json=structure_json, section_key=spec.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["display"] == display


# ---------------------------------------------------------------------------
# 2. 同 Wiki 两个 endpoint（v1/v2）不串用
# ---------------------------------------------------------------------------


def test_two_endpoint_display_not_crossed(api_env):
    client, engine = api_env
    b1 = _built("v1", content="# v1 正文")
    b2 = _built("v2", content="# v2 正文")
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b1, "sec-v1", order_index=0, section_key=b1.section_key),
        _row(b2, "sec-v2", order_index=1, section_key=b2.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    secs = r.json()["sections"]
    assert [s["section_role"] for s in secs] == ["endpoint", "endpoint"]
    by_hash = {s["display"]["content_hash"]: s["display"] for s in secs}
    d_v1 = by_hash[b1.content_hash]
    d_v2 = by_hash[b2.content_hash]
    assert d_v1["endpoint"]["path"] == d_v2["endpoint"]["path"] == "/users"
    assert d_v1["version_scope"] == "v1" and d_v2["version_scope"] == "v2"
    assert d_v1["endpoint"]["description"] == "返回v1用户列表。"
    assert d_v2["endpoint"]["description"] == "返回v2用户列表。"
    assert d_v1["version_notes"] == [{"version_scope": "v1", "note": "新增 v1 版本说明"}]
    assert d_v2["version_notes"] == [{"version_scope": "v2", "note": "新增 v2 版本说明"}]
    # display 不串用：每个 section 的 display.content_hash 与自身 content 一致。
    for s in secs:
        assert s["display"]["content_hash"] == _hash(s["content"])


# ---------------------------------------------------------------------------
# 3. 单目标与 batch 共用同一投影函数
# ---------------------------------------------------------------------------


def test_single_and_batch_share_projection(api_env, monkeypatch):
    """单目标与 batch 共用默认 v3 的同一投影函数。

    依据：batch（wiki_skilled_batch_v3._synthesize_api_target）在函数体内 import
    wiki_skilled_default_v3._compile_api_from_pages（与单目标 synthesize 同一对象）；
    而 default_v3._compile_api_from_pages 内部调用本模块的 _derive_persist_sections
    （display 投影唯一入口）。断言分四层：
      - 源文本：batch 引用的 _compile_api_from_pages 属于 default_v3；
      - 源文本：default_v3._compile_api_from_pages 内只经 _derive_persist_sections 投影；
      - 运行期 spy：单目标 _compile_api_from_pages 确实调用 default_v3._derive_persist_sections，
        且 endpoint section 均带与正文 hash 一致的 display；
      - 确定性：同一 ApiCompileResult 连续两次投影结果完全相等。
    """
    import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
    from app.core.wiki_pipeline.pipelines import wiki_skilled_batch_v3 as batch_mod

    src = inspect.getsource(batch_mod._synthesize_api_target)
    assert "wiki_skilled_default_v3" in src and "_compile_api_from_pages" in src
    assert "_derive_persist_sections" in inspect.getsource(
        v3mod._compile_api_from_pages)

    calls = []
    original = v3mod._derive_persist_sections

    def _spy(result):
        calls.append(1)
        return original(result)

    monkeypatch.setattr(v3mod, "_derive_persist_sections", _spy)
    db = sessionmaker(bind=_engine_of(api_env))()
    _seed_page_and_evidence(db)
    db.commit()
    compiled = v3mod._compile_api_from_pages(db, ["p1"])
    db.close()
    assert calls, "_derive_persist_sections 未被单目标编译调用"
    endpoint_items = [it for it in compiled["sections"]
                      if it["structure"]["section_role"] == "endpoint"]
    assert endpoint_items, "编译产物缺少 endpoint section"
    for item in endpoint_items:
        assert item["validation_status"] == "pass"
        assert "display" in item["structure"]
        d = item["structure"]["display"]
        assert d["content_hash"] == item["content_hash"] == _hash(item["content"])
    ra = compiled["result"]
    monkeypatch.undo()
    assert v3mod._derive_persist_sections(ra) == original(ra)


# ---------------------------------------------------------------------------
# 4. 当前与历史 revision 各自返回自己的结构
# ---------------------------------------------------------------------------


def test_current_and_historical_revision_own_structure(api_env):
    client, engine = api_env
    b_old = _built("v1", content="# 旧版正文 GET /users v1")
    b_new = _built("v2", content="# 新版正文 GET /users v2")
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev-old", [
        _row(b_old, "sec-old", section_key=b_old.section_key),
    ], current=False)
    _seed_revision(engine, "w1", "rev-new", [
        _row(b_new, "sec-new", section_key=b_new.section_key),
    ], current=True)
    _override(ADMIN)
    cur = client.get("/api/wiki/w1")
    assert cur.status_code == 200
    assert cur.json()["viewing_revision_id"] == "rev-new"
    sec_new = cur.json()["sections"][0]
    assert sec_new["display"]["version_scope"] == "v2"
    assert sec_new["display"]["content_hash"] == b_new.content_hash

    old = client.get("/api/wiki/w1?revision_id=rev-old")
    assert old.status_code == 200
    assert old.json()["viewing_revision_id"] == "rev-old"
    sec_old = old.json()["sections"][0]
    assert sec_old["display"]["version_scope"] == "v1"
    assert sec_old["display"]["content_hash"] == b_old.content_hash
    assert sec_old["display"] != sec_new["display"]


# ---------------------------------------------------------------------------
# 5. missing/null/坏 JSON/未知 schema_version/未知 role → 不 500
# ---------------------------------------------------------------------------


def test_missing_null_bad_json_unknown_schema_and_role(api_env):
    client, engine = api_env
    b_ok = _built("v1", content="# ok 正文")
    _seed_wiki(engine)
    rows = [
        {"id": "s0", "order_index": 0, "content": "no-structure",
         "structure_json": None, "validation_status": None, "content_hash": None},
        {"id": "s1", "order_index": 1, "content": "bad-json",
         "structure_json": "{not-json", "validation_status": None,
         "content_hash": None},
        {"id": "s2", "order_index": 2, "content": "overview-role",
         "structure_json": json.dumps({"section_role": "overview",
                                       "section_key": "overview"}),
         "validation_status": "pass", "content_hash": None},
        # 未知 schema_version（display 不通过版本校验）。
        {"id": "s3", "order_index": 3, "content": "unknown-schema",
         "structure_json": json.dumps({
             "section_role": "endpoint", "section_key": "k3",
             "display": {"schema_version": "other/v9",
                         "section_role": "endpoint"}}),
         "validation_status": "pass", "content_hash": None},
        # role endpoint 但无 display（历史/未知版本数据）。
        {"id": "s4", "order_index": 4, "content": "endpoint-no-display",
         "structure_json": json.dumps({
             "section_role": "endpoint", "section_key": "k4"}),
         "validation_status": "pass", "content_hash": None},
        # 未知 role。
        {"id": "s5", "order_index": 5, "content": "unknown-role",
         "structure_json": json.dumps({"section_role": "robot",
                                       "section_key": "k5"}),
         "validation_status": "pass", "content_hash": None},
        # 正常对照行。
        _row(b_ok, "s6", order_index=6, section_key=b_ok.section_key),
    ]
    _seed_revision(engine, "w1", "rev1", rows)
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    secs = {s["id"]: s for s in r.json()["sections"]}
    assert secs["s0"]["section_role"] is None and secs["s0"]["display"] is None
    assert secs["s1"]["section_role"] is None and secs["s1"]["display"] is None
    assert secs["s2"]["section_role"] == "overview" and secs["s2"]["display"] is None
    assert secs["s3"]["section_role"] == "endpoint" and secs["s3"]["display"] is None
    assert secs["s4"]["section_role"] == "endpoint" and secs["s4"]["display"] is None
    assert secs["s5"]["section_role"] is None and secs["s5"]["display"] is None
    assert secs["s6"]["section_role"] == "endpoint" and secs["s6"]["display"] == b_ok.display


# ---------------------------------------------------------------------------
# 6. 伪造内部字段不出现在响应
# ---------------------------------------------------------------------------


def test_forged_internal_fields_dropped(api_env):
    client, engine = api_env
    b = _built("v1", content="# 防伪造正文")
    # 伪造：顶层 + 各层子对象塞内部键（值带可检测 marker）。
    display = json.loads(json.dumps(b.display, ensure_ascii=False))
    display["prompt"] = {"system": "PROMPT_LEAK_77"}
    display["evidence_ids"] = ["EVID_LEAK_88"]
    display["evidence"] = [{"id": "EVID_LEAK_99"}]
    display["secret"] = "SECRET_LEAK_11"
    display["skill_decision"] = "SKILL_LEAK_22"
    display["endpoint"]["evidence"] = "EP_EVID_LEAK_33"
    display["parameters"][0]["evidence_id"] = "PARAM_EVID_LEAK_44"
    display["responses"][0]["evidence_ids"] = ["RESP_EVID_LEAK_55"]
    display["error_codes"][0]["diagnostics"] = "DIAG_LEAK_66"
    display["version_notes"][0]["raw_acl"] = "ACL_LEAK_77"
    display["conflicts"][0]["evidence_ids"] = ["CONF_EVID_LEAK_88"]
    structure = b.spec.to_dict()
    structure["display"] = display
    structure_json = json.dumps(structure, ensure_ascii=False, sort_keys=True)
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-f", structure_json=structure_json,
             section_key=b.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    body = r.json()
    sec = body["sections"][0]
    assert sec["section_role"] == "endpoint"
    assert sec["display"] == b.display  # 重建后与合法投影完全一致（无任何内部键）
    blob = json.dumps(body, ensure_ascii=False)
    for marker in ("PROMPT_LEAK_77", "EVID_LEAK", "SECRET_LEAK_11",
                   "SKILL_LEAK_22", "EP_EVID_LEAK_33", "PARAM_EVID_LEAK_44",
                   "RESP_EVID_LEAK_55", "DIAG_LEAK_66", "ACL_LEAK_77",
                   "CONF_EVID_LEAK_88"):
        assert marker not in blob
    # 不返回 section_key / content_hash / skill / diagnostics 等内部字段。
    for forbidden in ("section_key", "skill_key", "content_hash", "diagnostics",
                      "structure_json", "evidence_id"):
        assert forbidden not in sec


# ---------------------------------------------------------------------------
# 7. 正文 hash 不匹配 → display None
# ---------------------------------------------------------------------------


def test_content_hash_mismatch_drops_display(api_env):
    client, engine = api_env
    b = _built("v1", content="# 真实正文")
    other = _hash("# 另一段正文")
    structure = b.spec.to_dict()
    structure["display"] = dict(b.display, content_hash=other)
    structure_json = json.dumps(structure, ensure_ascii=False, sort_keys=True)
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-h", structure_json=structure_json,
             section_key=b.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["section_role"] == "endpoint"
    assert sec["display"] is None


# ---------------------------------------------------------------------------
# 8. 人工编辑后不展示旧表格（lifecycle 复制出新行 structure_json=NULL）
# ---------------------------------------------------------------------------


def test_manual_edit_copy_clears_structure(api_env):
    client, engine = api_env
    b = _built("v1", content="# 原始自动内容")
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev-orig", [
        _row(b, "sec-orig", section_key=b.section_key),
    ], current=True)

    db = sessionmaker(bind=engine)()
    page = db.get(WikiPage, "w1")
    from app.core.knowledge_compiler_v3.wiki_lifecycle import (
        edit_wiki_section_current,
    )
    new_rev = edit_wiki_section_current(
        db, page, "sec-orig", "人工编辑后的内容", ADMIN["username"])
    new_rev_id = new_rev.id
    db.close()

    # lifecycle 复制出的新行不携带 P44 结构字段。
    rows = _get_sections(engine, new_rev_id)
    copied = [s for s in rows if s.section_key is None and s.structure_json is None]
    assert len(copied) == 1
    edited = copied[0]
    assert edited.content == "人工编辑后的内容"
    assert edited.content_origin == "manual"
    assert edited.merge_policy == "protected"
    assert edited.locked is True

    _override(ADMIN)
    # 当前 revision（新行）→ role/display null，内容保持人工内容。
    cur = client.get("/api/wiki/w1")
    assert cur.status_code == 200
    sec = cur.json()["sections"][0]
    assert sec["content"] == "人工编辑后的内容"
    assert sec["section_role"] is None
    assert sec["display"] is None
    # 历史 revision（旧自动行）仍返回结构化 display。
    old = client.get("/api/wiki/w1?revision_id=rev-orig")
    assert old.status_code == 200
    old_sec = old.json()["sections"][0]
    assert old_sec["display"] == b.display


# ---------------------------------------------------------------------------
# 9. protected/manual 复制内容不变（structure_json 随行保留但 display=null）
# ---------------------------------------------------------------------------


def test_protected_copy_keeps_content_but_hides_display(api_env):
    client, engine = api_env
    b = _built("v1", content="# 受保护正文内容")
    structure_json = b.structure_json
    _seed_wiki(engine)
    # 模拟 v3 _write_protected_section：structure_json 随行保留、content 原样。
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-prot", structure_json=structure_json,
             content="# 受保护正文内容", origin="manual", merge="protected",
             locked=True, section_key=b.section_key),
    ])
    db = sessionmaker(bind=engine)()
    row = db.get(WikiSection, "sec-prot")
    assert row.content == "# 受保护正文内容"
    assert row.structure_json is not None
    db.close()
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["content"] == "# 受保护正文内容"  # 内容原样
    assert sec["section_role"] == "endpoint"
    assert sec["display"] is None  # manual/protected → 前端回退 Markdown


# ---------------------------------------------------------------------------
# 10. 无权限 Wiki / 非 published revision 仍被拒
# ---------------------------------------------------------------------------


def test_acl_and_published_revision_rules_unchanged(api_env):
    client, engine = api_env
    b = _built("v1", content="# acl 正文")
    # 可见且 published → 普通用户 200。
    _seed_wiki(engine, wid="w-pub", acl_scope='{"groups": ["eng"]}')
    _seed_revision(engine, "w-pub", "rev-pub", [
        _row(b, "sec-pub", section_key=b.section_key),
    ], current=True)
    _override({"id": "u1", "username": "eng", "groups": ["eng"]})
    r = client.get("/api/wiki/w-pub")
    assert r.status_code == 200

    # 普通用户对不可见 Wiki → 404。
    _seed_wiki(engine, wid="w-hidden", acl_scope='{"groups": ["sales"]}')
    _seed_revision(engine, "w-hidden", "rev-pub2", [
        _row(b, "sec-h2", section_key=b.section_key),
    ], current=True)
    r2 = client.get("/api/wiki/w-hidden")
    assert r2.status_code == 404

    # 普通用户对非 published revision 的 Wiki → 404（只读规则沿用）。
    _seed_wiki(engine, wid="w-draft", acl_scope='{"groups": ["eng"]}')
    _seed_revision(engine, "w-draft", "rev-draft", [
        _row(b, "sec-d", section_key=b.section_key),
    ], current=True, status="draft")
    _override({"id": "u1", "username": "eng", "groups": ["eng"]})
    r3 = client.get("/api/wiki/w-draft")
    assert r3.status_code == 404

    # admin 不受组限制可见所有。
    _override(ADMIN)
    r4 = client.get("/api/wiki/w-hidden")
    assert r4.status_code == 200


# ---------------------------------------------------------------------------
# 11. 投影超限不产生展示
# ---------------------------------------------------------------------------


def test_oversize_and_overdepth_produce_no_display(api_env, monkeypatch):
    from app.core.wiki_skills.api_reference import display as disp_mod
    client, engine = api_env

    # 深度超限：examples.content 嵌套过深 → build 返回 None。
    deep = {}
    node = deep
    for _ in range(40):
        node["k"] = {}
        node = node["k"]
    ir, ep = _make_ir("v1", deep_content=deep)
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    assert disp_mod.build_section_display(ir, spec, _hash("# x")) is None

    # 字节超限：monkeypatch 把边界调小 → build / sanitize 均返回 None。
    b = _built("v1", content="# 正常正文")
    assert b.display is not None
    monkeypatch.setattr(disp_mod, "DISPLAY_MAX_BYTES", 10)
    assert disp_mod.build_section_display(b.ir, b.spec, b.content_hash) is None
    assert disp_mod.sanitize_section_display(b.display) is None

    # 读侧：存一个合法 display，但读时字节边界调小 → 响应 display null（不 500）。
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-big", section_key=b.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["section_role"] == "endpoint"
    assert sec["display"] is None


def test_compile_plan_omits_display_when_build_none(api_env, monkeypatch):
    """build 返回 None → _derive_persist_sections 不把 display 写入 structure。"""
    import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
    from app.core.wiki_skills.api_reference import display as disp_mod
    monkeypatch.setattr(disp_mod, "DISPLAY_MAX_BYTES", 10)
    db = sessionmaker(bind=_engine_of(api_env))()
    _seed_page_and_evidence(db)
    db.commit()
    compiled = v3mod._compile_api_from_pages(db, ["p1"])
    db.close()
    endpoint_items = [it for it in compiled["sections"]
                      if it["structure"]["section_role"] == "endpoint"]
    assert endpoint_items
    for item in endpoint_items:
        assert "display" not in item["structure"]


# ---------------------------------------------------------------------------
# 12. 发布失败不残留带 display 的孤儿 Section
# ---------------------------------------------------------------------------


def test_publish_rollback_leaves_no_section(api_env):
    """display 随既有发布事务写入：_persist_api_revision 后 rollback → 零残留。"""
    import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
    client, engine = api_env
    _seed_wiki(engine)
    db = sessionmaker(bind=engine)()
    _seed_page_and_evidence(db)
    db.commit()
    page = db.get(WikiPage, "w1")
    compiled = v3mod._compile_api_from_pages(db, ["p1"])
    assert compiled["publishable"]
    endpoint_items = [it for it in compiled["sections"]
                      if it["structure"]["section_role"] == "endpoint"]
    assert endpoint_items and "display" in endpoint_items[0]["structure"]
    # 同事务内写 Revision + Sections（_persist_api_revision 只 flush 不 commit）。
    v3mod._persist_api_revision(
        db, {}, {}, compiled, page, "api_reference", "1", "rev-fail")
    db.flush()
    assert db.query(WikiSection).filter(
        WikiSection.revision_id == "rev-fail").count() > 0
    assert db.query(WikiSectionEvidenceBinding).count() > 0
    # 发布失败（回滚）：整批不残留 —— display 无独立 commit，随事务一起消失。
    db.rollback()
    assert db.query(WikiRevision).filter(WikiRevision.id == "rev-fail").count() == 0
    assert db.query(WikiSection).filter(
        WikiSection.revision_id == "rev-fail").count() == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0
    db.close()


# ---------------------------------------------------------------------------
# 13. Phase 8B.1：参数类型只读显式声明（不猜测）
# ---------------------------------------------------------------------------


def test_declared_param_type_read_only_not_guessed(api_env):
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiParameter,
    )

    params = (
        ApiParameter(name="id", location="path", required=True,
                     description="用户ID", schema={"type": "integer"}),
        ApiParameter(name="name", location="query", required=False,
                     description="名称", schema={"type": "string"}),
        # 显式多类型（OpenAPI 3.1 风格）→ 约定展示；不推测。
        ApiParameter(name="tags", location="query", required=False,
                     description="标签", schema={"type": ["string", "null"]}),
        # 只有 example / format / default，无 type → 绝不猜。
        ApiParameter(name="ts", location="query", required=False,
                     description="时间", schema={"example": "2026-01-01",
                                                 "format": "date-time"}),
        ApiParameter(name="bare", location="query", required=False,
                     description="无schema", schema={}),
    )
    ep = ApiEndpoint(method="GET", path="/p", version_scope="v1",
                     summary="", description="",
                     path_parameters=(params[0],), query_parameters=params[1:],
                     headers=(), responses=(), error_codes=(), examples=())
    ir = ApiDocumentIR(endpoints=(ep,))
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    display = build_section_display(ir, spec, _hash("# p"))
    assert display is not None
    by_name = {p["name"]: p for p in display["parameters"]}
    assert by_name["id"]["type"] == "integer"
    assert by_name["name"]["type"] == "string"
    assert by_name["tags"]["type"] == "string | null"
    assert by_name["ts"]["type"] == ""  # example/format 不用于猜测
    assert by_name["bare"]["type"] == ""


# ---------------------------------------------------------------------------
# 14. 响应/请求体媒体类型保留 + 空 schema 保守判定
# ---------------------------------------------------------------------------


def test_response_media_types_preserved_and_empty_schema_conservative(api_env):
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiResponse,
    )

    responses = (
        # 两个媒体类型都保留；json 有 schema；text/plain 为显式空 → unspecified。
        ApiResponse(status_code="200", description="成功",
                    content={"application/json": {"type": "object"},
                             "text/plain": {}}),
        # 无 content → 无媒体类型（不虚构）。
        ApiResponse(status_code="404", description="不存在", content={}),
        # content 只有空 schema 字典 → 不得误称具有具体 Schema。
        ApiResponse(status_code="500", description="内部错误",
                    content={"application/problem+json": {}}),
    )
    ep = ApiEndpoint(method="GET", path="/r", version_scope="v1",
                     summary="", description="", query_parameters=(),
                     headers=(), path_parameters=(), responses=responses,
                     error_codes=(), examples=())
    ir = ApiDocumentIR(endpoints=(ep,))
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    display = build_section_display(ir, spec, _hash("# r"))
    assert display is not None
    by_code = {resp["status_code"]: resp for resp in display["responses"]}
    # 两个媒体类型均保留名称（原正文会展示的信息不丢）。
    assert {m["media_type"] for m in by_code["200"]["media_types"]} == {
        "application/json", "text/plain"}
    assert by_code["200"]["media_types"] == [
        {"media_type": "application/json", "schema_status": "present"},
        {"media_type": "text/plain", "schema_status": "unspecified"},
    ]
    assert by_code["404"]["media_types"] == []
    # content={"media": {}} 不得标为 present（无法区分缺失与显式空）。
    assert by_code["500"]["media_types"] == [
        {"media_type": "application/problem+json",
         "schema_status": "unspecified"}]


def test_request_body_media_types_same_semantics(api_env):
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiRequestBody,
        ApiResponse,
    )

    ep = ApiEndpoint(
        method="POST", path="/rb", version_scope="v1",
        summary="", description="",
        request_body=ApiRequestBody(
            required=True, description="体",
            content={"application/json": {"type": "object"},
                     "application/x-www-form-urlencoded": {}}),
        query_parameters=(), headers=(), path_parameters=(),
        responses=(ApiResponse(status_code="200", description="ok",
                               content={}),),
        error_codes=(), examples=())
    ir = ApiDocumentIR(endpoints=(ep,))
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    display = build_section_display(ir, spec, _hash("# rb"))
    assert display is not None
    assert display["request_body"]["media_types"] == [
        {"media_type": "application/json", "schema_status": "present"},
        {"media_type": "application/x-www-form-urlencoded",
         "schema_status": "unspecified"},
    ]


# ---------------------------------------------------------------------------
# 15. example.description 可见（投影已含 → 保持到 DTO）
# ---------------------------------------------------------------------------


def test_example_description_preserved(api_env):
    from app.core.wiki_skills.api_reference.schemas import (
        ApiDocumentIR,
        ApiEndpoint,
        ApiExample,
        ApiResponse,
    )

    ep = ApiEndpoint(
        method="GET", path="/ex", version_scope="v1",
        summary="", description="",
        examples=(ApiExample(title="示例A", description="示例A说明",
                             media_type="application/json",
                             content={"ok": True}),),
        query_parameters=(), headers=(), path_parameters=(),
        responses=(ApiResponse(status_code="200", description="ok",
                               content={}),),
        error_codes=())
    ir = ApiDocumentIR(endpoints=(ep,))
    from app.core.wiki_skills.api_reference.blueprint import plan_document
    from app.core.wiki_skills.api_reference.display import build_section_display
    spec = next(s for s in plan_document(ir).sections
                if s.section_role == "endpoint")
    display = build_section_display(ir, spec, _hash("# ex"))
    assert display is not None
    assert display["examples"][0]["description"] == "示例A说明"
    # 经 DB + API 往返仍保留。
    engine = _engine_of(api_env)
    _seed_wiki(engine)
    structure = spec.to_dict()
    structure["display"] = display
    structure_json = json.dumps(structure, ensure_ascii=False, sort_keys=True)
    _seed_revision(engine, "w1", "rev1", [
        _row(SimpleNamespace(content="# ex", content_hash=_hash("# ex"),
                             structure_json=structure_json),
             "sec-ex", structure_json=structure_json,
             section_key=spec.section_key),
    ])
    _override(ADMIN)
    r = api_env[0].get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["display"] == display
    assert sec["display"]["examples"][0]["description"] == "示例A说明"


# ---------------------------------------------------------------------------
# 16. 旧 display（v1 / 缺新增字段）→ 降级 Markdown（不回填猜测值）
# ---------------------------------------------------------------------------


def test_old_v1_and_missing_fields_degrade(api_env):
    from app.core.wiki_skills.api_reference import display as disp_mod
    client, engine = api_env
    b = _built("v1", content="# 老内容")
    # v1：版本不符 → 降级。
    old_v1 = dict(b.display, schema_version="api-section-display/v1")
    assert disp_mod.sanitize_section_display(old_v1) is None
    # v2 但 parameters 缺 type（新增必需字段）→ 降级，不补猜测值。
    missing_type = json.loads(json.dumps(b.display))
    del missing_type["parameters"][0]["type"]
    assert disp_mod.sanitize_section_display(missing_type) is None
    # v2 但 responses 缺 media_types → 降级。
    missing_media = json.loads(json.dumps(b.display))
    missing_media["responses"][0].pop("media_types", None)
    assert disp_mod.sanitize_section_display(missing_media) is None

    # API 读侧：v1 display 落库 → role endpoint、display None，正文可读不 500。
    structure = b.spec.to_dict()
    structure["display"] = old_v1
    _seed_wiki(engine)
    _seed_revision(engine, "w1", "rev1", [
        _row(b, "sec-v1", structure_json=json.dumps(
            structure, ensure_ascii=False, sort_keys=True),
             section_key=b.section_key),
    ])
    _override(ADMIN)
    r = client.get("/api/wiki/w1")
    assert r.status_code == 200
    sec = r.json()["sections"][0]
    assert sec["section_role"] == "endpoint"
    assert sec["display"] is None
    assert sec["content"] == b.content


# ---------------------------------------------------------------------------
# 17. 读取边界：超大 structure_json 直接安全降级，不完整解析
# ---------------------------------------------------------------------------


def test_structure_json_length_guard(monkeypatch):
    from app.core.wiki_skills.api_reference import display as disp_mod

    def build_chars(n: int, ch: str = "x") -> str:
        return ch * n

    # 字符长度超上限（廉价判断先行）。
    role, display = disp_mod.section_api_view(
        build_chars(disp_mod.STRUCTURE_JSON_MAX_CHARS + 1), "# x")
    assert (role, display) == (None, None)

    # 字符未超、但 UTF-8 字节超上限 → 同样安全降级（不解析）。
    wide = build_chars(int(disp_mod.STRUCTURE_JSON_MAX_CHARS * 0.9), "✓")
    assert len(wide) <= disp_mod.STRUCTURE_JSON_MAX_CHARS
    role2, display2 = disp_mod.section_api_view(wide, "# x")
    assert (role2, display2) == (None, None)

    # 正常结构不受影响。
    b = _built("v1", content="# 正常")
    role3, display3 = disp_mod.section_api_view(
        b.structure_json, b.content,
        content_origin="auto", merge_policy="auto",
        locked=False, validation_status="pass")
    assert role3 == "endpoint"
    assert display3 is not None
