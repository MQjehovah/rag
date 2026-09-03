"""Phase 6 Skill API 测试（20.8：字段过滤 / 权限 / override/unlock）。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.main import app
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    WikiPage,
    WikiRevision,
    WikiWorkspace,
    init_db,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'skill.db').as_posix()}"
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


@pytest.fixture(autouse=True)
def _clean_skill_registry():
    from app.core.wiki_skills import registry as sreg

    sreg.clear_for_tests()
    yield
    sreg.clear_for_tests()


def _register_default():
    from app.core.wiki_skills import service as skill_service

    skill_service.register_default_skill()


def _override(user: dict):
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: user


def _db(engine):
    return sessionmaker(bind=engine)()


def _seed_wiki(engine, *, acl_json='{"groups": ["group_a"]}'):
    db = _db(engine)
    ws = WikiWorkspace(
        id="ws1", key="ws-k1", name="组A空间",
        acl_scope=acl_json, scope_id="group:group_a", status="active",
    )
    db.add(ws)
    db.flush()
    wiki = WikiPage(
        id="w1", title="主题", workspace_id="ws1", status="published",
        acl_scope=acl_json,
    )
    db.add(wiki)
    db.commit()
    db.close()
    return "w1"


# ---------------------------------------------------------------------------
# GET /api/wiki-skills
# ---------------------------------------------------------------------------


def test_skills_list_safe_fields(client):
    c, _ = client
    _register_default()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.get("/api/wiki-skills")
    assert r.status_code == 200
    skills = r.json()["skills"]
    keys = [s["key"] for s in skills]
    assert "default" in keys
    d = next(s for s in skills if s["key"] == "default")
    for field in ("key", "active_version", "versions", "label", "description", "availability"):
        assert field in d
    blob = json.dumps(d)
    for forbidden in ("runtime_key", "instructions", "extraction.schema.json",
                      "blueprint.schema.json", "python_path", ".py"):
        assert forbidden not in blob


def test_skill_detail(client):
    c, _ = client
    _register_default()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.get("/api/wiki-skills/default")
    assert r.status_code == 200
    assert r.json()["active_version"] == "1"


def test_skill_detail_unknown_404(client):
    c, _ = client
    _register_default()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.get("/api/wiki-skills/ghost")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/wiki/{id}/skill-override
# ---------------------------------------------------------------------------

_ADMIN = {"id": "admin", "username": "a", "groups": ["admins"]}
_EDITOR = {"id": "e1", "username": "e", "groups": ["group_a", "editors"]}
_NORMAL = {"id": "u1", "username": "u", "groups": ["group_a"]}
_OUTSIDER = {"id": "u2", "username": "u2", "groups": ["group_b"]}


def test_normal_user_cannot_override(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_NORMAL)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True,
    })
    assert r.status_code == 403


def test_normal_user_cannot_unlock(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_NORMAL)
    r = c.post("/api/wiki/w1/skill-unlock", json={})
    assert r.status_code == 403


def test_outsider_invisible_wiki_forbidden(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_OUTSIDER)  # group_b 看不到 group_a scope 的 wiki → 403（沿用既有编辑语义）
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1",
    })
    assert r.status_code == 403
    r2 = c.post("/api/wiki/w1/skill-unlock", json={})
    assert r2.status_code == 403


def test_editor_can_override_own_scope(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_EDITOR)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True,
    })
    assert r.status_code == 200
    db = _db(engine)
    wiki = db.get(WikiPage, "w1")
    assert wiki.content_skill == "default"
    assert wiki.skill_version == "1"
    assert wiki.skill_selected_by == "manual"
    assert wiki.skill_locked is True
    assert json.loads(wiki.skill_decision_json)["reason_code"] == "MANUAL_OVERRIDE"
    db.close()


def test_admin_can_override(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_ADMIN)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True,
    })
    assert r.status_code == 200


def test_unknown_key_version_controlled(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_ADMIN)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "ghost", "skill_version": "1",
    })
    assert r.status_code in (400, 404)
    r2 = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "99",
    })
    assert r2.status_code in (400, 404)


def test_override_does_not_change_workspace_or_acl(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_ADMIN)
    c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True,
    })
    db = _db(engine)
    wiki = db.get(WikiPage, "w1")
    assert wiki.workspace_id == "ws1"
    assert json.loads(wiki.acl_scope) == {"groups": ["group_a"]}
    db.close()


def test_lock_respected_and_recompile_false_no_revision(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_ADMIN)
    # 先 lock + recompile=false：不创建 Revision。
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True, "recompile": False,
    })
    assert r.status_code == 200
    db = _db(engine)
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == "w1").count() == 0
    db.close()


def test_unlock_keeps_current_skill(client):
    c, engine = client
    _register_default()
    _seed_wiki(engine)
    _override(_ADMIN)
    c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True,
    })
    r = c.post("/api/wiki/w1/skill-unlock", json={})
    assert r.status_code == 200
    db = _db(engine)
    wiki = db.get(WikiPage, "w1")
    assert wiki.skill_locked is False
    assert wiki.content_skill == "default"  # unlock 保留当前 skill/version
    assert wiki.skill_version == "1"
    assert json.loads(wiki.skill_decision_json)["reason_code"] == "MANUAL_UNLOCK"
    db.close()


def test_recompile_true_creates_v2_run(client, monkeypatch):
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        register_default_pipeline_v2,
    )
    from app.core.wiki_skills import registry as sreg

    pregs.clear_for_tests()
    _register_default()
    register_default_pipeline()
    register_default_pipeline_v2()
    assert pregs.get_pipeline("wiki.default", "2") is not None
    c, engine = client
    _seed_wiki(engine)
    _override(_ADMIN)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True, "recompile": True,
    })
    assert r.status_code == 200
    run_info = r.json()["run"]
    assert run_info is not None
    assert run_info["pipeline_version"] == "2"
    assert run_info["trigger_type"] == "manual_rebuild"
    db = _db(engine)
    run = db.query(CompileRun).filter(CompileRun.id == run_info["run_id"]).first()
    assert run is not None
    assert run.pipeline_version == "2"
    db.close()


def test_api_never_returns_internal_fields(client):
    c, _ = client
    _register_default()
    _override(_ADMIN)
    r = c.get("/api/wiki-skills/default")
    blob = r.text
    for forbidden in ("runtime_key", "python_path", ".py", "instructions.md",
                      "loader", "registry"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# Phase 6.1：recompile=true 失败 → 409 + 全量回滚（不半提交、不残留 Run）
# ---------------------------------------------------------------------------


def _seed_wiki_nows(engine, *, acl_json='{"groups": ["group_a"]}'):
    db = _db(engine)
    db.add(WikiPage(
        id="w2", title="无空间主题", workspace_id=None, status="published",
        acl_scope=acl_json,
    ))
    db.commit()
    db.close()


def _skill_field_snapshot(engine, wiki_id="w2"):
    db = _db(engine)
    w = db.get(WikiPage, wiki_id)
    snap = {
        "content_skill": w.content_skill,
        "skill_version": w.skill_version,
        "skill_selected_by": w.skill_selected_by,
        "skill_confidence": w.skill_confidence,
        "skill_locked": w.skill_locked,
        "skill_decision_json": w.skill_decision_json,
    }
    db.close()
    return snap


def _compile_run_count(engine):
    db = _db(engine)
    n = db.query(CompileRun).count()
    db.close()
    return n


def test_override_no_workspace_recompile_rollback(client):
    c, engine = client
    _register_default()
    _seed_wiki_nows(engine)
    _override(_ADMIN)
    before = _skill_field_snapshot(engine)
    runs_before = _compile_run_count(engine)
    r = c.post("/api/wiki/w2/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True, "recompile": True,
    })
    assert r.status_code == 409
    assert _skill_field_snapshot(engine) == before  # 字段全部保持原值
    assert _compile_run_count(engine) == runs_before  # 不残留 Run


def test_unlock_no_workspace_recompile_rollback(client):
    c, engine = client
    _register_default()
    _seed_wiki_nows(engine)
    db = _db(engine)
    w = db.get(WikiPage, "w2")
    w.content_skill = "default"
    w.skill_version = "1"
    w.skill_selected_by = "manual"
    w.skill_confidence = 1.0
    w.skill_locked = True
    import json as _json
    w.skill_decision_json = _json.dumps({"schema_version": "skill-decision/v1"})
    db.commit()
    db.close()
    before = _skill_field_snapshot(engine)
    runs_before = _compile_run_count(engine)
    _override(_ADMIN)
    r = c.post("/api/wiki/w2/skill-unlock", json={"recompile": True})
    assert r.status_code == 409
    # 解锁失败：原锁定状态 + Skill + decision_json 全部保持不变。
    assert _skill_field_snapshot(engine) == before
    assert _compile_run_count(engine) == runs_before


def test_override_v2_not_registered_recompile_rollback(client):
    from app.core.wiki_pipeline import registry as pregs

    pregs.clear_for_tests()  # wiki.default v2 未注册
    c, engine = client
    _register_default()
    _seed_wiki(engine)  # w1 有 workspace
    before = _skill_field_snapshot(engine, "w1")
    runs_before = _compile_run_count(engine)
    _override(_ADMIN)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True, "recompile": True,
    })
    assert r.status_code == 409
    assert _skill_field_snapshot(engine, "w1") == before  # 数据全部回滚
    assert _compile_run_count(engine) == runs_before


def test_override_recompile_create_run_exception_rollback(client, monkeypatch):
    import app.core.wiki_pipeline.executor as exec_mod
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        register_default_pipeline_v2,
    )

    pregs.clear_for_tests()
    _register_default()
    register_default_pipeline()
    register_default_pipeline_v2()
    c, engine = client
    _seed_wiki(engine)
    before = _skill_field_snapshot(engine, "w1")
    runs_before = _compile_run_count(engine)

    def _boom(**kw):
        raise RuntimeError("create_run exploded")
    monkeypatch.setattr(exec_mod, "create_run", _boom)
    _override(_ADMIN)
    r = c.post("/api/wiki/w1/skill-override", json={
        "skill_key": "default", "skill_version": "1", "lock": True, "recompile": True,
    })
    assert r.status_code == 409
    # Wiki 与 CompileRun 均无半提交。
    assert _skill_field_snapshot(engine, "w1") == before
    assert _compile_run_count(engine) == runs_before
