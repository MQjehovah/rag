# -*- coding: utf-8 -*-
"""Phase 9A 集成测试：真实 alembic head 临时库 + TestClient 真实路由 + 真实 v3 pipeline。

覆盖 CONTRACT §4 全部后端断言：
- A：refresh-page-dirty 正式入口建 queued run → executor 真实 v3 执行到终态
  （w-eng-default/w-eng-api/w-sales-default succeeded；w-fail fault 开首轮 failed，
  关 flag 后真实 HTTP retry → succeeded）；每步刷新 DB 断言
  Revision/Section/Binding/WikiPage current_revision_id/dirty/Manifest 落库；
- B：api wiki「不同路径 Endpoint 展示」——/v1 与 /v2 路径分别成节（section_key 含
  /v1、/v2 路径），validation pass / structure display / binding 行与 evidence 关联。
  说明：真实 executor 链路（db_adapter）当前将 ApiSourceDocument.version_scope 恒置为
  "" → 归一 "unversioned"，故「同一 Endpoint 的 version_scope 隔离」在真实执行链上
  尚未被本链路验证，不当作已覆盖；不同路径 Endpoint 展示与 version 相关能力留待 9B
  按原计划审定，本轮不新增版本推断规则；
- C：真实图谱（graph_rec 委托 _default_graph_runner）后 V4GraphEntity/V4GraphRelation
  含目标 wiki 相关行，并经 /api/v4/graph/subgraph 返回相关节点；
- D：权限/隔离全矩阵（三个真实 token 走生产鉴权链路）；
- E：cancel 语义（block marker 可控阻塞 + queued 立即取消）；
- F：调用记录断言（record JSONL：default 至少一次 wiki-synthesis；graph 含 wiki 目标）。

门禁：仅跑本文件（cd backend；python -m pytest tests/test_phase9a_integration.py -q）。
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

from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_pipeline import executor  # noqa: E402
from app.core.wiki_pipeline import registry as pregs  # noqa: E402
from app.core.wiki_pipeline import worker as wiki_worker  # noqa: E402
from app.core.wiki_pipeline.pipelines import wiki_default as wd  # noqa: E402
from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline  # noqa: E402
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
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    User,
    UserGroup,
    V4GraphEntity,
    V4GraphEntityEvidence,
    V4GraphRelation,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    WikiWorkspace,
    init_db,
)
from phase9a import bootstrap_db as bdb  # noqa: E402
from phase9a import fixtures  # noqa: E402

_P44_REVISION = "a9b8c7d6e5f4"
_WIKI_IDS = list(fixtures.WIKI_OK_IDS) + [fixtures.WIKI_FAIL_ID]
# 默认 wiki 的 revision sections（v1 _append_revision 产 summary + facts）。
_WIKI_DEFAULT_OK = (fixtures.WIKI_DEFAULT_ID, fixtures.WIKI_SALES_ID)

# 附加的 cancel/block 测试对象（仅测试本地创建，不进 seed）。
_BLOCK_WIKI = "w-block"
_BLOCK_PAGE = "p-block"
_QUEUED_WIKI = "w-queued"
_QUEUED_PAGE = "p-queued"

_TIMEOUT = 90.0


@pytest.fixture(autouse=True)
def _isolate_phase9a_registries():
    """每个测试前后清空 pipeline/skill registry 与外部 runner。"""
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()
    app.dependency_overrides.clear()
    yield
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
    """单个测试的隔离环境句柄。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = Path(tmp_path)
        self.db_dir = self.tmp / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "phase9a.db"
        self.db_url = _sqlite_url(self.db_path)
        self.record_file = self.tmp / "records" / "calls.jsonl"
        self.fault_flag = self.tmp / "records" / "fault.flag"
        self._monkeypatch = monkeypatch

        # 1) 真实 alembic upgrade head（子进程，cwd=backend，env DATABASE_URL 注入）。
        envmap = dict(os.environ)
        envmap["DATABASE_URL"] = self.db_url
        envmap["PYTHONIOENCODING"] = "utf-8"
        heads = bdb.alembic_single_head(bdb._BACKEND, envmap)
        assert [h for h in heads.split("|") if h] == [_P44_REVISION], heads
        bdb.alembic_upgrade_head(bdb._BACKEND, envmap)

        # 2) 连接（pragma 与生产 sqlite 对齐）。
        self.engine = create_engine(self.db_url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA busy_timeout=8000")

        init_db(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # 3) 进程配置（scheduler / graph / kill-switch 全部读 settings）。
        monkeypatch.setattr(settings, "database_url", self.db_url)
        monkeypatch.setattr(settings, "wiki_pipeline_active_version", "3")
        monkeypatch.setattr(settings, "wiki_topic_enabled", True)
        monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
        monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
        monkeypatch.setattr(deps, "_engine", self.engine)

        # 4) bootstrap（builtin skills + v1/v2/v3 + active=3）。
        skill_service.register_builtin_skills()
        register_default_pipeline()
        register_default_pipeline_v2()
        register_default_pipeline_v3()
        pregs.set_active_version("wiki.default", "3")

        # 5) seed（ORM 直插，fixtures 允许）。
        self.db = self.Session()
        fixtures.apply_seed(self.db)
        self.db.commit()

        # 6) recorder + runner 注入。
        self.recorder = fixtures.Recorder(self.record_file, self.fault_flag,
                                          block_timeout=60.0)
        executor.configure_external_runners(
            llm_runner=self.recorder.llm_runner(),
            graph_runner=self.recorder.graph_runner(wd._default_graph_runner),
        )

        # 7) TestClient（不进 with → 不触发 lifespan；测试自行 bootstrap 已完成）。
        self.c = TestClient(app)

        # 8) 三个真实用户登录（走生产鉴权）。
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

    def set_fault(self, present: bool) -> None:
        if present:
            self.fault_flag.parent.mkdir(parents=True, exist_ok=True)
            self.fault_flag.write_text("", encoding="utf-8")
        else:
            if self.fault_flag.exists():
                self.fault_flag.unlink()

    def close(self) -> None:
        try:
            self.app_cleanup()
        finally:
            self.db.close()
            self.engine.dispose()

    def app_cleanup(self) -> None:
        app.dependency_overrides.clear()

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
        self.db.expire_all()  # 丢弃身份映射中的 stale 行（其它 session/engine 可能已改状态）
        executor.execute_run(self.db, run_id)
        self.db.expire_all()
        run = self.db.get(CompileRun, run_id)
        assert run is not None
        return run

    def wiki(self, wid: str) -> WikiPage:
        self.db.expire_all()
        return self.db.get(WikiPage, wid)

    def current_sections(self, wid: str) -> list[WikiSection]:
        w = self.wiki(wid)
        assert w and w.current_revision_id
        return (self.db.query(WikiSection)
                .filter(WikiSection.revision_id == w.current_revision_id)
                .order_by(WikiSection.order_index, WikiSection.id).all())

    def current_revision(self, wid: str) -> WikiRevision:
        w = self.wiki(wid)
        assert w and w.current_revision_id
        rev = self.db.get(WikiRevision, w.current_revision_id)
        assert rev is not None
        return rev


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


# ---------------------------------------------------------------------------
# 编译触发与执行（正式入口 refresh-page-dirty → 真实 execute_run）
# ---------------------------------------------------------------------------


def refresh_and_collect_runs(env: Env, wiki_ids=None) -> dict[str, CompileRun]:
    """以 admin 身份调 POST /api/wiki/refresh-page-dirty，返回每 wiki 最新 queued run。"""
    wiki_ids = list(wiki_ids or _WIKI_IDS)
    r = env.c.post("/api/wiki/refresh-page-dirty", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    assert body["submitted"] >= len(wiki_ids)
    found: dict[str, CompileRun] = {}
    for wid in wiki_ids:
        found[wid] = env.run_for_wiki(wid)
    return found


def run_default_ok(env: Env) -> None:
    """对 default/sales 两条 wiki 执行并断言 succeeded + 落库。"""
    runs = refresh_and_collect_runs(env, wiki_ids=list(_WIKI_DEFAULT_OK))
    for wid in _WIKI_DEFAULT_OK:
        run = env.drive_run(runs[wid].id)
        assert run.status == "succeeded", (
            wid, run.safe_error_code, run.safe_error_message)
        w = env.wiki(wid)
        assert w.dirty is False
        assert w.current_revision_id is not None
        assert w.status == "published"
        assert env.current_sections(wid), f"{wid} has no sections"


def _drive_run_until_terminal(env: Env, run_id: str, expected: str) -> CompileRun:
    run = env.drive_run(run_id)
    assert run.status == expected, (run.status, run.safe_error_code,
                                    run.safe_error_message)
    return run


# ---------------------------------------------------------------------------
# 冒烟：alembic 单 head + seed + 登录角色
# ---------------------------------------------------------------------------


def test_smoke_alembic_seed_and_login(env: Env):
    # 真实 alembic head 已被 Env 夹具校验；这里再核对 version 表与 ORM 齐全。
    from sqlalchemy import text as sa_text

    row = env.db.execute(sa_text("SELECT version_num FROM alembic_version")).fetchone()
    assert row[0] == _P44_REVISION
    env.db.expire_all()
    assert env.db.query(User).count() == 3
    assert env.db.query(UserGroup).count() == sum(len(g) for g in fixtures.USERS.values())
    assert env.db.query(WikiWorkspace).count() == 2
    assert env.db.query(NotebookWorkspaceBinding).count() == 3
    assert env.db.query(Page).count() == 5
    assert env.db.query(EvidenceItem).count() == 2
    assert env.db.query(WikiPage).count() == 4
    for wid in _WIKI_IDS:
        w = env.wiki(wid)
        assert w.dirty is True and w.current_revision_id is None

    # 无 token → 401。
    assert env.c.get("/api/wiki").status_code == 401
    # 生产 auth：/me 返回角色。
    me = env.c.get("/api/auth/me", headers=env.admin).json()
    assert me["is_admin"] is True
    ed = env.c.get("/api/auth/me", headers=env.editor).json()
    assert ed["is_admin"] is False and ed["is_wiki_editor"] is True
    assert set(ed["groups"]) == {"eng", "editors"}
    rd = env.c.get("/api/auth/me", headers=env.reader).json()
    assert rd["is_admin"] is False and rd["is_wiki_editor"] is False


# ---------------------------------------------------------------------------
# A：编译成功/失败/重试 + Manifest + 调用记录（F）
# ---------------------------------------------------------------------------


def test_compile_fail_and_retry_with_records(env: Env):
    env.set_fault(True)
    runs = refresh_and_collect_runs(env)

    # 三个 OK wiki → succeeded；w-fail → failed（fault flag 存在 + marker）。
    for wid in fixtures.WIKI_OK_IDS:
        run = env.drive_run(runs[wid].id)
        assert run.status == "succeeded", (wid, run.safe_error_code,
                                           run.safe_error_message)
        w = env.wiki(wid)
        assert w.dirty is False
        assert w.current_revision_id is not None
        assert w.status == "published"

    fail_run = env.drive_run(runs[fixtures.WIKI_FAIL_ID].id)
    assert fail_run.status == "failed", (fail_run.safe_error_code,
                                         fail_run.safe_error_message)
    wfail = env.wiki(fixtures.WIKI_FAIL_ID)
    assert wfail.current_revision_id is None  # 旧 current 不变（无发布）
    assert wfail.dirty is True

    # Manifest / Stage 时间线真实。
    manifest = (env.db.query(CompileArtifact)
                .filter(CompileArtifact.run_id == fail_run.id,
                        CompileArtifact.artifact_type
                        == wd.ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).all())
    assert manifest, "failed run must persist publish manifest"
    stages = (env.db.query(StageRun)
              .filter(StageRun.run_id == fail_run.id).all())
    assert stages and any(s.status == "failed" for s in stages)
    ok_run = env.run_for_wiki(fixtures.WIKI_DEFAULT_ID, status="succeeded")
    ok_stages = (env.db.query(StageRun)
                 .filter(StageRun.run_id == ok_run.id).all())
    assert {s.status for s in ok_stages} <= {"succeeded", "skipped"}
    assert all(s.status == "succeeded" for s in ok_stages)

    # 关 fault flag → 真实 HTTP retry → succeeded。
    env.set_fault(False)
    retry = env.c.post(f"/api/wiki-compile/runs/{fail_run.id}/retry",
                       headers=env.admin)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "queued"
    retried = env.drive_run(fail_run.id)
    assert retried.status == "succeeded", (retried.safe_error_code,
                                           retried.safe_error_message)
    wfail2 = env.wiki(fixtures.WIKI_FAIL_ID)
    assert wfail2.current_revision_id is not None
    assert wfail2.dirty is False
    rev = env.db.get(WikiRevision, wfail2.current_revision_id)
    assert rev is not None and rev.status == "published"

    # 成功 wiki 的 Manifest 落库 + graph_targets。
    for wid in list(fixtures.WIKI_OK_IDS) + [fixtures.WIKI_FAIL_ID]:
        w = env.wiki(wid)
        mrows = (env.db.query(CompileArtifact)
                 .filter(CompileArtifact.run_id == env.run_for_wiki(wid).id,
                         CompileArtifact.artifact_type
                         == wd.ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST)
                 .order_by(CompileArtifact.created_at.desc()).all())
        assert mrows
        latest = json.loads(mrows[0].payload_json)
        assert latest["outcome"] == "published"
        assert any(g.get("kind") == "wiki" and g.get("wiki_page_id") == wid
                   for g in latest["graph_targets"])

    # F：调用记录断言。
    records = fixtures.load_records(env.record_file)
    llm = [r for r in records if r.get("kind") == "llm"]
    graph = [r for r in records if r.get("kind") == "graph"]
    assert llm, "must record llm calls"
    ctxs = {r.get("context") for r in llm}
    assert "wiki-synthesis" in ctxs, "default wiki must hit wiki-synthesis"
    assert all("content" not in r for r in records)
    assert all(r.get("prompt_len", -1) >= 0 for r in llm)
    graph_wikis = {g.get("wiki_page_id") for g in graph}
    assert fixtures.WIKI_API_ID in graph_wikis  # graph 记录含 wiki 目标
    # 无 api-reference-compile 调用（OpenAPI 源不调 LLM）。
    assert "api-reference-compile" not in ctxs


# ---------------------------------------------------------------------------
# B：api wiki「不同路径 Endpoint 展示」（/v1、/v2 路径）+ binding / evidence 关联
#    （非「同一 Endpoint 的 version_scope 隔离」——真实链路恒 unversioned，见文件头）
# ---------------------------------------------------------------------------


def test_api_wiki_distinct_path_endpoint_sections_and_binding(env: Env):
    runs = refresh_and_collect_runs(env, wiki_ids=[fixtures.WIKI_API_ID])
    run = env.drive_run(runs[fixtures.WIKI_API_ID].id)
    assert run.status == "succeeded", (run.safe_error_code, run.safe_error_message)
    w = env.wiki(fixtures.WIKI_API_ID)
    assert w.current_revision_id is not None and w.dirty is False

    sections = env.current_sections(fixtures.WIKI_API_ID)
    by_key = {s.section_key: s for s in sections if s.section_key}
    assert "api_endpoint|get|/v1/users|unversioned" in by_key
    assert "api_endpoint|get|/v2/users|unversioned" in by_key
    for key in ("api_endpoint|get|/v1/users|unversioned",
                "api_endpoint|get|/v2/users|unversioned"):
        sec = by_key[key]
        assert sec.skill_key == "api_reference"
        assert sec.skill_version == "1"
        assert sec.validation_status == "pass"
        struct = json.loads(sec.structure_json or "{}")
        assert struct["section_key"] == key
        display = struct.get("display")
        assert display, f"endpoint section must carry display: {key}"
        assert display["endpoint"]["method"] == "GET"
        assert display["endpoint"]["path"] in (key.split("|")[2], )
        # 投影白名单：parameters / responses.media_types / error_codes。
        params = display.get("parameters") or []
        assert any(p.get("name") == "limit" and p.get("location") == "query"
                   for p in params)
        assert display["endpoint"]["path"].startswith("/v")
        media = [r.get("media_types") or [] for r in (display.get("responses") or [])]
        assert any(any(x.get("media_type") == "application/json" for x in m)
                   for m in media)
        assert any(e.get("code") in ("400", "401", "403", "404", "500")
                   for e in (display.get("error_codes") or []))

    # Binding 行存在且 evidence 关联正确：audit section → ev-hidden；users section → ev-api。
    audit_sec = next((s for s in sections
                      if s.section_key == "api_endpoint|get|/v2/audit|unversioned"), None)
    assert audit_sec is not None, "w-eng-api must publish /v2/audit (from p-hidden)"
    audit_binds = (env.db.query(WikiSectionEvidenceBinding)
                   .filter(WikiSectionEvidenceBinding.section_id == audit_sec.id).all())
    assert audit_binds and {b.evidence_id for b in audit_binds} == {fixtures.EVIDENCE_HIDDEN_ID}

    v1_sec = by_key["api_endpoint|get|/v1/users|unversioned"]
    v1_binds = (env.db.query(WikiSectionEvidenceBinding)
                .filter(WikiSectionEvidenceBinding.section_id == v1_sec.id).all())
    ev_ids = {b.evidence_id for b in v1_binds}
    assert fixtures.EVIDENCE_API_ID in ev_ids
    assert all(len(b.evidence_content_hash) == 64 for b in v1_binds)

    # §4.2.2：reader 对 /v2/audit section → 200 total=0（隐藏来源不计）；admin → total>=1。
    path = (f"/api/wiki/{fixtures.WIKI_API_ID}/revisions/{w.current_revision_id}"
            f"/sections/{audit_sec.id}/evidence")
    r_reader = env.c.get(path, headers=env.reader)
    assert r_reader.status_code == 200, r_reader.text
    assert r_reader.json()["total"] == 0 and r_reader.json()["items"] == []
    r_admin = env.c.get(path, headers=env.admin)
    assert r_admin.status_code == 200
    assert r_admin.json()["total"] >= 1
    assert {i["evidence_id"] for i in r_admin.json()["items"]} == {fixtures.EVIDENCE_HIDDEN_ID}

    # §4.2.3：无 Binding 的 Section（default wiki summary/facts）→ total=0（不回退整 Page）。
    runs_default = refresh_and_collect_runs(env, wiki_ids=[fixtures.WIKI_DEFAULT_ID])
    run_d = env.drive_run(runs_default[fixtures.WIKI_DEFAULT_ID].id)
    assert run_d.status == "succeeded"
    wd_ = env.wiki(fixtures.WIKI_DEFAULT_ID)
    fact_sec = next((s for s in env.current_sections(fixtures.WIKI_DEFAULT_ID)
                     if s.section_type in ("facts", "summary")), None)
    assert fact_sec is not None
    bind_count = (env.db.query(WikiSectionEvidenceBinding)
                  .filter(WikiSectionEvidenceBinding.section_id == fact_sec.id).count())
    assert bind_count == 0
    path_fact = (f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/revisions/{wd_.current_revision_id}"
                 f"/sections/{fact_sec.id}/evidence")
    body = env.c.get(path_fact, headers=env.admin).json()
    assert body["total"] == 0 and body["items"] == []

    # reader GET /api/evidence/ev-hidden → 404；admin → 200。
    assert env.c.get(f"/api/evidence/{fixtures.EVIDENCE_HIDDEN_ID}",
                     headers=env.reader).status_code == 404
    assert env.c.get(f"/api/evidence/{fixtures.EVIDENCE_HIDDEN_ID}",
                     headers=env.admin).status_code == 200
    assert env.c.get(f"/api/evidence/{fixtures.EVIDENCE_API_ID}",
                     headers=env.reader).status_code == 200


# ---------------------------------------------------------------------------
# D：权限/隔离全矩阵（含历史 revision）
# ---------------------------------------------------------------------------


def _ensure_two_revisions(env: Env):
    """w-eng-default 已编译一次后，改动来源内容再造第二个历史 revision。"""
    w = env.wiki(fixtures.WIKI_DEFAULT_ID)
    assert w.current_revision_id is not None, "caller must compile default once"
    rev1 = w.current_revision_id

    # 改动来源内容（换 input hash）后再刷新一次 → 新 revision。
    page = env.db.get(Page, fixtures.PAGE_DEFAULT_ID)
    page.content = page.content + "\n补充说明段落，用于制造第二次编译的输入变化。"
    page.content_hash = fixtures.sha256_hex(page.content)
    w.dirty = True
    env.db.commit()
    runs2 = refresh_and_collect_runs(env, wiki_ids=[fixtures.WIKI_DEFAULT_ID])
    run2 = env.run_for_wiki(fixtures.WIKI_DEFAULT_ID)
    env.drive_run(run2.id)
    env.db.expire_all()
    w2 = env.db.get(WikiPage, fixtures.WIKI_DEFAULT_ID)
    assert w2.current_revision_id != rev1
    return rev1, w2.current_revision_id


def test_permission_and_isolation_matrix(env: Env):
    # 编译 ws-eng（default+api）与 ws-sales；再为 default 造第二个历史 revision。
    runs = refresh_and_collect_runs(env)
    for wid in list(fixtures.WIKI_OK_IDS):
        r = env.drive_run(runs[wid].id)
        assert r.status == "succeeded", (wid, r.safe_error_code)
    env.set_fault(False)
    fr = env.drive_run(runs[fixtures.WIKI_FAIL_ID].id)
    assert fr.status == "succeeded", (fr.safe_error_code, fr.safe_error_message)
    old_rev, cur_rev = _ensure_two_revisions(env)

    env.db.expire_all()

    # --- 目录与详情（§4.2.1）---
    w = env.wiki(fixtures.WIKI_API_ID)
    for wid in (fixtures.WIKI_DEFAULT_ID, fixtures.WIKI_API_ID):
        r = env.c.get(f"/api/wiki/{wid}", headers=env.reader)
        assert r.status_code == 200, (wid, r.text)
        assert r.json()["status"] == "published"
    r = env.c.get("/api/wiki", headers=env.reader)
    assert r.status_code == 200
    titles = {p["id"] for p in r.json()["pages"]}
    assert fixtures.WIKI_DEFAULT_ID in titles and fixtures.WIKI_API_ID in titles
    assert fixtures.WIKI_SALES_ID not in titles  # 列表不混入 sales
    # reader 对 sales wiki/workspace 一律 404。
    assert env.c.get(f"/api/wiki/{fixtures.WIKI_SALES_ID}",
                     headers=env.reader).status_code == 404
    rws = env.c.get("/api/wiki-workspaces", headers=env.reader)
    assert rws.status_code == 200
    ws_ids = {x["id"] for x in rws.json()["workspaces"]}
    assert fixtures.WS_ENG_ID in ws_ids and fixtures.WS_SALES_ID not in ws_ids

    # 未发布的 dirty（初始 draft）读者不可见（在编译后均已 published，此项覆盖 sales reader）。
    assert env.c.get(f"/api/wiki/{fixtures.WIKI_SALES_ID}",
                     headers=env.reader).status_code == 404

    # --- 历史 revision（§4.2.4）---
    def _old_section_id(wid, rid):
        sec = (env.db.query(WikiSection)
               .filter(WikiSection.revision_id == rid).first())
        assert sec is not None
        return sec.id

    old_sec = _old_section_id(fixtures.WIKI_DEFAULT_ID, old_rev)
    cur_sec = _old_section_id(fixtures.WIKI_DEFAULT_ID, cur_rev)
    old_path = (f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/revisions/{old_rev}"
                f"/sections/{old_sec}/evidence")
    # admin 可取历史（200）；reader/editor 历史（非 current published）→ 404。
    assert env.c.get(old_path, headers=env.admin).status_code == 200
    assert env.c.get(old_path, headers=env.reader).status_code == 404
    assert env.c.get(old_path, headers=env.editor).status_code == 404
    cur_path = (f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/revisions/{cur_rev}"
                f"/sections/{cur_sec}/evidence")
    assert env.c.get(cur_path, headers=env.reader).status_code == 200

    # diagnostics：editor 200 / reader 403 / admin 200；revision 绑定一致。
    dpath = f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/diagnostics"
    assert env.c.get(dpath, headers=env.reader).status_code == 403
    ed = env.c.get(dpath, headers=env.editor)
    assert ed.status_code == 200 and ed.json()["revision_id"] == cur_rev
    ad = env.c.get(dpath, headers=env.admin)
    assert ad.status_code == 200 and ad.json()["revision_id"] == cur_rev
    # admin 可查任意 revision；editor 非 current 历史 → 404。
    assert env.c.get(dpath, params={"revision_id": old_rev},
                     headers=env.admin).json()["revision_id"] == old_rev
    assert env.c.get(dpath, params={"revision_id": old_rev},
                     headers=env.editor).status_code == 404
    assert env.c.get(dpath, params={"revision_id": "no-such-rev"},
                     headers=env.admin).status_code == 404

    # editor 对不可见 wiki（sales）diagnostics → 404。
    assert env.c.get(f"/api/wiki/{fixtures.WIKI_SALES_ID}/diagnostics",
                     headers=env.editor).status_code == 404

    # --- 交叉 ID（§4.2.5）：属于另一 wiki 的 revision/section → 404 ---
    api_rev = env.wiki(fixtures.WIKI_API_ID).current_revision_id
    api_sec = env.current_sections(fixtures.WIKI_API_ID)[0].id
    cross1 = (f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/revisions/{api_rev}"
              f"/sections/{api_sec}/evidence")
    assert env.c.get(cross1, headers=env.admin).status_code == 404
    assert env.c.get(f"/api/wiki/{fixtures.WIKI_DEFAULT_ID}/revisions/{api_rev}"
                     f"/sections/{api_sec}/evidence",
                     headers=env.reader).status_code == 404
    assert env.c.get(f"/api/wiki/missing-wiki/revisions/{api_rev}"
                     f"/sections/{api_sec}/evidence",
                     headers=env.admin).status_code == 404

    # --- compile runs（§4.2.5）：仅 admin；workspace 过滤不混入 ---
    assert env.c.get("/api/wiki-compile/runs", headers=env.reader).status_code == 403
    assert env.c.get("/api/wiki-compile/runs", headers=env.editor).status_code == 403
    all_runs = env.c.get("/api/wiki-compile/runs", headers=env.admin)
    assert all_runs.status_code == 200
    runs_eng = env.c.get("/api/wiki-compile/runs",
                         params={"workspace_id": fixtures.WS_ENG_ID},
                         headers=env.admin).json()
    assert runs_eng["total"] >= 4
    assert all(r["workspace_id"] == fixtures.WS_ENG_ID for r in runs_eng["runs"])
    assert all(r["id"] != env.run_for_wiki(fixtures.WIKI_SALES_ID).id
               for r in runs_eng["runs"])  # 不混入 sales
    # 每个 run 都能被面板获取（admin）。
    detail = env.c.get(f"/api/wiki-compile/runs/"
                       f"{env.run_for_wiki(fixtures.WIKI_API_ID).id}",
                       headers=env.admin)
    assert detail.status_code == 200
    assert detail.json()["wiki_page_id"] == fixtures.WIKI_API_ID
    assert isinstance(detail.json()["stages"], list)
    assert isinstance(detail.json()["artifacts"], list)


# ---------------------------------------------------------------------------
# C：真实图谱建图 + subgraph API（不以“已调度”当完成）
# ---------------------------------------------------------------------------


def test_graph_real_rows_and_subgraph(env: Env):
    runs = refresh_and_collect_runs(env)
    for wid in list(fixtures.WIKI_OK_IDS) + [fixtures.WIKI_FAIL_ID]:
        r = env.drive_run(runs[wid].id)
        assert r.status == "succeeded", (wid, r.safe_error_code,
                                         r.safe_error_message)

    env.db.expire_all()
    # DB：V4GraphEntity/V4GraphRelation 相关行（wiki 目标 provenance）。
    rel_prov = (env.db.query(V4GraphRelationEvidence)
                .filter(V4GraphRelationEvidence.wiki_page_id
                        == fixtures.WIKI_API_ID).all())
    assert rel_prov, "graph relation provenance rows must exist for w-eng-api"
    ent_prov = (env.db.query(V4GraphEntityEvidence)
                .filter(V4GraphEntityEvidence.wiki_page_id
                        == fixtures.WIKI_API_ID).all())
    assert ent_prov, "graph entity provenance rows must exist for w-eng-api"
    # 至少存在与 Overview 图谱短语相关的实体/关系。
    rel_ids = {p.relation_id for p in rel_prov}
    rels = (env.db.query(V4GraphRelation)
            .filter(V4GraphRelation.id.in_(rel_ids)).all())
    assert rels
    ents = (env.db.query(V4GraphEntity).filter(
        V4GraphEntity.normalized_name.contains(fixtures.GRAPH_ENTITY_WORD)).all())
    assert ents, "graph entity from overview phrase must exist"

    # graph 调用记录含该 wiki 目标。
    graph = [r for r in fixtures.load_records(env.record_file)
             if r.get("kind") == "graph"]
    assert any(g.get("wiki_page_id") == fixtures.WIKI_API_ID for g in graph)

    # HTTP subgraph：admin 与 reader 均能经真实 API 查到相关节点/边（不只“已调度”）。
    # q 过滤会把目标边另一端节点裁掉（边两端必须都在返回集），因此先取全量子图
    # 断言边，再用 q 断言实体节点确实来自我们的图谱短语。
    for headers in (env.admin, env.reader):
        full = env.c.get("/api/v4/graph/subgraph",
                         params={"depth": 2, "limit": 120},
                         headers=headers)
        assert full.status_code == 200, full.text
        data = full.json()
        assert data["nodes"], "subgraph must return related nodes"
        names = [n["display_name"] for n in data["nodes"]]
        assert any(fixtures.GRAPH_ENTITY_WORD in n for n in names)
        assert data["edges"], "subgraph must return related edges"
        # q 命中节点本身（无越权、能检索到）。
        hit = env.c.get("/api/v4/graph/subgraph",
                        params={"q": fixtures.GRAPH_ENTITY_WORD},
                        headers=headers)
        assert hit.status_code == 200
        assert any(fixtures.GRAPH_ENTITY_WORD in n["display_name"]
                   for n in hit.json()["nodes"])


# ---------------------------------------------------------------------------
# E：cancel 语义（可控阻塞 + queued 立即取消）
# ---------------------------------------------------------------------------


def _add_block_wiki(env: Env, wid: str, pid: str, content: str) -> WikiPage:
    """向 ws-eng 添加 locked default wiki + 来源 page（测试本地构造）。"""
    env.db.add(Notebook(id="nb-block", name="阻塞来源", group_id="eng"))
    env.db.flush()
    env.db.add(Page(id=pid, notebook_id="nb-block", title="阻塞来源页",
                    content=content, content_hash=fixtures.sha256_hex(content),
                    wiki_dirty=True))
    binding = env.db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb-block").first()
    if binding is None:
        env.db.add(NotebookWorkspaceBinding(id="b-nb-block", notebook_id="nb-block",
                                            workspace_id=fixtures.WS_ENG_ID,
                                            status="active"))
    env.db.flush()
    wiki = WikiPage(id=wid, title="阻塞主题", summary="", acl_scope='{"groups": ["eng"]}',
                    category="资料", status="draft",
                    source_page_ids=json.dumps([pid], ensure_ascii=False),
                    dirty=True, workspace_id=fixtures.WS_ENG_ID,
                    content_skill="default", skill_version="1", skill_locked=True)
    env.db.add(wiki)
    env.db.commit()
    return wiki


def _deactivate_frozen_wikis(env: Env) -> None:
    """把 seed 的 4 个 dirty wiki 清 dirty，避免影响 cancel/queued 用例。"""
    for wid in _WIKI_IDS:
        w = env.db.get(WikiPage, wid)
        if w is not None:
            w.dirty = False
    env.db.commit()


def test_cancel_running_block_marker(env: Env):
    env.set_fault(True)
    _deactivate_frozen_wikis(env)
    _add_block_wiki(env, _BLOCK_WIKI, _BLOCK_PAGE, fixtures.page_block_text())

    # 正式入口触发 run（block wiki 是唯一 dirty wiki）。
    runs = refresh_and_collect_runs(env, wiki_ids=[_BLOCK_WIKI])
    run_id = runs[_BLOCK_WIKI].id

    # 后台线程真实执行（进入 LLM 后会被 block marker + fault flag 卡住）。
    errors: list[BaseException] = []

    def _bg() -> None:
        sess = env.Session()
        try:
            executor.execute_run(sess, run_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            sess.close()

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    # 等 recorder 进入阻塞点（不猜 sleep），并确认 run 已 running。
    assert env.recorder.block_entered.wait(timeout=_TIMEOUT), "llm 未进入阻塞"

    def _status() -> str | None:
        env.db.expire_all()
        row = env.db.get(CompileRun, run_id)
        return row.status if row else None

    _wait_for(lambda: _status() == "running", "run status=running", interval=0.2)

    # HTTP cancel：running → 置 cancel_requested（不等于直接 cancelled）。
    cancel = env.c.post(f"/api/wiki-compile/runs/{run_id}/cancel",
                        headers=env.admin)
    assert cancel.status_code == 200, cancel.text
    payload = cancel.json()
    assert payload["status"] == "running"
    assert payload["cancel_requested"] is True
    env.db.expire_all()
    row = env.db.get(CompileRun, run_id)
    assert row.status == "running" and row.cancel_requested is True

    # 释放阻塞（删 fault flag）→ 让执行继续并归一为合法终态。
    env.set_fault(False)
    t.join(timeout=_TIMEOUT)
    assert not t.is_alive(), "后台执行线程未结束"
    assert not errors, errors

    env.db.expire_all()
    row = env.db.get(CompileRun, run_id)
    # cancel_requested 后 fence 拒绝 stage 写入，run 保持 running → 由真实 recovery 归一。
    if row.status == "running":
        wiki_worker.requeue_stale_runs(env.db, timeout_seconds=0)
        env.db.expire_all()
        row = env.db.get(CompileRun, run_id)
    assert row.status == "cancelled", row.status
    assert row.cancel_requested is True
    assert row.finished_at is not None
    # 未发布：wiki 仍 dirty、无 revision。
    w = env.wiki(_BLOCK_WIKI)
    assert w.dirty is True and w.current_revision_id is None


def test_cancel_queued_run_immediate(env: Env):
    _deactivate_frozen_wikis(env)
    _add_block_wiki(env, _QUEUED_WIKI, _QUEUED_PAGE, fixtures.page_block_text())
    runs = refresh_and_collect_runs(env, wiki_ids=[_QUEUED_WIKI])
    run_id = runs[_QUEUED_WIKI].id
    env.db.expire_all()
    assert env.db.get(CompileRun, run_id).status == "queued"

    cancel = env.c.post(f"/api/wiki-compile/runs/{run_id}/cancel",
                        headers=env.admin)
    assert cancel.status_code == 200, cancel.text
    payload = cancel.json()
    assert payload["status"] == "cancelled"
    assert payload["cancel_requested"] is False
    env.db.expire_all()
    row = env.db.get(CompileRun, run_id)
    assert row.status == "cancelled"
    # 非 admin 不能取消：为同 wiki 直接建一条新的 queued run 验证 403（不经 HTTP 入口）。
    run2 = executor.create_run(
        env.db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=_QUEUED_WIKI,
        workspace_id=fixtures.WS_ENG_ID, wiki_page_id=_QUEUED_WIKI,
    )
    env.db.commit()
    run2_id = run2.id
    env.db.expire_all()
    assert env.db.get(CompileRun, run2_id).status == "queued"
    assert env.c.post(f"/api/wiki-compile/runs/{run2_id}/cancel",
                      headers=env.reader).status_code == 403
    # 终态再 cancel → 409。
    assert env.c.post(f"/api/wiki-compile/runs/{run_id}/cancel",
                      headers=env.admin).status_code == 409


def test_v3_llm_failure_after_publish_keeps_old_revision_and_dirty(env: Env):
    """真实 v3 链：已发布 Wiki 再次编译时 LLM 失败 → 保留旧发布版、dirty 保持、不重复发布。"""
    env.set_fault(False)  # 先让 w-fail 成功发布一次
    runs = refresh_and_collect_runs(env, wiki_ids=[fixtures.WIKI_FAIL_ID])
    ok_run = env.drive_run(runs[fixtures.WIKI_FAIL_ID].id)
    assert ok_run.status == "succeeded", (ok_run.safe_error_code,
                                          ok_run.safe_error_message)
    w = env.wiki(fixtures.WIKI_FAIL_ID)
    assert w.current_revision_id is not None and w.dirty is False
    old_rev = w.current_revision_id
    old_sections = env.current_sections(fixtures.WIKI_FAIL_ID)
    old_section_keys = {s.section_key or s.section_type for s in old_sections}

    # 二次 manual_rebuild：变更来源内容（产生新 input_hash/幂等键），fault 注入使 LLM 抛错。
    page = env.db.get(Page, "p-fail")
    assert page is not None
    page.content = (page.content or "") + "\n" + fixtures.MARKER_FAIL + " 变更段"
    env.db.commit()
    env.set_fault(True)
    w2 = env.wiki(fixtures.WIKI_FAIL_ID)
    w2.dirty = True
    env.db.commit()
    runs2 = refresh_and_collect_runs(env, wiki_ids=[fixtures.WIKI_FAIL_ID])
    fail_run = env.drive_run(runs2[fixtures.WIKI_FAIL_ID].id)
    assert fail_run.status == "failed", (fail_run.safe_error_code,
                                         fail_run.safe_error_message)
    w3 = env.wiki(fixtures.WIKI_FAIL_ID)
    assert w3.current_revision_id == old_rev, "LLM 失败不得切换 current_revision"
    assert w3.dirty is True, "LLM 失败必须保持 dirty"
    # 失败的 run 不产出新 published Revision / 成功 run 数不增。
    assert fail_run.output_revision_id is None
    succeeded_runs = (env.db.query(CompileRun)
                      .filter(CompileRun.wiki_page_id == fixtures.WIKI_FAIL_ID,
                              CompileRun.status == "succeeded")
                      .count())
    assert succeeded_runs == 1, "不得因失败产生新的 succeeded run"
    # 历史 revision/section 原样保留（稳定 key/语义，不要求行 ID 不变）。
    cur_sections = env.current_sections(fixtures.WIKI_FAIL_ID)
    assert {s.section_key or s.section_type for s in cur_sections} == old_section_keys
    env.set_fault(False)
