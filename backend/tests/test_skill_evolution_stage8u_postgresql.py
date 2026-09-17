"""WikiSkill PostgreSQL/pgvector 隔离验收（stage8u）。

仅在显式提供 WIKISKILL_PG_ACCEPTANCE_URL 时执行。普通 pytest 无 URL 则 skip，
不得把未执行写成通过。全部使用真实 PostgreSQL + SQLAlchemy Session；
并发用例使用两个独立 Engine/连接，不以 sleep 制造竞争。
不读取生产 .env 作为连接依据，不调用真实模型，不操作生产库。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import grader_registry as greg
from app.core.skill_evolution.injector import set_hash_for_members

BACKEND = Path(__file__).resolve().parent.parent
import sys
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from tools import pg_wikiskill_acceptance as pgtool  # noqa: E402

PG_URL = os.environ.get("WIKISKILL_PG_ACCEPTANCE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="未设置 WIKISKILL_PG_ACCEPTANCE_URL：普通环境 skip，不记为通过",
)

HEAD = pgtool.HEAD_REVISION
P44 = "a9b8c7d6e5f4"
P51 = "d974815b4a91"
P52 = "653bbcf9847b"
P53 = HEAD
GRADER_V1 = "wiki-default-grader/v1"
WS_PREFIX = "ws-pg8u-"


def _url() -> str:
    return pgtool.require_isolated_pg_url(PG_URL)


def _engine(url: str | None = None):
    return pgtool.sqlalchemy_engine(url or _url())


def _session(engine=None):
    eng = engine or _engine()
    return sessionmaker(bind=eng)(), eng


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
    purpose_md = ("## 来源\n- 8u\n## 改进目的\n- PG 验收\n"
                  "## 演化历史\n- v1\n")
    return skill_md, purpose_md


def _add_ver(db, skill_id, seq, marker, *, ns=""):
    skill_id = f"{ns}{skill_id}" if ns else skill_id
    version_id = f"{skill_id}:{seq:04d}"
    skill_md, purpose_md = _md(skill_id, seq, marker)
    ch = _payload_hash(skill_id, skill_md, purpose_md)
    db.execute(text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:vid,:sid,:seq,'skill-evolution/v1','wiki_compile.default',"
        "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
        "'8u',CURRENT_TIMESTAMP)"),
        {"vid": version_id, "sid": skill_id, "seq": seq, "sm": skill_md,
         "pm": purpose_md, "ch": ch})
    db.commit()
    return {"skill_id": skill_id, "version_id": version_id,
            "content_hash": ch, "seq": seq, "skill_md": skill_md,
            "purpose_md": purpose_md}


def _claim(v):
    return {"skill_id": v["skill_id"], "version_id": v["version_id"],
            "content_hash": v["content_hash"], "seq": v["seq"]}


def _accepted(claims):
    specs = bops.canonical_binding_members(claims)
    return {"mode": "versions", "members": specs,
            "set_hash": set_hash_for_members(specs)}


def _exp_row(exp_id, doc, *, grader=GRADER_V1):
    js = json.dumps(doc, ensure_ascii=False)
    return SimpleNamespace(experiment_id=exp_id, best_skill_set_json=js,
                           current_skill_set_json=js, best_score_passed=5,
                           best_score_total=5, grader_version=grader,
                           dataset_version="wiki-default-v3")


def _ensure_ws(db, ws_id: str):
    row = db.execute(text(
        "SELECT id FROM wiki_workspaces WHERE id=:w"), {"w": ws_id}).fetchone()
    if row is None:
        db.execute(text(
            "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
            "status) VALUES (:w,:k,:w,'public','public','active')"),
            {"w": ws_id, "k": "k-" + ws_id})
        db.commit()
    return ws_id


def _new_ws(db) -> str:
    return _ensure_ws(db, WS_PREFIX + uuid.uuid4().hex[:12])


def _count(db, table, ws=None):
    if table == "evolution_skill_bindings" and ws:
        return db.execute(text(
            "SELECT count(*) c FROM evolution_skill_bindings "
            "WHERE kind='business' AND workspace_id=:w"),
            {"w": ws}).scalar()
    if table == "evolution_business_events" and ws:
        return db.execute(text(
            "SELECT count(*) c FROM evolution_business_events "
            "WHERE workspace_id=:w"), {"w": ws}).scalar()
    return db.execute(text(f"SELECT count(*) c FROM {table}")).scalar()


def _insert_eval(db, exp_id, eid="ev-pg8u"):
    db.execute(text(
        "INSERT INTO evolution_evaluations (evaluation_id, idempotency_key, "
        "experiment_id, kind, proposal_id, base_set_json, candidate_set_json, "
        "task_ids_json, per_task_results_json, config_json, main_passed, "
        "main_total, valid) VALUES (:eid,:idem,:e,'candidate','p','{}','{}',"
        "'[]','[]','{}',5,5,TRUE)"),
        {"eid": eid, "idem": "idem-" + eid, "e": exp_id})
    db.commit()


def _insert_gate(db, exp_id, claims):
    doc = _accepted(claims)
    db.execute(text(
        "INSERT INTO evolution_gate_events (event_id, experiment_id, "
        "candidate_evaluation_id, decision, reason, candidate_version_ids_json, "
        "previous_set_json, next_set_json, status_rev_before) VALUES "
        "(:eid,:e,'ev-anchor','accepted','ok','[]','{}',:nxt,1)"),
        {"eid": "ge-" + uuid.uuid4().hex[:12], "e": exp_id,
         "nxt": json.dumps(doc, ensure_ascii=False)})
    db.commit()


def _insert_run(db, exp_id, *, real=False, status="completed", used=3,
                dataset="wiki-default-v3", extra=None):
    cfg = {"runner": {"mode": "real" if real else "simulated", "real": real},
           "budget": {"max_model_calls": 90, "max_tool_calls": 40,
                      "max_seconds": 3600}}
    if extra:
        cfg.update(extra)
    rid = "run-" + uuid.uuid4().hex[:16]
    db.execute(text(
        "INSERT INTO evolution_runs (run_id, experiment_id, workspace_id, "
        "domain, dataset_version, init_mode, config_json, "
        "initial_skill_set_json, max_iterations, current_iteration, status, "
        "used_model_calls, used_tool_calls, used_estimated_chars, "
        "pause_requested, cancel_requested, reserved_in_flight_json, "
        "created_at) VALUES (:r,:e,'ws','wiki_compile.default',:ds,'paper',"
        ":cfg,'{}',3,1,:st,:used,0,0,FALSE,FALSE,'[]',CURRENT_TIMESTAMP)"),
        {"r": rid, "e": exp_id, "ds": dataset,
         "cfg": json.dumps(cfg, ensure_ascii=False), "st": status,
         "used": used})
    db.commit()
    return rid


def _compile_run(db, ws_id, run_id):
    db.execute(text(
        "INSERT INTO knowledge_compile_runs (id, pipeline_key, "
        "pipeline_version, trigger_type, workspace_id, status, input_hash) "
        "VALUES (:id,'wiki.default','3','batch_rebuild',:w,'queued',:h)"),
        {"id": run_id, "w": ws_id, "h": hashlib.sha256(
            run_id.encode()).hexdigest()})
    db.commit()


@pytest.fixture(scope="session")
def pg_main():
    url = _url()
    if pgtool.current_revision(url) != HEAD:
        pgtool.alembic_upgrade(url, "head")
        again = pgtool.alembic_upgrade(url, "head")  # 幂等
        assert HEAD in (pgtool.current_revision(url) or "")
        assert again is not None
    assert pgtool.current_revision(url) == HEAD
    return url


@pytest.fixture()
def isolated(monkeypatch, pg_main):
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    monkeypatch.setattr(settings, "wikiskill_business_compile_enabled", True)
    db, eng = _session()
    ws = _new_ws(db)
    ns = ws.split("-")[-1] + "_"
    yield {"db": db, "engine": eng, "ws": ws, "url": pg_main, "ns": ns}
    db.close()
    eng.dispose()


# ---------------------------------------------------------------------------
# A. 基础能力
# ---------------------------------------------------------------------------


def test_a1_postgresql16_connectable(pg_main):
    info = pgtool.check_server(pg_main)
    assert info["ok"]
    assert str(info["server_version"]).startswith("16.")
    assert info["port"] == 5432  # 容器内端口
    assert info["database"] == "wikiskill_acceptance"


def test_a2_pgvector_create_and_query(pg_main):
    vec = pgtool.check_pgvector(pg_main)
    assert vec["ok"]
    assert vec["extension"] == "vector"
    assert vec["distance_smoke"] > 0


def test_a3_psycopg2_sqlalchemy_venv(pg_main):
    import psycopg2
    import sqlalchemy
    eng = _engine(pg_main)
    with eng.connect() as conn:
        v = conn.execute(text("SELECT 1")).scalar()
        assert v == 1
    eng.dispose()
    assert "psycopg2" in psycopg2.__file__.replace("\\", "/")
    assert sqlalchemy.__version__


def test_a4_transaction_commit_rollback_isolation(pg_main):
    txn = pgtool.check_transaction(pg_main)
    assert txn["ok"]
    assert txn["rollback_ok"]
    assert txn["isolation_ok"]


def test_a5_uses_isolated_database(pg_main):
    parsed_ok = pgtool.require_isolated_pg_url(pg_main)
    assert "55432" in parsed_ok
    assert "wikiskill_acceptance" in parsed_ok
    info = pgtool.check_server(pg_main)
    assert info["database"] == "wikiskill_acceptance"
    with pytest.raises(pgtool.AcceptanceError):
        pgtool.require_isolated_pg_url("sqlite:///./data/notes.db")
    with pytest.raises(pgtool.AcceptanceError):
        pgtool.require_isolated_pg_url(
            "postgresql+psycopg2://u:p@127.0.0.1:5432/wikiskill_acceptance")
    with pytest.raises(pgtool.AcceptanceError):
        pgtool.require_isolated_pg_url(
            "postgresql+psycopg2://u:p@example.com:55432/wikiskill_acceptance")


# ---------------------------------------------------------------------------
# B. 全新数据库迁移
# ---------------------------------------------------------------------------


def test_b_fresh_upgrade_head_schema_and_idempotent(pg_main):
    heads = pgtool.alembic_heads()
    assert heads == [HEAD]
    assert pgtool.current_revision(pg_main) == HEAD
    schema = pgtool.verify_wikiskill_schema(pg_main)
    assert schema["ok"], schema
    assert not schema["missing_tables"]
    assert not schema["missing_p52_columns"]
    assert not schema["missing_p53_columns"]
    # 模型三列存在
    cols = pgtool.table_columns(pg_main, "evolution_skill_bindings")
    assert {"rev", "set_hash", "members_json"} <= cols
    # 无 SQLite 专用泄漏：information_schema 可读
    db, eng = _session()
    try:
        n = db.execute(text("SELECT count(*) FROM alembic_version")).scalar()
        assert n == 1
    finally:
        db.close()
        eng.dispose()
    pgtool.alembic_upgrade(pg_main, "head")
    assert pgtool.current_revision(pg_main) == HEAD


# ---------------------------------------------------------------------------
# C. 历史升级兼容（独立库）
# ---------------------------------------------------------------------------


def test_c_path1_p44_to_head(pg_main):
    name = "wikiskill_acceptance_p44head"
    try:
        pgtool.drop_database(pg_main, name)
    except pgtool.AcceptanceError:
        pass
    pgtool.create_database(pg_main, name)
    url = pgtool.replace_dbname(pg_main, name)
    pgtool.alembic_upgrade(url, P44)
    assert pgtool.current_revision(url) == P44
    tables = pgtool.existing_tables(url)
    assert "evolution_skill_bindings" not in tables
    pgtool.alembic_upgrade(url, "head")
    assert pgtool.current_revision(url) == HEAD
    schema = pgtool.verify_wikiskill_schema(url)
    assert schema["ok"], schema
    pgtool.drop_database(pg_main, name)


def test_c_path2_p51_legacy_binding_to_p52_p53(pg_main):
    name = "wikiskill_acceptance_p51head"
    try:
        pgtool.drop_database(pg_main, name)
    except pgtool.AcceptanceError:
        pass
    pgtool.create_database(pg_main, name)
    url = pgtool.replace_dbname(pg_main, name)
    pgtool.alembic_upgrade(url, P51)
    assert pgtool.current_revision(url) == P51
    cols = pgtool.table_columns(url, "evolution_skill_bindings")
    assert "rev" not in cols and "members_json" not in cols
    db, eng = _session(create_engine(url, poolclass=NullPool))
    try:
        v = _add_ver(db, "default", 1, "P51-legacy")
        db.execute(text(
            "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
            "status) VALUES ('ws-p51','k-p51','p51','public','public','active')"))
        db.execute(text(
            "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
            "domain, set_kind, skill_id, version_id, created_by, created_at, "
            "updated_at) VALUES ('bind-p51','business','ws-p51',"
            "'wiki_compile.default','skill','default',:vid,'hist',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
            {"vid": v["version_id"]})
        db.execute(text(
            "INSERT INTO evolution_business_events (event_id, idempotency_key, "
            "action, workspace_id, domain, from_version_id, to_version_id, "
            "evidence_json, reason, created_by, created_at) VALUES "
            "('bev-p51','idem-p51','promote','ws-p51','wiki_compile.default',"
            "NULL,:vid,'{}','legacy promote','hist',CURRENT_TIMESTAMP)"),
            {"vid": v["version_id"]})
        db.commit()
        legacy_hash = v["content_hash"]
        legacy_vid = v["version_id"]
    finally:
        db.close()
        eng.dispose()
    pgtool.alembic_upgrade(url, P52)
    assert pgtool.current_revision(url) == P52
    pgtool.alembic_upgrade(url, P53)
    assert pgtool.current_revision(url) == HEAD
    db, eng = _session(create_engine(url, poolclass=NullPool))
    try:
        row = db.execute(text(
            "SELECT rev, set_hash, members_json, skill_id, version_id "
            "FROM evolution_skill_bindings WHERE id='bind-p51'")).fetchone()
        assert int(row.rev) == 1
        assert row.members_json is None
        assert row.set_hash is None
        assert row.version_id == legacy_vid
        st = bops.resolve_business_binding_set(db, "ws-p51")
        assert st is not None
        assert st["legacy"] is True
        assert st["rev"] == 1
        assert len(st["members"]) == 1
        assert st["members"][0]["version_id"] == legacy_vid
        assert st["members"][0]["content_hash"] == legacy_hash
        assert st["canonical_set_hash"] == bops.binding_set_hash(
            [_claim(st["members"][0])])
        assert st["set_hash"] is None
        ev = db.execute(text(
            "SELECT action, to_version_id, from_members_json "
            "FROM evolution_business_events WHERE event_id='bev-p51'"
        )).fetchone()
        assert ev.action == "promote" and ev.to_version_id == legacy_vid
        assert ev.from_members_json is None
        n = db.execute(text(
            "SELECT count(*) FROM evolution_skill_bindings "
            "WHERE workspace_id='ws-p51'")).scalar()
        assert n == 1
    finally:
        db.close()
        eng.dispose()
    pgtool.drop_database(pg_main, name)


# ---------------------------------------------------------------------------
# D. 集合绑定与业务操作
# ---------------------------------------------------------------------------


def test_d_legacy_read_and_set_roundtrip(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    v = _add_ver(db, "default", 1, "legacy-read", ns=ns)
    db.execute(text(
        "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
        "domain, set_kind, skill_id, version_id, rev, set_hash, "
        "members_json, created_by, created_at, updated_at) VALUES "
        "(:id,'business',:w,'wiki_compile.default','skill',:sid,:vid,"
        "1,NULL,NULL,'8u',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
        {"id": "bl-" + ws, "w": ws, "sid": v["skill_id"], "vid": v["version_id"]})
    db.commit()
    st = bops.resolve_business_binding_set(db, ws)
    assert st["legacy"] is True
    assert [m["version_id"] for m in st["members"]] == [v["version_id"]]
    a = _add_ver(db, "alpha", 1, "集合-A", ns=ns)
    b = _add_ver(db, "beta", 1, "集合-B", ns=ns)
    claims = [_claim(a), _claim(b)]
    doc = _accepted(claims)
    res = bops.promote(db, db, _exp_row("e-set", doc), workspace_id=ws,
                       created_by="t", allow_simulated=True,
                       expected_rev=1,
                       expected_set_hash=st["canonical_set_hash"])
    assert res["changed"] is True
    got = bops.resolve_business_binding_set(db, ws)
    assert [m["skill_id"] for m in got["members"]] == [a["skill_id"], b["skill_id"]]
    assert got["canonical_set_hash"] == bops.binding_set_hash(
        [_claim(m) for m in got["members"]])
    assert got["set_hash"] == got["canonical_set_hash"]
    assert got["legacy"] is False


def test_d_promote_rollback_full_set_same_txn_audit(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "alpha", 1, "P-A", ns=ns)
    b = _add_ver(db, "beta", 1, "P-B", ns=ns)
    doc = _accepted([_claim(a), _claim(b)])
    r1 = bops.promote(db, db, _exp_row("e-pr-" + ns, doc), workspace_id=ws,
                      created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                      expected_set_hash=bops.NO_BINDING_HASH,
                      idempotency_key="promo-set-" + ws)
    assert r1["changed"] is True
    assert len(r1["members"]) == 2
    assert _count(db, "evolution_business_events", ws) == 1
    c = _add_ver(db, "alpha", 2, "P-A2", ns=ns)
    doc2 = _accepted([_claim(c)])
    st = bops.business_state(db, ws)
    r2 = bops.promote(db, db, _exp_row("e-pr2-" + ns, doc2), workspace_id=ws,
                      created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=st["expected_rev"],
                      expected_set_hash=st["expected_set_hash"],
                      idempotency_key="promo-single-" + ws)
    assert [m["version_id"] for m in r2["members"]] == [c["version_id"]]
    rb = bops.rollback(db, workspace_id=ws, created_by="t",
                       explicit_cas=True, expected_rev=r2["rev"],
                       expected_set_hash=r2["set_hash"],
                       idempotency_key="rb-set-" + ws)
    assert [m["skill_id"] for m in rb["members"]] == [a["skill_id"], b["skill_id"]]
    got = bops.resolve_business_binding_set(db, ws)
    assert [m["skill_id"] for m in got["members"]] == [a["skill_id"], b["skill_id"]]
    assert _count(db, "evolution_business_events", ws) == 3


def test_d_audit_failure_rolls_back_binding(isolated, monkeypatch):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "AUD", ns=ns)
    doc = _accepted([_claim(a)])
    real_append = bops._append_event

    def boom(*args, **kwargs):
        raise RuntimeError("audit-boom")

    monkeypatch.setattr(bops, "_append_event", boom)
    with pytest.raises(RuntimeError):
        bops.promote(db, db, _exp_row("e-aud", doc), workspace_id=ws,
                     created_by="t", allow_simulated=True)
    monkeypatch.setattr(bops, "_append_event", real_append)
    assert _count(db, "evolution_skill_bindings", ws) == 0
    assert _count(db, "evolution_business_events", ws) == 0


def test_d_compile_injection_full_set_and_frozen_retry(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "alpha", 1, "指令-ALPHA", ns=ns)
    b = _add_ver(db, "beta", 1, "指令-BETA", ns=ns)
    doc = _accepted([_claim(a), _claim(b)])
    bops.promote(db, db, _exp_row("e-inj", doc), workspace_id=ws,
                 created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                 expected_set_hash=bops.NO_BINDING_HASH)
    run_old = "cr-" + uuid.uuid4().hex[:12]
    _compile_run(db, ws, run_old)
    ctx = {}
    bops.freeze_binding_for_run(ctx, db, run_old, ws)
    pin = bops._load_pin(db, run_old)
    assert pin["mode"] == "bound"
    assert len(pin["members"]) == 2
    assert [m["skill_id"] for m in pin["members"]] == [a["skill_id"], b["skill_id"]]
    block = bops.render_set_block(bops.resolve_business_binding_set(db, ws)["members"])
    assert "指令-ALPHA" in block and "指令-BETA" in block
    assert block.index("指令-ALPHA") < block.index("指令-BETA")
    assert "members[0]" not in json.dumps(pin)
    c = _add_ver(db, "gamma", 1, "指令-GAMMA", ns=ns)
    st = bops.business_state(db, ws)
    bops.promote(db, db, _exp_row("e-inj2", _accepted([_claim(c)])),
                 workspace_id=ws, created_by="t", allow_simulated=True,
                 explicit_cas=True, expected_rev=st["expected_rev"],
                 expected_set_hash=st["expected_set_hash"])
    ctx2 = {}
    bops.freeze_binding_for_run(ctx2, db, run_old, ws)
    pin2 = bops._load_pin(db, run_old)
    assert [m["skill_id"] for m in pin2["members"]] == [a["skill_id"], b["skill_id"]]
    run_new = "cr-" + uuid.uuid4().hex[:12]
    _compile_run(db, ws, run_new)
    ctx3 = {}
    bops.freeze_binding_for_run(ctx3, db, run_new, ws)
    pin3 = bops._load_pin(db, run_new)
    assert [m["skill_id"] for m in pin3["members"]] == [c["skill_id"]]
    st2 = bops.business_state(db, ws)
    bops.rollback(db, workspace_id=ws, created_by="t", explicit_cas=True,
                  expected_rev=st2["expected_rev"],
                  expected_set_hash=st2["expected_set_hash"])
    ctx4 = {}
    bops.freeze_binding_for_run(ctx4, db, run_new, ws)
    assert [m["skill_id"] for m in bops._load_pin(db, run_new)["members"]] == [
        c["skill_id"]]
    run_after = "cr-" + uuid.uuid4().hex[:12]
    _compile_run(db, ws, run_after)
    ctx5 = {}
    bops.freeze_binding_for_run(ctx5, db, run_after, ws)
    assert [m["skill_id"] for m in bops._load_pin(db, run_after)["members"]] == [
        a["skill_id"], b["skill_id"]]


# ---------------------------------------------------------------------------
# E. CAS / 并发 / ABA（双独立连接）
# ---------------------------------------------------------------------------


def test_e_cas_expected_rev_and_hash(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "CAS-A", ns=ns)
    b = _add_ver(db, "default", 2, "CAS-B", ns=ns)
    r = bops.promote(db, db, _exp_row("e-cas", _accepted([_claim(a)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                     expected_set_hash=bops.NO_BINDING_HASH)
    ok = bops.promote(db, db, _exp_row("e-cas2", _accepted([_claim(b)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=r["rev"],
                      expected_set_hash=r["set_hash"])
    assert ok["changed"] is True
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(db, db, _exp_row("e-cas3", _accepted([_claim(a)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=r["rev"],
                     expected_set_hash=ok["set_hash"])
    assert ei.value.code == "binding_conflict"
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.promote(db, db, _exp_row("e-cas4", _accepted([_claim(a)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=ok["rev"],
                     expected_set_hash=r["set_hash"])
    assert ei2.value.code == "binding_conflict"


def test_e_concurrent_update_two_connections(isolated):
    db, ws, url, ns = isolated["db"], isolated["ws"], isolated["url"], isolated["ns"]
    a = _add_ver(db, "default", 1, "CU-A", ns=ns)
    b = _add_ver(db, "default", 2, "CU-B", ns=ns)
    c = _add_ver(db, "refx", 1, "CU-C", ns=ns)
    r = bops.promote(db, db, _exp_row("e-cu", _accepted([_claim(a)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                     expected_set_hash=bops.NO_BINDING_HASH)
    barrier = threading.Barrier(2)
    results, errors, extras = [], [], []

    def worker(i, target):
        eng = create_engine(url, poolclass=NullPool)
        s = sessionmaker(bind=eng)()
        try:
            barrier.wait()
            out = bops.promote(
                s, s, _exp_row(f"e-cu-{i}", _accepted([_claim(target)])),
                workspace_id=ws, created_by=f"w{i}", allow_simulated=True,
                explicit_cas=True, expected_rev=r["rev"],
                expected_set_hash=r["set_hash"],
                idempotency_key=f"cu-{ws}-{i}")
            s.commit()
            results.append(out)
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            extras.append(type(exc).__name__ + ":" + pgtool.redact_text(str(exc), url))
        finally:
            s.close()
            eng.dispose()

    threads = [
        threading.Thread(target=worker, args=(0, b)),
        threading.Thread(target=worker, args=(1, c)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert extras == []
    assert len(results) == 1, (results, errors)
    assert errors == ["binding_conflict"]
    db.expire_all()
    st = bops.resolve_business_binding_set(db, ws)
    assert st["rev"] == 2
    assert _count(db, "evolution_business_events", ws) == 2  # 首绑 + 一个成功


def test_e_first_bind_concurrent_single_winner(isolated):
    db, ws, url, ns = isolated["db"], isolated["ws"], isolated["url"], isolated["ns"]
    a = _add_ver(db, "default", 1, "FB-A", ns=ns)
    b = _add_ver(db, "refy", 1, "FB-B", ns=ns)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(i, target):
        eng = create_engine(url, poolclass=NullPool)
        s = sessionmaker(bind=eng)()
        try:
            barrier.wait()
            out = bops.promote(
                s, s, _exp_row(f"e-fb-{i}", _accepted([_claim(target)])),
                workspace_id=ws, created_by=f"f{i}", allow_simulated=True,
                explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                expected_set_hash=bops.NO_BINDING_HASH,
                idempotency_key=f"fb-{ws}-{i}")
            results.append(out)
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)
        finally:
            s.close()
            eng.dispose()

    threads = [threading.Thread(target=worker, args=(0, a)),
               threading.Thread(target=worker, args=(1, b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 1
    assert errors == ["binding_conflict"]
    db.expire_all()
    assert _count(db, "evolution_skill_bindings", ws) == 1
    assert _count(db, "evolution_business_events", ws) == 1


def test_e_aba_stale_rev_rejected(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "ABA-1", ns=ns)
    b = _add_ver(db, "default", 2, "ABA-2", ns=ns)
    r1 = bops.promote(db, db, _exp_row("e-aba1", _accepted([_claim(a)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                      expected_set_hash=bops.NO_BINDING_HASH)
    r2 = bops.promote(db, db, _exp_row("e-aba2", _accepted([_claim(b)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=r1["rev"],
                      expected_set_hash=r1["set_hash"])
    r3 = bops.promote(db, db, _exp_row("e-aba3", _accepted([_claim(a)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=r2["rev"],
                      expected_set_hash=r2["set_hash"])
    assert r3["rev"] == 3
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(db, db, _exp_row("e-aba4", _accepted([_claim(b)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=r1["rev"],
                     expected_set_hash=r1["set_hash"])
    assert ei.value.code == "binding_conflict"
    st = bops.resolve_business_binding_set(db, ws)
    assert st["rev"] == 3
    assert st["members"][0]["version_id"] == a["version_id"]


# ---------------------------------------------------------------------------
# F. 请求级幂等
# ---------------------------------------------------------------------------


def test_f_idempotent_replay_and_content_conflict(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "ID-A", ns=ns)
    b = _add_ver(db, "default", 2, "ID-B", ns=ns)
    key = "idem-" + ws
    r1 = bops.promote(db, db, _exp_row("e-id", _accepted([_claim(a)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                      expected_set_hash=bops.NO_BINDING_HASH,
                      idempotency_key=key)
    replay = bops.promote(db, db, _exp_row("e-id", _accepted([_claim(a)])),
                          workspace_id=ws, created_by="t", allow_simulated=True,
                          explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                          expected_set_hash=bops.NO_BINDING_HASH,
                          idempotency_key=key)
    assert replay.get("replay") is True
    assert replay["rev"] == r1["rev"]
    assert _count(db, "evolution_business_events", ws) == 1
    st = bops.resolve_business_binding_set(db, ws)
    assert st["rev"] == 1
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(db, db, _exp_row("e-id", _accepted([_claim(b)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                     expected_set_hash=bops.NO_BINDING_HASH,
                     idempotency_key=key)
    assert ei.value.code == "idem_content_conflict"
    r2 = bops.promote(db, db, _exp_row("e-id2", _accepted([_claim(b)])),
                      workspace_id=ws, created_by="t", allow_simulated=True,
                      explicit_cas=True, expected_rev=st["rev"],
                      expected_set_hash=st["canonical_set_hash"],
                      idempotency_key=key + "-2")
    rb = bops.rollback(db, workspace_id=ws, created_by="t", explicit_cas=True,
                       expected_rev=r2["rev"], expected_set_hash=r2["set_hash"],
                       idempotency_key=key + "-rb")
    again = bops.promote(db, db, _exp_row("e-id3", _accepted([_claim(b)])),
                         workspace_id=ws, created_by="t", allow_simulated=True,
                         explicit_cas=True, expected_rev=rb["rev"],
                         expected_set_hash=rb["set_hash"],
                         idempotency_key=key + "-re")
    assert again["changed"] is True
    lost = bops.promote(db, db, _exp_row("e-id3", _accepted([_claim(b)])),
                        workspace_id=ws, created_by="t", allow_simulated=True,
                        explicit_cas=True, expected_rev=rb["rev"],
                        expected_set_hash=rb["set_hash"],
                        idempotency_key=key + "-re")
    assert lost.get("replay") is True
    other = _add_ver(db, "other", 1, "ID-O", ns=ns)
    fp = bops._request_fingerprint(
        action="promote", workspace_id=ws, domain=bops.DOMAIN,
        target_members=bops.canonical_binding_members([_claim(a), _claim(other)]),
        expected_rev=1, expected_set_hash="x" * 64,
        evidence_ref="e")
    fp2 = bops._request_fingerprint(
        action="promote", workspace_id=ws, domain=bops.DOMAIN,
        target_members=bops.canonical_binding_members([_claim(a)]),
        expected_rev=1, expected_set_hash="x" * 64,
        evidence_ref="e")
    fp3 = bops._request_fingerprint(
        action="promote", workspace_id=ws, domain=bops.DOMAIN,
        target_members=bops.canonical_binding_members([_claim(a)]),
        expected_rev=2, expected_set_hash="x" * 64,
        evidence_ref="e")
    fp4 = bops._request_fingerprint(
        action="promote", workspace_id=ws, domain=bops.DOMAIN,
        target_members=bops.canonical_binding_members([_claim(a)]),
        expected_rev=1, expected_set_hash="y" * 64,
        evidence_ref="e")
    assert fp != fp2 and fp2 != fp3 and fp2 != fp4


def test_f_concurrent_replay_no_double_write(isolated):
    db, ws, url, ns = isolated["db"], isolated["ws"], isolated["url"], isolated["ns"]
    a = _add_ver(db, "default", 1, "CR-A", ns=ns)
    key = "idem-conc-" + ws
    doc = _accepted([_claim(a)])
    barrier = threading.Barrier(2)
    results, errors, extras = [], [], []

    def worker(_i):
        eng = create_engine(url, poolclass=NullPool)
        s = sessionmaker(bind=eng)()
        try:
            barrier.wait()
            out = bops.promote(
                s, s, _exp_row("e-cr", doc), workspace_id=ws,
                created_by="t", allow_simulated=True, explicit_cas=True,
                expected_rev=bops.FIRST_BIND_REV,
                expected_set_hash=bops.NO_BINDING_HASH,
                idempotency_key=key)
            results.append(out)
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            extras.append(pgtool.redact_text(str(exc), url))
        finally:
            s.close()
            eng.dispose()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert extras == []
    changed = [r for r in results if r.get("changed")]
    replays = [r for r in results if r.get("replay")]
    db.expire_all()
    assert extras == []
    assert _count(db, "evolution_skill_bindings", ws) == 1
    assert _count(db, "evolution_business_events", ws) == 1
    assert len(changed) == 1
    # 并发同键：失败者在 IntegrityError/CAS 冲突后回看幂等事件，必须重放原结果。
    assert len(replays) == 1, (results, errors)
    assert errors == []
    st = bops.resolve_business_binding_set(db, ws)
    assert st["rev"] == 1
    assert st["members"][0]["version_id"] == a["version_id"]


# ---------------------------------------------------------------------------
# G. P53 证据与晋升门禁
# ---------------------------------------------------------------------------


def test_g_grader_v2_business_promotion_rejected(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "V2", ns=ns)
    claims = [_claim(a)]
    exp_id = "e-v2-" + ns.rstrip("_")
    _insert_eval(db, exp_id, "ev-v2-" + ns.rstrip("_"))
    _insert_gate(db, exp_id, claims)
    _insert_run(db, exp_id, real=True, used=4)
    v2 = greg.grader_state(greg.GRADER_V2)
    assert v2["labels_approved"] is True
    assert v2["real_calibration"] is False
    assert v2["calibration"] == "engineering_only"
    assert v2["allow_business_promotion"] is False
    with pytest.raises(greg.GraderRegistryError):
        greg.require_business_promotable(greg.GRADER_V2)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(db, db, _exp_row(exp_id, _accepted(claims),
                                     grader=greg.GRADER_V2),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                     expected_set_hash=bops.NO_BINDING_HASH)
    assert ei.value.code == "grader_not_promotable"
    assert _count(db, "evolution_skill_bindings", ws) == 0


def test_g_v1_mechanism_not_real_effect(isolated):
    db, ws, ns = isolated["db"], isolated["ws"], isolated["ns"]
    a = _add_ver(db, "default", 1, "V1", ns=ns)
    res = bops.promote(db, db, _exp_row("e-v1", _accepted([_claim(a)])),
                       workspace_id=ws, created_by="t", allow_simulated=True)
    assert res["effect_verified"] is False
    assert res.get("simulated_evidence") is True
    ev = db.execute(text(
        "SELECT evidence_json FROM evolution_business_events "
        "WHERE workspace_id=:w"), {"w": ws}).fetchone()
    payload = json.loads(ev.evidence_json)
    assert payload["effect_verified"] is False
    assert payload["model_mode_evidence"] == "simulated"


# ---------------------------------------------------------------------------
# H. 重启恢复
# ---------------------------------------------------------------------------


def test_h_reconnect_preserves_binding_pin_idem_and_budget(isolated):
    db, ws, url, ns = isolated["db"], isolated["ws"], isolated["url"], isolated["ns"]
    a = _add_ver(db, "alpha", 1, "RST-A", ns=ns)
    b = _add_ver(db, "beta", 1, "RST-B", ns=ns)
    key = "idem-rst-" + ws
    res = bops.promote(db, db, _exp_row("e-rst", _accepted([_claim(a), _claim(b)])),
                       workspace_id=ws, created_by="t", allow_simulated=True,
                       explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                       expected_set_hash=bops.NO_BINDING_HASH,
                       idempotency_key=key)
    rid = _insert_run(db, "e-rst", extra={"frozen": True})
    db.execute(text(
        "UPDATE evolution_runs SET reserved_in_flight_json=:j WHERE run_id=:r"),
        {"j": json.dumps(["call-1"]), "r": rid})
    cr = "cr-rst-" + uuid.uuid4().hex[:8]
    _compile_run(db, ws, cr)
    ctx = {}
    bops.freeze_binding_for_run(ctx, db, cr, ws)
    old_rev, old_hash = res["rev"], res["set_hash"]
    db.close()
    isolated["engine"].dispose()
    eng2 = create_engine(url, poolclass=NullPool)
    db2 = sessionmaker(bind=eng2)()
    try:
        st = bops.resolve_business_binding_set(db2, ws)
        assert st["rev"] == old_rev
        assert st["canonical_set_hash"] == old_hash
        assert len(st["members"]) == 2
        replay = bops.promote(
            db2, db2, _exp_row("e-rst", _accepted([_claim(a), _claim(b)])),
            workspace_id=ws, created_by="t", allow_simulated=True,
            explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
            expected_set_hash=bops.NO_BINDING_HASH, idempotency_key=key)
        assert replay.get("replay") is True
        run = db2.execute(text(
            "SELECT reserved_in_flight_json, config_json, used_model_calls "
            "FROM evolution_runs WHERE run_id=:r"), {"r": rid}).fetchone()
        assert json.loads(run.reserved_in_flight_json) == ["call-1"]
        assert json.loads(run.config_json)["budget"]["max_model_calls"] == 90
        pin = bops._load_pin(db2, cr)
        assert [m["skill_id"] for m in pin["members"]] == [a["skill_id"], b["skill_id"]]
        c = _add_ver(db2, "gamma", 1, "RST-G", ns=ns)
        bops.promote(db2, db2, _exp_row("e-rst2", _accepted([_claim(c)])),
                     workspace_id=ws, created_by="t", allow_simulated=True,
                     explicit_cas=True, expected_rev=st["rev"],
                     expected_set_hash=st["canonical_set_hash"])
        ctxn = {}
        bops.freeze_binding_for_run(ctxn, db2, cr, ws)
        assert [m["skill_id"] for m in bops._load_pin(db2, cr)["members"]] == [
            a["skill_id"], b["skill_id"]]
    finally:
        db2.close()
        eng2.dispose()


# ---------------------------------------------------------------------------
# I. 备份与恢复
# ---------------------------------------------------------------------------


def _digest_row(row) -> str:
    payload = json.dumps(list(row), ensure_ascii=False, default=str,
                         sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_MIN_PASSWORD_SCAN_LEN = 8


def _db_password(url: str) -> str:
    return unquote(urlparse(url).password or "")


def _assert_text_has_no_db_secrets(text: str, url: str) -> None:
    """文本报告不得含完整 URL 或实际口令。password_hash 字段名不视为泄漏。"""
    if url and url in text:
        raise AssertionError("文本报告含完整数据库 URL")
    password = _db_password(url)
    if password and len(password) >= _MIN_PASSWORD_SCAN_LEN and password in text:
        raise AssertionError("文本报告含数据库口令")


def _assert_dump_has_no_db_secrets(dump: Path, url: str) -> None:
    raw = dump.read_bytes()
    if url and url.encode("utf-8") in raw:
        raise AssertionError("二进制 dump 含完整数据库 URL")
    password = _db_password(url)
    if password and len(password) >= _MIN_PASSWORD_SCAN_LEN:
        if password.encode("utf-8") in raw:
            raise AssertionError("二进制 dump 含数据库口令")


def test_i_backup_restore_consistent(isolated):
    db, ws, url, ns = isolated["db"], isolated["ws"], isolated["url"], isolated["ns"]
    a = _add_ver(db, "alpha", 1, "BK-A", ns=ns)
    b = _add_ver(db, "beta", 1, "BK-B", ns=ns)
    key = "idem-bk-" + ws
    res = bops.promote(db, db, _exp_row("e-bk", _accepted([_claim(a), _claim(b)])),
                       workspace_id=ws, created_by="t", allow_simulated=True,
                       explicit_cas=True, expected_rev=bops.FIRST_BIND_REV,
                       expected_set_hash=bops.NO_BINDING_HASH,
                       idempotency_key=key)
    rid = _insert_run(db, "e-bk")
    cr = "cr-bk-" + uuid.uuid4().hex[:8]
    _compile_run(db, ws, cr)
    bops.freeze_binding_for_run({}, db, cr, ws)
    report_dir = Path(os.environ.get(
        "PGACCEPT_REPORT_DIR",
        str(BACKEND / "reports" / (
            "pg-acceptance-" + datetime.now().strftime("%Y%m%d-%H%M%S")))))
    report_dir.mkdir(parents=True, exist_ok=True)
    dump = pgtool.pg_dump_custom(url, report_dir / "wikiskill_acceptance.dump")
    assert dump.exists() and dump.stat().st_size > 0
    _assert_dump_has_no_db_secrets(dump, url)
    restore_db = "wikiskill_acceptance_restore"
    try:
        pgtool.drop_database(url, restore_db)
    except pgtool.AcceptanceError:
        pass
    pgtool.pg_restore_custom(url, dump, restore_db)
    rurl = pgtool.replace_dbname(url, restore_db)
    assert pgtool.current_revision(rurl) == HEAD
    src, eng_s = _session(create_engine(url, poolclass=NullPool))
    dst, eng_d = _session(create_engine(rurl, poolclass=NullPool))
    try:
        tables = [
            "evolution_skill_bindings", "evolution_business_events",
            "evolution_skill_versions", "evolution_runs",
            "knowledge_compile_artifacts",
        ]
        summary = {"alembic_version": HEAD, "workspace": ws,
                   "binding_rev": res["rev"], "set_hash": res["set_hash"],
                   "tables": {}}
        for tname in tables:
            sc = src.execute(text(f"SELECT count(*) FROM {tname}")).scalar()
            dc = dst.execute(text(f"SELECT count(*) FROM {tname}")).scalar()
            assert sc == dc, (tname, sc, dc)
            summary["tables"][tname] = sc
        brow = dst.execute(text(
            "SELECT rev, set_hash, members_json FROM evolution_skill_bindings "
            "WHERE workspace_id=:w AND kind='business'"), {"w": ws}).fetchone()
        assert int(brow.rev) == res["rev"]
        assert brow.set_hash == res["set_hash"]
        members = json.loads(brow.members_json)
        assert [m["skill_id"] for m in members] == [a["skill_id"], b["skill_id"]]
        evn = dst.execute(text(
            "SELECT count(*) FROM evolution_business_events "
            "WHERE workspace_id=:w AND idempotency_key=:k"),
            {"w": ws, "k": key}).scalar()
        assert evn == 1
        pin = dst.execute(text(
            "SELECT payload_json FROM knowledge_compile_artifacts "
            "WHERE run_id=:r"), {"r": cr}).fetchone()
        assert json.loads(pin.payload_json)["set_hash"] == res["set_hash"]
        run = dst.execute(text(
            "SELECT run_id FROM evolution_runs WHERE run_id=:r"),
            {"r": rid}).fetchone()
        assert run is not None
        digest = hashlib.sha256(json.dumps({
            "rev": int(brow.rev), "set_hash": brow.set_hash,
            "members": members, "events": evn,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        summary["digest"] = digest
        evidence = report_dir / "restore-compare.json"
        evidence.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        blob = evidence.read_text(encoding="utf-8")
        _assert_text_has_no_db_secrets(blob, url)
    finally:
        src.close()
        dst.close()
        eng_s.dispose()
        eng_d.dispose()
        pgtool.drop_database(url, restore_db)
