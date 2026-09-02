"""V4 Phase J-4：回答反馈、知识缺口与访问申请反例测试（最终数据一致性封板）。"""
from __future__ import annotations

import inspect
import json
import pathlib
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.feedback_service import (
    CLASS_DEGRADED,
    CLASS_GLOBALLY_MISSING,
    CLASS_HIDDEN_SUFFICIENT,
    CLASS_VISIBLE_SUFFICIENT,
    REASON_INCORRECT,
    REASON_INCOMPLETE,
    ClassificationResult,
    classify_full_library,
    create_answer_snapshot,
    record_access_request,
    record_answer_needed,
    revalidate_access_requests_for_notebook_change,
    revalidate_global_debts_for_change,
    save_answer_feedback,
)
from app.models.database import (
    AccessRequest,
    AnswerFeedback,
    AnswerNeeded,
    AnswerSnapshot,
    FeedbackCluster,
    FeedbackClusterUser,
    KnowledgeDebt,
    Notebook,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
    check_managed_migrations,
    init_db,
)

FRONTEND_ROOT = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _user(groups, uid="u1"):
    return {"id": uid, "username": uid, "groups": groups}


def _snap(db, *, user_id="u1", query="水箱容量", answer_eligible=False, retrieval_completed=True,
          service_degraded=False, response_mode=None, groups=None):
    if groups is None:
        groups = ["engineering"]
    current_user = {"id": user_id, "username": user_id, "groups": groups}
    snap = create_answer_snapshot(
        db,
        answer_id=str(uuid.uuid4()),
        user_id=user_id,
        original_query=query,
        response={
            "response_mode": response_mode or ("answer" if answer_eligible else "insufficient"),
            "retrieval_completed": retrieval_completed,
            "answer_eligible": answer_eligible,
            "service_degraded": service_degraded,
        },
        current_user=current_user,
    )
    db.commit()
    return snap


def _seed_page(db, page_id, title, content, group="engineering"):
    nb = Notebook(id=f"nb-{page_id}", name=f"nb-{page_id}", group_id=group)
    db.add(nb); db.flush()
    page = Page(id=page_id, notebook_id=nb.id, title=title, content=content)
    db.add(page); db.flush()
    db.add(PageChunk(id=f"{page_id}-c0", page_id=page_id, chunk_index=0, content=content, content_type="text"))
    db.flush()
    return page


