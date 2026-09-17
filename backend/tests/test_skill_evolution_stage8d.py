"""阶段 8D：业务晋升/回退 + 编译绑定读取（隔离业务库；离线）。

覆盖：
A. 编译桥接（business_ops.freeze_binding_for_run @ executor）：
   功能关闭 → 旧行为不变；绑定 v2 → 捕获编译请求含 v2 指令；回退绑定 v1 → 新编译
   请求含 v1；绑定内容损坏 → fail loud；缺 evolution schema + 功能开关开 → 明确
   拒绝（编译 fail loud，不静默忽略绑定却报告成功）；开关关 + 缺 schema → 旧行为
   照常；同一 compile run 中断恢复仍使用冻结版本（中途晋升/回退不改变固定版本）。
B. 晋升/回退服务：模拟证据默认阻止（allow_simulated 显式放行）、幂等、版本复制
   完整性、作用域校验、schema 缺失 503、rollback 到历史生效版本、审计事件。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse as _up

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _graph_noop(**kw):
    return None


def _md_payload():
    return {"summary": "摘要", "content": "正文" + "x" * 80}


@pytest.fixture(autouse=True)
def _isolate():
    from app.core.wiki_pipeline import executor as exe
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_skills import registry as sreg
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    exe.reset_external_runners()
    yield
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    exe.reset_external_runners()


@pytest.fixture()
def business_db():
    """内存业务库：业务表 +（可选）evolution schema；可复用引擎建第二次。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    from app.models.database import init_db
    init_db(engine)
    s = sessionmaker(bind=engine)()
    yield {"engine": engine, "db": s, "evolution": False}
    s.close()
    engine.dispose()


def _with_evolution(business_db):
    from app.models import evolution as ev
    ev.metadata.create_all(business_db["engine"])
    business_db["evolution"] = True


def _ensure_ws(business_db, ws_id="ws-prod-1"):
    db = business_db["db"]
    row = db.execute(
        __import__("sqlalchemy").text(
            "SELECT id FROM wiki_workspaces WHERE id=:w"),
        {"w": ws_id}).fetchone()
    if row is None:
        db.execute(__import__("sqlalchemy").text(
            "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
            "status) VALUES (:w, 'k', :w, 'public', 'public', 'active')"),
            {"w": ws_id})
        db.commit()
    return ws_id


def _add_business_version(business_db, version_id="default:0002",
                          marker="8D 指令第二版"):
    db = business_db["db"]
    skill_md = ("---\nskill_id: default\ndomain: wiki_compile.default\n"
                "runtime_ref: wiki.compile.default.runtime/v1\n"
                "schema_version: 1\n---\n# default\n\n## 适用条件\n- 通用\n"
                f"## 不适用条件\n- 无\n## 操作步骤\n- {marker}\n")
    purpose_md = ("## 来源\n- 测试\n## 改进目的\n- x\n## 演化历史\n- v\n")
    payload = json.dumps({"skill_id": "default",
                          "domain": "wiki_compile.default",
                          "runtime_ref": "wiki.compile.default.runtime/v1",
                          "schema_version": "skill-evolution/v1",
                          "skill_md": skill_md, "purpose_md": purpose_md},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    ch = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    seq = int(version_id.rsplit(":", 1)[1])
    db.execute(__import__("sqlalchemy").text(
        "INSERT OR IGNORE INTO evolution_skill_versions "
        "(version_id, skill_id, seq, schema_version, domain, runtime_ref, "
        "parent_version_id, skill_md, purpose_md, content_hash, source_type, "
        "created_by, created_at) VALUES (:vid,'default',:seq,"
        "'skill-evolution/v1','wiki_compile.default',"
        "'wiki.compile.default.runtime/v1','default:0001',:sm,:pm,:ch,"
        "'manual_seed','8d-test',CURRENT_TIMESTAMP)"),
        {"vid": version_id, "seq": seq, "sm": skill_md, "pm": purpose_md,
         "ch": ch})
    db.commit()
    return skill_md


def _bind_business(business_db, ws_id, version_id=None):
    db = business_db["db"]
    cur = db.execute(__import__("sqlalchemy").text(
        "SELECT id FROM evolution_skill_bindings WHERE kind='business' "
        "AND workspace_id=:w AND domain='wiki_compile.default'"),
        {"w": ws_id}).fetchone()
    if cur is None:
        db.execute(__import__("sqlalchemy").text(
            "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
            "domain, set_kind, skill_id, version_id, created_by, created_at, "
            "updated_at) VALUES (:i,'business',:w,'wiki_compile.default',"
            ":sk,:sid,:vid,'8d-test',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
            {"i": "b1", "w": ws_id, "sk": "skill" if version_id else "empty",
             "sid": "default" if version_id else None, "vid": version_id})
    else:
        db.execute(__import__("sqlalchemy").text(
            "UPDATE evolution_skill_bindings SET set_kind=:sk, skill_id=:sid,"
            " version_id=:vid WHERE id=:i"),
            {"sk": "skill" if version_id else "empty",
             "sid": "default" if version_id else None, "vid": version_id,
             "i": cur.id})
    db.commit()


# ---------------------------------------------------------------------------
# 编译管线 harness（batch default 编译；捕获全部模型请求）
# ---------------------------------------------------------------------------


def _boot(db):
    from app.core.wiki_skills import service as skill_service
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        register_default_pipeline)
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        register_default_pipeline_v2)
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        register_default_pipeline_v3)
    skill_service.register_builtin_skills()
    register_default_pipeline()
    register_default_pipeline_v2()
    register_default_pipeline_v3()


