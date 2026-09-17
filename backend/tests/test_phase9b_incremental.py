# -*- coding: utf-8 -*-
"""Phase 9B 增量一致性（Agent A）：真实 alembic head 临时库 + 真实 v3 Pipeline。

覆盖 CONTRACT §3 场景 1–6 的增量发布一致性：
- S1 新增 Endpoint（api）：来源 spec 增 /v3/users → manual_rebuild → 新 current
  Revision 出现新 section，原 /v1、/v2 Endpoint section 的 structure/display/
  Binding 不被串改，旧 Revision 原样保留，Manifest 正确，真实图谱重建到新
  Revision（无旧 provenance 残留）；
- S2 修改参数（api）：section_key 不变，该 section content/structure 变化，
  无关 section（overview、/v2/users）structure/content 语义等价；
- S3 删除唯一来源：default wiki（单来源）→ create_page_deleted_run →
  status=archived、source_page_ids=[]、dirty=False、旧 current_revision 仍可查、
  图谱清除失效当前关系；
- S4 删除非唯一来源：api wiki 删 p-hidden → draft/dirty/membership 剩 p-api/旧
  current 不变 → 再 manual_rebuild → 新 revision 无 /v2/audit、旧 revision 保留、
  图谱当前状态无 audit 来源，不影响 ws-sales；
- S5a protected/manual（default）：人工编辑 current summary（manual/protected）
  后再重编译 → 该 section 内容不被合成覆盖，其它 facts 更新；
- S5b protected/manual（api lock 复制语义）：lock 会复制 section 且按实际行为
  section_key→NULL；随后重编译保留 protected section（不静默覆盖）；
- S6 当前 vs 历史 + Evidence 一致性：两次发布后历史 revision/sections 完整
  保留；随后“来源已改但证据 hash 未同步”在发布边界（compile 与 publish 之间被
  外部写入打断）→ publish 前 _reverify_api_publish 检测 EVIDENCE_STALE → run
  failed、零新 Revision、dirty 保持。

== 版本来源待决策阻断记录（CONTRACT §2 结论，本轮不接线）==
勘察确认：项目不存在“明确且已有契约支持的 API version_scope 来源”：
- db_adapter.py:247 恒置 version_scope="" → 归一 "unversioned"；
- parser 不读 info.version；Page/Evidence/导入载荷无 API 版本列；
- info.version 是最接近的结构化候选，但无项目契约规定其应映射 version_scope，
  既有 fixture openapi_users_v1.json/v2.yaml 亦不含 info.version。
→ 本轮不接线、不新增推断规则、不新增 DB 字段；沿用「/v1、/v2 不同路径
Endpoint」展示口径，真实执行链恒 |unversioned（与 phase9a CONTRACT §2/§4.1 同）。

== 已有覆盖 → 本轮缺口（简短表）==
| 覆盖 | 既有测试 | 本轮新断言/缺口 |
|---|---|---|
| 单次 api 发布 + binding + v1/v2 分节 | test_phase9a_integration 场景 B | 增量二次发布的历史保留/无串改、图谱 provenance 指向新 current |
| 单次 default manual_rebuild 发布 | test_wiki_pipeline_7d / test_wiki_skill_default_v3 | 删除语义（unique 归档/多源 draft）经真实 run + 图谱清理 |
| EVIDENCE_STALE/PAGE_STALE 零发布（单测直调 publish） | test_wiki_skill_default_v3 #6-8 | 经真实 executor 全链（调度 → claim → stage 事务）触发并断言零新 Revision |
| protected/manual 复制（api 直调） | test_wiki_skill_default_v3 #12 | default 分支人工编辑后重编译；api 用 HTTP lock 后重编译的“如实”行为 |
| 版本来源隔离 | phase9a B 已书面注明留待 9B | 本轮按 CONTRACT §2 记录阻断，不新增测试 |

门禁：cd backend；python -m pytest tests/test_phase9b_incremental.py -q
"""
from __future__ import annotations

import copy
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

from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_pipeline import executor  # noqa: E402
from app.core.wiki_pipeline import registry as pregs  # noqa: E402
from app.core.wiki_pipeline import worker as wiki_worker  # noqa: E402
from app.core.wiki_pipeline.pipelines import wiki_default as wd  # noqa: E402
from app.core.wiki_pipeline.pipelines.wiki_default import (  # noqa: E402
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    create_page_deleted_run,
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
    V4GraphEntityEvidence,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    init_db,
)
from phase9a import bootstrap_db as bdb  # noqa: E402
from phase9a import fixtures  # noqa: E402
from phase9b_migration.migrate import ALEMBIC_HEAD_EXPECTED

_HEAD_REVISION = ALEMBIC_HEAD_EXPECTED

