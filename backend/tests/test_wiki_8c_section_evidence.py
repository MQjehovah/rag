"""Phase 8C：Section Evidence 追溯 / 编辑者只读诊断 / CompileRun workspace 过滤。

覆盖（契约 §1-§3 + §6 反例的后端相关部分）：
- 交叉 ID（section 不属于 revision / revision 不属于 wiki / wiki 不存在）→ 统一 404；
- Wiki 可见但来源 Page 不可见 → 该 evidence 不返回、total 不含；Wiki 不可见 → 404；
- 两 section 证据不混合；同 evidence 多字段绑定去重但 bindings 保留全部 field_path/usage_type；
- stale/rejected/active+hash不匹配/active+匹配 的 state；evidence 物理缺失不伪造；
- 历史 revision 无 binding → items=[]（不回退整 Page）；admin 可取历史 revision，
  普通读者只能当前 published；
- 只读：查询前后 wiki_sections/section bindings/evidence 行数不变；
- 响应无内部键：source_path/locator_json/ACL/connection/payload/prompt/skill_decision_json；
- content 截断 2000 + content_truncated；locator 白名单；limit 分页 total 授权后计数；
- diagnostics：admin/wiki_editor 可、普通读者 403、其它组 editor 404；skill 受控字段；
  validation summary 派生；
- compile runs：仅 admin；workspace_id 先过滤后分页 total。

全部请求只读：只查询、不 flush/commit（fixture 内 commit 仅用于准备数据）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.main import app
from app.models.database import (
    EvidenceItem,
    KnowledgeCompileRun as CompileRun,
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    WikiWorkspace,
    init_db,
)

_H64 = "a" * 64


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> _dt.datetime:
    return _dt.datetime.now()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'p8c.db').as_posix()}"
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

    db = sessionmaker(bind=engine)()
    c = TestClient(app)
    yield c, db, url
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def _override(user: dict | None):
    """user=None 表示移除覆盖（走真实 get_current_user → 无 token）。"""
    if user is None:
        app.dependency_overrides.pop(jwt_utils.get_current_user, None)
    else:
        app.dependency_overrides[jwt_utils.get_current_user] = lambda: user


def _reader():
    return {"id": "u-reader", "username": "reader", "groups": ["group_a"], "is_admin": False}


def _editor():
    return {"id": "u-editor", "username": "editor", "groups": ["group_a", "editors"], "is_admin": False}


def _other_editor():
    return {"id": "u-oe", "username": "oe", "groups": ["group_b", "editors"], "is_admin": False}


def _admin():
    return {"id": "u-admin", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}


# ---------------------------------------------------------------------------
# 种子 helpers（准备数据专用；commit 只在此发生）
# ---------------------------------------------------------------------------


def _seed_page_scope(db):
    db.add(Notebook(id="nb-a", name="工程知识库", group_id="group_a"))
    db.add(Notebook(id="nb-b", name="销售知识库", group_id="group_b"))
    db.flush()
    db.add(Page(id="p-vis", notebook_id="nb-a", title="水箱安装指导"))
    db.add(Page(id="p-hidden", notebook_id="nb-b", title="销售报价"))
    db.flush()


def _seed_published_wiki(db, *, wid="wp1", rid="rev1", section_ids=("sec1",), current="rev1"):
    """创建已发布 wiki + revision；返回 wp。"""
    wp = WikiPage(
        id=wid, title="主题", acl_scope='{"groups": ["group_a"]}',
        status="published", dirty=False,
    )
    db.add(wp)
    db.flush()
    db.add(WikiRevision(id=rid, wiki_page_id=wid, title="主题", status="published", edit_type="auto"))
    db.flush()
    wp.current_revision_id = current
    for i, sid in enumerate(section_ids):
        db.add(WikiSection(
            id=sid, revision_id=rid, section_type="evidence",
            heading=f"章节{i}", order_index=i,
        ))
    db.flush()
    return wp


def _add_revision(db, *, wid, rid, status="draft"):
    db.add(WikiRevision(id=rid, wiki_page_id=wid, title="主题", status=status, edit_type="auto"))
    db.flush()
    return rid


def _add_section(db, *, sid, rid, heading="章节", section_type="evidence", validation_status=None, order=0):
    db.add(WikiSection(
        id=sid, revision_id=rid, section_type=section_type, heading=heading,
        order_index=order, validation_status=validation_status,
    ))
    db.flush()
    return sid


def _add_evidence(db, *, eid, page_id, content="证据正文", content_hash=None, status="active",
                  locator_json=None, evidence_type="text"):
    if content_hash is None:
        content_hash = _hash(content or "内容")
    db.add(EvidenceItem(
        id=eid, source_page_id=page_id, source_doc_hash=_H64, content=content,
        content_hash=content_hash, locator_json=locator_json,
        status=status, evidence_type=evidence_type,
    ))
    db.flush()
    return eid


def _add_binding(db, *, bid, section_id, evidence_id, field_path, usage_type="support",
                 evidence_content_hash=None, created_at=None):
    if evidence_content_hash is None:
        evidence_content_hash = _H64
    db.add(WikiSectionEvidenceBinding(
        id=bid, section_id=section_id, evidence_id=evidence_id,
        field_path=field_path, usage_type=usage_type,
        evidence_content_hash=evidence_content_hash,
        created_at=created_at or _now(),
    ))
    db.flush()
    return bid


# ---------------------------------------------------------------------------
# §6 交叉 ID → 404
# ---------------------------------------------------------------------------

def test_cross_id_404s(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    # 另一 wiki 的 revision（path wiki=wp1 但 revision 属于 wpB）
    db.add(WikiPage(id="wpB", title="其它", acl_scope='{"groups": ["group_a"]}', status="published"))
    db.flush()
    db.add(WikiRevision(id="revB", wiki_page_id="wpB", title="其它", status="published"))
    db.flush()
    # 历史 revision（不属于当前 revision 但属于同 wiki）
    db.add(WikiRevision(id="rev2", wiki_page_id="wp1", title="主题", status="draft"))
    db.flush()
    # wpB 也有一个 sec_foreign（属于 revB）
    db.add(WikiSection(id="secB", revision_id="revB", section_type="x", heading="b"))
    db.flush()
    db.commit()

    _override(_reader())
    # wiki 不存在
    assert c.get("/api/wiki/wp-missing/revisions/rev1/sections/sec1/evidence").status_code == 404
    # revision 不属于该 wiki
    assert c.get("/api/wiki/wp1/revisions/revB/sections/secB/evidence").status_code == 404
    # revision 不存在
    assert c.get("/api/wiki/wp1/revisions/rev-nope/sections/sec1/evidence").status_code == 404
    # section 不属于该 revision（secB 属于 revB，不是 rev1）
    assert c.get("/api/wiki/wp1/revisions/rev1/sections/secB/evidence").status_code == 404
    # section 不存在
    assert c.get("/api/wiki/wp1/revisions/rev1/sections/sec-missing/evidence").status_code == 404
    # section 属于该 wiki 其它 revision（rev2），请求 current rev1 → 404
    sec2 = WikiSection(id="sec2", revision_id="rev2", section_type="x", heading="h2")
    db.add(sec2)
    db.commit()
    assert c.get("/api/wiki/wp1/revisions/rev1/sections/sec2/evidence").status_code == 404


# ---------------------------------------------------------------------------
# 来源 Page 可见性 / Wiki 可见性
# ---------------------------------------------------------------------------

def test_evidence_source_page_visibility_and_wiki_invisible(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    # 可见来源 p-vis + 不可见来源 p-hidden
    _add_evidence(db, eid="e-vis", page_id="p-vis", content="可见正文", content_hash=_hash("可见正文"))
    _add_evidence(db, eid="e-hid", page_id="p-hidden", content="隐藏正文", content_hash=_hash("隐藏正文"))
    _add_binding(db, bid="b1", section_id="sec1", evidence_id="e-vis", field_path="a",
                 evidence_content_hash=_hash("可见正文"))
    _add_binding(db, bid="b2", section_id="sec1", evidence_id="e-hid", field_path="a",
                 evidence_content_hash=_hash("隐藏正文"))
    db.commit()

    path = "/api/wiki/wp1/revisions/rev1/sections/sec1/evidence"
    _override(_reader())
    r = c.get(path)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert [i["evidence_id"] for i in body["items"]] == ["e-vis"]
    assert body["items"][0]["source_display_name"] == "水箱安装指导"

    # 另一组用户（group_b）看不到该 wiki（ACL group_a）→ 404
    _override({"id": "u-b", "username": "b", "groups": ["group_b"], "is_admin": False})
    assert c.get(path).status_code == 404

    # admin 可见全部来源（get_visible_page_ids 含 p-hidden）
    _override(_admin())
    body = c.get(path).json()
    assert body["total"] == 2
    assert {i["evidence_id"] for i in body["items"]} == {"e-vis", "e-hid"}


# ---------------------------------------------------------------------------
# 两 section 不混合 / 同 evidence 多绑定去重聚合
# ---------------------------------------------------------------------------

def test_sections_not_mixed_and_bindings_aggregated(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1", "sec2"))
    _add_evidence(db, eid="e1", page_id="p-vis", content="聚合正文", content_hash=_hash("聚合正文"))
    _add_evidence(db, eid="e2", page_id="p-vis", content="第二章节正文", content_hash=_hash("第二章节正文"))
    t0 = _now()
    # sec1 上 e1 两条绑定（不同 field_path），sec2 上 e2 一条
    _add_binding(db, bid="b1", section_id="sec1", evidence_id="e1", field_path="responses.200",
                 usage_type="support", evidence_content_hash=_hash("聚合正文"), created_at=t0)
    _add_binding(db, bid="b2", section_id="sec1", evidence_id="e1", field_path="responses.404",
                 usage_type="conflict", evidence_content_hash=_hash("聚合正文"),
                 created_at=t0 + _dt.timedelta(seconds=1))
    _add_binding(db, bid="b3", section_id="sec2", evidence_id="e2", field_path="topics",
                 usage_type="support", evidence_content_hash=_hash("第二章节正文"))
    db.commit()

    _override(_reader())
    sec1 = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence").json()
    sec2 = c.get("/api/wiki/wp1/revisions/rev1/sections/sec2/evidence").json()

    # 两 section 证据不混合
    assert sec1["total"] == 1 and [i["evidence_id"] for i in sec1["items"]] == ["e1"]
    assert sec2["total"] == 1 and [i["evidence_id"] for i in sec2["items"]] == ["e2"]

    # 同 evidence 去重为单 item；bindings 保留全部 field_path/usage_type 且稳定排序
    item = sec1["items"][0]
    assert item["content"] == "聚合正文"
    assert item["bindings"] == [
        {"field_path": "responses.200", "usage_type": "support"},
        {"field_path": "responses.404", "usage_type": "conflict"},
    ]


# ---------------------------------------------------------------------------
# state / hash_matches / evidence 物理缺失
# ---------------------------------------------------------------------------

def test_evidence_states_and_missing_evidence(client):
    c, db, url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    cur_h = _hash("当前正文")
    # active + 匹配
    _add_evidence(db, eid="e-cur", page_id="p-vis", content="当前正文", content_hash=cur_h)
    _add_binding(db, bid="bc", section_id="sec1", evidence_id="e-cur", field_path="x",
                 evidence_content_hash=cur_h)
    # active + 不匹配
    _add_evidence(db, eid="e-chg", page_id="p-vis", content="当前正文", content_hash=cur_h)
    _add_binding(db, bid="bd", section_id="sec1", evidence_id="e-chg", field_path="x",
                 evidence_content_hash="b" * 64)
    # stale（即便 hash 匹配也是 stale）
    _add_evidence(db, eid="e-sta", page_id="p-vis", content="旧正文", content_hash=_hash("旧正文"),
                  status="stale")
    _add_binding(db, bid="be", section_id="sec1", evidence_id="e-sta", field_path="x",
                 evidence_content_hash=_hash("旧正文"))
    # rejected
    _add_evidence(db, eid="e-rej", page_id="p-vis", content="被拒正文", content_hash=_hash("被拒正文"),
                  status="rejected")
    _add_binding(db, bid="bf", section_id="sec1", evidence_id="e-rej", field_path="x",
                 evidence_content_hash=_hash("被拒正文"))
    db.commit()

    # 无 evidence 的悬挂 binding（FK off 注入模拟 legacy/异常数据）
    _inject_dangling_binding(db, url, section_id="sec1", bid="b-dang")

    _override(_reader())
    body = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence").json()
    states = {i["evidence_id"]: i for i in body["items"]}
    assert body["total"] == 4  # 悬挂 binding 不计
    assert "e-missing-ev" not in {i["evidence_id"] for i in body["items"]}
    assert states["e-cur"]["state"] == "active_current"
    assert states["e-cur"]["hash_matches"] is True
    assert states["e-chg"]["state"] == "changed"
    assert states["e-chg"]["hash_matches"] is False
    assert states["e-sta"]["state"] == "stale"
    assert states["e-rej"]["state"] == "rejected"
    # 悬挂 binding 不伪造记录（无该 evidence_id）
    assert body["total"] == 4


def _inject_dangling_binding(db, url, *, section_id, bid):
    """在 FK 关闭的独立连接里写入一条指向不存在 Evidence 的 binding。"""
    engine = create_engine(url, connect_args={"check_same_thread": False})
    s = sessionmaker(bind=engine)()
    try:
        s.add(WikiSectionEvidenceBinding(
            id=bid, section_id=section_id, evidence_id="e-missing-ev",
            field_path="x", usage_type="support", evidence_content_hash="c" * 64,
        ))
        s.commit()
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 历史 revision
# ---------------------------------------------------------------------------

def test_historical_revision_empty_and_admin_access(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    _add_evidence(db, eid="e1", page_id="p-vis", content="正文", content_hash=_hash("正文"))
    _add_binding(db, bid="b1", section_id="sec1", evidence_id="e1", field_path="x",
                 evidence_content_hash=_hash("正文"))
    # 历史 revision（无 binding）+ 其 section
    _add_revision(db, wid="wp1", rid="rev2", status="superseded")
    _add_section(db, sid="sec2", rid="rev2", heading="历史", section_type="evidence")
    db.commit()

    path2 = "/api/wiki/wp1/revisions/rev2/sections/sec2/evidence"
    # 普通读者：历史 revision 非 current → 404
    _override(_reader())
    assert c.get(path2).status_code == 404
    # admin：可取历史 revision；该 section 无 binding → items=[]（不回退整 Page 证据）
    _override(_admin())
    body = c.get(path2).json()
    assert body["total"] == 0 and body["items"] == []
    # 同一 wiki 当前 revision 的证据不泄漏进历史 section
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# 分页 / 截断 / locator 白名单 / 无内部键
# ---------------------------------------------------------------------------

def test_pagination_truncation_locator_and_no_internal_keys(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    long_text = "长" * 2500
    _add_evidence(db, eid="e-long", page_id="p-vis", content=long_text,
                  content_hash=_hash("长"), locator_json=json.dumps({
                      "page_number": 3, "heading": "安装", "image_id": "img-1",
                      "content_type": "text", "chunk_id": "chunk-x", "bbox": [1, 2],
                  }))
    _add_binding(db, bid="b-long", section_id="sec1", evidence_id="e-long", field_path="a",
                 evidence_content_hash=_hash("长"))
    # 非法 locator JSON → 全 null
    _add_evidence(db, eid="e-badloc", page_id="p-vis", content="坏定位",
                  content_hash=_hash("坏定位"), locator_json="{not-json")
    _add_binding(db, bid="b-badloc", section_id="sec1", evidence_id="e-badloc", field_path="a",
                 evidence_content_hash=_hash("坏定位"))
    # 其余 4 条可见 → 共 6 条授权
    for i in range(1, 5):
        _add_evidence(db, eid=f"e{i}", page_id="p-vis", content=f"正文{i}",
                      content_hash=_hash(f"正文{i}"))
        _add_binding(db, bid=f"b{i}", section_id="sec1", evidence_id=f"e{i}", field_path="a",
                     evidence_content_hash=_hash(f"正文{i}"))
    db.commit()

    _override(_reader())
    # limit 默认 50 / 上限 100
    all_body = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence").json()
    assert all_body["total"] == 6 and len(all_body["items"]) == 6
    assert all_body["limit"] == 50 and all_body["offset"] == 0

    # 截断
    long_item = next(i for i in all_body["items"] if i["evidence_id"] == "e-long")
    assert len(long_item["content"]) == 2000
    assert long_item["content_truncated"] is True

    # locator 白名单（绝不返回 chunk_id/bbox）
    assert long_item["locator"] == {
        "page_number": 3, "heading": "安装", "image_id": "img-1", "content_type": "text",
    }
    badloc = next(i for i in all_body["items"] if i["evidence_id"] == "e-badloc")
    assert badloc["locator"] == {"page_number": None, "heading": None,
                                 "image_id": None, "content_type": None}

    # limit=2 分页：total 恒定（授权后），offset 有效
    p1 = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence", params={"limit": 2, "offset": 0}).json()
    p2 = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence", params={"limit": 2, "offset": 2}).json()
    p3 = c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence", params={"limit": 2, "offset": 6}).json()
    assert p1["total"] == p2["total"] == p3["total"] == 6
    assert len(p1["items"]) == 2 and len(p2["items"]) == 2 and p3["items"] == []
    # limit 越界
    assert c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence",
                 params={"limit": 101}).status_code == 422

    # 响应无内部键
    raw = json.dumps(all_body, ensure_ascii=False).lower()
    for token in ("source_path", "locator_json", "chunk_id", "bbox", "acl", "connection",
                  "payload", "prompt", "source_page_id"):
        assert token not in raw


# ---------------------------------------------------------------------------
# 只读（行数不变）
# ---------------------------------------------------------------------------

def test_readonly_no_writes(client):
    c, db, _url = client
    _seed_page_scope(db)
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    _add_evidence(db, eid="e1", page_id="p-vis", content="正文", content_hash=_hash("正文"))
    _add_binding(db, bid="b1", section_id="sec1", evidence_id="e1", field_path="x",
                 evidence_content_hash=_hash("正文"))
    db.commit()

    def snapshot():
        db.expire_all()
        return (
            db.query(WikiSection).count(),
            db.query(WikiSectionEvidenceBinding).count(),
            db.query(EvidenceItem).count(),
        )

    before = snapshot()
    _override(_reader())
    assert c.get("/api/wiki/wp1/revisions/rev1/sections/sec1/evidence").status_code == 200
    assert snapshot() == before


# ---------------------------------------------------------------------------
# diagnostics：权限
# ---------------------------------------------------------------------------

def test_diagnostics_permissions(client):
    c, db, _url = client
    _seed_published_wiki(db, wid="wp1", rid="rev1", section_ids=("sec1",))
    db.commit()

    path = "/api/wiki/wp1/diagnostics"
    # wiki_editor → 200
    _override(_editor())
    r = c.get(path)
    assert r.status_code == 200
    assert r.json()["editable"] is True
    assert r.json()["is_current_wiki_config"] is True
    # 普通读者 → 403
    _override(_reader())
    assert c.get(path).status_code == 403
    # 其它组 editor（不可见 wiki）→ 404
    _override(_other_editor())
    assert c.get(path).status_code == 404
    # admin → 200
    _override(_admin())
    assert c.get(path).status_code == 200
    # wiki 不存在 → 404
    assert c.get("/api/wiki/wp-missing/diagnostics").status_code == 404


# ---------------------------------------------------------------------------
# diagnostics：skill 受控字段
# ---------------------------------------------------------------------------

def test_diagnostics_skill_fields_and_reason_code(client):
    c, db, _url = client
    from app.core.wiki_skills import registry as skill_registry
    from app.core.wiki_skills.schemas import SkillDescriptor

    decision = {
        "schema_version": "skill-decision/v1",
        "target_key": "wp-dx",
        "selected_skill": "api_reference",
        "selected_version": "1",
        "selected_by": "auto",
        "confidence": 0.97,
        "status": "selected",
        "reason_code": "DETERMINISTIC_HIGH_CONFIDENCE",
    }
    wp = WikiPage(
        id="wp-dx", title="API 参考", acl_scope='{"groups": ["group_a"]}',
        status="published", dirty=False,
        content_skill="api_reference", skill_version="1", skill_selected_by="auto",
        skill_locked=False, skill_confidence=0.97,
        skill_decision_json=json.dumps(decision, ensure_ascii=False),
    )
    db.add(wp)
    db.flush()
    db.add(WikiRevision(id="dx-rev", wiki_page_id="wp-dx", title="API", status="published"))
    db.flush()
    wp.current_revision_id = "dx-rev"
    db.add(WikiSection(id="dx-sec", revision_id="dx-rev", section_type="summary",
                       heading="概览", validation_status="pass"))
    db.commit()

    # 注册临时 descriptor 供 display_name 解析（测试后清理）
    skill_registry.clear_for_tests()
    try:
        skill_registry.register_descriptor(SkillDescriptor(
            key="api_reference", version="1", label="API Reference",
        ))
        _override(_editor())
        body = c.get("/api/wiki/wp-dx/diagnostics").json()
        skill = body["skill"]
        assert skill["key"] == "api_reference"
        assert skill["display_name"] == "API Reference"
        assert skill["version"] == "1"
        assert skill["selection"] == "auto"
        assert "selected_by" in skill
        assert skill["locked"] is False
        assert skill["reason_code"] == "DETERMINISTIC_HIGH_CONFIDENCE"

        # 未知 reason_code → "unknown"（不回放原始字符串）
        wp.skill_decision_json = json.dumps({**decision, "reason_code": "内部机密XYZ"})
        db.commit()
        body = c.get("/api/wiki/wp-dx/diagnostics").json()
        assert body["skill"]["reason_code"] == "unknown"
    finally:
        skill_registry.clear_for_tests()


def test_diagnostics_skill_no_data_and_manual_locked(client):
    c, db, _url = client
    wp = WikiPage(
        id="wp-dx2", title="无字段", acl_scope='{"groups": ["group_a"]}',
        status="published", dirty=False,
        content_skill="default", skill_version="1", skill_selected_by="manual",
        skill_locked=True,
    )
    db.add(wp)
    db.flush()
    db.add(WikiRevision(id="dx2-rev", wiki_page_id="wp-dx2", title="无", status="published"))
    db.flush()
    wp.current_revision_id = "dx2-rev"
    db.add(WikiSection(id="dx2-sec", revision_id="dx2-rev", section_type="summary",
                       heading="", validation_status=None))
    db.commit()

    _override(_editor())
    body = c.get("/api/wiki/wp-dx2/diagnostics").json()
    assert body["skill"]["key"] == "default"
    assert body["skill"]["display_name"] is None  # 未注册 → null
    assert body["skill"]["selection"] == "manual"
    assert body["skill"]["locked"] is True
    assert body["skill"]["reason_code"] == "unknown"


# ---------------------------------------------------------------------------
# diagnostics：validation summary 派生
# ---------------------------------------------------------------------------

def test_diagnostics_validation_summary(client):
    c, db, _url = client

    def _wiki(wid, rid, statuses):
        wp = WikiPage(id=wid, title=wid, acl_scope='{"groups": ["group_a"]}',
                      status="published", dirty=False)
        db.add(wp)
        db.flush()
        db.add(WikiRevision(id=rid, wiki_page_id=wid, title=wid, status="published"))
        db.flush()
        wp.current_revision_id = rid
        for i, vs in enumerate(statuses):
            _add_section(db, sid=f"{wid}-s{i}", rid=rid, heading=f"H{i}" if i else "",
                         section_type="summary", validation_status=vs, order=i)
        db.flush()
        return wp

    _wiki("wp-ok", "ok-rev", ["pass", "pass"])
    _wiki("wp-unk", "unk-rev", ["pass", None])
    _wiki("wp-fail", "fail-rev", ["pass", "fail"])
    _wiki("wp-mix", "mix-rev", ["fail", None])
    db.commit()

    _override(_admin())
    assert c.get("/api/wiki/wp-ok/diagnostics").json()["validation"]["summary"] == "pass"
    assert c.get("/api/wiki/wp-unk/diagnostics").json()["validation"]["summary"] == "unknown"
    assert c.get("/api/wiki/wp-fail/diagnostics").json()["validation"]["summary"] == "fail"
    assert c.get("/api/wiki/wp-mix/diagnostics").json()["validation"]["summary"] == "fail"
    unk = c.get("/api/wiki/wp-unk/diagnostics").json()["validation"]["sections"]
    assert unk[1] == {"heading": "H1", "validation_status": "unknown"}
    ok = c.get("/api/wiki/wp-ok/diagnostics").json()["validation"]["sections"]
    # heading 空 → 回退 section_type
    assert ok[0]["heading"] == "summary"


def test_diagnostics_no_internal_keys(client):
    c, db, _url = client
    wp = WikiPage(id="wp-sec", title="安全", acl_scope='{"groups": ["group_a"]}',
                  status="published", dirty=False,
                  content_skill="default", skill_selected_by="auto",
                  skill_decision_json='{"selected_by":"auto","reason_code":"SKILL_STICKY_CURRENT"}')
    db.add(wp)
    db.flush()
    db.add(WikiRevision(id="sec-rev", wiki_page_id="wp-sec", title="安全", status="published"))
    db.flush()
    wp.current_revision_id = "sec-rev"
    db.add(WikiSection(id="sec-sec", revision_id="sec-rev", section_type="summary",
                       heading="h", validation_status="pass"))
    db.commit()

    _override(_editor())
    r = c.get("/api/wiki/wp-sec/diagnostics")
    raw = json.dumps(r.json(), ensure_ascii=False).lower()
    for token in ("skill_decision_json", "prompt", "candidate", "acl", "payload",
                  "source_path", "connection"):
        assert token not in raw


# ---------------------------------------------------------------------------
# compile list：workspace_id 过滤 + 分页（仅 admin）
# ---------------------------------------------------------------------------

def _seed_workspace(db, ws_id):
    db.add(WikiWorkspace(id=ws_id, key=f"key_{ws_id}", name=ws_id,
                         acl_scope='{"groups": ["__local_admin__"]}',
                         scope_id="admin", status="active"))
    db.flush()


def _seed_compile_runs(db):
    _seed_workspace(db, "ws-a")
    _seed_workspace(db, "ws-b")
    base = _now()
    runs = [
        CompileRun(id="cr-a1", pipeline_key="wiki.default", pipeline_version="3",
                   trigger_type="manual_rebuild", workspace_id="ws-a", input_hash=_H64,
                   status="succeeded", attempt=1, max_attempts=3,
                   created_at=base - _dt.timedelta(seconds=20)),
        CompileRun(id="cr-a2", pipeline_key="wiki.default", pipeline_version="3",
                   trigger_type="manual_rebuild", workspace_id="ws-a", input_hash=_H64,
                   status="queued", attempt=0, max_attempts=3,
                   created_at=base - _dt.timedelta(seconds=10)),
        CompileRun(id="cr-b1", pipeline_key="wiki.default", pipeline_version="3",
                   trigger_type="manual_rebuild", workspace_id="ws-b", input_hash=_H64,
                   status="succeeded", attempt=1, max_attempts=3,
                   created_at=base - _dt.timedelta(seconds=5)),
        CompileRun(id="cr-null", pipeline_key="wiki.default", pipeline_version="3",
                   trigger_type="manual_rebuild", workspace_id=None, input_hash=_H64,
                   status="succeeded", attempt=1, max_attempts=3,
                   created_at=base),
    ]
    db.add_all(runs)
    db.flush()
    db.commit()
    return runs


def test_compile_runs_admin_only_and_workspace_filter(client):
    c, db, _url = client
    _seed_compile_runs(db)

    # 非 admin（含 wiki_editor）→ 403
    _override(_editor())
    assert c.get("/api/wiki-compile/runs").status_code == 403
    assert c.get("/api/wiki-compile/runs", params={"workspace_id": "ws-a"}).status_code == 403
    _override(_reader())
    assert c.get("/api/wiki-compile/runs").status_code == 403

    _override(_admin())
    before = db.query(CompileRun).count()
    # 全量
    all_body = c.get("/api/wiki-compile/runs").json()
    assert all_body["total"] == 4
    # workspace 过滤：先过滤再 count/分页
    a_body = c.get("/api/wiki-compile/runs", params={"workspace_id": "ws-a"}).json()
    assert a_body["total"] == 2
    assert {r["id"] for r in a_body["runs"]} == {"cr-a1", "cr-a2"}
    assert all(r["workspace_id"] == "ws-a" for r in a_body["runs"])

    # 组合过滤（workspace + status）
    comb = c.get("/api/wiki-compile/runs",
                 params={"workspace_id": "ws-a", "status": "queued"}).json()
    assert comb["total"] == 1 and comb["runs"][0]["id"] == "cr-a2"

    # 分页 total 基于过滤后
    page = c.get("/api/wiki-compile/runs",
                 params={"workspace_id": "ws-a", "limit": 1, "offset": 0}).json()
    assert page["total"] == 2 and len(page["runs"]) == 1
    assert page["runs"][0]["id"] == "cr-a2"  # created_at desc
    b_body = c.get("/api/wiki-compile/runs", params={"workspace_id": "ws-b"}).json()
    assert b_body["total"] == 1 and b_body["runs"][0]["id"] == "cr-b1"
    # workspace_id 为 None 的 run 只出现在无过滤列表里
    assert "cr-null" not in {r["id"] for r in a_body["runs"] + b_body["runs"]}
    # 只读：行数不变
    assert db.query(CompileRun).count() == before


def test_compile_runs_unknown_workspace_empty(client):
    c, db, _url = client
    _seed_compile_runs(db)
    _override(_admin())
    body = c.get("/api/wiki-compile/runs", params={"workspace_id": "ws-zzz"}).json()
    assert body["total"] == 0 and body["runs"] == []