def _seed_wiki(db, wiki_id, title, acl_groups, content=None, source_page_ids=None):
    page = WikiPage(id=wiki_id, title=title, summary="", acl_scope=json.dumps({"groups": acl_groups}), status="published")
    if source_page_ids:
        page.source_page_ids = json.dumps(source_page_ids)
    db.add(page); db.flush()
    rev = WikiRevision(id=f"{wiki_id}-rev", wiki_page_id=wiki_id, title=title, summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id=f"{wiki_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content or title, order_index=1))
    page.current_revision_id = rev.id
    db.flush()
    return page


# ---------------------------------------------------------------------------
# 零 Card/KO import
# ---------------------------------------------------------------------------

def test_no_card_or_ko_import():
    import app.core.retrieval.feedback_service as fs
    import app.api.feedback_v4 as fv
    for mod in (fs, fv):
        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in ("KnowledgeCard", "KnowledgeClaim", "KnowledgeObject", "KnowledgeCardSource"):
            assert forbidden not in joined, f"{mod.__name__} 不应 import {forbidden}"


# ---------------------------------------------------------------------------
# 1. Chat 零结果但未点击不建债
# ---------------------------------------------------------------------------

def test_chat_zero_result_no_auto_debt(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps, rag_chat
    from app.core import jwt_utils
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiRetrievalResult
    from app.core.retrieval.raw_retriever import RawRetrievalResult

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    outcome = OrchestrationOutcome(
        mode="need_community_expansion",
        wiki_results=WikiRetrievalResult(),
        raw_results=RawRetrievalResult(),
    )

    class _FakeOrchestrator:
        async def retrieve_async(self, query, user):
            return outcome

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", lambda db: _FakeOrchestrator())

    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}
    client = TestClient(app)
    try:
        for groups in (["engineering"], ["engineering", "sales"], [], ["__local_admin__"]):
            app.dependency_overrides[jwt_utils.get_current_user] = lambda g=groups: {"id": "u1", "username": "u", "groups": g, "is_admin": "__local_admin__" in g}
            r = client.post("/api/chat", json={"query": "水箱容量"})
            assert r.status_code == 200
            assert r.json()["response_mode"] == "insufficient"
        s = sessionmaker(bind=engine)()
        assert s.query(KnowledgeDebt).count() == 0
        assert s.query(AccessRequest).count() == 0
        assert s.query(AnswerNeeded).count() == 0
        s.close()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. 快照失败不返回虚假 answer_id
# ---------------------------------------------------------------------------

def test_snapshot_failure_no_fake_answer_id(db, monkeypatch):
    from app.api import rag_chat
    from app.core.retrieval.degradation import build_rag_response
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiHit, WikiRetrievalResult

    def _boom(*a, **k):
        raise RuntimeError("snapshot boom")

    monkeypatch.setattr(rag_chat, "create_answer_snapshot", _boom)

    hit = WikiHit(wiki_page_id="w1", title="t", summary="", content="c", score=0.9, acl_scope="{}")
    outcome = OrchestrationOutcome(mode="wiki_hit", wiki_results=WikiRetrievalResult(hits=[hit]))
    resp = build_rag_response(db, outcome, llm_answer="a", llm_called=True, llm_ok=True)
    final = rag_chat._finalize_chat_response(db, {"id": "u1", "groups": ["engineering"]}, "水箱容量", resp)
    assert "answer_id" not in final
    assert db.query(AnswerSnapshot).count() == 0


def test_answer_id_persisted_and_unpredictable(db):
    from app.api.rag_chat import _finalize_chat_response
    from app.core.retrieval.degradation import build_rag_response
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiHit, WikiRetrievalResult

    hit = WikiHit(wiki_page_id="w1", title="t", summary="", content="c", score=0.9, acl_scope="{}")
    outcome = OrchestrationOutcome(mode="wiki_hit", wiki_results=WikiRetrievalResult(hits=[hit]))
    ids = set()
    for _ in range(3):
        resp = build_rag_response(db, outcome, llm_answer="回答", llm_called=True, llm_ok=True)
        final = _finalize_chat_response(db, {"id": "u1", "groups": ["engineering"]}, "水箱容量", resp)
        assert final["answer_id"]
        assert final["answer_id"] not in ids
        ids.add(final["answer_id"])
        snap = db.get(AnswerSnapshot, final["answer_id"])
        assert snap is not None
        assert snap.user_id == "u1"


# ---------------------------------------------------------------------------
# 3. insufficient/degraded 快照不能伪造差评
# ---------------------------------------------------------------------------

def test_insufficient_snapshot_cannot_feedback(db):
    snap = _snap(db, user_id="u1", answer_eligible=False, response_mode="insufficient")
    r = save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    assert r["ok"] is False
    assert db.query(AnswerFeedback).count() == 0
    assert db.query(FeedbackCluster).count() == 0


def test_degraded_snapshot_cannot_feedback(db):
    snap = _snap(db, user_id="u1", answer_eligible=False, service_degraded=True, retrieval_completed=False, response_mode="insufficient")
    r = save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# 反馈基本
# ---------------------------------------------------------------------------

def test_helpful_feedback_saved(db):
    snap = _snap(db, answer_eligible=True)
    r = save_answer_feedback(db, snap.id, _user(["engineering"]), True, REASON_INCORRECT, "note")
    assert r["ok"] is True
    fb = db.query(AnswerFeedback).filter(AnswerFeedback.answer_id == snap.id).one()
    assert fb.helpful is True
    assert fb.reason is None
    assert fb.note is None


def test_negative_feedback_no_reason_no_note(db):
    snap = _snap(db, answer_eligible=True)
    r = save_answer_feedback(db, snap.id, _user(["engineering"]), False, None, None)
    assert r["ok"] is True
    fb = db.query(AnswerFeedback).filter(AnswerFeedback.answer_id == snap.id).one()
    assert fb.helpful is False
    assert fb.reason is None


def test_feedback_reason_only_incorrect_incomplete_null(db):
    snap = _snap(db, answer_eligible=True)
    assert save_answer_feedback(db, snap.id, _user(["engineering"]), False, "bogus", None)["ok"] is False
    assert save_answer_feedback(db, snap.id, _user(["engineering"]), False, REASON_INCORRECT, None)["ok"] is True
    assert save_answer_feedback(db, snap.id, _user(["engineering"]), False, REASON_INCOMPLETE, None)["ok"] is True


def test_same_user_repeated_feedback_upserts(db):
    snap = _snap(db, answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"]), False, REASON_INCORRECT, None)
    save_answer_feedback(db, snap.id, _user(["engineering"]), True, None, None)
    rows = db.query(AnswerFeedback).filter(AnswerFeedback.answer_id == snap.id).all()
    assert len(rows) == 1
    assert rows[0].helpful is True


def test_other_user_cannot_feedback(db):
    snap = _snap(db, user_id="u1", answer_eligible=True)
    r = save_answer_feedback(db, snap.id, _user(["engineering"], uid="u2"), False, None, None)
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# 阈值 / 合并
# ---------------------------------------------------------------------------

def test_single_negative_feedback_no_debt(db):
    snap = _snap(db, user_id="u1", answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    assert db.query(KnowledgeDebt).count() == 0
    assert db.query(AccessRequest).count() == 0


def test_two_users_threshold_single_classification(db):
    snap = _snap(db, user_id="u1", answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    assert db.query(KnowledgeDebt).count() == 0
    snap2 = _snap(db, user_id="u2", answer_eligible=True)
    save_answer_feedback(db, snap2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)
    assert db.query(KnowledgeDebt).count() == 1
    cluster = db.query(FeedbackCluster).one()
    assert cluster.classified_at is not None


def test_reason_not_merged(db):
    for i, reason in enumerate([REASON_INCORRECT, REASON_INCOMPLETE, None]):
        s1 = _snap(db, user_id=f"u{i}a", answer_eligible=True)
        s2 = _snap(db, user_id=f"u{i}b", answer_eligible=True)
        save_answer_feedback(db, s1.id, _user(["engineering"], uid=f"u{i}a"), False, reason, None)
        save_answer_feedback(db, s2.id, _user(["engineering"], uid=f"u{i}b"), False, reason, None)
    debts = db.query(KnowledgeDebt).all()
    assert len({d.cluster_key for d in debts}) == 3


def test_version_not_merged(db):
    def _two_feedback(version_query, uid_a, uid_b):
        s1 = _snap(db, user_id=uid_a, query=version_query, answer_eligible=True)
        s2 = _snap(db, user_id=uid_b, query=version_query, answer_eligible=True)
        save_answer_feedback(db, s1.id, _user(["engineering"], uid=uid_a), False, REASON_INCORRECT, None)
        save_answer_feedback(db, s2.id, _user(["engineering"], uid=uid_b), False, REASON_INCORRECT, None)

    _two_feedback("Titan 2.0 电池容量", "ua", "ub")
    _two_feedback("Titan 3.0 电池容量", "uc", "ud")
    debts = db.query(KnowledgeDebt).all()
    versions = {d.version_label for d in debts}
    assert "2.0" in versions
    assert "3.0" in versions
    assert len(debts) == 2


def test_similar_questions_same_threshold(db):
    s1 = _snap(db, user_id="u1", query="水箱容量", answer_eligible=True)
    s2 = _snap(db, user_id="u2", query="水箱的容量", answer_eligible=True)
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)
    assert db.query(KnowledgeDebt).count() == 1
    assert db.query(FeedbackCluster).count() == 1


# ---------------------------------------------------------------------------
# 反馈撤销/换原因（精确 cluster 关联）
# ---------------------------------------------------------------------------

def test_down_then_up_removes_from_threshold(db):
    snap = _snap(db, user_id="u1", answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), True, None, None)

    snap2 = _snap(db, user_id="u2", answer_eligible=True)
    save_answer_feedback(db, snap2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)

    assert db.query(KnowledgeDebt).count() == 0
    users = {u.user_id for u in db.query(FeedbackClusterUser).all()}
    assert users == {"u2"}


def test_change_reason_uses_exact_old_cluster_key(db):
    snap = _snap(db, user_id="u1", answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    old_key = db.query(AnswerFeedback).filter(AnswerFeedback.answer_id == snap.id).one().feedback_cluster_key
    assert old_key is not None

    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCOMPLETE, None)
    # 旧 incorrect cluster 无 u1 成员
    assert db.query(FeedbackClusterUser).filter(
        FeedbackClusterUser.cluster_key == old_key, FeedbackClusterUser.user_id == "u1"
    ).count() == 0
    new_key = db.query(AnswerFeedback).filter(AnswerFeedback.answer_id == snap.id).one().feedback_cluster_key
    assert new_key != old_key
    assert db.query(FeedbackClusterUser).filter(
        FeedbackClusterUser.cluster_key == new_key, FeedbackClusterUser.user_id == "u1"
    ).count() == 1


def test_same_user_two_negative_same_cluster_keep_member(db):
    s1 = _snap(db, user_id="u1", query="水箱容量", answer_eligible=True)
    s2 = _snap(db, user_id="u1", query="水箱的容量", answer_eligible=True)
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)

    fbs = db.query(AnswerFeedback).filter(AnswerFeedback.user_id == "u1", AnswerFeedback.helpful.is_(False)).all()
    assert len(fbs) == 2
    keys = {f.feedback_cluster_key for f in fbs}
    assert len(keys) == 1
    cluster_key = fbs[0].feedback_cluster_key

    # 撤销其中一条（改成 👍）：该用户仍有一条负面指向同 cluster，保留成员
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), True, None, None)
    assert db.query(FeedbackClusterUser).filter(
        FeedbackClusterUser.cluster_key == cluster_key, FeedbackClusterUser.user_id == "u1"
    ).count() == 1


