"""语义评审结果契约与追加式审计存储（Grader v2 的 C 级判定通道）。

规则（保守）：
- 每条判定绑定 (task_id, 来源资料, 被评输出, 检查契约) 的内容哈希；
  任何内容改变后旧判定不可复用（resolver 只采用 bind 仍匹配的记录）。
- 追加式审计：reviews.jsonl 只追加不覆盖；历史结果永不改写/删除。
- 判定只能经本入口提交：human 判定必须携带人工 reviewer 标识（由调用方 CLI/界面
  显式传入），外部文本内容无法自行写入 human approved；
  model 判定必须携带 model_ref + prompt_version。
- 本模块只存判定与理由；判定不会流入执行者/提议者的私有答案通道
  （运行时角色从不读取本存储——另由测试断言确认）。

契约字段：check_id、verdict(pass|fail|needs_review)、reason、来源证据位置
source_evidence_loc、被评输出位置 candidate_loc、评审方式 review_method(human|model)、
review_version。
"""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from pathlib import Path
from typing import Any, Iterable

REVIEW_METHOD_HUMAN = "human"
REVIEW_METHOD_MODEL = "model"
ALLOWED_VERDICTS = ("pass", "fail", "needs_review")

BIND_FIELDS = ("task_hash", "source_hash", "candidate_hash", "contract_hash")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()


def bind_hashes(*, task_id: str, sources: list[str],
                candidate_output: dict, contract: dict) -> dict[str, str]:
    """由实际材料计算绑定哈希（复用判定前必须一致）。"""
    return {
        "task_hash": _sha256({"task_id": task_id}),
        "source_hash": _sha256(sources),
        "candidate_hash": _sha256(candidate_output),
        "contract_hash": _sha256(contract),
    }


def submit_review(
    *,
    store_path: Path,
    task_id: str,
    check_id: str,
    verdict: str,
    reason: str,
    source_evidence_loc: str,
    candidate_loc: str,
    method: str,
    review_version: str,
    reviewer: str | None = None,
    model_ref: str | None = None,
    prompt_version: str | None = None,
    sources: list[str],
    candidate_output: dict,
    contract: dict,
) -> dict:
    """校验并追加一条评审记录（幂等：同 bind+check_id 重复提交拒绝并返回已有）。"""
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError(f"非法 verdict: {verdict!r}")
    if not reason or not reason.strip():
        raise ValueError("reason 不能为空")
    if method not in (REVIEW_METHOD_HUMAN, REVIEW_METHOD_MODEL):
        raise ValueError(f"非法评审方式: {method!r}")
    if method == REVIEW_METHOD_HUMAN:
        if not reviewer or not reviewer.strip():
            raise ValueError("human 判定必须提供人工 reviewer 标识（不能由内容自批准）")
        if model_ref or prompt_version:
            raise ValueError("human 判定不得携带 model_ref/prompt_version")
        version = f"human@{reviewer}|{review_version}"
    else:
        if not model_ref or not prompt_version:
            raise ValueError("model 判定必须提供 model_ref 与 prompt_version")
        if reviewer:
            raise ValueError("model 判定不得携带 reviewer 人工字段")
        version = f"model:{model_ref}@{prompt_version}"
    binds = bind_hashes(task_id=task_id, sources=sources,
                        candidate_output=candidate_output, contract=contract)
    record = {
        "review_id": "rev_" + _uuid.uuid4().hex[:20],
        "task_id": task_id, "check_id": check_id,
        "verdict": verdict, "reason": reason,
        "source_evidence_loc": source_evidence_loc,
        "candidate_loc": candidate_loc,
        "method": method, "review_version": version,
        "model_ref": model_ref, "prompt_version": prompt_version,
        "reviewer": reviewer,
        **binds,
        "appended": True,
    }
    store_path = Path(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load(store_path)
    for rec in existing:
        if rec["check_id"] == check_id and all(
                rec[k] == binds[k] for k in BIND_FIELDS):
            if rec.get("verdict") != verdict:
                # 同一材料绑定 + check_id 已存在不同 verdict：fail-closed，
                # 不得静默返回旧记录（人工/模型判定冲突必须显式暴露）。
                raise ValueError(
                    "冲突判定：同一材料绑定与 check_id 已存在不同 verdict"
                    "（fail-closed，拒绝覆盖/静默复用）")
            return {**rec, "duplicate_of_existing": True}
    with store_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _load(store_path: Path) -> list[dict]:
    store_path = Path(store_path)
    if not store_path.is_file():
        return []
    out = []
    for line in store_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 截断行忽略，不破坏追加式日志
    return out


def load_reviews(store_path: Path) -> list[dict]:
    return _load(Path(store_path))


def usable_reviews(reviews: Iterable[dict], *, task_id: str, sources: list[str],
                   candidate_output: dict, contract: dict) -> list[dict]:
    """只返回绑定哈希与当前材料一致的判定（内容改变 → 旧判定不可复用）。"""
    binds = bind_hashes(task_id=task_id, sources=sources,
                        candidate_output=candidate_output, contract=contract)
    out = []
    for rec in reviews:
        if rec.get("task_id") != task_id:
            continue
        if all(rec.get(k) == binds[k] for k in BIND_FIELDS):
            out.append(rec)
    return out


def to_resolutions(usable: Iterable[dict]) -> dict[str, str]:
    """usable 判定 → {check_id: verdict}；供 grader_v2.evaluate(resolutions=…)。"""
    out: dict[str, str] = {}
    for rec in usable:
        out[rec["check_id"]] = rec["verdict"]
    return out