def _page(business_db, pid, content):
    db = business_db["db"]
    from app.core.wiki_workspace.routing import ensure_notebook_workspace
    from app.models.database import Notebook, NotebookWorkspaceBinding, Page
    if db.query(Notebook).filter(Notebook.id == "nb-8d").first() is None:
        db.add(Notebook(id="nb-8d", name="库", group_id="eng"))
        db.flush()
    ws = ensure_notebook_workspace(db, db.get(Notebook, "nb-8d"))
    if db.get(Page, pid) is None:
        db.add(Page(id=pid, notebook_id="nb-8d", title=pid,
                    content=content, content_hash=_hash(content),
                    wiki_dirty=True))
    db.commit()
    return ws


def _compile_run(business_db, ws, capture: list, *, fail_first_synthesis=False):
    from app.core.wiki_pipeline import executor as exe
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        ARTIFACT_SCHEMA_WIKI_BATCH, ARTIFACT_TYPE_WIKI_BATCH_INPUT,
        _batch_input_hash, _page_full_hash)
    from app.models.database import KnowledgeCompileArtifact as Artifact
    db = business_db["db"]
    ids = ["p-8d"]
    state = {"fail_n": 0}

    def _llm(messages, context="", timeout=120.0):
        prompt = messages[0]["content"] if messages else ""
        capture.append(list(messages or []))
        if fail_first_synthesis and context in ("wiki-synthesis",
                                                "wiki-mapreduce"):
            state["fail_n"] += 1
            if state["fail_n"] == 1:
                raise RuntimeError("boom-synthesis-once")
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [{"action": "create",
                                             "title": "主题8D",
                                             "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return _md_payload()
        return {"worthy": True, "ops": []}

    exe.configure_external_runners(llm_runner=_llm, graph_runner=_graph_noop)
    run = exe.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="batch_rebuild", trigger_object_id=ws.id,
        workspace_id=ws.id, input_hash=_batch_input_hash(db, ws.id, ids),
        supersede_same_trigger=True)
    payload = {"workspace_id": ws.id, "page_ids": ids,
               "page_input_hashes": {pid: _page_full_hash(db, pid)
                                     for pid in ids}}
    db.add(Artifact(run_id=run.id,
                    artifact_type=ARTIFACT_TYPE_WIKI_BATCH_INPUT,
                    schema_version=ARTIFACT_SCHEMA_WIKI_BATCH,
                    payload_json=json.dumps(payload, ensure_ascii=False,
                                            sort_keys=True)))
    db.commit()
    executed = exe.execute_run(db, run.id)
    db.refresh(executed)
    return executed


# ---------------------------------------------------------------------------
# A. 编译桥接
# ---------------------------------------------------------------------------


def _run_compile(monkeypatch, business_db, *, enable_flag: bool,
                 with_schema: bool):
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled",
                        enable_flag)
    from app.models.database import init_db  # noqa: F401
    if with_schema:
        _with_evolution(business_db)
    ws = _page(business_db, "p-8d", "普通说明文字足够长用于编译构建测试。" * 5)
    if with_schema:
        _bind_business(business_db, ws.id, version_id="default:0002")
        _add_business_version(business_db, version_id="default:0002",
                              marker="8D 指令第二版")
    capture: list = []
    executed = _compile_run(business_db, ws, capture)
    return executed, capture, ws.id


