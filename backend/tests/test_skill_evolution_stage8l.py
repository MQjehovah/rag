"""阶段 8L（B2 只读解析）：业务绑定完整技能集合读取（business_ops，隔离库）。

本轮只覆盖“完整集合只读解析”，不触及晋升写入/回退/CAS/前端：
- resolve_business_binding_set：统一完整集合读取（rev + 完整成员 + 规范集合哈希
  + 版本内容；members_json=NULL 按旧单技能/旧空集合兼容读；空集合与旧记录明确
  区分）；任一成员缺失/损坏/重复/不兼容/顺序或集合哈希不一致 → 明确失败。
- resolve_business_binding：单技能调用方兼容（空集合→None；多成员集合 → 明确
  报错，绝不静默压成首成员）。

全部离线 SQLite；不调用真实模型、不操作生产。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import skill_store

DOMAIN = skill_store.DEFAULT_DOMAIN
RUNTIME_REF = skill_store.RUNTIME_REF
WS = "ws-l1"


# ---------------------------------------------------------------------------
# 隔离库工具
# ---------------------------------------------------------------------------


@pytest.fixture()
def biz_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    from app.models.database import init_db
    init_db(engine)
    from app.models import evolution as ev
    ev.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    db.execute(text(
        "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, "
        "status) VALUES ('ws-l1', 'k', '8l', 'public', 'public', 'active')"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _add_version(db, skill_id, version_id, seq, marker,
                 *, mutate_md_after=None):
    """插入 business 库版本行（规范 content_hash 与写入路径一致）。"""
    skill_md = (f"---\nskill_id: {skill_id}\ndomain: wiki_compile.default\n"
                "runtime_ref: wiki.compile.default.runtime/v1\n"
                "schema_version: 1\n---\n"
                f"# {skill_id}\n\n## 适用条件\n- 通用\n"
                "## 不适用条件\n- 无\n"
                f"## 操作步骤\n- {marker}\n")
    purpose_md = ("## 来源\n- 8l\n## 改进目的\n- 集合只读校验\n"
                  "## 演化历史\n- v1\n")
    payload = json.dumps({"skill_id": skill_id, "domain": DOMAIN,
                          "runtime_ref": RUNTIME_REF,
                          "schema_version": "skill-evolution/v1",
                          "skill_md": skill_md, "purpose_md": purpose_md},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    ch = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    db.execute(text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:vid,:sid,:seq,'skill-evolution/v1',:d,:rt,NULL,:sm,:pm,:ch,"
        "'manual_seed','8l-test',CURRENT_TIMESTAMP)"),
        {"vid": version_id, "sid": skill_id, "seq": seq, "d": DOMAIN,
         "rt": RUNTIME_REF, "sm": skill_md, "pm": purpose_md, "ch": ch})
    if mutate_md_after:
        db.execute(text(
            "UPDATE evolution_skill_versions SET "
            "skill_md = skill_md || ' ' WHERE version_id=:v"),
            {"v": version_id})
    db.commit()
    return skill_md


def _version_hash(db, version_id):
    return db.execute(text(
        "SELECT content_hash FROM evolution_skill_versions "
        "WHERE version_id=:v"), {"v": version_id}).scalar()


def _member(db, skill_id, version_id, seq):
    return {"skill_id": skill_id, "version_id": version_id,
            "content_hash": _version_hash(db, version_id), "seq": seq}


def _put_binding(db, *, set_kind="skill", skill_id="default",
                 version_id="default:0001", rev=1, set_hash=None,
                 members_json=None, ws=WS):
    if members_json is not None and not isinstance(members_json, str):
        members_json = json.dumps(members_json, ensure_ascii=False)
    db.execute(text(
        "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
        "domain, set_kind, skill_id, version_id, rev, set_hash, members_json, "
        "created_by, created_at, updated_at) VALUES (:i,'business',:w,:d,:sk,"
        ":sid,:vid,:rev,:sh,:mj,'8l-test',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
        {"i": "bl-" + json.dumps([set_kind, skill_id, version_id, rev],
                                 ensure_ascii=False)[:20].replace('"', ""),
         "w": ws, "d": DOMAIN, "sk": set_kind, "sid": skill_id, "vid": version_id,
         "rev": rev, "sh": set_hash, "mj": members_json})
    db.commit()


def _raise_code(exc_info) -> str:
    return exc_info.value.code


# ---------------------------------------------------------------------------
# 1) 旧单技能（members_json IS NULL）兼容读取
# ---------------------------------------------------------------------------


def test_legacy_single_skill_read_full_set_and_compat(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "单技能指令")
    _put_binding(biz_db, skill_id="default", version_id="default:0001")
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st is not None
    assert st["legacy"] is True            # members_json IS NULL 旧记录
    assert st["kind"] == "skill"
    assert st["rev"] == 1                  # 旧行未物化修订 → 1
    assert st["set_hash"] is None          # 旧行未物化集合哈希
    assert len(st["members"]) == 1
    m = st["members"][0]
    assert m["version_id"] == "default:0001"
    assert m["skill_id"] == "default"
    assert m["seq"] == 1
    assert m["content_hash"] == _version_hash(biz_db, "default:0001")
    assert "单技能指令" in m["skill_md"]  # 返回必要版本内容
    assert m["domain"] == DOMAIN and m["runtime_ref"] == RUNTIME_REF
    assert st["canonical_set_hash"] == bops.binding_set_hash(st["members"])
    # 单技能调用方兼容：原字典字段不变
    old = bops.resolve_business_binding(biz_db, WS)
    assert old == {"version_id": "default:0001", "skill_id": "default",
                   "content_hash": _version_hash(biz_db, "default:0001"),
                   "skill_md": m["skill_md"], "purpose_md": m["purpose_md"]}


# ---------------------------------------------------------------------------
# 2) 空集合与旧单技能明确区分
# ---------------------------------------------------------------------------


def test_empty_legacy_distinct_from_legacy_single(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "单技能指令")
    # 旧空集合行：set_kind='empty'、pair 列 NULL、members_json NULL
    _put_binding(biz_db, set_kind="empty", skill_id=None, version_id=None,
                 rev=1)
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["kind"] == "empty"
    assert st["members"] == []
    assert st["legacy"] is True
    assert st["rev"] == 1
    assert st["set_hash"] is None
    assert st["canonical_set_hash"] == bops.binding_set_hash([])
    # 单技能接口：空集合 → None（与单技能 dict 明确不同，不混淆）
    assert bops.resolve_business_binding(biz_db, WS) is None


def test_empty_new_format_read(biz_db):
    # 新格式空集合：members_json='[]' + set_hash(空) + rev
    _put_binding(biz_db, set_kind="empty", skill_id=None, version_id=None,
                 rev=5, set_hash=bops.binding_set_hash([]), members_json=[])
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["kind"] == "empty"
    assert st["members"] == []
    assert st["legacy"] is False            # 物化新格式，非旧行
    assert st["rev"] == 5
    assert st["set_hash"] == bops.binding_set_hash([])
    assert st["canonical_set_hash"] == st["set_hash"]
    assert bops.resolve_business_binding(biz_db, WS) is None


# ---------------------------------------------------------------------------
# 3) 双技能完整集合：顺序 + 成员内容 + rev + 集合哈希
# ---------------------------------------------------------------------------


def test_two_skill_set_full_read_preserves_order(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck")
    # 规范顺序 = (skill_id, seq) 升序 → default 在前、refcheck 在后
    members = [_member(biz_db, "default", "default:0001", 1),
               _member(biz_db, "refcheck", "refcheck:0001", 1)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=3, set_hash=bops.binding_set_hash(members),
                 members_json=members)
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["kind"] == "skill"
    assert st["legacy"] is False
    assert st["rev"] == 3
    assert [m["version_id"] for m in st["members"]] == [
        "default:0001", "refcheck:0001"]
    assert st["set_hash"] == st["canonical_set_hash"]
    assert st["canonical_set_hash"] == bops.binding_set_hash(st["members"])
    assert "指令-default" in st["members"][0]["skill_md"]
    assert "指令-refcheck" in st["members"][1]["skill_md"]
    assert st["members"][1]["content_hash"] == _version_hash(
        biz_db, "refcheck:0001")


def test_new_format_single_member_readable_by_legacy_caller(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    members = [_member(biz_db, "default", "default:0001", 1)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash(members),
                 members_json=members)
    old = bops.resolve_business_binding(biz_db, WS)  # 兼容读取，不报错
    assert old["version_id"] == "default:0001"
    assert "指令-default" in old["skill_md"]


# ---------------------------------------------------------------------------
# 4) 损坏 / 缺失 / 重复 / 顺序 / 集合哈希 → 明确失败
# ---------------------------------------------------------------------------


def test_second_member_corrupt_fails_loud(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    # refcheck 版本内容事后被篡改（skill_md 变、content_hash 未更新）
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck",
                 mutate_md_after=True)
    members = [_member(biz_db, "default", "default:0001", 1),
               _member(biz_db, "refcheck", "refcheck:0001", 1)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash(members),
                 members_json=members)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei) == "binding_version_corrupt"
    # 单技能兼容接口同样 fail loud，绝不只返回第一个健康成员
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.resolve_business_binding(biz_db, WS)
    assert _raise_code(ei2) == "binding_version_corrupt"


def test_member_version_missing_fails_loud(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck")
    members = [_member(biz_db, "default", "default:0001", 1),
               _member(biz_db, "refcheck", "refcheck:0001", 1)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash(members),
                 members_json=members)
    biz_db.execute(text(
        "DELETE FROM evolution_skill_versions WHERE version_id='refcheck:0001'"))
    biz_db.commit()
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei) == "binding_version_missing"
    assert "refcheck:0001" in ei.value.message


def test_duplicate_member_rejected(biz_db):
    # (a) 同一 skill_id 两个不同版本 → 每技能最多一个成员
    _add_version(biz_db, "default", "default:0001", 1, "v1")
    _add_version(biz_db, "default", "default:0002", 2, "v2")
    dup_skill = [_member(biz_db, "default", "default:0001", 1),
                 _member(biz_db, "default", "default:0002", 2)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash(dup_skill),
                 members_json=dup_skill)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei) == "binding_member_duplicate"
    # (b) 同一 version_id 被两个不同 skill 声明 → 重复版本
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "v1")
    biz_db.execute(text(
        "DELETE FROM evolution_skill_bindings WHERE kind='business' "
        "AND workspace_id=:w AND domain=:d"), {"w": WS, "d": DOMAIN})
    biz_db.commit()
    dup_version = [{"skill_id": "default", "version_id": "default:0001",
                    "content_hash": _version_hash(biz_db, "default:0001"),
                    "seq": 1},
                   {"skill_id": "refcheck",
                    "version_id": "default:0001",
                    "content_hash": _version_hash(biz_db, "default:0001"),
                    "seq": 1}]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=3, set_hash=bops.binding_set_hash(dup_version),
                 members_json=dup_version, ws=WS)
    with pytest.raises(bops.BusinessOpsError) as ei2:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei2) == "binding_member_duplicate"


def test_set_hash_mismatch_fails_loud(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck")
    members = [_member(biz_db, "default", "default:0001", 1),
               _member(biz_db, "refcheck", "refcheck:0001", 1)]
    # 存储哈希被人为写错（不等于按完整成员重算的规范哈希）
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash([]),
                 members_json=members)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei) == "binding_set_hash_mismatch"


def test_stored_member_order_must_be_canonical(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck")
    # 存储顺序被反排（refcheck 在 default 前），哈希按该错误顺序重算 → 哈希能
    # 对上，但顺序校验必须独立失败（不静默接受非规范集合）。
    bad_order = [_member(biz_db, "refcheck", "refcheck:0001", 1),
                 _member(biz_db, "default", "default:0001", 1)]
    _put_binding(biz_db, skill_id="refcheck", version_id="refcheck:0001",
                 rev=2, set_hash=bops.binding_set_hash(bad_order),
                 members_json=bad_order)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding_set(biz_db, WS)
    assert _raise_code(ei) == "binding_member_order"


# ---------------------------------------------------------------------------
# 5) 单技能调用方：多成员集合不得静默压成首成员
# ---------------------------------------------------------------------------


def test_multi_member_not_silently_flattened_to_first(biz_db):
    _add_version(biz_db, "default", "default:0001", 1, "指令-default")
    _add_version(biz_db, "refcheck", "refcheck:0001", 1, "指令-refcheck")
    members = [_member(biz_db, "default", "default:0001", 1),
               _member(biz_db, "refcheck", "refcheck:0001", 1)]
    _put_binding(biz_db, skill_id="default", version_id="default:0001",
                 rev=2, set_hash=bops.binding_set_hash(members),
                 members_json=members)
    with pytest.raises(bops.BusinessOpsError) as ei:
        bops.resolve_business_binding(biz_db, WS)
    assert _raise_code(ei) == "binding_set_multi_member"
    assert "不得静默取首成员" in ei.value.message


# ---------------------------------------------------------------------------
# 6) P51（未迁移 P52 列）生产库兼容：仍按旧行读取，不因缺列报错
# ---------------------------------------------------------------------------


def test_p51_without_p52_columns_reads_legacy(biz_db):
    # 模拟尚未执行 P52 迁移的库：去掉三个集合列
    for col in ("members_json", "set_hash", "rev"):
        biz_db.execute(text(
            f"ALTER TABLE evolution_skill_bindings DROP COLUMN {col}"))
    biz_db.commit()
    _add_version(biz_db, "default", "default:0001", 1, "P51 指令")
    db = biz_db
    db.execute(text(
        "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
        "domain, set_kind, skill_id, version_id, created_by, created_at, "
        "updated_at) VALUES ('bl-p51','business',:w,:d,'skill','default',"
        "'default:0001','8l',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
        {"w": WS, "d": DOMAIN})
    db.commit()
    st = bops.resolve_business_binding_set(biz_db, WS)
    assert st["legacy"] is True and st["kind"] == "skill"
    assert st["rev"] == 1 and st["set_hash"] is None
    assert st["members"][0]["version_id"] == "default:0001"
    assert st["canonical_set_hash"] == bops.binding_set_hash(st["members"])
    old = bops.resolve_business_binding(biz_db, WS)
    assert old["version_id"] == "default:0001"
    assert "P51 指令" in old["skill_md"]
