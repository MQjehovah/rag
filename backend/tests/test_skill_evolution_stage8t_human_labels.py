"""A1–A5 人工标签批准与离线核验（stage8t）。

覆盖（人工标签状态边界）：
- 15 条追加式人工记录完整、判定表准确、标识/版本正确、绑定当前材料；
- 材料变化 → 旧记录不可复用；重复 apply 幂等；冲突 verdict fail-closed；
- 原始验收集内容逐字未变（规范摘要常量比对）；仅顶层状态元数据变化；
- grader registry labels_approved=true，real_calibration=false，
  calibration=engineering_only，allow_business_promotion=false，
  require_business_promotable 仍拒绝；
- 管理 API /meta 状态一致；记录无秘密字段。

本文件为“人工标签离线核验”，不是真实评审校准。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user
from app.core.skill_evolution import grader_registry as greg
from app.core.skill_evolution.review_store import (
    bind_hashes, load_reviews, submit_review, usable_reviews)

BACKEND = Path(__file__).resolve().parent.parent
ACC = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-review.json"
STORE = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl"

REVIEWER = "human-project-owner"
DATE = "2026-09-08"
EXPECTED = {
    "A1": {"c1": "pass", "c2": "pass", "overall": "pass"},
    "A2": {"c1": "pass", "c2": "fail", "overall": "fail"},
    "A3": {"c1": "fail", "c2": "fail", "overall": "fail"},
    "A4": {"c1": "fail", "c2": "fail", "overall": "fail"},
    "A5": {"c1": "fail", "c2": "fail", "overall": "fail"},
}
SAMPLE_DIGEST = ("96e07ae8fab1bcab81f413cc2a10f359e48681652e6f0fbe2b99a805584fabb3")
SECRET_MARKERS = ("sk-", "Bearer ", "Authorization", "token=", "password")


def _canonical(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()


def _materials(sample: dict) -> tuple[str, str, dict]:
    return (sample["source_excerpt"], sample["candidate_text"],
            {"checks_text": sample["checks"], "disputes": sample.get("disputes"),
             "category": sample["category"],
             "ground_truth": sample.get("ground_truth")})


def _acc():
    return json.loads(ACC.read_text(encoding="utf-8"))


def _human_reviews():
    return [r for r in load_reviews(STORE)
            if r.get("method") == "human"
            and r.get("reviewer") == REVIEWER
            and str(r.get("review_version", "")).startswith("human@")]


# 1. 15 条人工记录完整
def test_1_fifteen_records_complete():
    recs = _human_reviews()
    assert len(recs) == 15
    got = {}
    for r in recs:
        got.setdefault(r["task_id"], {})[r["check_id"]] = r["verdict"]
    for task_id in EXPECTED:
        assert set(got.get(task_id, {})) == {"c1", "c2", "overall"}


# 2. 判定表准确
def test_2_decision_table_exact():
    recs = _human_reviews()
    got = {}
    for r in recs:
        got.setdefault(r["task_id"], {})[r["check_id"]] = r["verdict"]
    assert got == EXPECTED


# 3. reviewer/method/version 正确
def test_3_identity_and_version():
    for r in _human_reviews():
        assert r["method"] == "human"
        assert r["reviewer"] == REVIEWER
        assert r["review_version"] == f"human@{REVIEWER}|{DATE}"
        assert r["model_ref"] is None and r["prompt_version"] is None


# 4. 记录绑定当前材料哈希
def test_4_binding_matches_current_materials():
    acc = _acc()
    samples = {s["id"]: s for s in acc["samples"]}
    recs = _human_reviews()
    by = {(r["task_id"], r["check_id"]): r for r in recs}
    for task_id, sample in samples.items():
        src, cand, contract = _materials(sample)
        binds = bind_hashes(task_id=task_id, sources=[src],
                            candidate_output={"candidate_text": cand},
                            contract=contract)
        for cid in ("c1", "c2", "overall"):
            rec = by[(task_id, cid)]
            for k, v in binds.items():
                assert rec[k] == v, (task_id, cid, k)


# 5. 材料变化后批准失效
@pytest.mark.parametrize("mutate", ["source", "candidate", "checks"])
def test_5_approval_invalidated_on_material_change(mutate):
    acc = _acc()
    sample = next(s for s in acc["samples"] if s["id"] == "A1")
    src, cand, contract = _materials(sample)
    if mutate == "source":
        src = src + "（已篡改）"
    elif mutate == "candidate":
        cand = cand + "（已篡改）"
    else:
        contract = {**contract, "checks_text": "['篡改检查契约']"}
    usable = usable_reviews(_human_reviews(), task_id="A1", sources=[src],
                            candidate_output={"candidate_text": cand},
                            contract=contract)
    assert usable == []


# 6. 重复 apply 幂等（同绑定同 verdict 不新增行）
def test_6_duplicate_apply_idempotent(tmp_path):
    acc = _acc()
    sample = next(s for s in acc["samples"] if s["id"] == "A2")
    src, cand, contract = _materials(sample)
    store = tmp_path / "dup.jsonl"
    submit_review(store_path=store, task_id="A2", check_id="c1",
                  verdict="pass", reason="步骤动作和顺序存在。",
                  source_evidence_loc="source_excerpt", candidate_loc="candidate_text",
                  method="human", reviewer=REVIEWER, review_version=DATE,
                  sources=[src], candidate_output={"candidate_text": cand},
                  contract=contract)
    dup = submit_review(store_path=store, task_id="A2", check_id="c1",
                        verdict="pass", reason="步骤动作和顺序存在。",
                        source_evidence_loc="source_excerpt", candidate_loc="candidate_text",
                        method="human", reviewer=REVIEWER, review_version=DATE,
                        sources=[src], candidate_output={"candidate_text": cand},
                        contract=contract)
    assert dup.get("duplicate_of_existing") is True
    assert len(load_reviews(store)) == 1


# 7. 同 check 冲突 verdict fail-closed
def test_7_conflict_verdict_fails_closed(tmp_path):
    acc = _acc()
    sample = next(s for s in acc["samples"] if s["id"] == "A3")
    src, cand, contract = _materials(sample)
    store = tmp_path / "conflict.jsonl"
    submit_review(store_path=store, task_id="A3", check_id="c1",
                  verdict="fail", reason="mV 与 V 量级不同（单位归一化：mV≠V）。",
                  source_evidence_loc="source_excerpt", candidate_loc="candidate_text",
                  method="human", reviewer=REVIEWER, review_version=DATE,
                  sources=[src], candidate_output={"candidate_text": cand},
                  contract=contract)
    with pytest.raises(ValueError, match="冲突判定"):
        submit_review(store_path=store, task_id="A3", check_id="c1",
                      verdict="pass", reason="冲突判定注入",
                      source_evidence_loc="source_excerpt", candidate_loc="candidate_text",
                      method="human", reviewer=REVIEWER, review_version=DATE,
                      sources=[src], candidate_output={"candidate_text": cand},
                      contract=contract)
    assert len(load_reviews(store)) == 1   # 未覆盖/未新增


# 8. 原始样例内容未改（逐字不变：规范摘要固定 + 绑定一致）
def test_8_samples_verbatim_unchanged():
    acc = _acc()
    assert len(acc["samples"]) == 5
    digest = _canonical(acc["samples"])
    assert digest == SAMPLE_DIGEST
    assert [s["id"] for s in acc["samples"]] == ["A1", "A2", "A3", "A4", "A5"]


# 9–11. 顶层状态元数据
def test_9_11_status_metadata():
    acc = _acc()
    assert acc["status"] == "human_labels_approved"
    assert acc["used_for_development"] is False
    assert acc["reserved_for_future_independent_acceptance"] is False
    assert acc["independent_acceptance_consumed"] is True
    assert acc["reviewer"] == REVIEWER
    assert acc["human_reviewed_at"] == DATE
    assert acc["review_store"] == "eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl"
    recs = _human_reviews()
    order = {t: i for i, t in enumerate(("A1", "A2", "A3", "A4", "A5"))}
    corder = {"c1": 0, "c2": 1, "overall": 2}
    recs = sorted(recs, key=lambda r: (order[r["task_id"]],
                                       corder[r["check_id"]]))
    digest = _canonical([{k: v for k, v in r.items()
                          if k not in ("review_id", "appended",
                                       "duplicate_of_existing")}
                         for r in recs])
    assert acc["approval_record_hash"] == digest


# 12–16. grader registry 状态与晋升仍被阻止
def test_12_16_registry_states_and_promotion_blocked():
    v2 = greg.grader_state(greg.GRADER_V2)
    assert v2["labels_approved"] is True
    assert v2["real_calibration"] is False
    assert v2["calibration"] == "engineering_only"
    assert v2["allow_business_promotion"] is False
    with pytest.raises(greg.GraderRegistryError) as ei:
        greg.require_business_promotable(greg.GRADER_V2)
    msg = str(ei.value)
    assert "人工标签确认=True" in msg
    assert "真实校准=False" in msg
    assert "晋升被服务端阻止" in msg


# 17. 管理 API /meta 状态一致
def test_17_admin_meta_state(monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    with TestClient(app) as client:
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "a", "groups": ["__local_admin__"]}
        r = client.get("/api/evolution-admin/meta")
        assert r.status_code == 200
        data = r.json()
        v2 = next(d for d in data["datasets"]
                  if d["dataset_version"] == "wiki-default-v2dev")
        assert v2["labels_approved"] is True
        assert v2["real_calibration"] is False
        assert v2["grader_calibration"] == "engineering_only"
        assert v2["allow_business_promotion"] is False
        assert "人工标签批准" in data["grader_note"]


# 18. 不含秘密字段
def test_18_no_secrets_in_records_or_metadata():
    blob = (STORE.read_text(encoding="utf-8")
            + ACC.read_text(encoding="utf-8")).lower()
    for marker in SECRET_MARKERS:
        assert marker.lower() not in blob, marker