def test_compile_feature_off_no_injection(business_db, monkeypatch):
    _boot(business_db["db"])
    executed, capture, _ws = _run_compile(
        monkeypatch, business_db, enable_flag=False, with_schema=True)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert capture
    assert all("8D 指令第二版" not in "".join(
        m.get("content", "") for m in msgs) for msgs in capture)


def test_compile_bound_version_injected_into_requests(business_db, monkeypatch):
    _boot(business_db["db"])
    executed, capture, _ws = _run_compile(
        monkeypatch, business_db, enable_flag=True, with_schema=True)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert capture
    joined = "".join(m.get("content", "") for msgs in capture
                     for m in msgs)
    assert "8D 指令第二版" in joined


def test_compile_rollback_binding_uses_prior_version(business_db, monkeypatch):
    _boot(business_db["db"])
    _with_evolution(business_db)
    # 先绑定 v1 → 编译捕获 v1
    _add_business_version(business_db, version_id="default:0001",
                          marker="8D 指令第一版")
    ws = _page(business_db, "p-8d", "普通说明文字足够长用于编译构建测试。" * 5)
    _bind_business(business_db, ws.id, version_id="default:0001")
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled", True)
    c1: list = []
    e1 = _compile_run(business_db, ws, c1)
    assert e1.status == "succeeded"
    joined1 = "".join(m.get("content", "") for msgs in c1 for m in msgs)
    assert "8D 指令第一版" in joined1
    assert "8D 指令第二版" not in joined1
    # 晋升/回退绑定为 v2 → 新编译捕获 v2
    _add_business_version(business_db, version_id="default:0002",
                          marker="8D 指令第二版")
    _bind_business(business_db, ws.id, version_id="default:0002")
    c2: list = []
    e2 = _compile_run(business_db, ws, c2)
    assert e2.status == "succeeded"
    joined2 = "".join(m.get("content", "") for msgs in c2 for m in msgs)
    assert "8D 指令第二版" in joined2
    # 回退绑定为 v1 → 后续编译请求恢复使用 v1（页面固定；新 run 读新绑定）
    _bind_business(business_db, ws.id, version_id="default:0001")
    c3: list = []
    e3 = _compile_run(business_db, ws, c3)
    assert e3.status == "succeeded"
    joined3 = "".join(m.get("content", "") for msgs in c3 for m in msgs)
    assert "8D 指令第一版" in joined3


def test_compile_schema_missing_enabled_fails_loud(business_db, monkeypatch):
    """功能明确启用但必要 schema 缺失 → 拒绝（不静默忽略绑定却报告成功）。"""
    _boot(business_db["db"])
    executed, capture, _ws = _run_compile(
        monkeypatch, business_db, enable_flag=True, with_schema=False)
    assert executed.status == "failed"


def test_compile_schema_missing_disabled_old_behavior(business_db, monkeypatch):
    """功能关闭 + 缺 schema → 旧行为照常（不注入、不报错）。"""
    _boot(business_db["db"])
    executed, capture, _ws = _run_compile(
        monkeypatch, business_db, enable_flag=False, with_schema=False)
    assert executed.status == "succeeded"
    assert capture
    assert all("8D 指令第二版" not in "".join(
        m.get("content", "") for m in msgs) for msgs in capture)