def test_last_negative_removed_removes_member(db):
    s1 = _snap(db, user_id="u1", query="水箱容量", answer_eligible=True)
    s2 = _snap(db, user_id="u1", query="水箱的容量", answer_eligible=True)
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    cluster_key = db.query(AnswerFeedback).filter(
        AnswerFeedback.user_id == "u1", AnswerFeedback.helpful.is_(False)
    ).first().feedback_cluster_key

    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), True, None, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u1"), True, None, None)
    assert db.query(FeedbackClusterUser).filter(
        FeedbackClusterUser.cluster_key == cluster_key, FeedbackClusterUser.user_id == "u1"
    ).count() == 0


# ---------------------------------------------------------------------------
# 分类门禁
# ---------------------------------------------------------------------------

def test_classify_visible_sufficient(db):
    _seed_wiki(db, "w1", "水箱容量", ["engineering"])
    db.commit()
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_VISIBLE_SUFFICIENT


def test_classify_globally_missing(db):
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_GLOBALLY_MISSING


def test_negative_feedback_on_sufficient_no_debt(db):
    _seed_wiki(db, "w1", "水箱容量", ["engineering"])
    db.commit()
    snap = _snap(db, user_id="u1", answer_eligible=True)
    save_answer_feedback(db, snap.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    snap2 = _snap(db, user_id="u2", answer_eligible=True)
    save_answer_feedback(db, snap2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)
    assert db.query(KnowledgeDebt).count() == 0
    assert db.query(AccessRequest).count() == 0


def test_hidden_sufficient_creates_access_request(db):
    _seed_wiki(db, "w1", "水箱容量", ["sales"])
    db.commit()
    snap = _snap(db, user_id="u1", answer_eligible=False)
    record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    assert db.query(AccessRequest).count() == 1
    assert db.query(KnowledgeDebt).count() == 0
    req = db.query(AccessRequest).one()
    assert json.loads(req.requesting_groups) == ["engineering"]


def test_multi_group_user_not_single_group(db):
    _seed_wiki(db, "w1", "水箱容量", ["marketing"])
    db.commit()
    snap = _snap(db, user_id="u1", answer_eligible=False, groups=["engineering", "sales"])
    record_answer_needed(db, snap.id, _user(["engineering", "sales"], uid="u1"))
    req = db.query(AccessRequest).one()
    assert sorted(json.loads(req.requesting_groups)) == ["engineering", "sales"]


# ---------------------------------------------------------------------------
# answer-needed
# ---------------------------------------------------------------------------

def test_answer_needed_idempotent(db):
    snap = _snap(db, user_id="u1", answer_eligible=False)
    r1 = record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    r2 = record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    assert r1["ok"] and r2["ok"]
    assert db.query(AnswerNeeded).count() == 1


def test_answer_needed_requires_active_click(db):
    _snap(db, user_id="u1", answer_eligible=False)
    assert db.query(KnowledgeDebt).count() == 0
    assert db.query(AccessRequest).count() == 0


def test_answer_needed_rejects_degraded(db):
    snap = _snap(db, user_id="u1", answer_eligible=False, service_degraded=True, retrieval_completed=False)
    record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    assert db.query(KnowledgeDebt).count() == 0
    assert db.query(AccessRequest).count() == 0


def test_answer_needed_degraded_retryable(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs

    outcomes = [CLASS_DEGRADED, CLASS_GLOBALLY_MISSING]
    state = {"n": 0}

    def _fake_classify(dbs, question, current_user):
        idx = min(state["n"], len(outcomes) - 1)
        state["n"] += 1
        return ClassificationResult(outcomes[idx])

    monkeypatch.setattr(fs, "classify_full_library", _fake_classify)

    snap = _snap(db, user_id="u1", answer_eligible=False)
    r1 = record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    assert r1["ok"] is True
    assert db.query(AnswerNeeded).count() == 0  # degraded 不占位
    assert db.query(KnowledgeDebt).count() == 0

    r2 = record_answer_needed(db, snap.id, _user(["engineering"], uid="u1"))
    assert r2["ok"] is True
    assert db.query(AnswerNeeded).count() == 1
    assert db.query(KnowledgeDebt).count() == 1


# ---------------------------------------------------------------------------
# 分类失败回滚（classified_at 保持 NULL）
# ---------------------------------------------------------------------------

def test_classify_debt_write_failure_keeps_null(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs

    def _boom(db_, **kwargs):
        raise RuntimeError("debt write boom")

    monkeypatch.setattr(fs, "record_j4_debt", _boom)

    s1 = _snap(db, user_id="u1", answer_eligible=True)
    s2 = _snap(db, user_id="u2", answer_eligible=True)
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)

    cluster = db.query(FeedbackCluster).one()
    assert cluster.classified_at is None  # 写失败 rollback，未标记
    assert db.query(KnowledgeDebt).count() == 0


def test_classify_access_request_write_failure_keeps_null(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs

    _seed_wiki(db, "w1", "水箱容量", ["marketing"])
    db.commit()

    def _boom(db_, **kwargs):
        raise RuntimeError("access request write boom")

    monkeypatch.setattr(fs, "record_access_request", _boom)

    s1 = _snap(db, user_id="u1", answer_eligible=True, groups=["engineering"])
    s2 = _snap(db, user_id="u2", answer_eligible=True, groups=["engineering"])
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["engineering"], uid="u2"), False, REASON_INCORRECT, None)

    cluster = db.query(FeedbackCluster).one()
    assert cluster.classified_at is None
    assert db.query(AccessRequest).count() == 0


# ---------------------------------------------------------------------------
# 权限快照
# ---------------------------------------------------------------------------

def test_feedback_uses_snapshot_permissions(db):
    # 回答产生时 engineering 可见充分；提交反馈时当前组为 sales（不同）
    _seed_wiki(db, "w1", "水箱容量", ["engineering"])
    db.commit()
    snap = _snap(db, user_id="u1", answer_eligible=False, groups=["engineering"])
    record_answer_needed(db, snap.id, _user(["sales"], uid="u1"))
    # 分类用快照 engineering → visible_sufficient → 不建访问申请
    assert db.query(AccessRequest).count() == 0


def test_two_groups_threshold_separate_access_requests(db):
    _seed_wiki(db, "w1", "水箱容量", ["marketing"])
    db.commit()
    s1 = _snap(db, user_id="u1", query="水箱容量", answer_eligible=True, groups=["engineering"])
    s2 = _snap(db, user_id="u2", query="水箱容量", answer_eligible=True, groups=["sales"])
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["sales"], uid="u2"), False, REASON_INCORRECT, None)

    reqs = db.query(AccessRequest).all()
    assert len(reqs) == 2
    groups = {json.loads(r.requesting_groups)[0] for r in reqs}
    assert groups == {"engineering", "sales"}


