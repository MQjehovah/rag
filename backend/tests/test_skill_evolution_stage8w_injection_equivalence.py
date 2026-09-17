"""M2：实验注入与业务注入完全等价（离线）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.injector import (
    CONTENT_CONTEXTS,
    FrozenSkillSet,
    SkillInjectingRunner,
    SkillSetError,
    build_injected_messages,
    is_content_generation_context,
    render_execution_skill_text,
)
from app.core.wiki_pipeline import executor as wiki_exec
from app.core.wiki_pipeline import registry as wiki_reg

PURPOSE_MARKER = "PURPOSE-MUST-NOT-APPEAR-IN-INFERENCE"
SKILL_A = "MARKER-SKILL-A-ALPHA"
SKILL_B = "MARKER-SKILL-B-BETA"
NON_CONTENT = (
    "wiki-ingest-page", "wiki-classify", "routing", "skill_route", "skill-route",
)


@pytest.fixture(autouse=True)
def _iso():
    wiki_reg.REGISTRY.clear()
    wiki_exec.reset_external_runners()
    yield
    wiki_exec.reset_external_runners()
    wiki_reg.REGISTRY.clear()


def _md(skill_id, marker):
    skill_md = (
        f"---\nskill_id: {skill_id}\ndomain: wiki_compile.default\n"
        "runtime_ref: wiki.compile.default.runtime/v1\nschema_version: 1\n---\n"
        f"# {skill_id}\n\n## 适用条件\n- 通用\n## 不适用条件\n- 无\n"
        f"## 操作步骤\n- {marker}\n")
    purpose_md = f"## 来源\n- test\n## 改进目的\n- {PURPOSE_MARKER}\n"
    return skill_md, purpose_md


def _payload_hash(skill_id, skill_md, purpose_md):
    payload = json.dumps(
        {"skill_id": skill_id, "domain": "wiki_compile.default",
         "runtime_ref": "wiki.compile.default.runtime/v1",
         "schema_version": "skill-evolution/v1",
         "skill_md": skill_md, "purpose_md": purpose_md},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rows(markers=(SKILL_A, SKILL_B)):
    out = []
    for i, marker in enumerate(markers, start=1):
        sid = f"skill-{chr(96 + i)}"
        sm, pm = _md(sid, marker)
        out.append(SimpleNamespace(
            skill_id=sid, version_id=f"{sid}:0001", seq=i,
            content_hash=_payload_hash(sid, sm, pm),
            skill_md=sm, purpose_md=pm))
    return out


def _capture():
    seen = []

    def inner(messages, context="", timeout=120.0):
        seen.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
            "context": context,
            "roles": [m.get("role") for m in (messages or [])],
        })
        return {"summary": "s", "content": "ok"}
    return seen, inner


def test_same_set_experiment_and_business_messages_deeply_equal(tmp_path):
    rows = _rows()
    plan = FrozenSkillSet.from_versions(rows)
    user = [{"role": "user", "content": "请生成 Wiki"}]
    exp_seen, exp_inner = _capture()
    SkillInjectingRunner(plan, exp_inner, lambda _e: None)(
        user, context="wiki-synthesis")

    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    db = skill_store.session_for(lab)
    try:
        for r in rows:
            db.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
                    "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
                    "purpose_md, content_hash, source_type, created_by, created_at) "
                    "VALUES (:vid,:sid,:seq,'skill-evolution/v1','wiki_compile.default',"
                    "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
                    "'8w',CURRENT_TIMESTAMP)"),
                {"vid": r.version_id, "sid": r.skill_id, "seq": r.seq,
                 "sm": r.skill_md, "pm": r.purpose_md, "ch": r.content_hash})
        db.commit()
        biz_seen, biz_inner = _capture()
        members = [{"skill_id": r.skill_id, "version_id": r.version_id,
                    "content_hash": r.content_hash, "seq": r.seq} for r in rows]
        pin = {"mode": "bound", "members": members,
               "set_hash": plan.set_hash}
        ctx = {"llm_runner": biz_inner}
        bops._apply_pin(ctx, db, pin, "wiki_compile.default")
        ctx["llm_runner"](user, context="wiki-synthesis")
    finally:
        db.close()

    assert exp_seen and biz_seen
    assert exp_seen[0]["messages"] == biz_seen[0]["messages"]
    assert exp_seen[0]["context"] == biz_seen[0]["context"]
    assert exp_seen[0]["roles"] == biz_seen[0]["roles"]
    blob = json.dumps(exp_seen[0]["messages"], ensure_ascii=False)
    assert PURPOSE_MARKER not in blob
    assert SKILL_A in blob and SKILL_B in blob
    # 多技能顺序与渲染函数一致
    text = render_execution_skill_text(rows)
    assert blob.find(SKILL_A) < blob.find(SKILL_B)
    assert text == exp_seen[0]["messages"][0]["content"]


def test_purpose_never_in_inference_and_content_contexts_only():
    rows = _rows((SKILL_A,))
    plan = FrozenSkillSet.from_versions(rows)
    user = [{"role": "user", "content": "x"}]
    for ctx in CONTENT_CONTEXTS:
        seen, inner = _capture()
        SkillInjectingRunner(plan, inner, lambda _e: None)(user, context=ctx)
        assert seen[0]["messages"][0]["role"] == "system"
        assert SKILL_A in seen[0]["messages"][0]["content"]
        assert PURPOSE_MARKER not in json.dumps(seen[0]["messages"])
        assert is_content_generation_context(ctx) is True
    for ctx in NON_CONTENT:
        seen, inner = _capture()
        SkillInjectingRunner(plan, inner, lambda _e: None)(user, context=ctx)
        assert seen[0]["messages"] == user
        assert PURPOSE_MARKER not in json.dumps(seen[0]["messages"])
        assert is_content_generation_context(ctx) is False


def test_empty_and_disabled_keep_old_behavior():
    user = [{"role": "user", "content": "x"}]
    for plan in (FrozenSkillSet.empty(), FrozenSkillSet.disabled()):
        seen, inner = _capture()
        SkillInjectingRunner(plan, inner, lambda _e: None)(
            user, context="wiki-synthesis")
        assert seen[0]["messages"] == user


def test_oversized_set_fails_loud_no_truncate():
    huge = "X" * 50_000
    rows = [SimpleNamespace(
        skill_id="skill-huge", version_id="skill-huge:0001", seq=1,
        content_hash="h" * 64, skill_md=huge, purpose_md="p")]
    with pytest.raises(SkillSetError, match="不静默截断"):
        FrozenSkillSet.from_versions(rows)


def test_business_pin_frozen_across_binding_switch(tmp_path):
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    db = skill_store.session_for(lab)
    try:
        rows = _rows((SKILL_A, SKILL_B))
        for r in rows:
            db.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
                    "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
                    "purpose_md, content_hash, source_type, created_by, created_at) "
                    "VALUES (:vid,:sid,:seq,'skill-evolution/v1','wiki_compile.default',"
                    "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
                    "'8w',CURRENT_TIMESTAMP)"),
                {"vid": r.version_id, "sid": r.skill_id, "seq": r.seq,
                 "sm": r.skill_md, "pm": r.purpose_md, "ch": r.content_hash})
        db.commit()
        old_plan = FrozenSkillSet.from_versions(rows[:1])
        new_plan = FrozenSkillSet.from_versions(rows)
        old_pin = {"mode": "bound",
                   "members": [{"skill_id": rows[0].skill_id,
                                "version_id": rows[0].version_id,
                                "content_hash": rows[0].content_hash,
                                "seq": rows[0].seq}],
                   "set_hash": old_plan.set_hash}
        new_pin = {"mode": "bound",
                   "members": [{"skill_id": r.skill_id, "version_id": r.version_id,
                                "content_hash": r.content_hash, "seq": r.seq}
                               for r in rows],
                   "set_hash": new_plan.set_hash}
        user = [{"role": "user", "content": "gen"}]
        old_seen, old_inner = _capture()
        ctx_old = {"llm_runner": old_inner}
        bops._apply_pin(ctx_old, db, old_pin, "wiki_compile.default")
        # 绑定切换到双技能后，旧 compile run 仍只用冻结旧集合
        ctx_old["llm_runner"](user, context="wiki-synthesis")
        new_seen, new_inner = _capture()
        ctx_new = {"llm_runner": new_inner}
        bops._apply_pin(ctx_new, db, new_pin, "wiki_compile.default")
        ctx_new["llm_runner"](user, context="wiki-synthesis")
    finally:
        db.close()
    old_blob = json.dumps(old_seen[0]["messages"], ensure_ascii=False)
    new_blob = json.dumps(new_seen[0]["messages"], ensure_ascii=False)
    assert SKILL_A in old_blob and SKILL_B not in old_blob
    assert SKILL_A in new_blob and SKILL_B in new_blob
    exp_old, inner_old = _capture()
    SkillInjectingRunner(old_plan, inner_old, lambda _e: None)(
        user, context="wiki-synthesis")
    assert old_seen[0]["messages"] == exp_old[0]["messages"]


def test_single_system_message_builder_is_shared():
    text = render_execution_skill_text(_rows((SKILL_A,)))
    msgs, injected, reason = build_injected_messages(
        [{"role": "user", "content": "u"}], "wiki-synthesis",
        instruction_text=text, mode="versions")
    assert injected is True and reason == "content_generation"
    assert msgs[0] == {"role": "system", "content": text}
    msgs2, injected2, reason2 = build_injected_messages(
        [{"role": "user", "content": "u"}], "wiki-ingest-page",
        instruction_text=text, mode="versions")
    assert injected2 is False and reason2 == "context_not_content"
    assert msgs2 == [{"role": "user", "content": "u"}]
    assert bops.render_instruction_block({
        "skill_id": "skill-a", "version_id": "skill-a:0001", "seq": 1,
        "skill_md": _md("skill-a", SKILL_A)[0],
    }) == text