def test_compile_corrupt_binding_fails_loud(business_db, monkeypatch):
    _boot(business_db["db"])
    _with_evolution(business_db)
    ws = _page(business_db, "p-8d", "普通说明文字足够长用于编译构建测试。" * 5)
    _add_business_version(business_db, version_id="default:0002",
                          marker="8D 指令第二版")
    _bind_business(business_db, ws.id, version_id="default:0002")
    # 人为损坏版本行内容（哈希失配）
    db = business_db["db"]
    db.execute(__import__("sqlalchemy").text(
        "UPDATE evolution_skill_versions SET skill_md=skill_md || ' ' "
        "WHERE version_id='default:0002'"))
    db.commit()
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled", True)
    capture: list = []
    executed = _compile_run(business_db, ws, capture)
    # 首次模型调用即抛错 → 编译 run 失败（fail loud，不静默按无绑定继续）
    assert executed.status == "failed"


def test_same_run_interrupted_resume_keeps_pinned_version(business_db,
                                                               monkeypatch):
    """同一 compile run 中断（attempt 失败）后恢复仍用冻结技能版本：
    恢复前 rollback 绑定为 v1，retry 后该 run 请求仍含 v2；新建 run 才读 v1。"""
    from app.core.wiki_pipeline import executor as exe
    _boot(business_db["db"])
    _with_evolution(business_db)
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled", True)
    _add_business_version(business_db, version_id="default:0001",
                          marker="8D 指令第一版")
    _add_business_version(business_db, version_id="default:0002",
                          marker="8D 指令第二版")
    ws = _page(business_db, "p-8d", "普通说明文字足够长用于编译构建测试。" * 5)
    _bind_business(business_db, ws.id, version_id="default:0002")
    cap1: list = []
    e1 = _compile_run(business_db, ws, cap1, fail_first_synthesis=True)
    assert e1.status == "failed"
    db = business_db["db"]
    pins = db.execute(__import__("sqlalchemy").text(
        "SELECT payload_json FROM knowledge_compile_artifacts "
        "WHERE run_id=:r AND artifact_type='evolution_binding_pin'"),
        {"r": e1.id}).fetchall()
    assert pins and json.loads(pins[0].payload_json)["mode"] == "bound"
    assert json.loads(pins[0].payload_json)["version_id"] == "default:0002"
    # 中断期间业务绑定被 rollback 到 v1 —— 该 run 不得改用 v1
    _bind_business(business_db, ws.id, version_id="default:0001")
    from app.core.wiki_pipeline.executor import retry_run
    retry_run(db, e1.id)
    db.commit()
    cap2: list = []

    def _ok_llm(messages, context="", timeout=120.0):
        cap2.append(list(messages or []))
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [{"action": "create",
                                             "title": "主题8D",
                                             "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return _md_payload()
        return {"worthy": True, "ops": []}

    exe.configure_external_runners(llm_runner=_ok_llm, graph_runner=_graph_noop)
    e2 = exe.execute_run(db, e1.id)
    db.refresh(e2)
    assert e2.status == "succeeded", (e2.safe_error_code,
                                      e2.safe_error_message)
    joined2 = "".join(m.get("content", "") for msgs in cap2 for m in msgs)
    assert "8D 指令第二版" in joined2
    assert "8D 指令第一版" not in joined2
    # 新建 run（绑定已为 v1）→ 使用 v1
    cap3: list = []
    e3 = _compile_run(business_db, ws, cap3)
    assert e3.status == "succeeded"
    joined3 = "".join(m.get("content", "") for msgs in cap3 for m in msgs)
    assert "8D 指令第一版" in joined3
    assert "8D 指令第二版" not in joined3


# ---------------------------------------------------------------------------
# B. 晋升 / 回退服务
# ---------------------------------------------------------------------------


BACKEND = __import__("pathlib").Path(__file__).resolve().parent.parent
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"


def _seed_accepted_experiment(tmp_path, *, real: bool = False):
    """实验根：default:0001 当前成员 + 有效 best_score + 完成 run 记录。"""
    from app.core.skill_evolution import runenv, skill_store
    from app.core.skill_evolution.contracts import load_dataset
    from app.core.skill_evolution.gating import create_experiment
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    db = skill_store.session_for(root)
    try:
        ds_dir = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v3"
        ds = load_dataset(ds_dir)
        ws = "ws-prod-1"
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        v = skill_store.get_version(db, seed)
        member = {"skill_id": v.skill_id, "version_id": v.version_id,
                  "content_hash": v.content_hash, "seq": v.seq}
        exp = create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
            initial_members=[member])
        exp_id = str(exp.experiment_id)
        s = __import__("sqlalchemy")
        db.execute(s.text(
            "UPDATE evolution_experiments SET best_skill_set_json=:j, "
            "current_skill_set_json=:j, best_score_passed=5, best_score_total=5,"
            " best_evaluation_id='ev-1', baseline_evaluation_id='ev-1' "
            "WHERE experiment_id=:e"),
            {"j": json.dumps({"mode": "skill", "members": [member]},
                             ensure_ascii=False),
             "e": exp_id})
        db.execute(s.text(
            "INSERT OR IGNORE INTO evolution_runs (run_id, experiment_id, "
            "workspace_id, domain, dataset_version, init_mode, config_json, "
            "initial_skill_set_json, initial_experience_snapshot_json, "
            "max_iterations, current_iteration, status, stop_reason, "
            "used_model_calls, used_tool_calls, used_estimated_chars, "
            "reserved_in_flight_json) VALUES ('run-ev1', :e, :w, "
            "'wiki_compile.default', :dv, 'business', :cfg, '{}', '{}', 1, 1, "
            "'completed', 'max_iterations', 4, 2, 0, '[]')"),
            {"e": exp_id, "w": ws, "dv": ds.dataset_version,
             "cfg": json.dumps({"runner": {"mode": "real" if real
                                           else "simulated",
                                           "real": real},
                                "budget": {}}, ensure_ascii=False)})
        db.execute(s.text(
            "INSERT OR IGNORE INTO evolution_evaluations (evaluation_id, "
            "idempotency_key, experiment_id, kind, proposal_id, base_set_json,"
            " candidate_set_json, task_ids_json, per_task_results_json, "
            "config_json, main_passed, main_total, valid) VALUES "
            "('ev-1', 'idem-1', :e, 'candidate', 'p-1', '{}', '{}', '[]', '[]',"
            " '{}', 5, 5, 1)"),
            {"e": exp_id})
        db.commit()
        return root, exp_id
    finally:
        db.close()


def _business_file(tmp_path):
    from app.models.database import init_db
    from sqlalchemy import create_engine
    from app.models import evolution as ev
    path = tmp_path / "biz-8d.db"
    engine = create_engine(f"sqlite:///{(path).as_posix()}")
    init_db(engine)
    ev.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    return path, engine, maker


def test_promote_blocks_simulated_by_default_and_allows_explicit(
        tmp_path, monkeypatch):
    from app.core.skill_evolution import business_ops as bops
    root, exp_id = _seed_accepted_experiment(tmp_path)
    from app.core.skill_evolution import skill_store
    db_exp = skill_store.session_for(root)
    _path, engine, maker = _business_file(tmp_path)
    db_biz = maker()
    _ensure_ws({"db": db_biz}, "ws-prod-1")
    try:
        row = db_exp.execute(__import__("sqlalchemy").text(
            "SELECT * FROM evolution_experiments WHERE experiment_id=:e"),
            {"e": exp_id}).fetchone()
        from types import SimpleNamespace
        exp = SimpleNamespace(**{k: row._mapping[k] for k in row._mapping.keys()})
        with pytest.raises(bops.BusinessOpsError) as ei:
            bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                         created_by="t")
        assert "模拟" in ei.value.message
        # 服务端旁路开关关闭时：即使请求 allow_simulated=true 也被拒绝（生产 API
        # 不能通过请求参数开启模拟晋升旁路）。
        with pytest.raises(bops.BusinessOpsError) as ei2:
            bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                         created_by="t", allow_simulated=True)
        assert "旁路未在服务端开启" in ei2.value.message
        # 隔离测试环境：服务端开启旁路后才允许显式放行（永不标注为真实效果）
        monkeypatch.setattr(settings,
                            "wikiskill_evolution_allow_simulated_promotion", True)
        monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
        res = bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                           created_by="t", allow_simulated=True)
        assert res["changed"] is True
        assert res["simulated_evidence"] is True
        assert res["effect_verified"] is False
        state = bops.business_state(db_biz, "ws-prod-1")
        assert state["current"]["version_id"] == "default:0001"
        assert state["effective_history"] == ["default:0001"]
        # 幂等：同版本再次 promote
        res2 = bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                            created_by="t", allow_simulated=True)
        assert res2["changed"] is False and res2["already_current"] is True
    finally:
        db_biz.close()
        db_exp.close()
        engine.dispose()