def test_company_user_creates_company_access_request(db):
    _seed_wiki(db, "w1", "水箱容量", ["marketing"])
    db.commit()
    snap = _snap(db, user_id="u1", answer_eligible=False, groups=[])
    record_answer_needed(db, snap.id, _user([], uid="u1"))
    reqs = db.query(AccessRequest).all()
    assert len(reqs) == 1
    assert json.loads(reqs[0].requesting_groups) == []


# ---------------------------------------------------------------------------
# 访问申请严格目标隔离
# ---------------------------------------------------------------------------

def test_similar_query_different_target_not_merged(db):
    record_access_request(
        db, groups=["engineering"], normalized_query="水箱容量", original_query="水箱容量",
        version_label=None, version_status="unspecified", target_notebook_id="nb-a", target_wiki_page_id=None, user_ids=["u1"],
    )
    record_access_request(
        db, groups=["engineering"], normalized_query="水箱的容量", original_query="水箱的容量",
        version_label=None, version_status="unspecified", target_notebook_id="nb-b", target_wiki_page_id=None, user_ids=["u1"],
    )
    assert db.query(AccessRequest).count() == 2


# ---------------------------------------------------------------------------
# 重验证：相关候选 + truncated
# ---------------------------------------------------------------------------

def test_notebook_change_only_revalidates_related(db):
    page_a = _seed_page(db, "p-a", "水箱容量", "水箱容量为 500L 的完整说明", group=None)
    page_b = _seed_page(db, "p-b", "电池电压", "电池电压为 12V 的完整说明", group=None)
    db.commit()
    record_access_request(
        db, groups=["sales"], normalized_query="水箱容量", original_query="水箱容量",
        version_label=None, version_status="unspecified", target_notebook_id=page_a.notebook_id, target_wiki_page_id=None, user_ids=["u1"],
    )
    record_access_request(
        db, groups=["sales"], normalized_query="电池电压", original_query="电池电压",
        version_label=None, version_status="unspecified", target_notebook_id=page_b.notebook_id, target_wiki_page_id=None, user_ids=["u1"],
    )
    db.commit()

    revalidate_access_requests_for_notebook_change(db, page_a.notebook_id)
    reqs = {r.target_notebook_id: r.status for r in db.query(AccessRequest).all()}
    assert reqs[page_a.notebook_id] == "resolved"
    assert reqs[page_b.notebook_id] == "open"


def test_revalidate_truncated_false(db):
    for i in range(205):
        db.add(AccessRequest(
            id=f"ar{i:04d}", cluster_key=f"ck-{i}", normalized_query=f"q{i}", original_query=f"q{i}",
            version_label=None, version_status="unspecified", requesting_groups="[]",
            target_notebook_id=None, target_wiki_page_id=None, occurrence_count=1, affected_user_count=1, status="open",
        ))
    db.commit()
    result = revalidate_access_requests_for_notebook_change(db, None)
    assert result["truncated"] is False
    assert result["checked"] == 205
    assert result["batch_count"] >= 3


def test_multi_group_union_resolves_access_request(db):
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()
    record_access_request(
        db, groups=["engineering", "sales"], normalized_query="水箱容量", original_query="水箱容量",
        version_label=None, version_status="unspecified", target_notebook_id=None, target_wiki_page_id=None, user_ids=["u1"],
    )
    req = db.query(AccessRequest).one()
    assert req.status == "open"
    revalidate_access_requests_for_notebook_change(db, None)
    db.refresh(req)
    assert req.status == "resolved"


