"""阶段 8B：技能正文与精确版本差异只读展示验收（离线；不触发模型/写库/迁移）。

覆盖：
- 精确不可变版本正文（SKILL.md/PURPOSE.md）+ 来源/父版本/哈希/候选状态/完整性；
- 同一 skill 两版本正文 unified diff；跨 skill 拒绝；损坏/缺失/超长明确提示；
- 恶意正文按不可信文本原样透传（unsafe 标记），无渲染/无执行、库哈希不变；
- 权限：未登录/非管理员拒绝；未知根/越界路径拒绝。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app.core.jwt_utils import get_current_user

BACKEND = __import__("pathlib").Path(__file__).resolve().parent.parent
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"

A = "apply" * 0  # placeholder


def _client_app():
    from app.main import app
    return app


def _setup(tmp_path, monkeypatch, *, extra=False):
    """构造隔离实验根 + 若干技能版本，返回句柄。

    - seed : default:0001（内置种子）
    - v2   : default:0002（追加一行正文；extra=True 时创建）
    - vlong: default:000x（>20000 字符，仅展示截断场景）
    - other: 另一 skill 身份（diff 跨 skill 场景）
    """
    from app.config import settings
    from app.core.skill_evolution import gating, orchestrator as orch, runenv, skill_store
    from app.core.skill_evolution.contracts import load_dataset
    from app.core.skill_evolution.grader import GRADER_VERSION
    from app.core.skill_evolution.snapshot import SnapshotStore  # noqa: F401

    business = tmp_path / "business.db"
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        srow = skill_store.get_version(db, seed)
        base_md = srow.skill_md
        base_purpose = srow.purpose_md
        out = {"root": root, "default": srow.version_id,
               "default_md": base_md, "default_purpose": base_purpose}
        if extra:
            pkg2 = skill_store.build_package_from_texts(
                base_md + "\n- 8B 测试新增指令行\n",
                base_purpose, source_type="manual_seed", origin="8b-test")
            v2 = skill_store.add_version(db, pkg2, parent_version_id=srow.version_id,
                                         created_by="8b-test")
            long_md = base_md + "\n- " + "x" * 25000 + "\n"
            plong = skill_store.build_package_from_texts(
                long_md, base_purpose, source_type="manual_seed",
                origin="8b-long")
            vlong = skill_store.add_version(db, plong,
                                            parent_version_id=srow.version_id,
                                            created_by="8b-test")
            other = skill_store.build_package_from_texts(
                base_md.replace("skill_id: default", "skill_id: default-other"),
                base_purpose, source_type="manual_seed", origin="8b-other")
            o = skill_store.add_version(db, other, parent_version_id=None,
                                        created_by="8b-test")
            out["v2"] = v2
            out["vlong"] = vlong
            out["other"] = o
        ds_dir = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v3"
        ds = load_dataset(ds_dir)
        ws = "8b-ws"
        members = [{"skill_id": srow.skill_id, "version_id": srow.version_id,
                    "content_hash": srow.content_hash, "seq": srow.seq}]
        exp = gating.create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
            initial_members=members)
        out["exp"] = str(exp.experiment_id)
    finally:
        db.close()
    return out


def _admin(app, value: dict | None):
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return _setup(tmp_path, monkeypatch, extra=True)


def test_content_ok_immutable_and_integrity(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        r = client.get("/api/evolution-console/skills/"
                       f"{env['default']}/content", params={"root": "lab"})
        assert r.status_code == 200
        d = r.json()
        assert d["integrity"] == "ok"
        assert d["version_id"] == env["default"]
        assert d["skill_md"]["label"] == "SKILL.md"
        assert d["skill_md"]["chars"] == len(env["default_md"])
        assert d["skill_md"]["truncated"] is False
        assert d["skill_md"]["body"] == env["default_md"]
        assert d["purpose_md"]["body"] == env["default_purpose"]
        assert d["unsafe"] is True
        assert d["source_type"] == "builtin_seed"
        assert d["parent_version_id"] is None
        _admin(app, None)


def test_diff_same_skill_added_line(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        r = client.get("/api/evolution-console/skills/"
                       f"{env['v2']}/diff",
                       params={"root": "lab", "base_version_id": env["default"]})
        assert r.status_code == 200
        d = r.json()
        assert d["base_version_id"] == env["default"]
        assert d["version_id"] == env["v2"]
        assert d["same_content"] is False
        labels = {f["label"] for f in d["files"]}
        assert labels == {"SKILL.md", "PURPOSE.md"}
        skill_diff = next(f for f in d["files"] if f["label"] == "SKILL.md")
        assert skill_diff["truncated"] is False
        assert "8B 测试新增指令行" in skill_diff["body"]
        assert "+" in skill_diff["body"]
        purpose_diff = next(f for f in d["files"] if f["label"] == "PURPOSE.md")
        assert purpose_diff["lines"] == 0
        _admin(app, None)


def test_diff_rejects_cross_skill(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        r = client.get("/api/evolution-console/skills/"
                       f"{env['other']}/diff",
                       params={"root": "lab", "base_version_id": env["default"]})
        assert r.status_code == 422
        assert "同一 skill_id" in r.json()["detail"]
        _admin(app, None)


def test_corrupt_version_explicit_422(env, monkeypatch):
    import urllib.parse as _up
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        db_file = env["root"] / "skill_store.db"
        uri = "file:" + _up.quote(db_file.resolve().as_posix())
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("UPDATE evolution_skill_versions SET skill_md=skill_md || ' ' "
                         "WHERE version_id=?", (env["v2"],))
            conn.commit()
        finally:
            conn.close()
        r = client.get("/api/evolution-console/skills/"
                       f"{env['v2']}/content", params={"root": "lab"})
        assert r.status_code == 422
        assert "哈希与记录不符" in r.json()["detail"]
        r2 = client.get("/api/evolution-console/skills/"
                        f"{env['default']}/content", params={"root": "lab"})
        assert r2.status_code == 200  # 其他版本不受影响
        _admin(app, None)


def test_missing_version_and_unknown_root_404(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        assert client.get("/api/evolution-console/skills/nope-9999/content",
                          params={"root": "lab"}).status_code == 404
        assert client.get("/api/evolution-console/skills/"
                          f"{env['default']}/content",
                          params={"root": "nope"}).status_code == 404
        assert client.get("/api/evolution-console/skills/..%2F..%2Fetc/content",
                          params={"root": "lab"}).status_code == 404
        assert client.get("/api/evolution-console/skills/"
                          f"{env['default']}/diff",
                          params={"root": "lab",
                                  "base_version_id": "missing-0001"}
                          ).status_code == 404
        _admin(app, None)


def test_long_content_truncated_explicit(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        r = client.get("/api/evolution-console/skills/"
                       f"{env['vlong']}/content", params={"root": "lab"})
        assert r.status_code == 200
        d = r.json()
        sm = d["skill_md"]
        assert sm["truncated"] is True
        assert sm["chars"] > 20000
        assert len(sm["body"]) <= 20000
        assert d["integrity"] == "ok"
        _admin(app, None)


def test_hostile_text_passthrough_no_mutation(env, monkeypatch):
    import urllib.parse as _up
    app = _client_app()
    evil = "<script>alert(1)</script>\n## 操作步骤\n- x"
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        db_file = env["root"] / "skill_store.db"
        uri = "file:" + _up.quote(db_file.resolve().as_posix())
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("SELECT domain,runtime_ref,purpose_md FROM "
                               "evolution_skill_versions WHERE version_id=?",
                               (env["v2"],)).fetchone()
            domain, runtime, purpose = row
            payload = json.dumps({"skill_id": "default-other",
                                  "domain": domain, "runtime_ref": runtime,
                                  "schema_version": "skill-evolution/v1",
                                  "skill_md": evil,
                                  "purpose_md": purpose}, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
            h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            conn.execute("UPDATE evolution_skill_versions SET skill_md=?, "
                         "content_hash=?, skill_id='default-other' "
                         "WHERE version_id=?", (evil, h, env["v2"]))
            conn.commit()
        finally:
            conn.close()
        before = hashlib.sha256(db_file.read_bytes()).hexdigest()
        r = client.get("/api/evolution-console/skills/"
                       f"{env['v2']}/content", params={"root": "lab"})
        assert r.status_code == 200
        d = r.json()
        assert d["integrity"] == "ok"
        assert "<script>alert(1)</script>" in d["skill_md"]["body"]
        assert d["unsafe"] is True
        raw = r.text
        assert "<script>alert(1)</script>" in raw  # 原样文本（渲染端转义）
        assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before
        _admin(app, None)


def test_skills_summary_flips_diff_supported(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        sk = client.get("/api/evolution-console/experiments/"
                        f"{env['exp']}/skills", params={"root": "lab"}).json()
        assert sk["content_diff_supported"] is True
        assert "diff" in sk["note"]
        _admin(app, None)


def test_permissions_deny_nonadmin_and_anon(env, monkeypatch):
    app = _client_app()
    with TestClient(app) as client:
        r = client.get("/api/evolution-console/skills/"
                       f"{env['default']}/content", params={"root": "lab"})
        assert r.status_code == 401
        _admin(app, {"id": "u", "groups": ["company"]})
        r = client.get("/api/evolution-console/skills/"
                       f"{env['default']}/diff",
                       params={"root": "lab", "base_version_id": env["v2"]})
        assert r.status_code == 403
        _admin(app, None)