def test_rollback_to_previously_effective_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,
                        "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    from app.core.skill_evolution import business_ops as bops, skill_store
    root, exp_id = _seed_accepted_experiment(tmp_path)
    db_exp = skill_store.session_for(root)
    _path, engine, maker = _business_file(tmp_path)
    db_biz = maker()
    _ensure_ws({"db": db_biz}, "ws-prod-1")
    try:
        row = db_exp.execute(__import__("sqlalchemy").text(
            "SELECT * FROM evolution_experiments WHERE experiment_id=:e"),
            {"e": exp_id}).fetchone()
        from types import SimpleNamespace
        exp = SimpleNamespace(**{k: row._mapping[k] for k in row._mapping.keys()})
        # 晋升 v1（接受集为 default:0001）
        bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                     created_by="t", allow_simulated=True)
        # 第二个实验接受 default:0002 → 晋升覆盖为 v2
        _add_business_version({"db": db_biz}, version_id="default:0002",
                              marker="第二版晋升")
        # 直接把业务绑定推进到 v2（模拟第二次晋升；审计链加入 v2）
        bops._upsert_binding(db_biz, "ws-prod-1", "default:0002", "t")
        bops._append_event(db_biz, "promote:ws-prod-1:wiki_compile.default"
                                 ":default:0002", "promote", "ws-prod-1",
                           "default:0001", "default:0002", exp_id, None, {},
                           "second promote", "t")
        db_biz.commit()
        # 回退 → v1（历史明确生效版本）
        rb = bops.rollback(db_biz, workspace_id="ws-prod-1", created_by="t")
        assert rb["changed"] is True
        assert rb["version_id"] == "default:0001"
        state = bops.business_state(db_biz, "ws-prod-1")
        assert state["current"]["version_id"] == "default:0001"
        # 再次回退 → 恢复到此前的明确生效版本 v2（审计链内反复切换）
        rb2 = bops.rollback(db_biz, workspace_id="ws-prod-1", created_by="t")
        assert rb2["changed"] is True
        assert rb2["version_id"] == "default:0002"
        state2 = bops.business_state(db_biz, "ws-prod-1")
        assert state2["current"]["version_id"] == "default:0002"
        assert state2["effective_history"] == ["default:0001", "default:0002",
                                               "default:0001", "default:0002"]
    finally:
        db_biz.close()
        db_exp.close()
        engine.dispose()