def test_notebook_change_to_company_resolves_access_request(db):
    page = _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="sales")
    db.commit()
    record_access_request(
        db, groups=["engineering"], normalized_query="水箱容量", original_query="水箱容量",
        version_label=None, version_status="unspecified", target_notebook_id=page.notebook_id, target_wiki_page_id=None, user_ids=["u1"],
    )
    req = db.query(AccessRequest).one()
    assert req.status == "open"

    nb = db.get(Notebook, page.notebook_id)
    nb.group_id = None  # company：所有登录用户可见
    db.commit()
    revalidate_access_requests_for_notebook_change(db, nb.id)
    db.refresh(req)
    assert req.status == "resolved"


def test_revalidate_over_200_no_permanent_miss(db):
    for i in range(250):
        db.add(KnowledgeDebt(
            id=f"gd{i:04d}", debt_type="missing_knowledge", description=f"d{i}", related_question=f"无关 {i}",
            status="open", root_cause="missing_knowledge", original_query=f"无关 {i}",
            normalized_query=f"无关{i}", cluster_key=f"ck-{i}", scope_id=None,
            debt_reason="query_missing", occurrence_count=1, affected_user_count=1,
        ))
    db.commit()
    result = revalidate_global_debts_for_change(db, "水箱容量 更换")
    assert result["batch_count"] >= 3
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# 异常安全降级（sufficiency judge 抛异常）
# ---------------------------------------------------------------------------

