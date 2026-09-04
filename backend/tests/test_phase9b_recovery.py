# -*- coding: utf-8 -*-
"""Phase 9B Agent B：图谱故障一次失败→正式 retry 不重复发布 + 跨进程崩溃恢复 + lease 接线。

覆盖（phase9b/CONTRACT §4.1 / §4.2 / §4.3 / §6）：
1. lease 接线单元（§4.2 最小产品改动，默认值=现值，生产行为不变）：
   - executor.claim_by_id 写入的到期长度运行时读 settings.wiki_pipeline_lease_seconds
     （回退 300）；
   - worker.heartbeat 推进长度与 claim 同源读同一字段（防短 lease 被心跳顶回 300s）；
   - LeaseRenewer 未显式传 interval 时默认读 renew 字段；
   - requeue_stale_runs 未显式传 timeout 时读 heartbeat_timeout 字段（stale 为 OR
     语义：heartbeat 超时 或 lease_expires_at 过期，二者任一即触发）。
2. 图谱阶段一次故障 → 正式 retry（§4.1，in-process：真实 alembic + TestClient +
   真实 executor + 一次性 graph wrapper 委托真实 _default_graph_runner）：
   - 首跑 schedule_graph 抛错一次 → run failed(GRAPH_BUILD_FAILED)；已发布 Revision
     保持有效（current_revision_id 不变 / wiki.dirty=False / revision 数不增）；
   - 清 flag 后经 HTTP POST .../retry → succeeded；重放 schedule_graph 从 Artifact
     manifest 读目标重建图谱；Revision 不增加、不重复发布、图谱无重复关系。
3. 跨进程崩溃恢复（§4.2，必须真实进程边界）：进程 A（server.py 真实 lifespan +
   worker）领取 run 后阻塞于 LLM（MARKER_BLOCK + fault flag）→ 先证明正常心跳续租
   （连续两次 lease_expires_at 递增）→ terminate 进程 A → 老化窗口后 → 进程 B 同 DB
   真实 startup recovery + worker 消费到 succeeded（attempt=2）。断言旧 running Stage
   收尾 failed(worker_lost)/queued→skipped、无 ghost running、attempt 未超 max、
   Artifact manifest / Revision / dirty / 图谱最终正确。全程不直接调用 recovery helper
   代替跨进程验收，只观察真实进程行为与 DB。

隔离：in-process 走 pytest tmp；跨进程 DB 位于 <repo>\\.phase9a\\<session9b>\\db
（server.py 路径守卫要求），会话目录测试结束后自行删除（gitignore）。不连真实模型/
业务库；不改 .env*；只 kill 本测试 Popen 的进程。

门禁：cd backend; python -m pytest tests/test_phase9b_recovery.py \
      tests/test_wiki_pipeline_core.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event, text as sa_text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_pipeline import executor, registry as pregs, worker  # noqa: E402
from app.core.wiki_pipeline import fake as wiki_fake  # noqa: E402
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
    KnowledgeCompileArtifact as CompileArtifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Page,
    V4GraphEntityEvidence,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiWorkspace,
    init_db,
)
from phase9a import bootstrap_db as bdb  # noqa: E402
from phase9a import fixtures  # noqa: E402

_P44_REVISION = "a9b8c7d6e5f4"
_PHASE9A_ROOT = _BACKEND.parent / ".phase9a"

# ---- 跨进程短 lease 注入（settings env；生产默认不变）----
_CRASH_LEASE_SECONDS = 6
_CRASH_POLL_SECONDS = 0.5
_CRASH_RENEW_SECONDS = 1.0

# ---- 冻结/测试标识 ----
_BLOCK_WIKI = "w-9b-crash"
_BLOCK_PAGE = "p-9b-block"
_BLOCK_NOTEBOOK = "nb-9b-block"

_TIMEOUT_HTTP_READY = 120.0
_TIMEOUT_BLOCK = 90.0
_TIMEOUT_TERMINAL = 240.0


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def _now_dt() -> datetime:
    return datetime.now()


def _wait_until(pred, description: str, timeout: float, interval: float = 0.4):
    """轮询直到 pred() 返回真值；超时抛 AssertionError（任何异常都视为未满足）。"""
    end = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < end:
        try:
            value = pred()
        except Exception as exc:  # noqa: BLE001
            last = exc
            value = None
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"等待超时: {description} (last={last!r})")


# ---------------------------------------------------------------------------
# 1) lease 接线单元（真实 claim/heartbeat/requeue 数据路径，settings 注入）
# ---------------------------------------------------------------------------


def _mk_fake_run(db):
    """fake pipeline + workspace + queued run（claim/heartbeat/requeue 目标）。"""
    if db.get(WikiWorkspace, "ws-wire") is None:
        db.add(WikiWorkspace(id="ws-wire", key="wire", name="wire",
                             acl_scope='{"groups":["g"]}', scope_id="group:g",
                             status="active"))
        db.flush()
    run = executor.create_run(
        db,
        pipeline_key="fake.wiki.compile.v1",
        trigger_type="manual_rebuild",
        trigger_object_id="wire-target",
        workspace_id="ws-wire",
    )
    db.commit()
    return run


@pytest.fixture(autouse=True)
def _phase9b_cleanup():
    """清 registry / runner / worker 线程，防跨测试污染。"""
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    wiki_fake.unregister_fake_pipelines()
    executor.reset_external_runners()
    if worker.worker_running():
        worker.stop_worker()
    app.dependency_overrides.clear()
    yield
    executor.reset_external_runners()
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    wiki_fake.unregister_fake_pipelines()
    if worker.worker_running():
        worker.stop_worker()
    app.dependency_overrides.clear()


@pytest.fixture()
def wire_db(tmp_path):
    """轻量库（无 alembic；init_db 建全表），供 lease 接线单元使用。"""
    url = _sqlite_url(tmp_path / "wire.db")
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")

    init_db(engine)
    session = sessionmaker(bind=engine)()
    wiki_fake.register_fake_pipelines()
    yield session
    session.close()
    engine.dispose()


def _lease_offsets(run, field: str) -> float:
    value = getattr(run, field)
    assert value is not None, field
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return (value - _now_dt()).total_seconds()


def test_claim_lease_expiry_and_heartbeat_push_share_lease_setting(wire_db, monkeypatch):
    """claim 写入的到期长度与 heartbeat 推进长度都读 settings lease 字段（同源）。"""
    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", 4)
    run = _mk_fake_run(wire_db)
    _, claimed = executor.claim_by_id(wire_db, run.id)
    assert claimed is not None and claimed.status == "running"
    offset1 = _lease_offsets(claimed, "lease_expires_at")
    assert 3.5 <= offset1 <= 4.5, offset1  # 4s 而非 300s

    ok = worker.heartbeat(wire_db, run.id,
                          lease_token=claimed.lease_token,
                          worker_id=claimed.worker_id)
    assert ok is True
    wire_db.expire_all()
    fresh = wire_db.get(CompileRun, run.id)
    offset2 = _lease_offsets(fresh, "lease_expires_at")
    # heartbeat 同样推进到 now+4s（顶不回 300s）；lease 长度以「到期时刻-心跳时刻」为准。
    assert 3.5 <= offset2 <= 4.5, offset2
    lease_len = (fresh.lease_expires_at.replace(tzinfo=None)
                 - fresh.heartbeat_at.replace(tzinfo=None)).total_seconds()
    assert 3.5 <= lease_len <= 4.5, f"heartbeat 必须把到期长度推进到 settings 的 4s（不是 300s）: {lease_len}"


def test_lease_renewer_default_interval_reads_renew_setting(wire_db, monkeypatch):
    """LeaseRenewer 未显式传 interval 时默认读 renew 字段（短间隔真实续租）。"""
    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", 8)
    monkeypatch.setattr(settings, "wiki_pipeline_lease_renew_interval_seconds", 0.3)
    run = _mk_fake_run(wire_db)
    _, claimed = executor.claim_by_id(wire_db, run.id)
    renewer = worker.LeaseRenewer(
        wire_db.get_bind(), run.id, claimed.lease_token, claimed.worker_id)
    renewer.start()
    try:
        samples: list[datetime] = []
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            wire_db.expire_all()
            row = wire_db.get(CompileRun, run.id)
            samples.append(row.heartbeat_at.replace(tzinfo=None))
            time.sleep(0.3)
        advance = sum(
            1 for a, b in zip(samples, samples[1:])
            if (b - a).total_seconds() >= 0.1)
        assert advance >= 3, f"0.3s 默认间隔应高频续租 (advance={advance})"
    finally:
        renewer.stop()


def test_requeue_stale_default_timeout_reads_heartbeat_setting(wire_db, monkeypatch):
    """requeue_stale_runs 未传 timeout 时读 heartbeat_timeout 字段。"""
    monkeypatch.setattr(settings, "wiki_pipeline_lease_seconds", 300)
    monkeypatch.setattr(settings, "wiki_pipeline_heartbeat_timeout_seconds", 1)
    run = _mk_fake_run(wire_db)
    _, claimed = executor.claim_by_id(wire_db, run.id)
    # 把 heartbeat 人为推老到 100s 前（lease 仍 300 未过期 → 走 heartbeat 分支）。
    claimed.heartbeat_at = _now_dt() - timedelta(seconds=100)
    wire_db.commit()
    requeued = worker.requeue_stale_runs(wire_db)  # 无显式 timeout → 读 settings
    assert requeued == 1
    wire_db.expire_all()
    row = wire_db.get(CompileRun, run.id)
    assert row.status == "queued" and row.attempt == 1  # stale requeue 不加 attempt


def test_claim_lease_defaults_preserved(wire_db, monkeypatch):
    """settings 未覆盖时 claim/heartbeat 仍以现值（300s）写入。"""
    run = _mk_fake_run(wire_db)
    _, claimed = executor.claim_by_id(wire_db, run.id)
    offset = _lease_offsets(claimed, "lease_expires_at")
    assert 295 <= offset <= 305, offset
    ok = worker.heartbeat(wire_db, run.id,
                          lease_token=claimed.lease_token,
                          worker_id=claimed.worker_id)
    assert ok is True
    wire_db.expire_all()
    fresh = wire_db.get(CompileRun, run.id)
    assert 295 <= _lease_offsets(fresh, "lease_expires_at") <= 305


# ---------------------------------------------------------------------------
# 2) in-process Env（真实 alembic + seed + TestClient 真实路由 + 真实 executor）
# ---------------------------------------------------------------------------


class Env:
    """单个 in-process 测试环境（对齐 test_phase9a_integration.Env，单写入者自用）。"""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = Path(tmp_path)
        self.db_path = self.tmp / "phase9b.db"
        self.db_url = _sqlite_url(self.db_path)
        self._monkeypatch = monkeypatch

        envmap = dict(os.environ)
        envmap["DATABASE_URL"] = self.db_url
        envmap["PYTHONIOENCODING"] = "utf-8"
        heads = bdb.alembic_single_head(bdb._BACKEND, envmap)
        assert [h for h in heads.split("|") if h] == [_P44_REVISION], heads
        bdb.alembic_upgrade_head(bdb._BACKEND, envmap)

        self.engine = create_engine(self.db_url, connect_args={"check_same_thread": False})

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

        self.record_file = self.tmp / "records" / "calls.jsonl"
        self.graph_flag = self.tmp / "records" / "graph.flag"
        self.recorder = fixtures.Recorder(self.record_file, self.tmp / "records" / "fault.flag")

        self.c = TestClient(app)
        self.admin = self._login(fixtures.ADMIN_USERNAME)

    def _login(self, username: str) -> dict:
        r = self.c.post("/api/auth/login", json={
            "username": username, "password": fixtures.PHASE9A_PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def close(self) -> None:
        self.db.close()
        self.engine.dispose()
        app.dependency_overrides.clear()

    # ---- 查询 helpers ----
    def expire(self) -> None:
        self.db.expire_all()

    def wiki(self, wid: str) -> WikiPage:
        self.db.expire_all()
        w = self.db.get(WikiPage, wid)
        assert w is not None, wid
        return w

    def latest_run(self, wid: str) -> CompileRun:
        self.db.expire_all()
        run = (self.db.query(CompileRun)
               .filter(CompileRun.wiki_page_id == wid)
               .order_by(CompileRun.created_at.desc(), CompileRun.id.desc())
               .first())
        assert run is not None, wid
        return run

    def drive_run(self, run_id: str) -> CompileRun:
        self.db.expire_all()
        executor.execute_run(self.db, run_id)
        self.db.expire_all()
        run = self.db.get(CompileRun, run_id)
        assert run is not None
        return run

    def set_graph_flag(self, present: bool) -> None:
        if present:
            self.graph_flag.parent.mkdir(parents=True, exist_ok=True)
            self.graph_flag.write_text("on", encoding="utf-8")
        else:
            if self.graph_flag.exists():
                self.graph_flag.unlink()

    def revision_count(self, wid: str) -> int:
        self.db.expire_all()
        return self.db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wid).count()

    def deactivate_all_wikis(self) -> None:
        for w in self.db.query(WikiPage).all():
            w.dirty = False
        self.db.commit()


class GraphFailOnce:
    """一次性图谱故障注入：委托真实 _default_graph_runner。

    仅当 flag 存在且目标是指定 wiki 时抛一次；之后永远委托真实实现。
    """

    def __init__(self, delegate, flag: Path, target_wiki: str):
        self.delegate = delegate
        self.flag = Path(flag)
        self.target = target_wiki
        self.fired = 0
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(dict(kw))
        wid = kw.get("wiki_page_id")
        if self.fired == 0 and wid == self.target and self.flag.exists():
            self.fired += 1
            raise RuntimeError("phase9b injected graph failure (once)")
        return self.delegate(**kw)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


@pytest.fixture(autouse=True)
def _no_pipelines_leak():
    """保证任何残留 worker/注册表不影响其他测试文件。"""
    yield


# ---------------------------------------------------------------------------
# 3) 图谱阶段一次故障 → 正式 retry（CONTRACT §4.1，真实建图边界注入）
# ---------------------------------------------------------------------------


def _trigger_manual_run(env: Env, wid: str) -> CompileRun:
    """置唯一 dirty wiki 后走 HTTP 正式入口，返回 queued run。"""
    env.deactivate_all_wikis()
    w = env.wiki(wid)
    w.dirty = True
    env.db.commit()
    r = env.c.post("/api/wiki/refresh-page-dirty", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    assert body["submitted"] >= 1
    return env.latest_run(wid)


def test_graph_failure_once_then_retry_no_duplicate_publish(env: Env):
    """w-eng-api 编译首跑 schedule_graph 抛一次 → run failed(GRAPH_BUILD_FAILED)；
    Revision 已发布保持有效；清 flag + HTTP retry → succeeded；Revision 不增、
    不重复发布、Manifest 重放、图谱无重复关系。"""
    wid = fixtures.WIKI_API_ID
    executor.configure_external_runners(
        llm_runner=env.recorder.llm_runner(),
        graph_runner=GraphFailOnce(wd._default_graph_runner, env.graph_flag, wid),
    )

    # ---- attempt 1：graph 故障 → failed（但 Revision 已发布）----
    env.set_graph_flag(True)
    run1 = _trigger_manual_run(env, wid)
    assert run1.status == "queued"
    failed = env.drive_run(run1.id)
    assert failed.status == "failed", (failed.safe_error_code, failed.safe_error_message)
    assert failed.safe_error_code == "GRAPH_BUILD_FAILED", failed.safe_error_code

    # 已发布 Revision 保持有效：current_revision 回填、dirty False、published。
    w = env.wiki(wid)
    assert w.current_revision_id is not None
    assert w.dirty is False
    assert w.status == "published"
    rev_before = w.current_revision_id
    assert env.revision_count(wid) == 1, "首跑发布恰好一个 Revision"

    # 图谱目标确实在 Manifest 里（供 retry 重放）。
    manifests = (env.db.query(CompileArtifact)
                 .filter(CompileArtifact.run_id == failed.id,
                         CompileArtifact.artifact_type
                         == wd.ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).all())
    assert manifests, "failed run 必须已持久化 publish manifest"
    first_manifest = json.loads(manifests[0].payload_json)
    assert any(g.get("kind") == "wiki" and g.get("wiki_page_id") == wid
               for g in first_manifest["graph_targets"])
    assert first_manifest["outcome"] == "published"

    # attempt1 的 graph 调用只有一次且抛错（wrapper fired=1）。
    wrapper = executor._GRAPH_RUNNER
    assert wrapper.fired == 1
    assert any(kw.get("wiki_page_id") == wid for kw in wrapper.calls)

    # 图谱在 attempt1 不应写入目标关系（delegate 未执行）。
    rel_prov_after_fail = (env.db.query(V4GraphRelationEvidence)
                           .filter(V4GraphRelationEvidence.wiki_page_id == wid).count())
    assert rel_prov_after_fail == 0, "graph 失败后不得有目标 wiki 的 provenance 残留"

    # ---- 清 flag → 正式 HTTP retry → attempt2 succeeded ----
    env.set_graph_flag(False)
    retry = env.c.post(f"/api/wiki-compile/runs/{failed.id}/retry", headers=env.admin)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "queued"
    ok = env.drive_run(failed.id)
    assert ok.status == "succeeded", (ok.safe_error_code, ok.safe_error_message)
    assert ok.attempt == 2

    env.db.expire_all()
    w2 = env.wiki(wid)
    assert w2.current_revision_id == rev_before, "retry 不得追加新 Revision"
    assert w2.dirty is False
    assert env.revision_count(wid) == 1, "Revision 数量不增（重试不重复发布）"

    # Manifest 重放证据：retry 的 publish 幂等守卫不产新 manifest（仍只有 attempt1 一份）。
    manifests_after = (env.db.query(CompileArtifact)
                       .filter(CompileArtifact.run_id == ok.id,
                               CompileArtifact.artifact_type
                               == wd.ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).all())
    assert len(manifests_after) == 1, "retry 不得重复写入 publish manifest"
    # schedule_graph 重试从 Artifact manifest 读到原目标并真实重建（成功）。
    graph_stage = (env.db.query(StageRun)
                   .filter(StageRun.run_id == ok.id,
                           StageRun.stage_key == "schedule_graph",
                           StageRun.attempt == ok.attempt).first())
    assert graph_stage is not None and graph_stage.status == "succeeded"
    metrics = json.loads(graph_stage.metrics_json or "{}")
    assert any(("wiki:" + wid) in r for r in metrics.get("rebuilt") or []), metrics

    # 图谱无重复关系：一次真实重建；provenance 不得有重复行（同关系同 section 只写一次）。
    env.db.expire_all()
    rel_rows = (env.db.query(V4GraphRelationEvidence)
                .filter(V4GraphRelationEvidence.wiki_page_id == wid).all())
    assert rel_rows, "retry 后真实图谱必须为该 wiki 产生关系 provenance"
    from sqlalchemy import func as sa_func  # noqa: PLC0415
    dup_rows = (env.db.query(V4GraphRelationEvidence.relation_id,
                             V4GraphRelationEvidence.section_id)
                .filter(V4GraphRelationEvidence.wiki_page_id == wid)
                .group_by(V4GraphRelationEvidence.relation_id,
                          V4GraphRelationEvidence.section_id)
                .having(sa_func.count() > 1).count())
    assert dup_rows == 0, "不得存在重复 provenance 行（同关系同 section 只写一次）"
    # provenance 的 revision 必须全部指向当前有效 Revision（无陈旧 provenance）。
    assert all(r.revision_id == w2.current_revision_id for r in rel_rows)


# ---------------------------------------------------------------------------
# 4) 跨进程崩溃恢复（CONTRACT §4.2；真实 server.py lifespan + worker）
# ---------------------------------------------------------------------------


def _pick_port(preferred: int) -> int:
    """优先 preferred；被占用则取临时空闲端口。"""
    import socket
    if preferred:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", preferred))
            sock.close()
            return preferred
        except OSError:
            sock.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CrashSession:
    """跨进程会话：<repo>\\.phase9a\\9b-<token>\\db\\crash.db + 两个后端进程。"""

    def __init__(self) -> None:
        token = f"9b-{uuid.uuid4().hex[:8]}"
        self.session_dir = _PHASE9A_ROOT / token
        self.db_dir = self.session_dir / "db"
        self.db_path = self.db_dir / "crash.db"
        self.logs = self.session_dir / "logs"
        self.record_file = self.session_dir / "records" / "calls.jsonl"
        self.fault_flag = self.session_dir / "records" / "fault.flag"
        self._procs: list[subprocess.Popen] = []
        self._log_handles: list[tuple] = []

        # 断言路径位于 .phase9a\<session>\db 下（server.py 守卫也会拒）。
        assert "db" in self.db_path.parts and ".phase9a" in self.db_path.parts
        for d in (self.db_dir, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.record_file.parent.mkdir(parents=True, exist_ok=True)
        self.base_env = self._make_env()
        self.port_a = _pick_port(8821)
        self.port_b = _pick_port(8822)

    def _make_env(self) -> dict:
        envmap = dict(os.environ)
        envmap["DATABASE_URL"] = _sqlite_url(self.db_path)
        envmap["PYTHONIOENCODING"] = "utf-8"
        envmap["PHASE9A_SESSION_DIR"] = str(self.session_dir)
        envmap["PHASE9A_RECORD_FILE"] = str(self.record_file)
        envmap["PHASE9A_FAULT_FLAG"] = str(self.fault_flag)
        envmap["LDAP_GROUP_MAP_WIKI_EDITOR"] = "editors"
        envmap["LDAP_GROUP_MAP_ADMIN"] = ""
        # 覆盖 backend/.env 的真实 LDAP 端点：子进程不得连真实 LDAP（未知用户登录
        # 需立即 401，不触发 LDAP connect 超时）。OS env 优先于 .env 合并值。
        for k in ("LDAP_SERVER_URL", "LDAP_BIND_DN", "LDAP_BIND_PASSWORD",
                  "LDAP_USER_BASE_DN", "LDAP_GROUP_BASE_DN", "LDAP_USER_FILTER",
                  "LDAP_GROUP_FILTER"):
            envmap[k] = ""
        envmap["WIKI_TOPIC_ENABLED"] = "true"
        envmap["AUTO_DAILY_SCAN_ENABLED"] = "false"
        envmap["AUTO_ORGANIZE_ENABLED"] = "false"
        # 替身进程不连真实模型（server.py 守卫）。
        for k in ("LLM_API_URL", "LLM_API_KEY", "EMBEDDING_API_URL", "RERANKER_API_URL"):
            envmap[k] = ""
        envmap["PDF_VISION_ENABLED"] = "false"
        # Phase 9B 短 lease 接线：与 claim/heartbeat/renew 同源（settings env 注入）。
        envmap["WIKI_PIPELINE_LEASE_SECONDS"] = str(_CRASH_LEASE_SECONDS)
        envmap["WIKI_PIPELINE_HEARTBEAT_TIMEOUT_SECONDS"] = str(_CRASH_LEASE_SECONDS)
        envmap["WIKI_PIPELINE_POLL_INTERVAL_SECONDS"] = str(_CRASH_POLL_SECONDS)
        envmap["WIKI_PIPELINE_LEASE_RENEW_INTERVAL_SECONDS"] = str(_CRASH_RENEW_SECONDS)
        return envmap

    def seed_db(self) -> None:
        """真实 alembic head + 冻结 seed（子进程，路径守卫）。"""
        cmd = [sys.executable, "phase9a/bootstrap_db.py", "--db", str(self.db_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(_BACKEND), env=self.base_env,
                              timeout=600)
        assert proc.returncode == 0, f"bootstrap_db failed: {proc.stderr}"
        assert "ALEMBIC_OK" in proc.stdout
        cmd = [sys.executable, "phase9a/seed.py", "--db", str(self.db_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(_BACKEND), env=self.base_env,
                              timeout=300)
        assert proc.returncode == 0, f"seed failed: {proc.stderr}"

    def prepare_dirty(self) -> None:
        """清全部 dirty；注入 block wiki/page；只置目标 wiki dirty（页面保持干净）。"""
        engine = create_engine(_sqlite_url(self.db_path),
                               connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA busy_timeout=15000")

        session = sessionmaker(bind=engine)()
        try:
            for w in session.query(WikiPage).all():
                w.dirty = False
            for p in session.query(Page).all():
                p.wiki_dirty = False
            # 复用一个已绑定 ws-eng 的 notebook（nb-eng 已绑定）。
            content = (
                fixtures.MARKER_BLOCK + "\n"
                + "Phase9A 崩溃恢复主题。本页用于验证进程崩溃后的任务恢复语义，"
                  "内容包含阻塞标记以模拟长时模型调用。"
                  "上位机系统承载全部接口，支持连接调试器诊断调用，返回认证失败与目标不存在错误码，"
                ) * 8
            page = session.get(Page, _BLOCK_PAGE)
            if page is None:
                session.add(Page(id=_BLOCK_PAGE, notebook_id=fixtures.NB_ENG_ID,
                                 title="崩溃恢复来源页", content=content,
                                 content_hash=fixtures.sha256_hex(content),
                                 wiki_dirty=False))
            else:
                page.content = content
                page.content_hash = fixtures.sha256_hex(content)
                page.wiki_dirty = False
            wiki = session.get(WikiPage, _BLOCK_WIKI)
            if wiki is None:
                session.add(WikiPage(id=_BLOCK_WIKI, title="Phase9A 崩溃恢复主题",
                                     summary="", acl_scope=session.get(
                                         WikiWorkspace, fixtures.WS_ENG_ID).acl_scope,
                                     category="资料", status="draft",
                                     source_page_ids=json.dumps([_BLOCK_PAGE],
                                                                ensure_ascii=False),
                                     dirty=True, workspace_id=fixtures.WS_ENG_ID,
                                     content_skill="default", skill_version="1",
                                     skill_locked=True))
            else:
                wiki.dirty = True
                wiki.status = "draft"
                wiki.current_revision_id = None
            session.commit()
        finally:
            session.close()
            engine.dispose()

    def start_server(self, port: int, *, fault_flag: bool) -> subprocess.Popen:
        envmap = dict(self.base_env)
        envmap["PHASE9A_BE_PORT"] = str(port)
        if fault_flag:
            self.fault_flag.parent.mkdir(parents=True, exist_ok=True)
            self.fault_flag.write_text("on", encoding="utf-8")
        else:
            self.fault_flag.unlink(missing_ok=True)
        out = open(self.logs / f"server-{port}.out.log", "a", encoding="utf-8")
        err = open(self.logs / f"server-{port}.err.log", "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, "phase9a/server.py"],
                cwd=str(_BACKEND), env=envmap,
                stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            )
        except Exception:
            out.close()
            err.close()
            raise
        self._procs.append(proc)
        self._log_handles.append((out, err))
        return proc

    @staticmethod
    def http_login(base: str) -> str:
        import requests  # noqa: PLC0415
        r = requests.post(base + "/api/auth/login", json={
            "username": fixtures.ADMIN_USERNAME, "password": fixtures.PHASE9A_PASSWORD},
            timeout=15)
        assert r.status_code == 200, r.text
        return r.json()["token"]

    @staticmethod
    def http_get(base: str, path: str, token: str):
        import requests  # noqa: PLC0415
        r = requests.get(base + path, headers={"Authorization": f"Bearer {token}"},
                         timeout=15)
        assert r.status_code == 200, r.text
        return r.json()

    @staticmethod
    def http_post(base: str, path: str, token: str, payload=None):
        import requests  # noqa: PLC0415
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            r = requests.post(base + path, json=payload, headers=headers, timeout=30)
        else:
            r = requests.post(base + path, headers=headers, timeout=30)
        assert r.status_code == 200, r.text
        return r.json()

    def kill_all(self) -> None:
        """只杀自己 Popen 的进程；terminate 后再等，超时强杀。"""
        for proc in list(self._procs):
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        deadline = time.monotonic() + 15
        for proc in list(self._procs):
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                proc.wait(timeout=10)
        self._procs.clear()
        for out, err in list(self._log_handles):
            try:
                out.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                err.close()
            except Exception:  # noqa: BLE001
                pass
        self._log_handles.clear()

    def cleanup(self) -> None:
        self.kill_all()
        shutil.rmtree(self.session_dir, ignore_errors=True)


def _row(db, sql: str, **params):
    with db.connect() as conn:
        return conn.execute(sa_text(sql), params).fetchone()


def test_cross_process_crash_recovery(monkeypatch):
    """进程 A 阻塞后 kill → 进程 B 真实 startup recovery 消费到 succeeded（attempt=2）。

    触发路径：prepare_dirty 置 w-9b-crash.dirty=True → 进程 A 真实 startup 的
    scheduler 恢复（recover_dirty_pages 建 manual_rebuild queued run）→ A 的 worker
    （真实 lifespan 启动）领取执行并在 LLM 处阻塞（MARKER_BLOCK + fault flag）。
    """
    session = CrashSession()
    db_engine = None
    try:
        session.seed_db()
        session.prepare_dirty()

        # ---------- 进程 A：真实 lifespan + worker（fault flag ON 提供阻塞点）----------
        proc_a = session.start_server(session.port_a, fault_flag=True)
        base_a = f"http://127.0.0.1:{session.port_a}"
        _wait_until(lambda: _probe_login_401(base_a), f"后端A 就绪 {base_a}",
                    _TIMEOUT_HTTP_READY, interval=0.8)

        # 独立 sqlite 轮询连接（只观察真实进程行为，绝不调 recovery helper）。
        db_engine = create_engine(_sqlite_url(session.db_path),
                                  connect_args={"check_same_thread": False})

        @event.listens_for(db_engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA busy_timeout=20000")

        def _run_row(run_id: str) -> tuple:
            row = _row(db_engine, "SELECT status, attempt, lease_expires_at, heartbeat_at, "
                                  "current_stage FROM knowledge_compile_runs WHERE id=:id",
                       id=run_id)
            assert row is not None
            return row

        def _running_stage_keys(run_id: str) -> list[str]:
            with db_engine.connect() as conn:
                rows = conn.execute(sa_text(
                    "SELECT stage_key FROM knowledge_compile_stage_runs "
                    "WHERE run_id=:id AND status='running'"), {"id": run_id}).fetchall()
            return [r[0] for r in rows]

        # A startup 已建 queued run；等待 running + attempt=1 + running Stage（LLM 阻塞）。
        run_id = _wait_until(lambda: _find_run_id(db_engine, _BLOCK_WIKI),
                             "找到 w-9b-crash 的 run", 60, interval=0.4)

        def _reached_block() -> str | None:
            row = _run_row(run_id)
            keys = _running_stage_keys(run_id)
            if row[0] == "running" and row[1] == 1 and keys:
                return keys[0]
            return None

        stage_blocked = _wait_until(_reached_block, "run running attempt=1 且存在 running Stage",
                                    _TIMEOUT_BLOCK, interval=0.6)

        # 先证明正常心跳续租（连续两次 lease_expires_at 递增），再 kill。
        r1 = _run_row(run_id)
        time.sleep(_CRASH_RENEW_SECONDS * 3 + 0.5)
        r2 = _run_row(run_id)
        assert r2[0] == "running", r2
        lease1 = _parse_sqlite_dt(r1[2])
        lease2 = _parse_sqlite_dt(r2[2])
        assert lease2 > lease1, f"正常续租应推进 lease (l1={lease1} l2={lease2})"
        hb1 = _parse_sqlite_dt(r1[3])
        hb2 = _parse_sqlite_dt(r2[3])
        assert hb2 > hb1, "heartbeat_at 必须在阻塞期间持续推进"
        assert r2[4] == r1[4], "阻塞阶段 current_stage 必须稳定"

        # ---------- 强杀进程 A（Windows terminate=TerminateProcess，不跑 shutdown）----------
        proc_a.terminate()
        try:
            proc_a.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc_a.kill()
            proc_a.wait(timeout=10)
        assert proc_a.poll() is not None, "进程 A 必须已退出"

        # 老化窗口（lease*3 + margin）后确认该 run 已 stale（lease 过期）。
        time.sleep(_CRASH_LEASE_SECONDS * 3 + 6)
        stale = _run_row(run_id)
        assert stale[0] == "running", "kill 后 run 应保持 running（由恢复收敛）"
        assert _parse_sqlite_dt(stale[2]) < _now_dt(), "lease 已过期 → stale"

        # ---------- 进程 B：同 DB，无 fault flag，真实 startup recovery + worker ----------
        proc_b = session.start_server(session.port_b, fault_flag=False)
        base_b = f"http://127.0.0.1:{session.port_b}"
        _wait_until(lambda: _probe_login_401(base_b), f"后端B 就绪 {base_b}",
                    _TIMEOUT_HTTP_READY, interval=0.8)

        # 观察（不调用 recovery）：run 被 B 消费到 succeeded，attempt=2。
        def _terminal() -> str | None:
            row = _run_row(run_id)
            if row[0] in ("succeeded", "failed", "cancelled"):
                return row[0]
            return None

        final_status = _wait_until(_terminal, "进程 B 消费 run 至终态", _TIMEOUT_TERMINAL,
                                   interval=0.8)
        assert final_status == "succeeded", final_status

        _assert_crash_outcome(session, db_engine, run_id, stage_blocked)
    finally:
        if db_engine is not None:
            db_engine.dispose()
        session.cleanup()


def _probe_login_401(base: str) -> bool:
    """后端就绪探测：真实 auth 路由对坏凭据回 401 即认为 up。"""
    import requests
    try:
        r = requests.post(base + "/api/auth/login",
                          json={"username": "x", "password": "x"}, timeout=3)
        return r.status_code == 401
    except Exception:  # noqa: BLE001
        return False


def _find_run_id(db_engine, wiki_id: str) -> str | None:
    row = _row(db_engine, "SELECT id FROM knowledge_compile_runs "
                          "WHERE wiki_page_id=:wid ORDER BY created_at, id LIMIT 1",
               wid=wiki_id)
    return row[0] if row else None


def _parse_sqlite_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text_value = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    raise AssertionError(f"cannot parse sqlite datetime: {value!r}")


def _stage_rows(db_engine, run_id: str) -> list[tuple]:
    with db_engine.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT stage_key, attempt, status, safe_error_code, stage_order "
            "FROM knowledge_compile_stage_runs WHERE run_id=:id "
            "ORDER BY attempt, stage_order, id"), {"id": run_id}).fetchall()
    return [tuple(r) for r in rows]


def _assert_crash_outcome(session: CrashSession, db_engine, run_id: str,
                          stage_blocked: str) -> None:
    """跨进程恢复的最终 DB 断言（全部在真实进程行为之后观察）。"""
    import requests  # noqa: PLC0415

    # run：succeeded / attempt=2 / 未超 max / 无 ghost running。
    run = _row(db_engine, "SELECT status, attempt, max_attempts, worker_id, "
                          "output_revision_id FROM knowledge_compile_runs WHERE id=:id",
               id=run_id)
    assert run[0] == "succeeded", run
    assert run[1] == 2, f"attempt 应为 2（claim 原子 +1），got {run[1]}"
    assert run[2] >= run[1], "attempt 未超 max_attempts"
    assert _row(db_engine, "SELECT COUNT(*) FROM knowledge_compile_runs WHERE status='running'")[0] == 0

    # Stage：attempt1 收尾（running→failed(worker_lost)/queued→skipped），attempt2 全成功。
    stage_rows = _stage_rows(db_engine, run_id)
    attempt1 = [s for s in stage_rows if s[1] == 1]
    attempt2 = [s for s in stage_rows if s[1] == 2]
    assert len(attempt1) == len(attempt2), f"每次 attempt 预建全部 stage 行 ({len(attempt1)} vs {len(attempt2)})"
    keys = {s[0] for s in attempt1}
    assert keys == {
        "resolve_context", "topic_route", "skill_route", "synthesize_by_skill",
        "validate_by_skill", "publish_by_skill", "finalize_compile_outcome",
        "schedule_graph",
    }, sorted(keys)
    # 阻塞 stage 必须存在于 attempt1 且被收尾为 failed(worker_lost)。
    blocked = [s for s in attempt1 if s[0] == stage_blocked]
    assert blocked and blocked[0][2] == "failed" and blocked[0][3] == "worker_lost", \
        f"attempt1 {stage_blocked} 应收尾 failed(worker_lost): {blocked}"
    # 无 ghost running；attempt1 除该失败外都是 succeeded（此前 stage）/skipped（之后 stage）。
    assert all(s[2] != "running" for s in stage_rows), "不允许残留 running stage"
    blocked_order = blocked[0][4]
    for s in attempt1:
        if s[0] == stage_blocked:
            continue
        if s[4] < blocked_order:
            assert s[2] == "succeeded", s
        else:
            assert s[2] == "skipped", s
    assert all(s[2] == "succeeded" for s in attempt2), \
        [s for s in attempt2 if s[2] != "succeeded"]

    # Wiki / Revision / Artifact / dirty。
    wiki = _row(db_engine, "SELECT current_revision_id, dirty, status FROM wiki_pages "
                           "WHERE id=:wid", wid=_BLOCK_WIKI)
    assert wiki is not None and wiki[0] is not None
    assert wiki[1] == 0, "发布成功后 wiki.dirty 必须 False"
    assert wiki[2] == "published"
    rev_count = _row(db_engine, "SELECT COUNT(*) FROM wiki_revisions "
                                "WHERE wiki_page_id=:wid", wid=_BLOCK_WIKI)[0]
    assert rev_count == 1, f"恢复后应恰好一个 Revision ({rev_count})"
    rev_status = _row(db_engine, "SELECT status FROM wiki_revisions WHERE id=:rid",
                      rid=wiki[0])[0]
    assert rev_status == "published"
    manifest = _row(db_engine, "SELECT payload_json FROM knowledge_compile_artifacts "
                               "WHERE run_id=:id AND artifact_type='wiki_publish_manifest' "
                               "ORDER BY created_at DESC LIMIT 1", id=run_id)
    assert manifest and manifest[0], "恢复 run 必须有 publish manifest"
    payload = json.loads(manifest[0])
    assert payload["outcome"] == "published"
    assert any(g.get("kind") == "wiki" and g.get("wiki_page_id") == _BLOCK_WIKI
               for g in payload["graph_targets"])

    # 图谱最终正确：schedule_graph(attempt2) 成功且以当前 Revision 重建；
    # 不残留指向其他 revision 的 provenance（真实 runner 幂等替换）。
    with db_engine.connect() as conn:
        sched = conn.execute(sa_text(
            "SELECT metrics_json FROM knowledge_compile_stage_runs "
            "WHERE run_id=:id AND stage_key='schedule_graph' AND attempt=2"), {"id": run_id}).fetchone()
    assert sched and sched[0]
    metrics = json.loads(sched[0])
    assert any(("wiki:" + _BLOCK_WIKI) in r for r in metrics.get("rebuilt") or []), metrics
    ent_bad = _row(db_engine, "SELECT COUNT(*) FROM v4_graph_entity_evidence "
                              "WHERE wiki_page_id=:wid AND revision_id IS NOT NULL "
                              "AND revision_id<>:rid", wid=_BLOCK_WIKI, rid=wiki[0])[0]
    rel_bad = _row(db_engine, "SELECT COUNT(*) FROM v4_graph_relation_evidence "
                              "WHERE wiki_page_id=:wid AND revision_id IS NOT NULL "
                              "AND revision_id<>:rid", wid=_BLOCK_WIKI, rid=wiki[0])[0]
    assert ent_bad == 0 and rel_bad == 0, "图谱 provenance 必须全部指向当前 Revision"

    # 调用记录证明：B 的 graph_runner 真实收到该 wiki 目标（不只“已调度”）。
    records = fixtures.load_records(session.record_file)
    graph = [r for r in records if r.get("kind") == "graph"]
    assert any(g.get("wiki_page_id") == _BLOCK_WIKI for g in graph), graph