def test_schema_missing_promotion_reports_explicit(tmp_path):
    from app.core.skill_evolution import business_ops as bops, skill_store
    root, exp_id = _seed_accepted_experiment(tmp_path)
    db_exp = skill_store.session_for(root)
    from app.models.database import init_db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    path = tmp_path / "biz-noschema.db"
    engine = create_engine(f"sqlite:///{(path).as_posix()}")
    init_db(engine)  # 只有业务表；不建 evolution schema
    maker = sessionmaker(bind=engine)
    db_biz = maker()
    _ensure_ws({"db": db_biz}, "ws-prod-1")
    try:
        row = db_exp.execute(__import__("sqlalchemy").text(
            "SELECT * FROM evolution_experiments WHERE experiment_id=:e"),
            {"e": exp_id}).fetchone()
        from types import SimpleNamespace
        exp = SimpleNamespace(**{k: row._mapping[k] for k in row._mapping.keys()})
        with pytest.raises(bops.BusinessOpsError) as ei:
            bops.promote(db_biz, db_exp, exp, workspace_id="ws-prod-1",
                         created_by="t", allow_simulated=True)
        assert ei.value.code == "schema_not_provisioned"
        assert "不自动迁移" in ei.value.message
    finally:
        db_biz.close()
        db_exp.close()
        engine.dispose()
