"""M3 浏览器整改反例：P1-3 v2/real 创建契约；rollback 幂等重放计数；meta 脱敏。

隔离库/本地 stub；不触真实模型与生产。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

BACKEND = Path(__file__).resolve().parent.parent
GRADER = "wiki-default-grader/v1"


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@pytest.fixture()
def biz_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    monkeypatch.setattr(settings,
                        "wikiskill_evolution_allow_simulated_promotion", True)
    engine = create_engine(f"sqlite:///{(tmp_path / 'biz.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    from app.models.database import init_db
    from app.models import evolution as ev
    init_db(engine)
    ev.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text(
        "INSERT INTO wiki_workspaces (id,key,name,acl_scope,scope_id,status) "
        "VALUES ('ws-1','k','n','public','public','active')"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _add(db, skill_id, seq, marker):
    sm = (f"---\nskill_id: {skill_id}\ndomain: wiki_compile.default\n"
          "runtime_ref: wiki.compile.default.runtime/v1\nschema_version: 1\n---\n"
          f"# {skill_id}\n\n## 适用条件\n- 通用\n## 不适用条件\n- 无\n"
          f"## 操作步骤\n- {marker}\n")
    pm = "## 来源\n- r\n## 改进目的\n- r\n## 演化历史\n- v1\n"
    payload = json.dumps({"skill_id": skill_id,
                          "domain": "wiki_compile.default",
                          "runtime_ref": "wiki.compile.default.runtime/v1",
                          "schema_version": "skill-evolution/v1",
                          "skill_md": sm, "purpose_md": pm},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    ch = hashlib.sha256(payload.encode()).hexdigest()
    vid = f"{skill_id}:{seq:04d}"
    db.execute(text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:v,:s,:q,'skill-evolution/v1','wiki_compile.default',"
        "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
        "'r',CURRENT_TIMESTAMP)"),
        {"v": vid, "s": skill_id, "q": seq, "sm": sm, "pm": pm, "ch": ch})
    db.commit()
    return {"skill_id": skill_id, "version_id": vid,
            "content_hash": ch, "seq": seq}


def _exp(db, exp_id, members):
    js = json.dumps({"mode": "versions", "members": members},
                    ensure_ascii=False)
    return SimpleNamespace(experiment_id=exp_id, best_skill_set_json=js,
                           current_skill_set_json=js, best_score_passed=5,
                           best_score_total=5, grader_version=GRADER,
                           dataset_version="wiki-default-v3")


# --- P1-3 后端门禁（control.create_experiment_and_run） ---
def test_v2_dataset_requires_review_v2(tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    root = tmp_path / "lab"
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(control.ControlError) as ei:
        control.create_experiment_and_run(
            root, dataset_version="wiki-default-v2dev", init_mode="business",
            max_iterations=2, budget={"max_model_calls": 10,
                                      "max_tool_calls": 4, "max_seconds": 120},
            model_mode="real", experience="full")
    assert ei.value.code == "review_required_for_v2"
    # 携带 review=v2 可正常创建（不落入缺 review 的不可运行记录）
    created = control.create_experiment_and_run(
        root, dataset_version="wiki-default-v2dev", init_mode="business",
        max_iterations=2, budget={"max_model_calls": 10,
                                  "max_tool_calls": 4, "max_seconds": 120},
        model_mode="real", experience="full", review="v2")
    assert created["status"] == "queued"


def test_v2_create_with_review_v2_records_both(tmp_path, monkeypatch):
    from app.core.skill_evolution import control, skill_store
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    root = tmp_path / "lab"
    root.mkdir(parents=True, exist_ok=True)
    created = control.create_experiment_and_run(
        root, dataset_version="wiki-default-v2dev", init_mode="business",
        max_iterations=2,
        budget={"max_model_calls": 30, "max_tool_calls": 10,
                "max_seconds": 600},
        model_mode="real", experience="full", review="v2")
    db = skill_store.session_for(root)
    try:
        from app.models import evolution as ev
        from sqlalchemy import text as t
        erow = db.execute(t(
            "SELECT grader_version, runner_config_json FROM "
            "evolution_experiments WHERE experiment_id=:e"),
            {"e": created["experiment_id"]}).fetchone()
        from app.core.skill_evolution.grader_registry import GRADER_V2 as _GV2
        assert erow.grader_version == _GV2
        assert "review" in erow.runner_config_json
        rrow = db.execute(t(
            "SELECT config_json FROM evolution_runs WHERE run_id=:r"),
            {"r": created["run_id"]}).fetchone()
        rcfg = json.loads(rrow.config_json)
        assert rcfg["runner"]["review"] == "v2"
        assert rcfg["runner"]["real"] is True
    finally:
        db.close()


def test_v1_dataset_with_review_v2_rejected(tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    root = tmp_path / "lab"
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(control.ControlError) as ei:
        control.create_experiment_and_run(
            root, dataset_version="wiki-default-v3", init_mode="business",
            max_iterations=2, budget={"max_model_calls": 10,
                                      "max_tool_calls": 4, "max_seconds": 120},
            model_mode="real", experience="full", review="v2")
    assert ei.value.code == "grader_review_mismatch"


# --- rollback 幂等重放：rev/事件各一次 ---
def test_rollback_replay_same_body_counts_once(biz_db, tmp_path, monkeypatch):
    from app.core.skill_evolution import business_ops as bops
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    monkeypatch.setattr(settings,
                        "wikiskill_evolution_allow_simulated_promotion", True)
    exp = biz_db
    a1 = _add(exp, "default", 1, "A")
    a2 = _add(exp, "default", 2, "B")
    bops._materialize_set_binding(exp, "ws-1", members=[a1],
                                  expected_rev=None, expected_set_hash=None,
                                  created_by="t")
    exp.commit()
    bops.promote(biz_db, exp, _exp(exp, "e1", [a1]), workspace_id="ws-1",
                 created_by="t", allow_simulated=True, idempotency_key="p1")
    bops.promote(biz_db, exp, _exp(exp, "e2", [a2]), workspace_id="ws-1",
                 created_by="t", allow_simulated=True, idempotency_key="p2")
    st = bops.business_state(biz_db, "ws-1")
    body = {"expected_rev": st["expected_rev"],
            "expected_set_hash": st["expected_set_hash"]}
    k = "rb-same"
    rb1 = bops.rollback(biz_db, workspace_id="ws-1", created_by="t",
                        expected_rev=body["expected_rev"],
                        expected_set_hash=body["expected_set_hash"],
                        idempotency_key=k)
    assert rb1["changed"] is True and rb1["rev"] == 3
    n_ev_1 = exp.execute(text(
        "SELECT count(*) FROM evolution_business_events")).fetchone()[0]
    # 同 body 同 key 重放（模拟响应丢失后的客户端重试）
    rb2 = bops.rollback(biz_db, workspace_id="ws-1", created_by="t",
                        expected_rev=body["expected_rev"],
                        expected_set_hash=body["expected_set_hash"],
                        idempotency_key=k)
    assert rb2.get("replay") is True
    n_ev_2 = exp.execute(text(
        "SELECT count(*) FROM evolution_business_events")).fetchone()[0]
    assert n_ev_2 == n_ev_1                # 审计只新增一次
    st2 = bops.business_state(biz_db, "ws-1")
    assert st2["current"]["rev"] == 3      # rev 只增加一次（未二次回退）
    # 同 key 不同 body → 422
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.rollback(biz_db, workspace_id="ws-1", created_by="t",
                      expected_rev=1, expected_set_hash="x" * 64,
                      idempotency_key=k)
    assert ei.value.code == "idem_content_conflict"
    # 陈旧 rev + 新键 → 409（不自动再回退一层）
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.rollback(biz_db, workspace_id="ws-1", created_by="t",
                      expected_rev=1, expected_set_hash="y" * 64,
                      idempotency_key="rb-new")
    assert ei2.value.code == "binding_conflict"


# --- meta 脱敏（API 层） ---
def test_meta_fields_redacted_and_complete(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.jwt_utils import get_current_user
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    with TestClient(app) as client:
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "a", "groups": ["__local_admin__"]}
        r = client.get("/api/evolution-admin/meta")
        assert r.status_code == 200
        data = r.json()
        blob = json.dumps(data, ensure_ascii=False)
        for secret in ("Bearer ", "llm_api_key", "Authorization"):
            assert secret.lower() not in blob.lower()
        assert "real_start_enabled" in data
        ds = {d["dataset_version"]: d for d in data["datasets"]}
        v2 = ds.get("wiki-default-v2dev")
        assert v2 and v2["required_review"] == "v2"
        assert v2["grader_calibration"] == "engineering_only"
        assert v2["allow_business_promotion"] is False
        # 人工标签已批准、真实校准未完成（meta 机器可读）
        assert v2["labels_approved"] is True
        assert v2["real_calibration"] is False
        assert "人工标签批准" in data["grader_note"]
        v1 = ds.get("wiki-default-v3")
        assert v1 and v1["required_review"] is None