WIKI_API = fixtures.WIKI_API_ID            # w-eng-api（api_reference）
WIKI_DEFAULT = fixtures.WIKI_DEFAULT_ID    # w-eng-default（default）
WIKI_SALES = fixtures.WIKI_SALES_ID        # w-sales-default
PAGE_DEFAULT = fixtures.PAGE_DEFAULT_ID    # p-default
PAGE_API = fixtures.PAGE_API_ID            # p-api
PAGE_HIDDEN = fixtures.PAGE_HIDDEN_ID      # p-hidden
EV_API = fixtures.EVIDENCE_API_ID          # ev-api
EV_HIDDEN = fixtures.EVIDENCE_HIDDEN_ID    # ev-hidden
WS_ENG = fixtures.WS_ENG_ID
WS_SALES = fixtures.WS_SALES_ID
NB_ENG = fixtures.NB_ENG_ID
NB_HIDDEN = fixtures.NB_HIDDEN_ID

_K_V1 = "api_endpoint|get|/v1/users|unversioned"
_K_V2 = "api_endpoint|get|/v2/users|unversioned"
_K_V3 = "api_endpoint|get|/v3/users|unversioned"
_K_AUDIT = "api_endpoint|get|/v2/audit|unversioned"

_TIMEOUT = 120.0

# 确定性 default LLM 正文里的图谱短语（真实图谱建图可产生 entity+relation）。
_GRAPH_PHRASE = (
    "上位机系统负责采集现场数据，支持连接调试器完成故障诊断，并记录诊断日志文件。"
)


@pytest.fixture(autouse=True)
def _isolate_phase9b_registries():
    """每测试前后清空 pipeline/skill registry、外部 runner、worker、依赖覆盖。"""
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