def test_wiki_judge_exception_degraded(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs
    _seed_wiki(db, "w1", "水箱容量", ["engineering"])
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("wiki judge boom")

    monkeypatch.setattr(fs, "judge_wiki_sufficiency", _boom)
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_DEGRADED


def test_raw_judge_exception_degraded(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("raw judge boom")

    monkeypatch.setattr(fs, "judge_raw_sufficiency", _boom)
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_DEGRADED


def test_wiki_sufficient_raw_fault_no_effect(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs
    _seed_wiki(db, "w1", "水箱容量", ["engineering"])
    db.commit()

    def _boom(self, db_, question, current_user, **kwargs):
        raise RuntimeError("raw boom")

    monkeypatch.setattr(fs.RawDocumentRetriever, "retrieve", _boom)
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_VISIBLE_SUFFICIENT


def test_raw_degraded_no_record(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs
    from app.core.retrieval.raw_retriever import RawRetrievalResult

    def _fake_raw_retrieve(self, db_, question, current_user, **kwargs):
        return RawRetrievalResult(hits=[], query=question, degraded=["embedding_dimension_mismatch"])

    monkeypatch.setattr(fs.RawDocumentRetriever, "retrieve", _fake_raw_retrieve)
    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_DEGRADED


# ---------------------------------------------------------------------------
# Wiki 目标定位
# ---------------------------------------------------------------------------

def test_wiki_target_not_overwritten_by_raw(db):
    nb_sales = Notebook(id="nb-sales", name="销售库", group_id="sales")
    nb_other = Notebook(id="nb-other", name="其他库", group_id="marketing")
    db.add_all([nb_sales, nb_other]); db.flush()
    db.add(Page(id="p-sales", notebook_id="nb-sales", title="水箱容量", content="水箱容量为 500L"))
    db.add(Page(id="p-other", notebook_id="nb-other", title="水箱", content="水箱是一种容器"))
    db.commit()
    _seed_wiki(db, "w1", "水箱容量", ["sales"], source_page_ids=["p-sales"])
    db.commit()

    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_HIDDEN_SUFFICIENT
    assert r.target_wiki_page_id == "w1"
    assert r.target_notebook_id == "nb-sales"


def test_hidden_wiki_locates_notebook_via_version_source(db):
    nb_sales = Notebook(id="nb-sales", name="销售库", group_id="sales")
    db.add(nb_sales); db.flush()
    db.add(Page(id="p-sales", notebook_id="nb-sales", title="水箱容量", content="水箱容量为 500L"))
    db.commit()
    _seed_wiki(db, "w1", "水箱容量", ["sales"])
    db.add(WikiVersionSource(id="wvs1", wiki_page_id="w1", version_label="common", page_id="p-sales"))
    db.commit()

    r = classify_full_library(db, "水箱容量", _user(["engineering"]))
    assert r.outcome == CLASS_HIDDEN_SUFFICIENT
    assert r.target_notebook_id == "nb-sales"


# ---------------------------------------------------------------------------
# 并发：达到阈值只分类一次、一次 occurrence
# ---------------------------------------------------------------------------

def test_concurrent_threshold_single_record(tmp_path):
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'j4.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})
        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)

    s = sessionmaker(bind=e1)()
    create_answer_snapshot(s, answer_id="a1", user_id="u1", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u1", "groups": ["engineering"]})
    create_answer_snapshot(s, answer_id="a2", user_id="u2", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u2", "groups": ["engineering"]})
    s.commit(); s.close()

    def worker(aid, uid):
        sess = sessionmaker(bind=_make_engine())()
        try:
            save_answer_feedback(sess, aid, {"id": uid, "username": uid, "groups": ["engineering"]}, False, REASON_INCORRECT, None)
        finally:
            sess.close()

    t1 = threading.Thread(target=worker, args=("a1", "u1"))
    t2 = threading.Thread(target=worker, args=("a2", "u2"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = sessionmaker(bind=e1)()
    debts = s.query(KnowledgeDebt).all()
    assert len(debts) == 1
    assert debts[0].occurrence_count == 1
    clusters = s.query(FeedbackCluster).all()
    assert len(clusters) == 1
    assert clusters[0].classified_at is not None
    s.close()
    e1.dispose()


# ---------------------------------------------------------------------------
# 普通响应无泄露
# ---------------------------------------------------------------------------

def test_chat_response_no_debt_fields(db):
    from app.api import rag_chat
    from app.core.retrieval.degradation import build_rag_response
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiHit, WikiRetrievalResult

    hit = WikiHit(wiki_page_id="w1", title="t", summary="", content="c", score=0.9, acl_scope="{}")
    outcome = OrchestrationOutcome(mode="wiki_hit", wiki_results=WikiRetrievalResult(hits=[hit]))
    resp = build_rag_response(db, outcome, llm_answer="a", llm_called=True, llm_ok=True)
    final = rag_chat._finalize_chat_response(db, {"id": "u1", "groups": ["engineering"]}, "水箱容量", resp)
    for forbidden in (
        "debt_created", "debt_recording_status", "debt_id", "debt_reopened",
        "access_request_created", "classification", "target_notebook_id", "target_wiki_page_id",
        "business_groups",
    ):
        assert forbidden not in final
    assert "answer_id" in final


# ---------------------------------------------------------------------------
# 管理员页面 / 访问申请 ACL
# ---------------------------------------------------------------------------

def test_debt_list_minimal_serialization(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    from app.core.retrieval.debt_service import build_cluster_key, normalize_query
    titles = ["如何给 Titan 810 更换轮胎", "Titan 810 标准电池容量是多少？", ""]
    for i, title in enumerate(titles):
        db2 = sessionmaker(bind=engine)()
        ck = build_cluster_key("company", normalize_query(title) or f"empty{i}")
        db2.add(KnowledgeDebt(
            id=f"d{i}", debt_type="missing_knowledge", description=title, related_question=title,
            status="open", root_cause="missing_knowledge", original_query=title,
            normalized_query=normalize_query(title) or f"empty{i}", cluster_key=ck,
            scope_id="company", occurrence_count=1, affected_user_count=1,
        ))
        db2.commit(); db2.close()

    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}
    client = TestClient(app)
    try:
        r = client.get("/api/v4/debts")
        assert r.status_code == 200
        for item in r.json()["debts"]:
            assert set(item.keys()) == {"id", "title", "status"}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_ordinary_user_cannot_list_access_requests(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}
    client = TestClient(app)
    try:
        assert client.get("/api/v4/access-requests").status_code == 403
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# 知识缺口自动解决
# ---------------------------------------------------------------------------

def test_page_content_resolves_global_debt(db):
    from app.core.retrieval.feedback_service import record_j4_debt
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page, RETRIEVAL_REASON_MISSING
    record_j4_debt(
        db, debt_reason="query_missing", version_label=None, version_status="unspecified",
        original_query="水箱容量", user_ids=["u1"], retrieval_reason=RETRIEVAL_REASON_MISSING,
    )
    db.commit()
    debt = db.query(KnowledgeDebt).one()
    assert debt.status == "open"
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L 的完整说明", group="engineering")
    db.commit()
    notify_knowledge_changed_for_page(db, "p1")
    db.refresh(debt)
    assert debt.status == "resolved"


def test_unrelated_page_does_not_resolve(db):
    from app.core.retrieval.feedback_service import record_j4_debt
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page, RETRIEVAL_REASON_MISSING
    record_j4_debt(
        db, debt_reason="query_missing", version_label=None, version_status="unspecified",
        original_query="水箱容量", user_ids=["u1"], retrieval_reason=RETRIEVAL_REASON_MISSING,
    )
    db.commit()
    _seed_page(db, "p1", "完全无关", "这是一个完全无关的文档", group="engineering")
    db.commit()
    notify_knowledge_changed_for_page(db, "p1")
    debt = db.query(KnowledgeDebt).one()
    assert debt.status == "open"


# ---------------------------------------------------------------------------
# 15. P37 malformed 全部 fail closed
# ---------------------------------------------------------------------------

def test_p37_missing_column_fail_closed():
    e = create_engine("sqlite://")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE answer_snapshots (id VARCHAR(36) PRIMARY KEY)"))
    problems = check_managed_migrations(e)
    assert any(p.startswith("answer_snapshots.") for p in problems)
    e.dispose()


def test_p37_wrong_nullable_fail_closed():
    e = create_engine("sqlite://")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE answer_snapshots (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36))"))
    problems = check_managed_migrations(e)
    assert "answer_snapshots.user_id:nullable=True" in problems
    e.dispose()


def test_p37_missing_business_groups_fail_closed():
    e = create_engine("sqlite://")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE answer_snapshots (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, "
            "original_query TEXT NOT NULL, normalized_query VARCHAR(255) NOT NULL, version_label VARCHAR(64), "
            "version_status VARCHAR(32), response_mode VARCHAR(32), retrieval_completed BOOLEAN, "
            "answer_eligible BOOLEAN, service_degraded BOOLEAN, has_visible_sufficient_evidence BOOLEAN, "
            "is_admin BOOLEAN, created_at DATETIME)"
        ))
    problems = check_managed_migrations(e)
    assert "answer_snapshots.business_groups" in problems
    e.dispose()


def test_p37_missing_feedback_cluster_key_fail_closed():
    e = create_engine("sqlite://")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE answer_snapshots (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, "
            "original_query TEXT NOT NULL, normalized_query VARCHAR(255) NOT NULL, version_label VARCHAR(64), "
            "version_status VARCHAR(32), response_mode VARCHAR(32), retrieval_completed BOOLEAN, "
            "answer_eligible BOOLEAN, service_degraded BOOLEAN, has_visible_sufficient_evidence BOOLEAN, "
            "business_groups TEXT, is_admin BOOLEAN, created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE answer_feedback (id VARCHAR(36) PRIMARY KEY, answer_id VARCHAR(36) NOT NULL, "
            "user_id VARCHAR(36) NOT NULL, helpful BOOLEAN NOT NULL, reason VARCHAR(32), note TEXT, "
            "created_at DATETIME, updated_at DATETIME, "
            "FOREIGN KEY(answer_id) REFERENCES answer_snapshots(id) ON DELETE CASCADE)"
        ))
    problems = check_managed_migrations(e)
    assert "answer_feedback.feedback_cluster_key" in problems
    e.dispose()


def test_p37_feedback_cluster_fk_wrong_ondelete_fail_closed():
    e = create_engine("sqlite://")
    with e.begin() as conn:
        conn.execute(text("CREATE TABLE pages (id VARCHAR(36) PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE answer_snapshots (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, "
            "original_query TEXT NOT NULL, normalized_query VARCHAR(255) NOT NULL, version_label VARCHAR(64), "
            "version_status VARCHAR(32), response_mode VARCHAR(32), retrieval_completed BOOLEAN, "
            "answer_eligible BOOLEAN, service_degraded BOOLEAN, has_visible_sufficient_evidence BOOLEAN, "
            "business_groups TEXT, is_admin BOOLEAN, created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE feedback_clusters (cluster_key VARCHAR(255) PRIMARY KEY, normalized_query VARCHAR(255) NOT NULL, "
            "version_label VARCHAR(64), version_status VARCHAR(32), reason VARCHAR(32) NOT NULL, classification VARCHAR(32), "
            "classified_at DATETIME, created_at DATETIME, last_seen_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE answer_feedback (id VARCHAR(36) PRIMARY KEY, answer_id VARCHAR(36) NOT NULL, "
            "user_id VARCHAR(36) NOT NULL, helpful BOOLEAN NOT NULL, reason VARCHAR(32), note TEXT, "
            "feedback_cluster_key VARCHAR(255), created_at DATETIME, updated_at DATETIME, "
            "FOREIGN KEY(answer_id) REFERENCES answer_snapshots(id) ON DELETE CASCADE, "
            "FOREIGN KEY(feedback_cluster_key) REFERENCES feedback_clusters(cluster_key) ON DELETE CASCADE)"
        ))
    problems = check_managed_migrations(e)
    assert "answer_feedback.feedback_cluster_key:ondelete=CASCADE" in problems
    e.dispose()


# ---------------------------------------------------------------------------
# 13. Sources 页面读取 notebook query 并定位权限项
# ---------------------------------------------------------------------------

def test_sources_reads_notebook_query():
    src = (FRONTEND_ROOT / "views" / "Sources.vue").read_text(encoding="utf-8")
    assert "useRoute" in src
    assert "route.query.notebook" in src
    assert "scrollIntoView" in src
    assert "notebookRowClassName" in src


# ---------------------------------------------------------------------------
# 历史数据不硬编码过滤
# ---------------------------------------------------------------------------

def test_no_hardcoded_history_filter():
    import app.core.retrieval.feedback_service as fs
    import app.api.debts_v4 as dv
    for mod in (fs, dv):
        src = inspect.getsource(mod)
        for forbidden in ("nihao", "Titan", "Skywalker", "社区 1"):
            assert forbidden not in src, f"{mod.__name__} 不应硬编码 {forbidden}"


# ---------------------------------------------------------------------------
# 本轮新增：分类任一权限视图降级整组 fail closed
# ---------------------------------------------------------------------------

def test_any_user_scope_degraded_rolls_back_whole_classification(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs
    from app.core.retrieval.feedback_service import ScopeAssessment

    # 先 u2(sales) 差评（distinct=1，不触发分类）。
    s2 = _snap(db, user_id="u2", query="水箱容量", answer_eligible=True, groups=["sales"])
    save_answer_feedback(db, s2.id, _user(["sales"], uid="u2"), False, REASON_INCORRECT, None)

    s1 = _snap(db, user_id="u1", query="水箱的容量", answer_eligible=True, groups=["engineering"])

    # 第一阶段先评估 engineering（insufficient，记录到内存），再评估 sales（degraded → 抛异常）。
    def _fake_assess(dbs, user, question):
        groups = user.get("groups") or []
        if "__local_admin__" in groups:
            return ScopeAssessment(sufficient=True, has_hits=True, degraded=False,
                                   target_notebook_id="nb-x", target_wiki_page_id="w1")
        if "sales" in groups:
            return ScopeAssessment(sufficient=False, has_hits=False, degraded=True)
        return ScopeAssessment(sufficient=False, has_hits=False, degraded=False)

    monkeypatch.setattr(fs, "_assess_scope", _fake_assess)
    # u1 差评 → distinct=2 触发分类，任一用户降级整组 rollback。
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)

    cluster = db.query(FeedbackCluster).one()
    assert cluster.classified_at is None
    assert cluster.classification is None
    assert db.query(AccessRequest).count() == 0
    assert db.query(KnowledgeDebt).count() == 0


# ---------------------------------------------------------------------------
# 本轮新增：统一锁（撤销先于差评，分类用当前反馈，不建债）
# ---------------------------------------------------------------------------

def test_concurrent_revoke_before_downvote_no_debt(tmp_path):
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'lock.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})
        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)

    s = sessionmaker(bind=e1)()
    create_answer_snapshot(s, answer_id="a1", user_id="u1", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u1", "groups": ["engineering"]})
    create_answer_snapshot(s, answer_id="a2", user_id="u2", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u2", "groups": ["engineering"]})
    s.commit(); s.close()

    sess = sessionmaker(bind=e1)()
    save_answer_feedback(sess, "a1", {"id": "u1", "username": "u1", "groups": ["engineering"]}, False, REASON_INCORRECT, None)
    sess.close()

    revoke_done = threading.Event()

    def worker_revoke():
        sess = sessionmaker(bind=_make_engine())()
        try:
            save_answer_feedback(sess, "a1", {"id": "u1", "username": "u1", "groups": ["engineering"]}, True, None, None)
            revoke_done.set()
        finally:
            sess.close()

    def worker_downvote():
        sess = sessionmaker(bind=_make_engine())()
        try:
            revoke_done.wait(timeout=10)
            save_answer_feedback(sess, "a2", {"id": "u2", "username": "u2", "groups": ["engineering"]}, False, REASON_INCORRECT, None)
        finally:
            sess.close()

    t_revoke = threading.Thread(target=worker_revoke)
    t_down = threading.Thread(target=worker_downvote)
    t_revoke.start()
    t_down.start()
    t_revoke.join(); t_down.join()

    s = sessionmaker(bind=e1)()
    assert s.query(KnowledgeDebt).count() == 0
    assert s.query(AccessRequest).count() == 0
    clusters = s.query(FeedbackCluster).all()
    assert all(c.classified_at is None for c in clusters)
    s.close()
    e1.dispose()


def test_concurrent_downvote_and_revoke_no_duplicate(tmp_path):
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'lock2.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})
        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)

    s = sessionmaker(bind=e1)()
    create_answer_snapshot(s, answer_id="a1", user_id="u1", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u1", "groups": ["engineering"]})
    create_answer_snapshot(s, answer_id="a2", user_id="u2", original_query="水箱容量",
                           response={"response_mode": "answer", "retrieval_completed": True, "answer_eligible": True, "service_degraded": False},
                           current_user={"id": "u2", "groups": ["engineering"]})
    s.commit(); s.close()

    sess = sessionmaker(bind=e1)()
    save_answer_feedback(sess, "a1", {"id": "u1", "username": "u1", "groups": ["engineering"]}, False, REASON_INCORRECT, None)
    sess.close()

    barrier = threading.Barrier(2)

    def worker_downvote():
        sess = sessionmaker(bind=_make_engine())()
        try:
            barrier.wait()
            save_answer_feedback(sess, "a2", {"id": "u2", "username": "u2", "groups": ["engineering"]}, False, REASON_INCORRECT, None)
        finally:
            sess.close()

    def worker_revoke():
        sess = sessionmaker(bind=_make_engine())()
        try:
            barrier.wait()
            save_answer_feedback(sess, "a1", {"id": "u1", "username": "u1", "groups": ["engineering"]}, True, None, None)
        finally:
            sess.close()

    t1 = threading.Thread(target=worker_downvote)
    t2 = threading.Thread(target=worker_revoke)
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = sessionmaker(bind=e1)()
    debts = s.query(KnowledgeDebt).all()
    assert len(debts) <= 1
    if debts:
        assert debts[0].occurrence_count == 1
    s.close()
    e1.dispose()


# ---------------------------------------------------------------------------
# 本轮新增：同一用户多条负面反馈选择最新权限快照
# ---------------------------------------------------------------------------

def test_same_user_latest_permission_snapshot(db):
    _seed_wiki(db, "w1", "水箱容量", ["marketing"])
    db.commit()
    s1 = _snap(db, user_id="u1", query="水箱容量", answer_eligible=True, groups=["engineering"])
    s2 = _snap(db, user_id="u1", query="水箱的容量", answer_eligible=True, groups=["sales"])
    save_answer_feedback(db, s1.id, _user(["engineering"], uid="u1"), False, REASON_INCORRECT, None)
    save_answer_feedback(db, s2.id, _user(["sales"], uid="u1"), False, REASON_INCORRECT, None)

    s3 = _snap(db, user_id="u2", query="水箱容量", answer_eligible=True, groups=["sales"])
    save_answer_feedback(db, s3.id, _user(["sales"], uid="u2"), False, REASON_INCORRECT, None)

    reqs = db.query(AccessRequest).all()
    assert len(reqs) == 1
    assert json.loads(reqs[0].requesting_groups) == ["sales"]


# ---------------------------------------------------------------------------
# 本轮新增：Notebook 重验证 SQL 有界（无独立无 LIMIT pages/wvs 全量扫描）
# ---------------------------------------------------------------------------

def test_revalidate_no_unbounded_page_wiki_scan(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs

    nb_a = Notebook(id="nb-a", name="A", group_id="engineering")
    nb_b = Notebook(id="nb-b", name="B", group_id="sales")
    db.add_all([nb_a, nb_b]); db.flush()
    db.add(Page(id="p-a", notebook_id="nb-a", title="t", content="c"))
    db.add(Page(id="p-b", notebook_id="nb-b", title="t", content="c"))
    db.commit()

    record_access_request(db, groups=["marketing"], normalized_query="qa", original_query="qa",
                          version_label=None, version_status="unspecified",
                          target_notebook_id="nb-a", target_wiki_page_id=None, user_ids=["u1"])
    record_access_request(db, groups=["marketing"], normalized_query="qb", original_query="qb",
                          version_label=None, version_status="unspecified",
                          target_notebook_id="nb-b", target_wiki_page_id=None, user_ids=["u1"])
    db.commit()

    monkeypatch.setattr(fs, "_request_scope_sufficient", lambda db_, req: False)

    captured = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    try:
        result = revalidate_access_requests_for_notebook_change(db, "nb-a")
    finally:
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert result["checked"] == 1
    req_b = db.query(AccessRequest).filter(AccessRequest.target_notebook_id == "nb-b").one()
    assert req_b.status == "open"

    for stmt in captured:
        low = stmt.lower()
        if "from pages" in low:
            assert "join" in low or "limit" in low, f"无界 pages 扫描: {stmt}"
        if "from wiki_version_sources" in low:
            assert "join" in low or "limit" in low, f"无界 wiki_version_sources 扫描: {stmt}"


def test_revalidate_keyset_processes_all_related(db, monkeypatch):
    import app.core.retrieval.feedback_service as fs

    nb = Notebook(id="nb-x", name="X", group_id="engineering")
    db.add(nb); db.commit()
    for i in range(105):
        db.add(AccessRequest(
            id=f"ar{i:04d}", cluster_key=f"ck-{i}", normalized_query=f"q{i}", original_query=f"q{i}",
            version_label=None, version_status="unspecified", requesting_groups="[]",
            target_notebook_id="nb-x", target_wiki_page_id=None,
            occurrence_count=1, affected_user_count=1, status="open",
        ))
    db.commit()

    monkeypatch.setattr(fs, "_request_scope_sufficient", lambda db_, req: True)
    result = revalidate_access_requests_for_notebook_change(db, "nb-x")
    assert result["checked"] == 105
    assert result["resolved"] == 105
    assert result["batch_count"] >= 2
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# 本轮新增：company / 单组 / 多组访问申请文案
# ---------------------------------------------------------------------------

def _request_title(groups, original_query):
    label = "、".join(f"{g}组" for g in groups) if groups else "全公司"
    return f"{label}需要 {original_query} 相关资料"


def test_access_request_title_company_single_multi():
    assert _request_title([], "Titan 810 轮胎") == "全公司需要 Titan 810 轮胎 相关资料"
    assert _request_title(["销售"], "Titan 810 轮胎") == "销售组需要 Titan 810 轮胎 相关资料"
    assert _request_title(["研发", "销售"], "Titan 810 轮胎") == "研发组、销售组需要 Titan 810 轮胎 相关资料"

    src = (FRONTEND_ROOT / "views" / "KnowledgeDebtV4.vue").read_text(encoding="utf-8")
    assert "requestTitle" in src
    assert "全公司" in src
    assert "${g}组" in src
    assert "join('、')" in src
