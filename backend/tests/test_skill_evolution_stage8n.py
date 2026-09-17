"""WikiSkill 业务闭环整改包反例测试（business_ops / skill_store / 编译注入）。

里程碑对应：
- M1：成功请求重放优先于 CAS（E）、同键不同请求（F）、预览后目标变化（G）、
  首绑显式令牌、完整集合回退（含初始无绑定）与多成员恢复（B）、并发仅一方成功/
  首次绑定竞争（H/I，stage8m 已覆盖，此处补显式令牌并发）、skill_store 写入口
  物化一致性、P52 写缺列 fail closed；
- M2：双技能晋升后编译注入包含全部指令（A）、跨中断冻结不变/新 run 读新集合
  （C）、真实证据资格（J）、生产模式旁路拒绝（K）。
全部隔离 SQLite + 本地 stub；不调用真实模型、不操作生产。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import skill_store
from app.models.database import init_db
from app.models import evolution as evmod

WS = "ws-prod-1"
GRADER = "wiki-default-grader/v1"


# ---------------------------------------------------------------------------
# 工具（实验库版本/集合文档 + 业务库）
# ---------------------------------------------------------------------------


def _payload_hash(skill_id, skill_md, purpose_md):
    payload = json.dumps(
        {"skill_id": skill_id, "domain": "wiki_compile.default",
         "runtime_ref": "wiki.compile.default.runtime/v1",
         "schema_version": "skill-evolution/v1",
         "skill_md": skill_md, "purpose_md": purpose_md},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _md(skill_id, seq, marker):
    skill_md = (f"---\nskill_id: {skill_id}\ndomain: wiki_compile.default\n"
                "runtime_ref: wiki.compile.default.runtime/v1\n"
                "schema_version: 1\n---\n"
                f"# {skill_id}\n\n## 适用条件\n- 通用\n"
                "## 不适用条件\n- 无\n"
                f"## 操作步骤\n- {marker}\n")
    purpose_md = ("## 来源\n- 8n\n## 改进目的\n- 闭环\n## 演化历史\n- v1\n")
    return skill_md, purpose_md


def _add_ver(db, skill_id, seq, marker, *, mutate=False):
    version_id = f"{skill_id}:{seq:04d}"
    skill_md, purpose_md = _md(skill_id, seq, marker)
    ch = _payload_hash(skill_id, skill_md, purpose_md)
    db.execute(text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:vid,:sid,:seq,'skill-evolution/v1','wiki_compile.default',"
        "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
        "'8n',CURRENT_TIMESTAMP)"),
        {"vid": version_id, "sid": skill_id, "seq": seq, "sm": skill_md,
         "pm": purpose_md, "ch": ch})
    if mutate:
        db.execute(text(
            "UPDATE evolution_skill_versions SET skill_md=skill_md || ' ' "
            "WHERE version_id=:v"), {"v": version_id})
    db.commit()
    return {"skill_id": skill_id, "version_id": version_id,
            "content_hash": ch, "seq": seq, "skill_md": skill_md,
            "purpose_md": purpose_md}


def _claim(v):
    return {"skill_id": v["skill_id"], "version_id": v["version_id"],
            "content_hash": v["content_hash"], "seq": v["seq"]}


def _accepted(claims):
    specs = bops.canonical_binding_members(claims)
    return {"mode": "versions" if specs else "empty", "members": specs}


def _exp_row(exp_id, doc):
    js = json.dumps(doc, ensure_ascii=False)
    return SimpleNamespace(experiment_id=exp_id, best_skill_set_json=js,
                           current_skill_set_json=js, best_score_passed=5,
                           best_score_total=5, grader_version=GRADER,
                           dataset_version="wiki-default-v3")


@pytest.fixture()
def exp_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'exp.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    evmod.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture()
def biz_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    engine = create_engine(f"sqlite:///{(tmp_path / 'biz.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    init_db(engine)
    evmod.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text(
        "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
        "status) VALUES ('ws-prod-1','k','8n','public','public','active')"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _count(db, table, ws=True):
    where = (" WHERE kind='business' AND workspace_id='ws-prod-1'"
             if table == "evolution_skill_bindings" else "")
    return db.execute(text(
        f"SELECT count(*) c FROM {table}{where}")).fetchone().c


# ---------------------------------------------------------------------------
# E. 成功后原请求（携带已过期预期令牌）重放 → 原结果，而不是 binding_conflict
# ---------------------------------------------------------------------------


def test_replay_after_success_with_stale_expected_returns_original(
        exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    a2 = _add_ver(exp_db, "default", 2, "A-v2")
    b1 = _add_ver(exp_db, "refcheck", 1, "B")
    doc_a = _accepted([_claim(a1)])
    doc_b = _accepted([_claim(a2), _claim(b1)])
    # 原请求：晋升多成员集合 B，预期令牌 = 首绑令牌（当前无绑定）
    key = "op-replay"
    r0 = bops.promote(biz_db, exp_db, _exp_row("e-b", doc_b),
                      workspace_id=WS, created_by="t", allow_simulated=True,
                      explicit_cas=True,
                      expected_rev=bops.FIRST_BIND_REV,
                      expected_set_hash=bops.NO_BINDING_HASH,
                      idempotency_key=key)
    assert r0["changed"] is True and r0["rev"] == 1
    # 另一操作把绑定切到 A（新键），当前 rev=2 —— 原预期令牌已过期
    bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="other-op")
    st = bops.business_state(biz_db, WS)
    assert st["current"]["rev"] == 2
    # 原样重放（同键 + 原始目标 + 已过期预期令牌）→ 必须返回原结果，不写、不冲突
    replay = bops.promote(biz_db, exp_db, _exp_row("e-b", doc_b),
                          workspace_id=WS, created_by="t",
                          allow_simulated=True, explicit_cas=True,
                          expected_rev=bops.FIRST_BIND_REV,
                          expected_set_hash=bops.NO_BINDING_HASH,
                          idempotency_key=key)
    assert replay.get("replay") is True
    assert [m["version_id"] for m in replay["members"]] == [
        "default:0002", "refcheck:0001"]
    assert _count(biz_db, "evolution_business_events") == 2  # 未重复写


# F. 同键不同请求（不同预期令牌）拒绝
def test_same_key_different_expected_rejected(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    doc_a = _accepted([_claim(a1)])
    bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                 expected_set_hash=bops.NO_BINDING_HASH,
                 idempotency_key="k-diff")
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=1,
                     expected_set_hash=bops.binding_set_hash(
                         [_claim(a1)]),
                     idempotency_key="k-diff")
    assert ei.value.code == "idem_content_conflict"


# G. 显式令牌：首绑令牌 / 已绑定误发首绑令牌 / 陈旧令牌 / 缺令牌
def test_explicit_cas_token_semantics(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    a2 = _add_ver(exp_db, "default", 2, "A-v2")
    doc_a = _accepted([_claim(a1)])
    doc_b = _accepted([_claim(a2)])
    # 缺令牌（explicit_cas 正式入口）→ 422
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     explicit_cas=True)
    assert ei.value.code == "cas_token_incomplete"
    # 有绑定行却发首绑令牌 → 409
    bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                 expected_set_hash=bops.NO_BINDING_HASH)
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.promote(biz_db, exp_db, _exp_row("e-b", doc_b),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                     expected_set_hash=bops.NO_BINDING_HASH)
    assert ei2.value.code == "binding_conflict"
    # 预览后另一操作切换绑定 → 原令牌被拒（G）
    st = bops.business_state(biz_db, WS)
    token_rev, token_hash = st["expected_rev"], st["expected_set_hash"]
    bops.promote(biz_db, exp_db, _exp_row("e-b", doc_b),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="move")  # rev -> 2
    with pytest.raises(bops.BusinessOpsError) as ei3:
        bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=token_rev,
                     expected_set_hash=token_hash)
    assert ei3.value.code == "binding_conflict"


# G2. 提交绑定预览目标：实验接受集合前移 → 拒绝（不悄悄改打新集合）
def test_previewed_target_no_silent_change(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    a2 = _add_ver(exp_db, "default", 2, "A-v2")
    preview_doc = _accepted([_claim(a1)])
    moved_doc = _accepted([_claim(a2)])   # 预览后实验已接受新集合
    # 执行时（非重放）请求绑定预览目标 A，但实验已前移到 A-v2
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-a", moved_doc),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     target_members=[_claim(a1)])
    assert ei.value.code == "binding_conflict"
    assert "重新预览" in ei.value.message


# B. 双成员回退恢复完整历史集合 + 初始无绑定；重复同键不自动改算
def test_rollback_multi_and_initial_none(exp_db, biz_db):
    d1 = _add_ver(exp_db, "default", 1, "A")
    r1 = _add_ver(exp_db, "refcheck", 1, "B")
    single = _add_ver(exp_db, "default", 2, "A-v2")
    multi = _accepted([_claim(d1), _claim(r1)])
    single_doc = _accepted([_claim(single)])
    # 首绑：多成员集合；随后切到单成员 A-v2
    bops.promote(biz_db, exp_db, _exp_row("e-m", multi),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                 expected_set_hash=bops.NO_BINDING_HASH, idempotency_key="p1")
    bops.promote(biz_db, exp_db, _exp_row("e-s", single_doc),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="p2")
    # 回退 → 完整多成员集合恢复（读回校验全部成员/内容）
    rb = bops.rollback(biz_db, workspace_id=WS, created_by="t",
                       idempotency_key="rb1")
    assert rb["changed"] is True
    assert [m["version_id"] for m in rb["members"]] == [
        "default:0001", "refcheck:0001"]
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert [m["version_id"] for m in st["members"]] == [
        "default:0001", "refcheck:0001"]
    assert st["members"][1]["content_hash"] == r1["content_hash"]
    # 再切换单成员并回退 → 再次恢复多成员（历史链内反复切换）
    bops.promote(biz_db, exp_db, _exp_row("e-s", single_doc),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="p3")
    rb2 = bops.rollback(biz_db, workspace_id=WS, created_by="t")
    assert [m["version_id"] for m in rb2["members"]] == [
        "default:0001", "refcheck:0001"]


def test_rollback_to_initial_none(exp_db, biz_db):
    d1 = _add_ver(exp_db, "default", 1, "A")
    r1 = _add_ver(exp_db, "refcheck", 1, "B")
    multi = _accepted([_claim(d1), _claim(r1)])
    # 从无绑定直接晋升多成员集合 → 回退应恢复“初始无绑定”状态
    bops.promote(biz_db, exp_db, _exp_row("e-m", multi),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                 expected_set_hash=bops.NO_BINDING_HASH, idempotency_key="p1")
    st0 = bops.business_state(biz_db, WS)
    rb = bops.rollback(biz_db, workspace_id=WS, created_by="t",
                       expected_rev=st0["expected_rev"],
                       expected_set_hash=st0["expected_set_hash"],
                       idempotency_key="rb-none")
    assert rb["changed"] is True and rb["members"] == []
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["kind"] == "empty" and st["members"] == []
    assert st["canonical_set_hash"] == bops.binding_set_hash([])
    assert st["canonical_set_hash"] != bops.NO_BINDING_HASH


def test_rollback_same_key_returns_original_not_recomputed(exp_db, biz_db):
    d1 = _add_ver(exp_db, "default", 1, "A")
    d2 = _add_ver(exp_db, "default", 2, "A-v2")
    bops.promote(biz_db, exp_db, _exp_row("e1", _accepted([_claim(d1)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k1")
    bops.promote(biz_db, exp_db, _exp_row("e2", _accepted([_claim(d2)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k2")
    st = bops.business_state(biz_db, WS)
    rb = bops.rollback(biz_db, workspace_id=WS, created_by="t",
                       expected_rev=st["expected_rev"],
                       expected_set_hash=st["expected_set_hash"],
                       idempotency_key="rb-k")
    assert rb["members"][0]["version_id"] == "default:0001"
    # 此后绑定被再次推进到 v2（历史变化）→ 同键重放仍返回原结果
    bops.promote(biz_db, exp_db, _exp_row("e2", _accepted([_claim(d2)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k3")
    replay = bops.rollback(biz_db, workspace_id=WS, created_by="t",
                           expected_rev=st["expected_rev"],
                           expected_set_hash=st["expected_set_hash"],
                           idempotency_key="rb-k")
    assert replay.get("replay") is True
    assert replay["members"][0]["version_id"] == "default:0001"


# skill_store 写入口物化一致性（不得只改 version_id 留下过期 members_json）
def test_skill_store_bind_materializes_consistent(exp_db, biz_db):
    v1 = _add_ver(exp_db, "default", 1, "A")
    v2 = _add_ver(exp_db, "default", 2, "B")
    skill_store.bind(exp_db, kind="experiment", workspace_id="ws-x",
                     domain=skill_store.DEFAULT_DOMAIN, version_id=v1["version_id"])
    skill_store.bind(exp_db, kind="experiment", workspace_id="ws-x",
                     domain=skill_store.DEFAULT_DOMAIN, version_id=v2["version_id"])
    row = exp_db.execute(text(
        "SELECT set_kind, skill_id, version_id, rev, set_hash, members_json "
        "FROM evolution_skill_bindings WHERE kind='experiment' AND "
        "workspace_id='ws-x'")).fetchone()
    assert row.version_id == v2["version_id"]
    assert row.rev == 2
    members = json.loads(row.members_json)
    assert [m["version_id"] for m in members] == [v2["version_id"]]
    assert row.set_hash == bops.binding_set_hash(members)
    assert members == [{"skill_id": "default", "version_id": "default:0002",
                        "content_hash": v2["content_hash"], "seq": 2}]


# P52 写缺列 fail closed（不带 legacy 写分支绕过）
def test_business_promote_fails_closed_without_p52(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    for col in ("members_json", "set_hash", "rev"):
        biz_db.execute(text(
            f"ALTER TABLE evolution_skill_bindings DROP COLUMN {col}"))
    biz_db.commit()
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-a", _accepted([_claim(a1)])),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "binding_p52_required"


# J. 真实证据资格：real 空 run / 无关 run / 集合锚点缺失 / 生产环境
def _insert_run(db, exp_id, *, real, completed=True, calls=4,
                dataset="wiki-default-v3"):
    db.execute(text(
        "INSERT OR IGNORE INTO evolution_runs (run_id, experiment_id, "
        "workspace_id, domain, dataset_version, init_mode, config_json, "
        "initial_skill_set_json, initial_experience_snapshot_json, "
        "max_iterations, current_iteration, status, stop_reason, "
        "used_model_calls, used_tool_calls, used_estimated_chars, "
        "reserved_in_flight_json, pause_requested, cancel_requested) VALUES "
        "(:rid, :e, 'ws-prod-1', 'wiki_compile.default', :ds, 'business', "
        ":cfg, '{}', '{}', 1, 1, :st, 'max_iterations', :calls, 0, 0, '[]', "
        "0, 0)"),
        {"rid": f"run-{exp_id}", "e": exp_id, "ds": dataset,
         "cfg": json.dumps({"runner": {"mode": "real" if real else "simulated",
                                       "real": real}}, ensure_ascii=False),
         "st": "completed" if completed else "failed", "calls": calls})
    db.commit()


def _insert_eval(db, exp_id, valid=True):
    db.execute(text(
        "INSERT OR IGNORE INTO evolution_evaluations (evaluation_id, "
        "idempotency_key, experiment_id, kind, proposal_id, base_set_json, "
        "candidate_set_json, task_ids_json, per_task_results_json, "
        "config_json, main_passed, main_total, valid) VALUES (:ev,'idem',:e,"
        "'candidate','p','{}','{}','[]','[]','{}',5,5,:v)"),
        {"ev": f"eval-{exp_id}", "e": exp_id, "v": 1 if valid else 0})
    db.commit()


def _insert_gate(db, exp_id, next_members):
    db.execute(text(
        "INSERT OR IGNORE INTO evolution_gate_events (event_id, "
        "experiment_id, candidate_evaluation_id, best_evaluation_id, "
        "decision, reason, candidate_version_ids_json, previous_set_json, "
        "next_set_json, status_rev_before, status_rev_after, created_at) "
        "VALUES (:g,:e,'eval-x','eval-x','accepted','ok','[]','{}',:ns,1,2,"
        "CURRENT_TIMESTAMP)"),
        {"g": f"gate-{exp_id}", "e": exp_id,
         "ns": json.dumps({"members": next_members}, ensure_ascii=False)})
    db.commit()


def test_real_empty_run_not_promotable(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    doc = _accepted([_claim(a1)])
    # real 配置但零调用/失败 → 不是真实证据；未放行模拟 → 阻止
    _insert_run(exp_db, "e-j1", real=True, completed=False, calls=0)
    _insert_eval(exp_db, "e-j1")
    pv = bops.promotion_preview(exp_db, _exp_row("e-j1", doc),
                                workspace_id=WS, allow_simulated=False)
    assert pv["promotable"] is False
    assert pv["real_evidence"] is False
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-j1", doc),
                     workspace_id=WS, created_by="t", allow_simulated=False)
    assert ei.value.code == "promotion_blocked"


def test_real_evidence_without_gate_anchor_blocked(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    doc = _accepted([_claim(a1)])
    _insert_run(exp_db, "e-j2", real=True)       # real 证据真实存在
    _insert_eval(exp_db, "e-j2")
    # 但没有 accepted 门控记录锚点 → 真实证据拒绝（不把配置 real 当证据）
    pv = bops.promotion_preview(exp_db, _exp_row("e-j2", doc),
                                workspace_id=WS, allow_simulated=True)
    assert pv["real_evidence"] is True
    assert pv["gate_anchor_ok"] is False
    assert pv["promotable"] is False
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-j2", doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "promotion_blocked"
    assert _count(biz_db, "evolution_skill_bindings") == 0


def test_real_evidence_with_matching_gate_anchor_promotable(exp_db, biz_db):
    a1 = _add_ver(exp_db, "default", 1, "A")
    doc = _accepted([_claim(a1)])
    _insert_run(exp_db, "e-j3", real=True)
    _insert_eval(exp_db, "e-j3")
    _insert_gate(exp_db, "e-j3", [_claim(a1)])   # 精确集合锚点
    pv = bops.promotion_preview(exp_db, _exp_row("e-j3", doc),
                                workspace_id=WS, allow_simulated=True)
    assert pv["real_evidence"] is True and pv["gate_anchor_ok"] is True
    assert pv["promotable"] is True
    res = bops.promote(biz_db, exp_db, _exp_row("e-j3", doc),
                       workspace_id=WS, created_by="t", allow_simulated=True)
    assert res["changed"] is True
    # 测试记录永不标为真实效果
    assert res["effect_verified"] is False


# K. 生产模式模拟旁路拒绝（即使误设开关 + 请求 allow_simulated=true）
def test_production_env_rejects_even_with_all_flags(exp_db, biz_db,
                                                    monkeypatch):
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "production")
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    a1 = _add_ver(exp_db, "default", 1, "A")
    doc = _accepted([_claim(a1)])
    _insert_run(exp_db, "e-k", real=True)
    _insert_eval(exp_db, "e-k")
    _insert_gate(exp_db, "e-k", [_claim(a1)])
    pv = bops.promotion_preview(exp_db, _exp_row("e-k", doc),
                                workspace_id=WS, allow_simulated=True)
    assert pv["promotable"] is False
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-k", doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "promotion_blocked"
    assert _count(biz_db, "evolution_skill_bindings") == 0


# 显式令牌下双连接并发：只一方成功（数据库层，非进程锁）
def test_concurrent_explicit_token_single_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    eng = create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}",
                        connect_args={"check_same_thread": False})
    evmod.metadata.create_all(eng)
    init_db(eng)
    db1 = sessionmaker(bind=eng)()
    d1 = _add_ver(db1, "default", 1, "A")
    db1.execute(text(
        "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, domain,"
        " set_kind, skill_id, version_id, rev, set_hash, members_json, "
        "created_by, created_at, updated_at) VALUES ('b1','business','ws-x',"
        "'wiki_compile.default','skill','default','default:0001',1,:h,:m,"
        "'t',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
        {"h": bops.binding_set_hash([_claim(d1)]),
         "m": json.dumps([_claim(d1)])})
    db1.commit()
    db1.close()
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(i):
        db = sessionmaker(bind=create_engine(
            f"sqlite:///{(tmp_path / 'c.db').as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=__import__("sqlalchemy.pool", fromlist=["NullPool"]).
            NullPool))()
        try:
            barrier.wait()
            out = bops._materialize_set_binding(
                db, "ws-x", members=[_claim(d1)],
                expected_rev=1,
                expected_set_hash=bops.binding_set_hash([_claim(d1)]),
                created_by=f"w{i}")
            db.commit()
            results.append(out)
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 1 and len(errors) == 1
    assert errors[0] == "binding_conflict"


# ---------------------------------------------------------------------------
# A / C. 编译注入：双技能晋升后新编译请求包含全部指令；同一 run 跨中断冻结不变
# ---------------------------------------------------------------------------


def _compile_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _graph_noop(**kw):
    return None


def _md_payload():
    return {"summary": "摘要", "content": "正文" + "x" * 80}


def _ensure_ws(db, ws_id="ws-prod-1"):
    row = db.execute(text(
        "SELECT id FROM wiki_workspaces WHERE id=:w"), {"w": ws_id}).fetchone()
    if row is None:
        db.execute(text(
            "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
            "status) VALUES (:w,'k',:w,'public','public','active')"), {"w": ws_id})
        db.commit()
    return ws_id


@pytest.fixture()
def compile_env(tmp_path, monkeypatch):
    """业务+编译共用库：完整 business schema + evolution schema；绑定即业务库。"""
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    monkeypatch.setattr(settings,
                        "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled", True)
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'compile.db').as_posix()}",
        connect_args={"check_same_thread": False})
    init_db(engine)
    evmod.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _ensure_ws(db)
    yield {"engine": engine, "db": db}
    db.close()
    engine.dispose()


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


def _page(env, pid, content):
    from app.core.wiki_workspace.routing import ensure_notebook_workspace
    from app.models.database import Notebook, Page
    db = env["db"]
    if db.query(Notebook).filter(Notebook.id == "nb-8n").first() is None:
        db.add(Notebook(id="nb-8n", name="库", group_id="eng"))
        db.flush()
    ws = ensure_notebook_workspace(db, db.get(Notebook, "nb-8n"))
    if db.get(Page, pid) is None:
        db.add(Page(id=pid, notebook_id="nb-8n", title=pid,
                    content=content, content_hash=_compile_hash(content),
                    wiki_dirty=True))
    db.commit()
    return ws


def _compile_run(env, ws, capture, *, fail_first_synthesis=False):
    from app.core.wiki_pipeline import executor as exe
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        ARTIFACT_SCHEMA_WIKI_BATCH, ARTIFACT_TYPE_WIKI_BATCH_INPUT,
        _batch_input_hash, _page_full_hash)
    from app.models.database import KnowledgeCompileArtifact as Artifact
    db = env["db"]
    ids = ["p-8n"]
    state = {"fail_n": 0}

    def _llm(messages, context="", timeout=120.0):
        capture.append(list(messages or []))
        if fail_first_synthesis and context in ("wiki-synthesis",
                                                "wiki-mapreduce"):
            state["fail_n"] += 1
            if state["fail_n"] == 1:
                raise RuntimeError("boom-synthesis-once")
        if context == "wiki-ingest-page":
            return {"worthy": True,
                    "ops": [{"action": "create", "title": "主题8N",
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


def test_compile_two_member_promotion_injects_all_instructions(
        exp_db, compile_env, monkeypatch):
    """A. 双技能晋升后，新编译请求包含全部正确指令（顺序=绑定顺序）。"""
    from app.core.skill_evolution import business_ops as _bops
    _boot(compile_env["db"])
    d1 = _add_ver(exp_db, "default", 1, "指令-DEFAULT-8N")
    r1 = _add_ver(exp_db, "refcheck", 1, "指令-REFCHECK-8N")
    doc = _accepted([_claim(d1), _claim(r1)])
    ws = _page(compile_env, "p-8n", "普通说明文字足够长用于编译构建测试。" * 5)
    # 晋升目标作用域 = 编译工作区（同一业务库）
    res = _bops.promote(compile_env["db"], exp_db, _exp_row("e-a", doc),
                        workspace_id=ws.id, created_by="t",
                        allow_simulated=True,
                        explicit_cas=True,
                        expected_rev=_bops.FIRST_BIND_REV,
                        expected_set_hash=_bops.NO_BINDING_HASH)
    assert res["changed"] is True
    capture: list = []
    executed = _compile_run(compile_env, ws, capture)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    joined = "".join(m.get("content", "") for msgs in capture
                     for m in msgs)
    assert "指令-DEFAULT-8N" in joined
    assert "指令-REFCHECK-8N" in joined
    # 顺序：default 在 refcheck 之前（(skill_id, seq) 规范顺序）
    assert joined.index("指令-DEFAULT-8N") < joined.index("指令-REFCHECK-8N")


def test_compile_same_run_frozen_across_interruption_new_run_reads_new_set(
        exp_db, compile_env, monkeypatch):
    """C. 同一 run 跨中断冻结不变；新 run 才读新集合。"""
    from app.core.skill_evolution import business_ops as _bops
    _boot(compile_env["db"])
    d1 = _add_ver(exp_db, "default", 1, "集合A-default")
    r1 = _add_ver(exp_db, "refcheck", 1, "集合A-refcheck")
    doc_a = _accepted([_claim(d1), _claim(r1)])
    single = _add_ver(exp_db, "default", 2, "集合B-only")
    doc_b = _accepted([_claim(single)])
    ws = _page(compile_env, "p-8n", "普通说明文字足够长用于编译构建测试。" * 5)
    _bops.promote(compile_env["db"], exp_db, _exp_row("e-a", doc_a),
                  workspace_id=ws.id, created_by="t", allow_simulated=True,
                  explicit_cas=True, expected_rev=_bops.FIRST_BIND_REV,
                  expected_set_hash=_bops.NO_BINDING_HASH)
    cap1: list = []
    e1 = _compile_run(compile_env, ws, cap1, fail_first_synthesis=True)
    assert e1.status == "failed"
    pins = compile_env["db"].execute(text(
        "SELECT payload_json FROM knowledge_compile_artifacts WHERE "
        "run_id=:r AND artifact_type='evolution_binding_pin'"),
        {"r": e1.id}).fetchall()
    assert pins and json.loads(pins[0].payload_json)["mode"] == "bound"
    assert len(json.loads(pins[0].payload_json)["members"]) == 2
    # 中断期间业务绑定被切到集合 B → 该 run 恢复仍用集合 A
    from app.core.skill_evolution import business_ops as _b2
    st = _b2.business_state(compile_env["db"], ws.id)
    _b2.promote(compile_env["db"], exp_db, _exp_row("e-b", doc_b),
                workspace_id=ws.id, created_by="t", allow_simulated=True,
                explicit_cas=True,
                expected_rev=st["expected_rev"],
                expected_set_hash=st["expected_set_hash"])
    from app.core.wiki_pipeline.executor import retry_run
    retry_run(compile_env["db"], e1.id)
    compile_env["db"].commit()
    cap2: list = []

    def _ok_llm(messages, context="", timeout=120.0):
        cap2.append(list(messages or []))
        if context == "wiki-ingest-page":
            return {"worthy": True,
                    "ops": [{"action": "create", "title": "主题8N",
                             "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return _md_payload()
        return {"worthy": True, "ops": []}

    from app.core.wiki_pipeline import executor as exe
    exe.configure_external_runners(llm_runner=_ok_llm,
                                   graph_runner=_graph_noop)
    e2 = exe.execute_run(compile_env["db"], e1.id)
    compile_env["db"].refresh(e2)
    assert e2.status == "succeeded", (e2.safe_error_code,
                                      e2.safe_error_message)
    joined2 = "".join(m.get("content", "") for msgs in cap2 for m in msgs)
    assert "集合A-default" in joined2 and "集合A-refcheck" in joined2
    assert "集合B-only" not in joined2
    # 新 run → 读新绑定集合 B
    cap3: list = []
    e3 = _compile_run(compile_env, ws, cap3)
    assert e3.status == "succeeded"
    joined3 = "".join(m.get("content", "") for msgs in cap3 for m in msgs)
    assert "集合B-only" in joined3
    assert "集合A-default" not in joined3
