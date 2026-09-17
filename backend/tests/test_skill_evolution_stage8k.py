"""B3 反例测试：CAS 集合哈希冲突、请求级幂等（重放/同键不同内容）、回退后再晋升、
审计失败整体回滚（business_ops，隔离库）。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core.skill_evolution import business_ops as bops, skill_store
from app.models.database import init_db
from app.models import evolution as evmod

BACKEND = Path(__file__).resolve().parent.parent
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"


def _make(tmp_path, monkeypatch, v2_exp=False):
    from app.core.skill_evolution import runenv, gating
    from app.core.skill_evolution.contracts import load_dataset
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    ds_dir = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v3"
    db = skill_store.session_for(root)
    ds = load_dataset(ds_dir)
    seed = skill_store.import_seed(db, skill_store.load_package(
        REPO_SEED, source_type="builtin_seed"))
    v = skill_store.get_version(db, seed)
    member = {"skill_id": v.skill_id, "version_id": v.version_id,
              "content_hash": v.content_hash, "seq": v.seq}
    exp = gating.create_experiment(
        db, workspace_id="ws-prod-1", domain="wiki_compile.default",
        dataset=ds, grader_version=ds.grader_version,
        runner_config={"profile": "faithful"},
        pipeline_key="wiki.default", pipeline_version="3",
        runtime_ref=skill_store.RUNTIME_REF,
        val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
        initial_members=[member])
    exp_id = str(exp.experiment_id)
    import sqlalchemy as sa
    js0 = json.dumps({"mode": "skill", "members": [member]},
                      ensure_ascii=False)
    db.execute(sa.text(
        "UPDATE evolution_experiments SET best_skill_set_json=:j, "
        "best_score_passed=5, best_score_total=5, best_evaluation_id='ev-ok', "
        "baseline_evaluation_id='ev-ok' WHERE experiment_id=:e"),
        {"j": js0, "e": exp_id})
    db.execute(sa.text(
        "INSERT OR IGNORE INTO evolution_evaluations (evaluation_id, "
        "idempotency_key, experiment_id, kind, proposal_id, base_set_json, "
        "candidate_set_json, task_ids_json, per_task_results_json, "
        "config_json, main_passed, main_total, valid) VALUES "
        "('ev-ok', 'idem-8k', :e, 'candidate', 'p', '{}', '{}', '[]', '[]', "
        "'{}', 5, 5, 1)"), {"e": exp_id})
    db.commit()
    if v2_exp:
        # 第二实验接受 default:0002（在同一实验库新增版本并指向）
        pkg = skill_store.build_package_from_texts(
            v.skill_md + "\n- B3 v2 变更\n", v.purpose_md,
            source_type="manual_seed")
        v2 = skill_store.add_version(db, pkg, parent_version_id=v.version_id,
                                     created_by="b3")
        v2row = skill_store.get_version(db, v2)
        m2 = {"skill_id": v2row.skill_id, "version_id": v2row.version_id,
              "content_hash": v2row.content_hash, "seq": v2row.seq}
        import sqlalchemy as sa
        js = json.dumps({"mode": "skill", "members": [m2]}, ensure_ascii=False)
        db.execute(sa.text(
            "UPDATE evolution_experiments SET current_skill_set_json=:j, "
            "best_skill_set_json=:j WHERE experiment_id=:e"),
            {"j": js, "e": exp_id})
        db.commit()
        return root, exp_id, v2row.version_id
    db.commit()
    return root, exp_id, v.version_id


def _biz(tmp_path, monkeypatch):
    path = tmp_path / "biz.db"
    engine = create_engine(f"sqlite:///{(path).as_posix()}")
    init_db(engine)
    evmod.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "wikiskill_evolution_allow_simulated_promotion",
                        True)
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    db = maker()
    db.execute(__import__("sqlalchemy").text(
        "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
        "status) VALUES ('ws-prod-1','k','ws-prod-1','public','public','active')"))
    db.commit()
    return db, engine


def _exp(db_exp, exp_id):
    row = db_exp.execute(__import__("sqlalchemy").text(
        "SELECT * FROM evolution_experiments WHERE experiment_id=:e"),
        {"e": exp_id}).fetchone()
    return SimpleNamespace(**{k: row._mapping[k] for k in row._mapping.keys()})


def test_promote_rollback_re_promote_and_replay(tmp_path, monkeypatch):
    root, exp_id, v1 = _make(tmp_path, monkeypatch)
    _root2, exp_id2, v2 = _make(tmp_path, monkeypatch, v2_exp=True)
    db_exp = skill_store.session_for(root)
    db_exp2 = skill_store.session_for(root)
    biz, engine = _biz(tmp_path, monkeypatch)
    try:
        e1 = _exp(db_exp, exp_id)
        e2 = _exp(db_exp2, exp_id2)
        # 晋升 v1（k1）→ 晋升 v2（k2）→ 回退 v1（k3）→ 再用新键 k4 晋升 v2
        r0 = bops.promote(biz, db_exp, e1, workspace_id="ws-prod-1",
                          created_by="t", allow_simulated=True,
                          idempotency_key="k1")
        assert r0["version_id"] == v1
        r1 = bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                          created_by="t", allow_simulated=True,
                          idempotency_key="k2")
        assert r1["changed"] is True and r1["version_id"] == v2
        st = bops.business_state(biz, "ws-prod-1")
        rb = bops.rollback(biz, workspace_id="ws-prod-1", created_by="t",
                           expected_set_hash=st["current_set_hash"],
                           idempotency_key="k3")
        assert rb["changed"] is True and rb["version_id"] == v1
        st2 = bops.business_state(biz, "ws-prod-1")
        # 回退后再晋升同一集合 v2（新操作键 k4；不以目标版本永久去重）
        r2 = bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                          created_by="t", allow_simulated=True,
                          expected_set_hash=st2["current_set_hash"],
                          idempotency_key="k4")
        assert r2["changed"] is True and r2["version_id"] == v2
        # 同请求重放（同键同指纹，含原始预期令牌）→ 返回原结果，不产生新事件
        replay = bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                              created_by="t", allow_simulated=True,
                              expected_set_hash=st2["current_set_hash"],
                              idempotency_key="k4")
        assert replay.get("replay") is True
        import sqlalchemy as sa
        n = biz.execute(sa.text(
            "SELECT count(*) c FROM evolution_business_events "
            "WHERE idempotency_key='k4'")).fetchone().c
        assert n == 1
    finally:
        db_exp.close(); db_exp2.close(); biz.close(); engine.dispose()


def test_same_key_different_content_rejected(tmp_path, monkeypatch):
    root, exp_id, v1 = _make(tmp_path, monkeypatch)
    _root2, exp_id2, v2 = _make(tmp_path, monkeypatch, v2_exp=True)
    db_exp = skill_store.session_for(root)
    db_exp2 = skill_store.session_for(root)
    biz, engine = _biz(tmp_path, monkeypatch)
    try:
        e1 = _exp(db_exp, exp_id)
        e2 = _exp(db_exp2, exp_id2)
        bops.promote(biz, db_exp, e1, workspace_id="ws-prod-1",
                     created_by="t", allow_simulated=True,
                     idempotency_key="shared-key")   # 目标 v1
        with pytest.raises(bops.BusinessOpsError) as ei:
            bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                         created_by="t", allow_simulated=True,
                         idempotency_key="shared-key")  # 目标 v2，同键
        assert ei.value.code == "idem_content_conflict"
    finally:
        db_exp.close(); db_exp2.close(); biz.close(); engine.dispose()


def test_stale_expected_hash_conflict_409_no_write(tmp_path, monkeypatch):
    root, exp_id, v1 = _make(tmp_path, monkeypatch)
    _root2, exp_id2, v2 = _make(tmp_path, monkeypatch, v2_exp=True)
    db_exp = skill_store.session_for(root)
    db_exp2 = skill_store.session_for(root)
    biz, engine = _biz(tmp_path, monkeypatch)
    try:
        e1 = _exp(db_exp, exp_id)
        e2 = _exp(db_exp2, exp_id2)
        bops.promote(biz, db_exp, e1, workspace_id="ws-prod-1",
                     created_by="t", allow_simulated=True)
        state = bops.business_state(biz, "ws-prod-1")
        # 并发把绑定改到 v2（绕过本请求）
        bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                     created_by="t", allow_simulated=True)
        with pytest.raises(bops.BusinessOpsError) as ei:
            bops.promote(biz, db_exp2, e2, workspace_id="ws-prod-1",
                         created_by="t", allow_simulated=True,
                         expected_set_hash=state["current_set_hash"])
        assert ei.value.code == "binding_conflict"
    finally:
        db_exp.close(); db_exp2.close(); biz.close(); engine.dispose()


def test_audit_failure_rolls_back_binding(tmp_path, monkeypatch):
    root, exp_id, v1 = _make(tmp_path, monkeypatch)
    db_exp = skill_store.session_for(root)
    biz, engine = _biz(tmp_path, monkeypatch)
    try:
        e1 = _exp(db_exp, exp_id)
        orig = bops._append_event

        def boom(*a, **k):
            raise RuntimeError("audit write failed")
        monkeypatch.setattr(bops, "_append_event", boom)
        try:
            bops.promote(biz, db_exp, e1, workspace_id="ws-prod-1",
                         created_by="t", allow_simulated=True)
            raise AssertionError("应抛审计失败")
        except RuntimeError:
            biz.rollback()                        # 事务整体回滚（不留下半写）
        state = bops.business_state(biz, "ws-prod-1")
        assert state["current"] is None           # 绑定未被半写
        import sqlalchemy as sa
        n = biz.execute(sa.text(
            "SELECT count(*) c FROM evolution_business_events")).fetchone().c
        assert n == 0
        monkeypatch.setattr(bops, "_append_event", orig)
    finally:
        db_exp.close(); biz.close(); engine.dispose()
