"""B2 写路径（完整集合晋升 + 集合 CAS）反例测试（business_ops，隔离库）。

本轮范围：完整集合晋升写路径 + rev/set_hash 集合 CAS + 可回退审计材料；
不扩展回退、编译注入、skill_store 或前端。

覆盖：
- 双成员晋升后集合读接口完整读回；实验→业务转换成员及顺序等价；
- 第二成员损坏 / 第二成员复制冲突 / 审计失败 → 整体无写入（同事务回滚）；
- 陈旧 rev/hash 拒绝；A→B→A 后旧 rev 请求仍拒绝（防 ABA）；
- 两个独立连接竞争更新 / 首次绑定竞争（真实并发线程，非顺序模拟）；
- 旧 NULL 单成员物化兼容（读取重算哈希 ≠ DB NULL，受保护升级）；
- 同请求重放不重复写入、同键不同内容拒绝；空集合策略明确、无静默退化；
- P53 审计迁移（隔离库单表升级/降级往返）。

全部隔离 SQLite 文件库；不调用真实模型、不操作生产。
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
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import skill_store
from app.core.skill_evolution.injector import (
    EMPTY_SET_HASH,
    FrozenSkillSet,
    set_hash_for_members,
)
from app.models.database import init_db
from app.models import evolution as evmod

WS = "ws-prod-1"
GRADER = "wiki-default-grader/v1"


# ---------------------------------------------------------------------------
# 隔离库工具
# ---------------------------------------------------------------------------


def _payload_hash(skill_id: str, skill_md: str, purpose_md: str) -> str:
    payload = json.dumps(
        {"skill_id": skill_id, "domain": "wiki_compile.default",
         "runtime_ref": "wiki.compile.default.runtime/v1",
         "schema_version": "skill-evolution/v1",
         "skill_md": skill_md, "purpose_md": purpose_md},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _md_for(skill_id: str, seq: int, marker: str) -> tuple[str, str]:
    skill_md = (f"---\nskill_id: {skill_id}\ndomain: wiki_compile.default\n"
                "runtime_ref: wiki.compile.default.runtime/v1\n"
                "schema_version: 1\n---\n"
                f"# {skill_id}\n\n## 适用条件\n- 通用\n"
                "## 不适用条件\n- 无\n"
                f"## 操作步骤\n- {marker}\n")
    purpose_md = ("## 来源\n- 8m\n## 改进目的\n- 集合晋升\n"
                  "## 演化历史\n- v1\n")
    return skill_md, purpose_md


def _add_version(db, skill_id: str, seq: int, marker: str,
                 *, mutate_after: bool = False) -> dict:
    version_id = f"{skill_id}:{seq:04d}"
    skill_md, purpose_md = _md_for(skill_id, seq, marker)
    ch = _payload_hash(skill_id, skill_md, purpose_md)
    db.execute(text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:vid,:sid,:seq,'skill-evolution/v1','wiki_compile.default',"
        "'wiki.compile.default.runtime/v1',NULL,:sm,:pm,:ch,'manual_seed',"
        "'8m',CURRENT_TIMESTAMP)"),
        {"vid": version_id, "sid": skill_id, "seq": seq, "sm": skill_md,
         "pm": purpose_md, "ch": ch})
    if mutate_after:
        db.execute(text(
            "UPDATE evolution_skill_versions SET skill_md=skill_md || ' ' "
            "WHERE version_id=:v"), {"v": version_id})
    db.commit()
    row = {"skill_id": skill_id, "version_id": version_id,
           "content_hash": ch, "seq": seq, "skill_md": skill_md,
           "purpose_md": purpose_md}
    return row


def _claim(row: dict) -> dict:
    return {"skill_id": row["skill_id"], "version_id": row["version_id"],
            "content_hash": row["content_hash"], "seq": row["seq"]}


def _accepted_doc(claims: list[dict]) -> dict:
    """FrozenSkillSet.to_dict() 形态的实验已接受集合文档。"""
    if not claims:
        return {"mode": "empty", "version_ids": [], "content_hashes": [],
                "set_hash": EMPTY_SET_HASH, "members": [],
                "injected_text_present": False, "instruction_chars": 0}
    specs = bops.canonical_binding_members(claims)
    return {"mode": "versions",
            "version_ids": [m["version_id"] for m in specs],
            "content_hashes": [m["content_hash"] for m in specs],
            "set_hash": set_hash_for_members(specs), "members": specs,
            "injected_text_present": True,
            "instruction_chars": 0}


def _exp_row(exp_id: str, doc: dict) -> SimpleNamespace:
    js = json.dumps(doc, ensure_ascii=False)
    return SimpleNamespace(
        experiment_id=exp_id, best_skill_set_json=js,
        current_skill_set_json=js, best_score_passed=5, best_score_total=5,
        grader_version=GRADER)


@pytest.fixture()
def exp_db(tmp_path):
    """实验库：仅版本行（evolution schema）；promote 只读它验证/复制成员。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'exp.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    evmod.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    yield db
    db.close()
    engine.dispose()