def _wait_for(fn, description: str, timeout: float = _TIMEOUT, interval: float = 0.2):
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
    """单测试隔离环境（同 test_phase9a_integration.Env：alembic head + seed + 登录）。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = Path(tmp_path)
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "phase9b.db"
        self.db_url = _sqlite_url(self.db_path)
        self.record_file = self.tmp / "records" / "calls.jsonl"
        self.fault_flag = self.tmp / "records" / "fault.flag"
        self._monkeypatch = monkeypatch

        envmap = dict(os.environ)
        envmap["DATABASE_URL"] = self.db_url
        envmap["PYTHONIOENCODING"] = "utf-8"
        heads = bdb.alembic_single_head(bdb._BACKEND, envmap)
        assert [h for h in heads.split("|") if h] == [_HEAD_REVISION], heads
        bdb.alembic_upgrade_head(bdb._BACKEND, envmap)

        self.engine = create_engine(self.db_url,
                                    connect_args={"check_same_thread": False})

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
        self.editor = self._login(fixtures.EDITOR_USERNAME)
        self.reader = self._login(fixtures.READER_USERNAME)

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
    def run_for_wiki(self, wid: str, status: str | None = None) -> CompileRun:
        self.db.expire_all()
        q = (self.db.query(CompileRun)
             .filter(CompileRun.wiki_page_id == wid)
             .order_by(CompileRun.created_at.desc(), CompileRun.id.desc()))
        if status:
            q = q.filter(CompileRun.status == status)
        run = q.first()
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

    def current_revision(self, wid: str) -> WikiRevision:
        w = self.wiki(wid)
        assert w and w.current_revision_id
        rev = self.db.get(WikiRevision, w.current_revision_id)
        assert rev is not None
        return rev

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
                .filter(WikiSectionEvidenceBinding.section_id == section_id).all())
        return {(b.field_path, b.usage_type, b.evidence_id) for b in rows}

    def latest_manifest(self, run_id: str) -> dict:
        rows = (self.db.query(CompileArtifact)
                .filter(CompileArtifact.run_id == run_id,
                        CompileArtifact.artifact_type
                        == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST)
                .order_by(CompileArtifact.created_at.desc()).all())
        assert rows, f"no publish manifest for run {run_id}"
        return json.loads(rows[0].payload_json)

    def graph_prov(self, wid: str) -> tuple[list, list]:
        self.db.expire_all()
        rel = (self.db.query(V4GraphRelationEvidence)
               .filter(V4GraphRelationEvidence.wiki_page_id == wid).all())
        ent = (self.db.query(V4GraphEntityEvidence)
               .filter(V4GraphEntityEvidence.wiki_page_id == wid).all())
        return rel, ent

    def wiki_graph_count(self, wid: str) -> int:
        rel, ent = self.graph_prov(wid)
        return len(rel) + len(ent)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


# ---------------------------------------------------------------------------
# 编译触发与数据变更 helper（真实入口，不预填 Revision/Section/Binding）
# ---------------------------------------------------------------------------


def only_dirty(env: Env, keep_ids) -> None:
    """只保留指定 wiki 的 dirty=True（其余清掉，避免 refresh 把其它 wiki 也入队）。"""
    keep = set(keep_ids)
    for row in env.db.query(WikiPage).all():
        row.dirty = row.id in keep
    env.db.commit()


def refresh_and_drive(env: Env, wid: str) -> CompileRun:
    """真实 POST /api/wiki/refresh-page-dirty → 取该 wiki 最新 queued run → drive。"""
    r = env.c.post("/api/wiki/refresh-page-dirty", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    run = env.run_for_wiki(wid)
    return env.drive_run(run.id)


def assert_run_succeeded(env: Env, wid: str) -> CompileRun:
    run = refresh_and_drive(env, wid)
    assert run.status == "succeeded", (run.safe_error_code, run.safe_error_message)
    w = env.wiki(wid)
    assert w.dirty is False
    assert w.status == "published"
    assert w.current_revision_id is not None
    return run


def api_source_text_with(base_text: str, mutate) -> str:
    """基于原 fixture 文件 spec 深拷贝并按 mutate 修改，返回确定性 JSON 文本。"""
    spec = json.loads(base_text)
    mutate(spec)
    return json.dumps(spec, ensure_ascii=False, sort_keys=False)


def _with_v3_users(base_text: str) -> str:
    def mutate(spec: dict) -> None:
        spec.setdefault("paths", {})["/v3/users"] = {
            "get": {
                "summary": "查询用户列表（v3）",
                "description": "v3 用户列表，分页返回。",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "description": "每页条数", "schema": {"type": "integer",
                                                          "default": 100}},
                ],
                "responses": {
                    "200": {
                        "description": "用户列表",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array",
                                           "items": {"type": "object"}}
                            }
                        },
                    },
                    "401": {"description": "未认证"},
                    "403": {"description": "无权限"},
                },
            }
        }
    return api_source_text_with(base_text, mutate)


def _with_v1_changed_summary_and_param(base_text: str) -> str:
    """改 /v1/users 摘要/描述 + limit 默认值，并新增一个 query 参数（其余路径不动）。"""
    def mutate(spec: dict) -> None:
        op = spec["paths"]["/v1/users"]["get"]
        op["summary"] = "查询用户列表（v1 增强版）"
        op["description"] = op.get("description", "") + " 增加排序能力。"
        params = op.setdefault("parameters", [])
        for p in params:
            if p.get("name") == "limit" and p.get("in") == "query":
                p["description"] = "每页条数（增强版默认 100）"
                p["schema"] = {"type": "integer", "default": 100}
        if not any(p.get("name") == "sort" for p in params):
            params.append({"name": "sort", "in": "query", "required": False,
                           "description": "排序字段（name/created_at）",
                           "schema": {"type": "string"}})
    return api_source_text_with(base_text, mutate)


def sync_api_source(env: Env, page_id: str, evidence_ids, content: str) -> None:
    """改 api 来源 Page 内容并同步该页 Evidence 的 content_hash/source_doc_hash。"""
    page = env.db.get(Page, page_id)
    assert page is not None
    page.content = content
    page.content_hash = fixtures.sha256_hex(content)
    for eid in evidence_ids:
        ev = env.db.get(EvidenceItem, eid)
        assert ev is not None
        ev.content = content
        ev.content_hash = fixtures.sha256_hex(content)
        ev.source_doc_hash = page.content_hash
    env.db.commit()
    env.db.expire_all()


def change_default_page(env: Env, append_text: str) -> None:
    """追加 default wiki 来源页正文（不涉及 Evidence，default 分支不校验证据 hash）。"""
    page = env.db.get(Page, PAGE_DEFAULT)
    assert page is not None
    page.content = (page.content or "") + "\n" + append_text
    page.content_hash = fixtures.sha256_hex(page.content)
    env.db.commit()
    env.db.expire_all()


def make_default_llm(answers: list[dict]) -> tuple:
    """确定性 default LLM runner：按调用序返回 answers（不含版本化产品号）。"""
    idx = [0]

    def _llm(messages, context: str = "", timeout: float = 120.0) -> dict:
        if context == "wiki-synthesis":
            ans = answers[min(idx[0], len(answers) - 1)]
            idx[0] += 1
            return dict(ans)
        if context in ("wiki-ingest-page",):
            return {"worthy": True, "ops": []}
        return {"worthy": True, "ops": []}

    return _llm


def zh_facts(marker: str) -> str:
    """够长、不含语义版本号的中文正文（含图谱短语，纯 ASCII 句点已避免）。"""
    tail = ("。" + marker) * 4
    return (_GRAPH_PHRASE + tail + "系统稳定运行，完成日常维护。") + "内容。" * 60


def install_llm(env: Env, llm) -> None:
    """仅替换 llm_runner（graph_runner 沿用 recorder→真实图谱委托）。"""
    executor.configure_external_runners(
        llm_runner=llm,
        graph_runner=env.recorder.graph_runner(wd._default_graph_runner),
    )


# ---------------------------------------------------------------------------
# S1：新增 Endpoint（api /v3/users）
# ---------------------------------------------------------------------------


def test_b1_api_add_endpoint_v3_new_current_old_intact_graph(env: Env):
    only_dirty(env, [WIKI_API])
    base_spec = fixtures.users_api_text()
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.current_revision(WIKI_API).id
    secs1 = env.sections_by_key(rev1)
    assert _K_V1 in secs1 and _K_V2 in secs1 and _K_AUDIT in secs1
    snap1 = {k: (secs1[k].content, secs1[k].structure_json)
             for k in (_K_V1, _K_V2, _K_AUDIT)}
    binds1 = {k: env.binding_key_set(secs1[k].id)
              for k in (_K_V1, _K_V2, _K_AUDIT)}
    # 图谱先指向 R1。
    _wait_for(lambda: env.wiki_graph_count(WIKI_API) > 0,
              "graph prov for api R1", interval=0.2)
    rel1, ent1 = env.graph_prov(WIKI_API)
    assert {r.revision_id for r in rel1} == {rev1}
    assert {e.revision_id for e in ent1} == {rev1}
    n_sections_r1 = len(env.sections_of(rev1))
    rev1_status = env.db.get(WikiRevision, rev1).status

    # 来源 spec 增加 /v3/users（同步 p-api evidence hash）。
    new_text = _with_v3_users(base_spec)
    sync_api_source(env, PAGE_API, [EV_API], new_text)
    w = env.wiki(WIKI_API)
    w.dirty = True
    env.db.commit()

    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "succeeded", (run2.safe_error_code,
                                        run2.safe_error_message)
    rev2 = env.current_revision(WIKI_API).id
    assert rev2 != rev1

    # 新 current 出现 /v3/users；原 /v1 /v2 /v2/audit section 的 structure/content
    # 不被串改；旧 Revision 原样保留（status 仍 published，自动发布不清 superseded）。
    secs2 = env.sections_by_key(rev2)
    assert _K_V3 in secs2
    for k in (_K_V1, _K_V2, _K_AUDIT):
        assert k in secs2
        assert (secs2[k].content, secs2[k].structure_json) == snap1[k], k
        assert env.binding_key_set(secs2[k].id) == binds1.get(k, set()), k
    v3_sec = secs2[_K_V3]
    assert v3_sec.validation_status == "pass"
    struct3 = json.loads(v3_sec.structure_json or "{}")
    assert struct3["display"]["endpoint"]["path"] == "/v3/users"
    binds_v3 = env.binding_key_set(v3_sec.id)
    assert any(ev == EV_API for (_fp, _ut, ev) in binds_v3)
    assert len(env.sections_of(rev2)) > n_sections_r1
    old_rev = env.db.get(WikiRevision, rev1)
    assert old_rev.status == rev1_status == "published"
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() == n_sections_r1

    w2 = env.wiki(WIKI_API)
    assert w2.dirty is False and w2.status == "published"
    assert json.loads(w2.source_page_ids) == sorted([PAGE_API, PAGE_HIDDEN])

    manifest = env.latest_manifest(run2.id)
    assert manifest["outcome"] == "published"
    assert manifest["wiki_page_ids"] == [WIKI_API]
    assert manifest["revision_ids"] == [rev2]
    assert {"kind": "wiki", "wiki_page_id": WIKI_API} in manifest["graph_targets"]

    # 真实图谱重建到新 Revision：provenance 全指向 rev2，无 rev1 残留。
    _wait_for(lambda: env.wiki_graph_count(WIKI_API) > 0,
              "graph prov for api R2", interval=0.2)
    rel2, ent2 = env.graph_prov(WIKI_API)
    assert {r.revision_id for r in rel2} == {rev2}, "stale relation provenance"
    assert {e.revision_id for e in ent2} == {rev2}, "stale entity provenance"

    # HTTP 只读核对：admin 按 revision_id 取历史/当前。
    cur = env.c.get(f"/api/wiki/{WIKI_API}", headers=env.admin)
    assert cur.status_code == 200
    assert cur.json()["viewing_revision_id"] == rev2
    old_http = env.c.get(f"/api/wiki/{WIKI_API}",
                         params={"revision_id": rev1}, headers=env.admin)
    assert old_http.status_code == 200
    assert old_http.json()["viewing_revision_id"] == rev1
    assert not any(
        (s.get("display") or {}).get("endpoint", {}).get("path") == "/v3/users"
        for s in old_http.json()["sections"])
    assert any(
        (s.get("display") or {}).get("endpoint", {}).get("path") == "/v3/users"
        for s in cur.json()["sections"])


# ---------------------------------------------------------------------------
# S2：修改参数（api /v1/users）——section_key 不变、内容变、无关 section 不变
# ---------------------------------------------------------------------------


def test_b2_api_change_params_section_key_stable_unrelated_unchanged(env: Env):
    only_dirty(env, [WIKI_API])
    base_spec = fixtures.users_api_text()
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.current_revision(WIKI_API).id
    secs1 = env.sections_by_key(rev1)
    assert _K_V1 in secs1 and _K_V2 in secs1
    untouched1 = {k: (secs1[k].content, secs1[k].structure_json,
                      json.loads(secs1[k].structure_json or "{}").get("display"))
                  for k in (_K_V2, "overview")}
    v1_before = (secs1[_K_V1].content, secs1[_K_V1].structure_json)

    new_text = _with_v1_changed_summary_and_param(base_spec)
    sync_api_source(env, PAGE_API, [EV_API], new_text)
    w = env.wiki(WIKI_API)
    w.dirty = True
    env.db.commit()

    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "succeeded", (run2.safe_error_code,
                                        run2.safe_error_message)
    rev2 = env.current_revision(WIKI_API).id
    assert rev2 != rev1
    secs2 = env.sections_by_key(rev2)

    # section_key 不变；该 section 的 content/structure 变化。
    assert _K_V1 in secs1 and _K_V1 in secs2
    assert secs1[_K_V1].section_key == secs2[_K_V1].section_key == _K_V1
    v1_after = (secs2[_K_V1].content, secs2[_K_V1].structure_json)
    assert v1_after != v1_before, "modified section must change"
    assert "增强" in secs2[_K_V1].content or "增强" in secs2[_K_V1].structure_json
    struct_v1 = json.loads(secs2[_K_V1].structure_json)
    param_names = {(p.get("name"), p.get("location")) for p in
                   ((struct_v1.get("display") or {}).get("parameters") or [])}
    assert ("sort", "query") in param_names

    # 无关 section 语义等价（structure 内容等价断言）。
    for k, want in untouched1.items():
        sec = secs2.get(k)
        assert sec is not None, k
        got = (sec.content, sec.structure_json,
               json.loads(sec.structure_json or "{}").get("display"))
        assert got == want, f"unrelated section {k} changed"

    # Binding 仍关联 ev-api；/v1/users 既有字段路径绑定保持一致，仅新增我们故意加
    # 的 sort 查询参数绑定。
    b1 = env.binding_key_set(secs1[_K_V1].id)
    b2 = env.binding_key_set(secs2[_K_V1].id)
    assert b1 and b2 and b1 <= b2
    extra = b2 - b1
    assert extra, "new query parameter must add a binding"
    assert all(fp.startswith("query_parameters.") for fp, _ut, _ev in extra)

    old_rev = env.db.get(WikiRevision, rev1)
    assert old_rev.status == "published"

    # HTTP 只读核对：?revision_id 命中不同内容（新参数 sort 出现在当前，历史无）。
    cur = env.c.get(f"/api/wiki/{WIKI_API}", headers=env.admin).json()
    assert cur["viewing_revision_id"] == rev2
    cur_params = []
    for s in cur["sections"]:
        d = (s.get("display") or {})
        if d.get("endpoint", {}).get("path") == "/v1/users":
            cur_params = d.get("parameters") or []
    assert any(p.get("name") == "sort" for p in cur_params)
    old_http = env.c.get(f"/api/wiki/{WIKI_API}",
                         params={"revision_id": rev1},
                         headers=env.admin).json()
    old_params = []
    for s in old_http["sections"]:
        d = (s.get("display") or {})
        if d.get("endpoint", {}).get("path") == "/v1/users":
            old_params = d.get("parameters") or []
    assert not any(p.get("name") == "sort" for p in old_params)


# ---------------------------------------------------------------------------
# S3：删除唯一来源（default wiki，单来源 → archived + 图谱清理）
# ---------------------------------------------------------------------------


def test_b3_default_delete_only_source_archived_graph_cleared(env: Env):
    only_dirty(env, [WIKI_DEFAULT])
    # 确定性 default LLM（正文含图谱短语 → 编译后真实图谱产生 provenance）。
    install_llm(env, make_default_llm([
        {"summary": "Phase9B 使用说明摘要甲", "content": zh_facts("甲")},
    ]))
    assert_run_succeeded(env, WIKI_DEFAULT)
    rev1 = env.current_revision(WIKI_DEFAULT).id
    _wait_for(lambda: env.wiki_graph_count(WIKI_DEFAULT) > 0,
              "graph prov for default R1", interval=0.2)
    rel1, ent1 = env.graph_prov(WIKI_DEFAULT)
    assert rel1 and ent1
    assert {r.revision_id for r in rel1} == {rev1}

    # 真实删除 run：唯一来源 p-default。
    run = create_page_deleted_run(env.db, page_id=PAGE_DEFAULT,
                                  workspace_id=WS_ENG, notebook_id=NB_ENG)
    env.db.commit()
    run_id = run.id
    executed = env.drive_run(run_id)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)

    w = env.wiki(WIKI_DEFAULT)
    assert w.status == "archived"
    assert json.loads(w.source_page_ids) == []
    assert w.dirty is False
    # 旧 current revision 仍在 DB（可查）但 wiki 不再 published。
    assert w.current_revision_id == rev1
    old_rev = env.db.get(WikiRevision, rev1)
    assert old_rev is not None and old_rev.wiki_page_id == WIKI_DEFAULT
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() >= 2

    # Manifest：page_remove + 受影响 wiki 目标。
    manifest = env.latest_manifest(executed.id)
    assert {"kind": "page_remove", "page_id": PAGE_DEFAULT} in \
        manifest["graph_targets"]
    assert any(g["kind"] == "wiki" and g["wiki_page_id"] == WIKI_DEFAULT
               for g in manifest["graph_targets"])

    # 图谱清除该 wiki 失效当前关系。
    _wait_for(lambda: env.wiki_graph_count(WIKI_DEFAULT) == 0,
              "default wiki graph cleared", interval=0.2)
    rel2, ent2 = env.graph_prov(WIKI_DEFAULT)
    assert rel2 == [] and ent2 == []

    # HTTP 只读：reader 404（archived 不再 published 展示），admin 可查历史 revision。
    assert env.c.get(f"/api/wiki/{WIKI_DEFAULT}",
                     headers=env.reader).status_code == 404
    hist = env.c.get(f"/api/wiki/{WIKI_DEFAULT}",
                     params={"revision_id": rev1}, headers=env.admin)
    assert hist.status_code == 200
    assert hist.json()["status"] == "archived"
    assert hist.json()["viewing_revision_id"] == rev1
    assert hist.json()["sections"], "old revision sections still retrievable"


# ---------------------------------------------------------------------------
# S4：删除非唯一来源（api wiki 删 p-hidden → draft+dirty → rebuild 无 audit）
# ---------------------------------------------------------------------------


def test_b4_api_delete_non_unique_source_then_rebuild_without_audit(env: Env):
    only_dirty(env, [WIKI_API])
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.current_revision(WIKI_API).id
    secs1 = env.sections_by_key(rev1)
    assert _K_AUDIT in secs1
    audit_sec1 = secs1[_K_AUDIT]
    audit_binds1 = env.binding_key_set(audit_sec1.id)
    assert audit_binds1

    # 删除非唯一来源 p-hidden（真实 page_deleted run）。
    run = create_page_deleted_run(env.db, page_id=PAGE_HIDDEN,
                                  workspace_id=WS_ENG, notebook_id=NB_HIDDEN)
    env.db.commit()
    executed = env.drive_run(run.id)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    w = env.wiki(WIKI_API)
    assert w.status == "draft"          # 非唯一来源：置 draft + dirty
    assert w.dirty is True
    assert json.loads(w.source_page_ids) == [PAGE_API]
    assert w.current_revision_id == rev1  # 旧 current_revision 不变
    assert len(env.revisions(WIKI_API)) == 1
    # 不影响 sales wiki。
    ws = env.db.get(WikiPage, WIKI_SALES)
    assert ws is not None and json.loads(ws.source_page_ids) == [
        fixtures.PAGE_SALES_ID]

    # 随后 manual_rebuild → 新 revision 不再含 /v2/audit section。
    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "succeeded", (run2.safe_error_code,
                                        run2.safe_error_message)
    rev2 = env.current_revision(WIKI_API).id
    assert rev2 != rev1
    w2 = env.wiki(WIKI_API)
    assert w2.status == "published" and w2.dirty is False
    assert json.loads(w2.source_page_ids) == [PAGE_API]
    secs2 = env.sections_by_key(rev2)
    assert _K_AUDIT not in secs2
    assert _K_V1 in secs2 and _K_V2 in secs2

    # 旧 revision 仍保留 audit section（含 binding）。
    old_secs = env.sections_by_key(rev1)
    assert _K_AUDIT in old_secs
    assert env.binding_key_set(old_secs[_K_AUDIT].id) == audit_binds1
    assert env.db.get(WikiRevision, rev1).status == "published"

    # 图谱当前状态：provenance 只指向 rev2；无 rev1 残留（旧 audit 关系随之失效）。
    _wait_for(lambda: env.wiki_graph_count(WIKI_API) > 0,
              "graph prov for api after audit removal", interval=0.2)
    rel2, ent2 = env.graph_prov(WIKI_API)
    assert {r.revision_id for r in rel2} == {rev2}
    assert {e.revision_id for e in ent2} == {rev2}

    # HTTP 只读核对：历史 revision 有 audit，当前没有。
    old_http = env.c.get(f"/api/wiki/{WIKI_API}",
                         params={"revision_id": rev1}, headers=env.admin)
    assert old_http.status_code == 200
    assert any(_K_AUDIT.split("|")[-2] == (s.get("display") or {}).get(
        "endpoint", {}).get("path") for s in old_http.json()["sections"]
               if (s.get("display") or {}).get("endpoint", {}).get("path"))
    cur = env.c.get(f"/api/wiki/{WIKI_API}", headers=env.reader)
    assert cur.status_code == 200
    assert cur.json()["viewing_revision_id"] == rev2
    cur_paths = [(s.get("display") or {}).get("endpoint", {}).get("path")
                 for s in cur.json()["sections"]]
    assert "/v2/audit" not in [p for p in cur_paths if p]


# ---------------------------------------------------------------------------
# S5a：protected/manual（default）——人工编辑 summary 后重编译不被合成覆盖
# ---------------------------------------------------------------------------


def test_b5_default_manual_section_preserved_on_recompile(env: Env):
    from app.core.knowledge_compiler_v3 import graph_refresh_scheduler as grs

    only_dirty(env, [WIKI_DEFAULT])
    answers = [
        {"summary": "自动摘要一", "content": zh_facts("第一版")},
        {"summary": "自动摘要二", "content": zh_facts("第二版")},
    ]
    install_llm(env, make_default_llm(answers))
    assert_run_succeeded(env, WIKI_DEFAULT)
    rev1 = env.current_revision(WIKI_DEFAULT).id
    secs1 = env.sections_by_key(rev1)
    summary1 = next(s for s in env.sections_of(rev1)
                    if s.section_type == "summary")
    facts1 = next(s for s in env.sections_of(rev1)
                  if s.section_type == "facts")
    assert summary1.content == "自动摘要一"
    assert facts1.content == zh_facts("第一版")

    # 人工编辑 current summary（真实 HTTP PATCH，走 edit_wiki_section_current）。
    manual = "人工修订后的摘要：此处不允许被自动刷新覆盖。"
    patch = env.c.patch(
        f"/api/wiki/{WIKI_DEFAULT}/revisions/{rev1}/sections/{summary1.id}",
        json={"content": manual}, headers=env.admin)
    assert patch.status_code == 200, patch.text
    grs.shutdown()  # 排干 PATCH 触发的异步图谱线程，保证后续断言确定
    env.db.expire_all()
    rev2 = env.current_revision(WIKI_DEFAULT).id
    assert rev2 != rev1
    secs2 = env.sections_by_key(rev2)
    m2 = next(s for s in env.sections_of(rev2) if s.section_type == "summary")
    assert m2.content == manual
    assert m2.content_origin == "manual"
    assert m2.merge_policy == "protected"
    assert m2.locked is True

    # 改来源内容再重编译（真实 refresh + drive）→ 新 revision。
    change_default_page(env, "补充描述：第二版正文由新来源驱动。")
    w = env.wiki(WIKI_DEFAULT)
    w.dirty = True
    env.db.commit()
    run3 = refresh_and_drive(env, WIKI_DEFAULT)
    assert run3.status == "succeeded", (run3.safe_error_code,
                                        run3.safe_error_message)
    env.db.expire_all()
    rev3 = env.current_revision(WIKI_DEFAULT).id
    assert rev3 != rev2 and rev3 != rev1

    # 该 section 内容不被合成覆盖；其它 facts 更新。
    secs3 = env.sections_of(rev3)
    m3 = next(s for s in secs3 if s.section_type == "summary")
    f3 = next(s for s in secs3 if s.section_type == "facts")
    assert m3.content == manual, "manual summary must not be overwritten"
    assert m3.content_origin == "manual" and m3.merge_policy == "protected"
    assert f3.content == zh_facts("第二版")
    assert f3.content != zh_facts("第一版")
    assert m3.content != "自动摘要二"

    # 历史完整保留（R1/R2 均在）。
    revs = [r.id for r in env.revisions(WIKI_DEFAULT)]
    assert rev1 in revs and rev2 in revs and rev3 in revs
    assert env.db.get(WikiRevision, rev1).status == "published"
    assert env.db.get(WikiRevision, rev2).status == "published"

    # HTTP 只读：当前 current 的摘要仍是人工内容。
    cur = env.c.get(f"/api/wiki/{WIKI_DEFAULT}", headers=env.reader)
    assert cur.status_code == 200
    cur_secs = cur.json()["sections"]
    sm = next(s for s in cur_secs if s["section_type"] == "summary")
    assert sm["content"] == manual
    assert sm["merge_policy"] == "protected"
    # HTTP 历史 R1 的摘要仍是自动摘要一。
    h1 = env.c.get(f"/api/wiki/{WIKI_DEFAULT}",
                   params={"revision_id": rev1}, headers=env.admin)
    assert h1.status_code == 200
    s1 = next(s for s in h1.json()["sections"] if s["section_type"] == "summary")
    assert s1["content"] == "自动摘要一"


# ---------------------------------------------------------------------------
# S5b：api lock 复制语义（HTTP lock → 复制 Revision；随后重编译保留 protected）
# ---------------------------------------------------------------------------


def test_b5b_api_lock_copy_semantics_retained_on_recompile(env: Env):
    only_dirty(env, [WIKI_API])
    base_spec = fixtures.users_api_text()
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.current_revision(WIKI_API).id
    secs1 = env.sections_by_key(rev1)
    target = secs1[_K_V1]
    protected_content1 = target.content
    assert protected_content1

    # 真实 HTTP lock：复制当前 Revision → 目标 section manual+protected+locked。
    lock = env.c.post(
        f"/api/wiki/{WIKI_API}/revisions/{rev1}/sections/{target.id}/lock",
        headers=env.admin)
    assert lock.status_code == 200, lock.text
    env.db.expire_all()
    rev2 = env.current_revision(WIKI_API).id
    assert rev2 != rev1
    m2s = [s for s in env.sections_of(rev2) if s.merge_policy == "protected"]
    assert m2s, "lock must produce a protected section"
    prot2 = m2s[0]
    assert prot2.content == protected_content1
    assert prot2.content_origin == "manual" and prot2.locked is True
    # 如实行为：lock 复制会置 section_key→NULL（lifecycle 复制不携带 section_key）。
    assert prot2.section_key is None
    # 旧 revision 原样保留（未静默覆盖）。
    assert env.db.get(WikiSection, target.id) is not None
    assert env.db.get(WikiSection, target.id).content == protected_content1

    # 来源变化（改变 /v1/users 摘要）→ 重编译：protected section 保留不覆盖。
    new_text = _with_v1_changed_summary_and_param(base_spec)
    sync_api_source(env, PAGE_API, [EV_API], new_text)
    w = env.wiki(WIKI_API)
    w.dirty = True
    env.db.commit()
    run3 = refresh_and_drive(env, WIKI_API)
    assert run3.status == "succeeded", (run3.safe_error_code,
                                        run3.safe_error_message)
    env.db.expire_all()
    rev3 = env.current_revision(WIKI_API).id
    assert rev3 != rev1 and rev3 != rev2
    m3s = [s for s in env.sections_of(rev3)
           if s.merge_policy == "protected" and s.content_origin == "manual"]
    assert m3s, "recompile must keep the protected/manual section"
    assert any(s.content == protected_content1 for s in m3s), \
        "protected content must not be silently overwritten"
    # 自动 /v1/users section 已被新来源更新（内容变化）。
    auto_v1 = next(s for s in env.sections_of(rev3)
                   if s.section_key == _K_V1 and s.merge_policy == "auto")
    assert auto_v1.content != protected_content1
    # 历史完整。
    rev_ids = {r.id for r in env.revisions(WIKI_API)}
    assert {rev1, rev2, rev3} <= rev_ids
    assert env.db.get(WikiRevision, rev1).status == "published"
    assert env.db.get(WikiRevision, rev2).status == "published"

    # HTTP 只读：历史 revision（rev2）的 protected 内容仍可读；当前 revision 也
    # 保留了 protected（manual）正文。
    h2 = env.c.get(f"/api/wiki/{WIKI_API}",
                   params={"revision_id": rev2}, headers=env.admin)
    assert h2.status_code == 200
    assert any(s["content_origin"] == "manual" and s["merge_policy"] == "protected"
               for s in h2.json()["sections"])
    cur = env.c.get(f"/api/wiki/{WIKI_API}", headers=env.reader)
    assert cur.status_code == 200
    assert any(s["merge_policy"] == "protected" for s in cur.json()["sections"])


# ---------------------------------------------------------------------------
# S6：当前 vs 历史 + Evidence 一致性（两次发布历史完整；stale → 零发布）
# ---------------------------------------------------------------------------


def test_b6_history_preserved_and_stale_evidence_zero_publish(env: Env,
                                                              monkeypatch):
    import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod

    only_dirty(env, [WIKI_API])
    base_spec = fixtures.users_api_text()
    # 第 1 次发布。
    assert_run_succeeded(env, WIKI_API)
    rev1 = env.current_revision(WIKI_API).id
    n_secs1 = len(env.sections_of(rev1))
    secs1 = env.sections_by_key(rev1)
    snap1 = {k: (secs1[k].content, secs1[k].structure_json)
             for k in (_K_V1, _K_V2, _K_AUDIT)}

    # 第 2 次发布（历史完整保留断言）。
    text2 = _with_v3_users(base_spec)
    sync_api_source(env, PAGE_API, [EV_API], text2)
    w = env.wiki(WIKI_API)
    w.dirty = True
    env.db.commit()
    run2 = refresh_and_drive(env, WIKI_API)
    assert run2.status == "succeeded", (run2.safe_error_code,
                                        run2.safe_error_message)
    rev2 = env.current_revision(WIKI_API).id
    assert rev2 != rev1
    assert len(env.revisions(WIKI_API)) == 2
    secs2_cur = env.sections_by_key(rev2)
    assert _K_V3 in secs2_cur
    for k, want in snap1.items():
        assert (secs2_cur[k].content, secs2_cur[k].structure_json) == want
    # 历史 R1 sections 完整保留。
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() == n_secs1
    assert env.db.get(WikiRevision, rev1).status == "published"

    # 第 3 次：来源再改但“证据 hash 未同步”会在发布边界被复验拦截。先同步使编译
    # 通过，再在 synthesize→publish 之间让外部写入打断（真实竞态窗口）。
    text3 = _with_v1_changed_summary_and_param(base_spec)
    sync_api_source(env, PAGE_API, [EV_API], text3)
    w3 = env.wiki(WIKI_API)
    w3.dirty = True
    env.db.commit()
    # 先调度（真实 HTTP 入口）产生 queued run。
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

    # 竞态窗口内：另一写入者把 ev-api 证据 hash 改回“未同步”状态。
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
    assert r3.safe_error_code in ("EVIDENCE_STALE", "PAGE_STALE"), \
        (r3.safe_error_code, r3.safe_error_message)

    # 零新 Revision；当前仍是 rev2；历史完整；dirty 保持 True。
    assert env.db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == WIKI_API).count() == 2
    w_final = env.wiki(WIKI_API)
    assert w_final.current_revision_id == rev2
    assert w_final.dirty is True
    assert w_final.status == "published"
    assert env.db.query(WikiSection).filter(
        WikiSection.revision_id == rev1).count() == n_secs1
    assert env.db.get(WikiRevision, rev1).status == "published"

    # 无 Manifest（未发布）。
    n_manifest = (env.db.query(CompileArtifact)
                  .filter(CompileArtifact.run_id == run3_id,
                          CompileArtifact.artifact_type
                          == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).count())
    assert n_manifest == 0

    # HTTP 只读：revision 列表含两条；GET wiki 当前仍 rev2 且 /v1/v2 section 在。
    rev_list = env.c.get(f"/api/wiki/{WIKI_API}/revisions",
                         headers=env.admin)
    assert rev_list.status_code == 200
    ids = {x["id"] for x in rev_list.json()["revisions"]}
    assert {rev1, rev2} <= ids
    cur = env.c.get(f"/api/wiki/{WIKI_API}", headers=env.reader)
    assert cur.status_code == 200
    assert cur.json()["viewing_revision_id"] == rev2
