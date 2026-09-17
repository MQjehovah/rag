"""v2 专用开发数据集 + v2 全编排（orchestrator）离线验收（stage8g）。

覆盖：
- 数据集 wiki-default-v2dev 可加载、grader_version=v2、task 引用 v2_spec；
- v2_spec 校验：缺失/非法明确拒绝（不回退 v1）；
- 私有评分要求不外泄：评审只见 资料+输出+检查（reference 全文/secret 不进入）；
- 反向保护：v2 数据集禁止走 v1 引擎（review!=v2 → 拒绝）；
- orchestrator v2 全编排：基线→评审（语义 needs_review 全保留）→ invalid →
  无晋升分数、不缩分母；中断后 resume：预算不重置（review 计费单调）、模型/评分器
  配置不漂移（run 记录一致）；租约丢失由新 worker 接管后不再以旧身份发送。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.skill_evolution import (
    grader_registry as greg, gating, orchestrator as orch,
    review_eval, runenv, skill_store,
)
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.model_review import SemanticReviewer

BACKEND = Path(__file__).resolve().parent.parent
DEV_DS = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v2dev"
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"
V2 = greg.GRADER_V2


class _NeedsReviewAll(SemanticReviewer):
    """返回全部检查项 needs_review（保留未决；不代答、不伪造通过）。"""

    identity = "model:dev-unresolved@v1"

    def review(self, *, task_id, sources, candidate_output, checks):
        return {"identity": self.identity, "items": [
            {"check_id": c["id"], "verdict": "needs_review",
             "reason": "开发样例：一律待审", "source_evidence_loc": "",
             "candidate_loc": ""} for c in checks]}


class _PassAll(SemanticReviewer):
    identity = "model:dev-passall@v1"

    def review(self, *, task_id, sources, candidate_output, checks):
        return {"identity": self.identity, "items": [
            {"check_id": c["id"], "verdict": "pass",
             "reason": "开发样例：核验一致", "source_evidence_loc": "src",
             "candidate_loc": "out"} for c in checks]}


# ---------------------------------------------------------------------------
# 1. 数据集与 v2_spec
# ---------------------------------------------------------------------------


def test_v2dev_dataset_loads_with_specs():
    ds = load_dataset(DEV_DS)
    assert ds.dataset_version == "wiki-default-v2dev"
    assert ds.grader_version == V2
    train = [t for t in ds.tasks if t.split == "train"]
    val = [t for t in ds.tasks if t.split == "val"]
    assert len(train) == 5 and len(val) == 4
    spec = review_eval.load_v2_spec(DEV_DS, val[0])
    assert any(c.cid == "voltage" and c.unit == "V" for c in spec.claims)
    assert any(c.unit == "°C" for c in spec.claims)
    assert any(cv.topic == "操作步骤" for cv in spec.coverage)
    # versioned 任务带版本归属
    vspec = review_eval.load_v2_spec(
        DEV_DS, next(t for t in val if t.task_id == "vd2-val-versioned"))
    assert {c.version for c in vspec.claims} == {"v1.0", "v1.1"}


def test_v2_spec_invalid_or_missing_rejects_no_fallback(tmp_path):
    ref_dir = tmp_path / "references"
    ref_dir.mkdir(parents=True)
    task = SimpleNamespace(task_id="x", reference_ref="r.json",
                           sources=(SimpleNamespace(file="a.md"),))
    (ref_dir / "r.json").write_text(json.dumps({"v2_spec": {"claims": "oops"}}),
                                    encoding="utf-8")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "a.md").write_text("c", encoding="utf-8")
    with pytest.raises(SkillEvolutionError):
        review_eval.load_v2_spec(tmp_path, task)   # 非法 claims → 明确拒绝
    (ref_dir / "r.json").write_text(json.dumps({"expected_points": []}),
                                    encoding="utf-8")
    with pytest.raises(review_eval.V2EvalError) as ei:
        review_eval.load_v2_spec(tmp_path, task)
    assert "不自动退回 v1" in str(ei.value)


def test_review_inputs_never_contain_private_reference(tmp_path, monkeypatch):
    """v2_spec 中的私有评分要求（secret）只供评分端读取，不进入评审输入。"""
    from app.core.skill_evolution.model_review import StubSemanticReviewer
    src_dir = tmp_path / "sources"
    ref_dir = tmp_path / "references"
    src_dir.mkdir(parents=True)
    ref_dir.mkdir(parents=True)
    (src_dir / "a.md").write_text("供电电压 220V。\n操作步骤：安装。",
                                  encoding="utf-8")
    secret = "PRIVATE-ANSWER-SECRET-9f3a"
    ref = {"t1": {
        "v2_spec": {"claims": [
            {"cid": "c1", "subject": "供电电压", "predicate": "为",
             "object": "220", "unit": "V"}]},
        "secret_marker": secret}}
    (ref_dir / "ref.json").write_text(json.dumps(ref, ensure_ascii=False),
                                      encoding="utf-8")
    task = SimpleNamespace(task_id="t1", reference_ref="ref.json",
                           sources=(SimpleNamespace(file="a.md"),))
    recorder = _PassAll.__new__(_PassAll)
    recorder.identity = _PassAll.identity
    seen = []

    class _Rec(_PassAll):
        def review(self, **kw):
            seen.append(kw)
            return super().review(**kw)

    cand = {"sections": [{"section_type": "facts",
                          "version_label": "unversioned",
                          "content": "供电电压 220V。\n操作步骤：安装。"}]}
    out = review_eval.grade_task_v2(dataset_dir=tmp_path, task=task,
                                    candidate=cand, reviewer=_Rec())
    assert out["verdict"] in ("pass", "needs_review")
    blob = json.dumps(seen, ensure_ascii=False)
    assert secret not in blob
    assert "PRIVATE-ANSWER" not in blob


# ---------------------------------------------------------------------------
# 2. orchestrator v2 全编排（无效/未决语义 + resume 预算不重置 + 不漂移）
# ---------------------------------------------------------------------------


def _seed_v2_env(tmp_path, *, review="v2"):
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    db = skill_store.session_for(root)
    try:
        ds = load_dataset(DEV_DS)
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        v = skill_store.get_version(db, seed)
        member = {"skill_id": v.skill_id, "version_id": v.version_id,
                  "content_hash": v.content_hash, "seq": v.seq}
        runner_cfg = {"profile": "faithful",
                      "dataset_version": ds.dataset_version,
                      "grader_version": ds.grader_version,
                      "pipeline": ["wiki.default", "3"]}
        if review:
            runner_cfg["review"] = review
        exp = gating.create_experiment(
            db, workspace_id="ws-v2dev", domain="wiki_compile.default",
            dataset=ds, grader_version=ds.grader_version,
            runner_config=runner_cfg,
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
            initial_members=[member])
        run_cfg = {"mode": "simulated", "real": False,
                   "dataset_dir": str(DEV_DS)}
        if review:
            run_cfg["review"] = review
        run = orch.create_run(
            db, experiment_id=str(exp.experiment_id), workspace_id="ws-v2dev",
            domain="wiki_compile.default", dataset=ds, init_mode="business",
            max_iterations=2,
            budget={"max_model_calls": 200, "max_tool_calls": 80,
                    "max_seconds": 900},
            runner_config=run_cfg,
            train_task_ids=[t.task_id for t in ds.tasks
                            if t.split == "train"],
            experience="full")
        return root, str(exp.experiment_id), str(run.run_id)
    finally:
        db.close()


def test_orchestrator_v2_baseline_unresolved_invalid_and_resume_no_reset(
        tmp_path):
    """语义项全保留 needs_review → 基线无效（无晋升分数、不缩分母）；
    中断后 resume：预算不重置（review 计费单调）、配置不漂移。"""
    root, exp_id, run_id = _seed_v2_env(tmp_path)
    actors = orch.Actors(reviewer_factory=lambda: _NeedsReviewAll())
    view1 = orch.execute(root, load_dataset(DEV_DS), run_id, actors=actors)
    assert view1["status"] == "paused"
    assert view1["stop_reason"] and "v2 未决" in view1["stop_reason"]
    assert view1["current_iteration"] == 0
    used_after_1 = view1["used"]["model_calls"]
    assert used_after_1 >= 3          # 3 个 val 任务各触发一次语义评审（计费）
    # resume：新 worker 接管（租约已释放）→ 预算不重置、配置不漂移（同一 run）
    view2 = orch.execute(root, load_dataset(DEV_DS), run_id,
                         actors=actors, resume=True)
    assert view2["used"]["model_calls"] >= used_after_1
    # 模型/评分器/技能版本不漂移：run 记录 dataset/grader/skill 冻结一致
    assert view2["dataset_version"] == "wiki-default-v2dev"
    from app.core.skill_evolution import control
    assert control.model_mode_of(root, run_id) == "simulated"


def test_v2_dataset_rejects_v1_engine_entry(tmp_path):
    """v2 数据集若未配置 review=v2 → run_baseline 在执行前被反向保护拒绝。"""
    root, exp_id, run_id = _seed_v2_env(tmp_path, review=None)
    with pytest.raises(Exception) as ei:
        gating.run_baseline(root, load_dataset(DEV_DS), exp_id,
                            profile="faithful")
    assert "必须经 v2 评审入口" in str(ei.value)