@pytest.fixture()
def biz_db(tmp_path, monkeypatch):
    """业务库（P52/P53 模型 create_all；promote/rollback/CAS 目标）。"""
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    engine = create_engine(f"sqlite:///{(tmp_path / 'biz.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    init_db(engine)
    evmod.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    db.execute(text(
        "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
        "status) VALUES ('ws-prod-1','k','8m','public','public','active')"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 1) 双成员晋升：完整读回 + 实验→业务转换等价 + 审计材料
# ---------------------------------------------------------------------------


def test_promote_two_member_set_full_readback_and_audit(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "指令-default")
    r1 = _add_version(exp_db, "refcheck", 1, "指令-refcheck")
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    res = bops.promote(biz_db, exp_db, _exp_row("exp-m", doc),
                       workspace_id=WS, created_by="t",
                       allow_simulated=True, idempotency_key="k-m")
    assert res["changed"] is True
    assert res["version_id"] == "default:0001"   # 规范顺序首成员（兼容字段）
    assert res["rev"] == 1
    assert [m["version_id"] for m in res["members"]] == [
        "default:0001", "refcheck:0001"]
    assert res["set_hash"] == bops.binding_set_hash(res["members"])
    # 集合读接口完整读回（非首成员压扁）
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["kind"] == "skill" and st["rev"] == 1 and st["legacy"] is False
    assert [m["version_id"] for m in st["members"]] == [
        "default:0001", "refcheck:0001"]
    assert st["canonical_set_hash"] == res["set_hash"]
    assert st["members"][0]["skill_md"] == d1["skill_md"]
    assert st["members"][1]["skill_md"] == r1["skill_md"]
    # 实验→业务等价：exp 成员声明(顺序/内容哈希) == 业务存储成员
    for exp_claim, stored in zip(doc["members"], st["members"]):
        assert {k: exp_claim[k] for k in ("skill_id", "version_id",
                                          "content_hash", "seq")} == {
            k: stored[k] for k in ("skill_id", "version_id",
                                   "content_hash", "seq")}
    # 两种哈希算法并存（字段/schema 明确区分），字符串不互比
    assert doc["set_hash"] != res["set_hash"]   # 算法不同（含 seq/排序差异）
    # 审计事件保存完整 to 集合 + 哈希 + 修订；多成员 pair 列留 NULL
    import sqlalchemy as sa
    ev = biz_db.execute(sa.text(
        "SELECT * FROM evolution_business_events WHERE idempotency_key='k-m'")
    ).fetchone()
    assert ev.to_version_id is None                 # 不把集合压成首版本
    assert json.loads(ev.to_members_json) == res["members"]
    assert ev.to_set_hash == res["set_hash"] and int(ev.to_rev) == 1
    evidence = json.loads(ev.evidence_json)
    assert evidence["exp_set"]["schema_version"] == "experiment-set/v1"
    assert evidence["exp_set"]["set_hash"] == doc["set_hash"]
    assert evidence["binding_schema_version"] == "business-binding-set/v1"
    # 同集合再晋升（新键）→ already_current，不写新事件
    res2 = bops.promote(biz_db, exp_db, _exp_row("exp-m", doc),
                        workspace_id=WS, created_by="t",
                        allow_simulated=True, idempotency_key="k-m2")
    assert res2["changed"] is False and res2["already_current"] is True
    n = biz_db.execute(sa.text(
        "SELECT count(*) c FROM evolution_business_events")).fetchone().c
    assert n == 1


def test_conversion_order_matches_actual_injection(exp_db, biz_db):
    """转换顺序 = 实验实际注入顺序（FrozenSkillSet.from_versions，(skill_id,seq)），
    与输入声明顺序无关。"""
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B")
    # 输入乱序 → 规范输出固定
    shuffled = [_claim(r1), _claim(d1)]
    canonical = bops.canonical_binding_members(shuffled)
    assert [m["version_id"] for m in canonical] == [
        "default:0001", "refcheck:0001"]
    rows = [skill_store.get_version(exp_db, v["version_id"])
            for v in shuffled]
    frozen = FrozenSkillSet.from_versions(rows)   # 实验实际注入顺序
    assert [m["version_id"] for m in frozen.members] == [
        "default:0001", "refcheck:0001"]
    assert canonical == list(frozen.members)      # 逐成员等价（含 seq/哈希）


# ---------------------------------------------------------------------------
# 2) 第二成员损坏 / 复制冲突 / 审计失败 → 整体无写入
# ---------------------------------------------------------------------------


def _versions_count(db) -> int:
    return db.execute(text(
        "SELECT count(*) c FROM evolution_skill_versions")).fetchone().c


def _bindings_count(db) -> int:
    return db.execute(text(
        "SELECT count(*) c FROM evolution_skill_bindings "
        "WHERE kind='business' AND workspace_id=:w"),
        {"w": WS}).fetchone().c


def _events_count(db) -> int:
    return db.execute(text(
        "SELECT count(*) c FROM evolution_business_events")).fetchone().c


def test_second_member_corrupt_fails_with_no_write(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B", mutate_after=True)  # 哈希失配
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("exp-x", doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "source_version_corrupt"
    assert _versions_count(biz_db) == 0
    assert _bindings_count(biz_db) == 0
    assert _events_count(biz_db) == 0


def test_second_member_missing_version_no_write(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B")
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    exp_db.execute(text(
        "DELETE FROM evolution_skill_versions WHERE version_id='refcheck:0001'"))
    exp_db.commit()
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("exp-x2", doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "version_not_found"
    assert _versions_count(biz_db) == 0 and _bindings_count(biz_db) == 0
    assert _events_count(biz_db) == 0


def test_second_member_copy_conflict_rolls_back_all(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B")
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    # 业务库预置同版本但不同内容 → 第二成员复制冲突
    _add_version(biz_db, "refcheck", 1, "与实验不同的内容 B'")
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("exp-c", doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "version_conflict"
    # 第一成员已复制的行也随事务回滚（全有或全无）
    assert _versions_count(biz_db) == 1          # 仅预置的 refcheck
    assert _bindings_count(biz_db) == 0 and _events_count(biz_db) == 0
    left = biz_db.execute(text(
        "SELECT version_id FROM evolution_skill_versions")).fetchone()
    assert left.version_id == "refcheck:0001"


def test_audit_failure_rolls_back_versions_and_binding(exp_db, biz_db,
                                                       monkeypatch):
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B")
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    orig = bops._append_event

    def boom(*a, **k):
        raise RuntimeError("audit write failed")
    monkeypatch.setattr(bops, "_append_event", boom)
    try:
        with pytest.raises(RuntimeError):
            bops.promote(biz_db, exp_db, _exp_row("exp-a", doc),
                         workspace_id=WS, created_by="t",
                         allow_simulated=True)
    finally:
        monkeypatch.setattr(bops, "_append_event", orig)
    # 版本复制、绑定写入、事件三者整体回滚（函数内部已 rollback，不留半写）
    assert _versions_count(biz_db) == 0
    assert _bindings_count(biz_db) == 0
    assert _events_count(biz_db) == 0


# ---------------------------------------------------------------------------
# 3) 完整集合 CAS：陈旧 rev/hash、A→B→A 防 ABA、真实并发
# ---------------------------------------------------------------------------


def _seed_biz_version(db, skill_id: str, seq: int, marker: str) -> dict:
    return _add_version(db, skill_id, seq, marker)


def test_stale_rev_and_hash_rejected_no_write(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    d2 = _add_version(exp_db, "default", 2, "A-v2")
    bops.promote(biz_db, exp_db, _exp_row("e-a", _accepted_doc([_claim(d1)])),
                 workspace_id=WS, created_by="t", allow_simulated=True)
    st = bops.business_state(biz_db, WS)
    assert st["current"]["rev"] == 1
    old_hash = st["current_set_hash"]
    # 并发推进到 v2
    bops.promote(biz_db, exp_db, _exp_row("e-b", _accepted_doc([_claim(d2)])),
                 workspace_id=WS, created_by="t", allow_simulated=True)
    st2 = bops.business_state(biz_db, WS)
    assert st2["current"]["rev"] == 2
    # 陈旧 hash → 409
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-b", _accepted_doc([_claim(d2)])),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     expected_set_hash=old_hash)
    assert ei.value.code == "binding_conflict"
    # 陈旧 rev → 409
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.promote(biz_db, exp_db, _exp_row("e-b", _accepted_doc([_claim(d2)])),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     expected_rev=1)
    assert ei2.value.code == "binding_conflict"
    assert bops.business_state(biz_db, WS)["current"]["rev"] == 2


def test_aba_old_rev_request_rejected(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    d2 = _add_version(exp_db, "default", 2, "A-v2")
    bops.promote(biz_db, exp_db, _exp_row("e1", _accepted_doc([_claim(d1)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k-1")
    bops.promote(biz_db, exp_db, _exp_row("e2", _accepted_doc([_claim(d2)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k-2")
    bops.promote(biz_db, exp_db, _exp_row("e1", _accepted_doc([_claim(d1)])),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k-3")
    st = bops.business_state(biz_db, WS)
    assert st["current"]["rev"] == 3
    # A→B→A 之后，旧 rev=1 的 B 请求必须拒绝（防 ABA：不因集合曾出现过而放行）
    doc_b = _accepted_doc([_claim(d2)])
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e2", doc_b),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     expected_rev=1,
                     expected_set_hash=bops.binding_set_hash(doc_b["members"]))
    assert ei.value.code == "binding_conflict"
    assert bops.business_state(biz_db, WS)["current"]["version_id"] == \
        "default:0001"  # 绑定未被写坏
    assert _events_count(biz_db) == 3


def _file_biz(tmp_path, name: str):
    """并发专用：独立 NullPool engine + session（单线程内使用）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / name).as_posix()}",
                           connect_args={"check_same_thread": False},
                           poolclass=NullPool)
    init_db(engine)
    evmod.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text(
        "INSERT OR IGNORE INTO wiki_workspaces (id, key, name, acl_scope, "
        "scope_id, status) VALUES ('ws-prod-1','k','8m','public','public',"
        "'active')"))
    db.commit()
    return db, engine


def test_concurrent_update_two_connections_only_one_succeeds(tmp_path,
                                                             monkeypatch):
    """两个独立连接同时 CAS 更新同一绑定：恰好一方成功，另一方 409。"""
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    # 预置：业务库 rev=1 绑定集合 [default:0001]
    biz1, _ = _file_biz(tmp_path, "conc.db")
    d1 = _add_version(biz1, "default", 1, "A")
    d2 = _add_version(biz1, "default", 2, "B")
    spec_a = [_claim(d1)]
    bops._materialize_set_binding(biz1, WS, members=spec_a,
                                  expected_rev=None, expected_set_hash=None,
                                  created_by="seed")
    biz1.commit()
    biz1.close()
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker(i: int):
        try:
            db, _eng = _file_biz(tmp_path, "conc.db")
            try:
                barrier.wait()
                out = bops._materialize_set_binding(
                    db, WS, members=[_claim(d2)],
                    expected_rev=1,
                    expected_set_hash=bops.binding_set_hash(spec_a),
                    created_by=f"w{i}")
                results.append(("ok", out))
                db.commit()
            finally:
                db.close()
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 1, f"应恰好一个成功: {results} {errors}"
    assert results[0][1]["rev"] == 2
    assert len(errors) == 1 and errors[0] == "binding_conflict"
    # 最终状态：rev=2、集合 B、仅一个 switch
    db, _eng = _file_biz(tmp_path, "conc.db")
    try:
        st = bops.resolve_business_binding_set(db, WS)
        assert st["rev"] == 2
        assert [m["version_id"] for m in st["members"]] == ["default:0002"]
    finally:
        db.close()


def test_first_bind_concurrent_unique_conflict(tmp_path, monkeypatch):
    """首次绑定竞争：唯一键约束兜底，一方成功一方 409（不后写覆盖）。"""
    monkeypatch.setattr(
        settings, "wikiskill_evolution_allow_simulated_promotion", True)
    biz1, _ = _file_biz(tmp_path, "first.db")
    d1 = _add_version(biz1, "default", 1, "A")
    biz1.commit()
    spec_a = [_claim(d1)]
    biz1.close()
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker(i: int):
        try:
            db, _eng = _file_biz(tmp_path, "first.db")
            try:
                barrier.wait()
                out = bops._materialize_set_binding(
                    db, WS, members=spec_a,
                    expected_rev=None, expected_set_hash=None,
                    created_by=f"f{i}")
                results.append(("ok", out))
                db.commit()
            finally:
                db.close()
        except bops.BusinessOpsError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 1, f"应恰好一个成功: {results} {errors}"
    assert len(errors) == 1 and errors[0] == "binding_conflict"
    db, _eng = _file_biz(tmp_path, "first.db")
    try:
        rows = db.execute(text(
            "SELECT count(*) c FROM evolution_skill_bindings WHERE "
            "kind='business' AND workspace_id=:w"), {"w": WS}).fetchone().c
        assert rows == 1
        st = bops.resolve_business_binding_set(db, WS)
        assert st["rev"] == 1
        assert st["members"][0]["version_id"] == "default:0001"
    finally:
        db.close()


def test_legacy_null_single_materialized_compat(exp_db, biz_db):
    """旧 NULL 单成员行：读取重算哈希 ≠ DB NULL，经 rev 受保护升级物化。"""
    # 业务库旧格式绑定行（members_json/set_hash NULL, rev=1）+ 版本行
    biz1_v = _add_version(biz_db, "default", 1, "旧 A")
    _add_version(biz_db, "default", 2, "新 B")
    biz_db.execute(text(
        "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, domain, "
        "set_kind, skill_id, version_id, rev, set_hash, members_json, "
        "created_by, created_at, updated_at) VALUES ('bl-legacy','business',"
        ":w,'wiki_compile.default','skill','default','default:0001',1,NULL,"
        "NULL,'8m',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {"w": WS})
    biz_db.commit()
    st0 = bops.business_state(biz_db, WS)
    assert st0["current"]["legacy"] is True
    assert st0["current"]["rev"] == 1
    # 用 business_state 给出的规范哈希/修订作 CAS 预期 → 允许（旧行 set_hash 为
    # NULL 也按重算哈希语义比较），成功后物化 rev2
    d2 = _add_version(exp_db, "default", 2, "新 B")
    doc = _accepted_doc([_claim(d2)])
    res = bops.promote(biz_db, exp_db, _exp_row("e-legacy", doc),
                       workspace_id=WS, created_by="t", allow_simulated=True,
                       expected_rev=st0["current"]["rev"],
                       expected_set_hash=st0["current_set_hash"],
                       idempotency_key="k-legacy")
    assert res["changed"] is True and res["rev"] == 2
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["legacy"] is False
    assert st["rev"] == 2
    assert st["members"][0]["version_id"] == "default:0002"
    assert st["set_hash"] == st["canonical_set_hash"] == res["set_hash"]
    # 同一行被升级而非追加；旧格式行的规范哈希与库中 NULL 明确区分
    rows = biz_db.execute(text(
        "SELECT count(*) c FROM evolution_skill_bindings WHERE kind='business' "
        "AND workspace_id=:w"), {"w": WS}).fetchone().c
    assert rows == 1
    # 审计含 from 集合（旧单成员，含其版本 seq）与修订
    import sqlalchemy as sa
    ev = biz_db.execute(sa.text(
        "SELECT * FROM evolution_business_events "
        "WHERE idempotency_key='k-legacy'")).fetchone()
    assert json.loads(ev.from_members_json) == [{
        "skill_id": "default", "version_id": "default:0001",
        "content_hash": biz1_v["content_hash"], "seq": 1}]
    assert ev.from_set_hash == st0["current_set_hash"]
    assert int(ev.from_rev) == 1 and int(ev.to_rev) == 2
    assert ev.to_version_id == "default:0002"   # 单成员镜像旧列
    # 读取快照也一致：legacy NULL 状态下 resolve 不把 NULL 当空集合
    assert st0["current_set_hash"] != bops.binding_set_hash([])


# ---------------------------------------------------------------------------
# 4) 幂等重放 / 同键不同内容 / 空集合策略
# ---------------------------------------------------------------------------


def test_idempotent_replay_and_same_key_diff_content(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    d2 = _add_version(exp_db, "default", 2, "B")
    doc_a = _accepted_doc([_claim(d1)])
    bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="shared")
    # 同键同请求重放 → 返回历史结果，不重复写入
    replay = bops.promote(biz_db, exp_db, _exp_row("e-a", doc_a),
                          workspace_id=WS, created_by="t",
                          allow_simulated=True, idempotency_key="shared")
    assert replay.get("replay") is True and replay["changed"] is False
    assert _events_count(biz_db) == 1
    # 同键不同内容（目标集合不同）→ 拒绝
    doc_b = _accepted_doc([_claim(d2)])
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-b", doc_b),
                     workspace_id=WS, created_by="t", allow_simulated=True,
                     idempotency_key="shared")
    assert ei.value.code == "idem_content_conflict"
    assert bops.business_state(biz_db, WS)["current"]["version_id"] == \
        "default:0001"


def test_multi_member_replay_returns_full_set(exp_db, biz_db):
    d1 = _add_version(exp_db, "default", 1, "A")
    r1 = _add_version(exp_db, "refcheck", 1, "B")
    doc = _accepted_doc([_claim(d1), _claim(r1)])
    bops.promote(biz_db, exp_db, _exp_row("e-m", doc),
                 workspace_id=WS, created_by="t", allow_simulated=True,
                 idempotency_key="k-replay")
    replay = bops.promote(biz_db, exp_db, _exp_row("e-m", doc),
                          workspace_id=WS, created_by="t",
                          allow_simulated=True, idempotency_key="k-replay")
    assert replay.get("replay") is True
    assert [m["version_id"] for m in (replay["members"] or [])] == [
        "default:0001", "refcheck:0001"]
    assert replay["set_hash"] == bops.binding_set_hash(replay["members"])
    assert _events_count(biz_db) == 1


def test_empty_accepted_set_no_silent_degradation(exp_db, biz_db):
    """空集合晋升策略明确：显式报 no_target；业务绑定不被静默改成空/清掉。"""
    d1 = _add_version(exp_db, "default", 1, "A")
    # 先有一个生效绑定
    bops.promote(biz_db, exp_db, _exp_row("e-a", _accepted_doc([_claim(d1)])),
                 workspace_id=WS, created_by="t", allow_simulated=True)
    empty_doc = _accepted_doc([])
    assert empty_doc["members"] == []
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.promote(biz_db, exp_db, _exp_row("e-empty", empty_doc),
                     workspace_id=WS, created_by="t", allow_simulated=True)
    assert ei.value.code == "no_target"
    # 绑定保持 A（rev 不变），无额外事件
    st = bops.business_state(biz_db, WS)
    assert st["current"]["version_id"] == "default:0001"
    assert st["current"]["rev"] == 1
    assert _events_count(biz_db) == 1
    # 超长集合 → 转换入口明确拒绝
    from app.core.skill_evolution import business_ops as _b
    many = [{"skill_id": f"s{i}", "version_id": f"s{i}:0001",
             "content_hash": "x" * 64, "seq": 1} for i in range(9)]
    with pytest.raises(bops.BusinessOpsError) as ei2:
        _b.canonical_binding_members(many)
    assert ei2.value.code == "exp_set_invalid"


# ---------------------------------------------------------------------------
# 5) P53 审计迁移：隔离库单表往返（不改已交付 P52）
# ---------------------------------------------------------------------------


def test_p53_event_columns_migration_roundtrip(tmp_path):
    db_path = tmp_path / "p53.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}",
                           connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE evolution_business_events ("
            "event_id VARCHAR(64) PRIMARY KEY, "
            "idempotency_key VARCHAR(64) NOT NULL, "
            "action VARCHAR(16) NOT NULL, "
            "workspace_id VARCHAR(64) NOT NULL, "
            "domain VARCHAR(64) NOT NULL, "
            "from_version_id VARCHAR(64), to_version_id VARCHAR(64), "
            "experiment_id VARCHAR(64), run_id VARCHAR(64), "
            "evidence_json TEXT, reason TEXT NOT NULL, "
            "created_by VARCHAR(64), created_at DATETIME, "
            "CONSTRAINT ux_evolution_business_event_idem UNIQUE (idempotency_key))"))
        conn.execute(text(
            "INSERT INTO evolution_business_events (event_id, idempotency_key,"
            " action, workspace_id, domain, to_version_id, reason, created_at) "
            "VALUES ('ev-old','k-old','promote','ws','d','v1','r',"
            "CURRENT_TIMESTAMP)"))
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    mig_file = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
                / "4f83c9e2a1d7_p53_business_event_set_columns.py")
    spec = importlib.util.spec_from_file_location("p53_migration", mig_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cols_after = None
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        mod.op = Operations(ctx)
        mod.upgrade()
        cols = {r[1] for r in conn.execute(text(
            "PRAGMA table_info(evolution_business_events)")).fetchall()}
        assert {"from_members_json", "to_members_json", "from_set_hash",
                "to_set_hash", "from_rev", "to_rev"} <= cols
        row = conn.execute(text(
            "SELECT to_version_id FROM evolution_business_events "
            "WHERE idempotency_key='k-old'")).fetchone()
        assert row.to_version_id == "v1"      # 既有行保留
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        mod.op = Operations(ctx)
        mod.downgrade()
        cols = {r[1] for r in conn.execute(text(
            "PRAGMA table_info(evolution_business_events)")).fetchall()}
        assert "to_members_json" not in cols
        row = conn.execute(text(
            "SELECT to_version_id FROM evolution_business_events "
            "WHERE idempotency_key='k-old'")).fetchone()
        assert row.to_version_id == "v1"
    engine.dispose()


def test_single_alembic_head_p53():
    from tests.alembic_head import current_alembic_head
    assert current_alembic_head()
