"""T7.4 知识审判器单元测试。

纯函数测试（无 DB）：覆盖 5 个检查器的正/反用例、judge 聚合、advice 拒答模板。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.retrieval.judge import KnowledgeJudge
from app.core.retrieval.sources import RetrievedKnowledge

judge = KnowledgeJudge()

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"


def _ko(**kw):
    base = {
        "card_id": "abc12345",
        "title": "默认标题",
        "body": "默认正文内容",
        "type": "fact",
        "scope": {},
        "confidence": 0.6,
        "status": "published",
        "source_page_id": "p1",
        "score": 0.9,
    }
    base.update(kw)
    return RetrievedKnowledge(**base)


# ---------- freshness ----------

def test_freshness_passes_when_valid():
    future = (datetime.now() + timedelta(days=30)).isoformat()
    j = judge._check_freshness([_ko(valid_to=future)])
    assert j["passed"] is True


def test_freshness_fails_when_expired():
    past = (datetime.now() - timedelta(days=1)).isoformat()
    j = judge._check_freshness([_ko(valid_to=past)])
    assert j["passed"] is False
    assert "过期" in j["detail"]


def test_freshness_fails_when_superseded():
    j = judge._check_freshness([_ko(superseded_by="new-ko-id")])
    assert j["passed"] is False
    assert "替代" in j["detail"]


# ---------- scope ----------

def test_scope_passes_without_context():
    j = judge._check_scope([_ko(scope={"product": "Titan 810"})], None)
    assert j["passed"] is True


def test_scope_passes_when_matching():
    ko = _ko(scope={"product": "Titan 810", "version": "V2.3"})
    j = judge._check_scope([ko], {"product": "Titan 810", "version": "V2.3"})
    assert j["passed"] is True


def test_scope_fails_when_product_mismatch():
    ko = _ko(scope={"product": "Skywalker50"})
    j = judge._check_scope([ko], {"product": "Titan 810"})
    assert j["passed"] is False
    assert "不匹配" in j["detail"]


def test_scope_passes_when_empty_scope_wildcard():
    j = judge._check_scope([_ko(scope={})], {"product": "Titan 810"})
    assert j["passed"] is True


# ---------- conflict ----------

def test_conflict_detects_numeric_difference():
    a = _ko(card_id="a", title="驱动最低", body="驱动最低 530 才能正常运行")
    b = _ko(card_id="b", title="驱动最低", body="驱动最低 535 才能正常运行")
    j = judge._check_conflict([a, b])
    assert j["passed"] is False
    assert "冲突" in j["detail"]


def test_conflict_passes_when_same_body():
    a = _ko(card_id="a", title="驱动最低", body="驱动最低 530 才能正常运行")
    b = _ko(card_id="b", title="驱动最低", body="驱动最低 530 才能正常运行")
    j = judge._check_conflict([a, b])
    assert j["passed"] is True


def test_conflict_passes_with_single_candidate():
    j = judge._check_conflict([_ko()])
    assert j["passed"] is True


# ---------- authority ----------

def test_authority_passes_for_pdf():
    j = judge._check_authority([_ko(source_mime_type=PDF)])
    assert j["passed"] is True


def test_authority_fails_for_xlsx():
    j = judge._check_authority([_ko(source_mime_type=XLSX)])
    assert j["passed"] is False
    assert "权威" in j["detail"]


def test_authority_passes_when_mime_unknown():
    j = judge._check_authority([_ko(source_mime_type=None)])
    assert j["passed"] is True


# ---------- evidence ----------

def test_evidence_fails_for_procedure_without_evidence():
    j = judge._check_evidence([_ko(type="procedure", evidence_ids=[])])
    assert j["passed"] is False
    assert "证据" in j["detail"]


def test_evidence_passes_for_procedure_with_evidence():
    j = judge._check_evidence([_ko(type="procedure", evidence_ids=["e1"])])
    assert j["passed"] is True


def test_evidence_passes_for_fact():
    j = judge._check_evidence([_ko(type="fact", evidence_ids=[])])
    assert j["passed"] is True


# ---------- judge 聚合 ----------

def test_judge_passes_all_healthy():
    ko = _ko(source_mime_type=PDF, type="fact", evidence_ids=[])
    j = judge.judge([ko], {"product": "Titan 810"})
    assert j["passed"] is True
    assert j["reasons"] == []


def test_judge_fails_and_reports_reasons():
    a = _ko(card_id="a", title="驱动最低", body="驱动最低 530", source_mime_type=XLSX)
    b = _ko(card_id="b", title="驱动最低", body="驱动最低 535", source_mime_type=XLSX)
    j = judge.judge([a, b])
    assert j["passed"] is False
    assert j["reasons"], "不通过时 reasons 不应为空"


def test_judge_fails_empty_candidates():
    """空候选 = 无答案，判不通过（W12 修复：使无答案查询进入债务识别）。"""
    j = judge.judge([])
    assert j["passed"] is False
    assert j["reasons"] == ["未检索到可用的已发布知识卡片"]


# ---------- advice ----------

def test_advice_conflict_template():
    a = _ko(card_id="aaaa", title="驱动最低", body="驱动最低 530 才能正常运行")
    b = _ko(card_id="bbbb", title="驱动最低", body="驱动最低 535 才能正常运行")
    advice = judge.judge([a, b])["advice"]
    assert "知识存在冲突" in advice
    assert "[卡片 #" in advice
    assert "建议" in advice


def test_advice_empty_when_no_conflict():
    j = judge.judge([_ko()])
    assert j["advice"] == ""
