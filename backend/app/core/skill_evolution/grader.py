"""阶段 1 最小确定性评分器（wiki-default-grader/v1）。

输入（分离）：
- execution 结果：candidate（published revision + sections）与 classify 结果；
- 评分参考：只允许评分器读取（reference 文件绝不进入模型消息/runner）；
- 任务来源快照（documents）。

检查维度（全部确定性程序化，无 LLM）：
- 编译状态与发布：run 成功且存在 published revision（compile 硬性检查）；
- 必需章节/要点：expected_points.phrase / value（在正文中出现）；
- 版本归属：value_in_version（值必须出现在对应 version_label 区段）、
  absent_in_version（其它版本的值不得混入本版本区段）、
  diff_notice（同版本来源冲突必须显式标注）；
- 禁止内容：reference.forbidden 中任何文本不得出现在候选任何 Section。

输出：逐项结果 + 任务级结论（verdict=pass/fail + failure_kind）。
不产出“未经校准的加权综合分”：本版只给逐项通过/失败，不做数值加权。

grader_version 固定为 wiki-default-grader/v1；结构非法/未知 kind → GraderConfigError。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.skill_evolution.adapter import classify_run
from app.core.skill_evolution.contracts import load_reference
from app.core.skill_evolution.errors import GraderConfigError

GRADER_VERSION = "wiki-default-grader/v1"

VALID_KINDS = (
    "phrase",
    "value",
    "value_in_version",
    "absent_in_version",
    "diff_notice",
)


def _body_parts(candidate: dict) -> list[dict]:
    """所有 facts 区段 + 摘要，供整体扫描。"""
    parts = []
    for sec in candidate.get("sections") or []:
        if sec.get("section_type") == "summary":
            parts.append({"label": "summary", "content": sec.get("content") or ""})
        else:
            parts.append({
                "label": sec.get("version_label") or "unversioned",
                "content": sec.get("content") or "",
                "diff_notice": sec.get("diff_notice") or "",
                "is_facts": True,
            })
    return parts


def _section_by_version(candidate: dict, version: str) -> dict | None:
    """定位指定版本（含 common/unversioned）的 facts 区段。"""
    for sec in candidate.get("sections") or []:
        if sec.get("section_type") == "summary":
            continue
        label = sec.get("version_label") or "unversioned"
        if label == version:
            return sec
    return None


def _check_point(candidate: dict, point: dict) -> dict:
    kind = point.get("kind")
    if kind not in VALID_KINDS:
        raise GraderConfigError(f"expected_point 未知 kind: {kind!r}")
    point_id = str(point.get("id") or "")
    if not point_id:
        raise GraderConfigError("expected_point 缺少 id")
    parts = _body_parts(candidate)
    all_text = "\n".join(p["content"] for p in parts)

    if kind == "phrase":
        text = point.get("text")
        if not text:
            raise GraderConfigError(f"point {point_id}: phrase 需要 text")
        found = text in all_text
        return {"id": point_id, "kind": kind, "passed": found,
                "detail": "正文包含要点文本" if found else f"正文缺少要点文本: {text}"}
    if kind == "value":
        value = point.get("value")
        if not value:
            raise GraderConfigError(f"point {point_id}: value 需要 value")
        found = value in all_text
        return {"id": point_id, "kind": kind, "passed": found,
                "detail": "关键值出现在正文" if found else f"关键值缺失: {value}"}
    if kind == "value_in_version":
        value = point.get("value")
        version = point.get("version")
        if not value or not version:
            raise GraderConfigError(f"point {point_id}: 需要 value 与 version")
        sec = _section_by_version(candidate, version)
        if sec is None:
            return {"id": point_id, "kind": kind, "passed": False,
                    "detail": f"缺少版本区段: {version}"}
        found = value in (sec.get("content") or "")
        detail = (f"值 {value} 归属版本 {version}"
                  if found else f"值 {value} 未出现在版本 {version} 区段")
        return {"id": point_id, "kind": kind, "passed": found, "detail": detail}
    if kind == "absent_in_version":
        value = point.get("value")
        version = point.get("version")
        if not value or not version:
            raise GraderConfigError(f"point {point_id}: 需要 value 与 version")
        sec = _section_by_version(candidate, version)
        if sec is None:
            return {"id": point_id, "kind": kind, "passed": False,
                    "detail": f"缺少版本区段: {version}"}
        leaked = value in (sec.get("content") or "")
        detail = (f"值 {value} 未混入版本 {version}（归属干净）"
                  if not leaked else f"值 {value} 混入了版本 {version} 区段（跨版本混合）")
        return {"id": point_id, "kind": kind, "passed": not leaked, "detail": detail}
    if kind == "diff_notice":
        version = point.get("version")
        if not version:
            raise GraderConfigError(f"point {point_id}: diff_notice 需要 version")
        sec = _section_by_version(candidate, version)
        if sec is None:
            return {"id": point_id, "kind": kind, "passed": False,
                    "detail": f"缺少版本区段: {version}"}
        has_notice = bool(sec.get("diff_notice"))
        return {"id": point_id, "kind": kind, "passed": has_notice,
                "detail": ("版本差异提示已标注" if has_notice
                           else f"版本 {version} 存在来源差异但未标注 diff_notice")}
    raise GraderConfigError(f"point {point_id}: 未覆盖 kind {kind}")  # pragma: no cover


def _check_forbidden(candidate: dict, forbidden: list[str]) -> list[dict]:
    out: list[dict] = []
    for text in forbidden:
        if not text:
            continue
        hits = []
        for part in _body_parts(candidate):
            if text in part["content"]:
                hits.append(part["label"])
        out.append({
            "id": f"forbidden:{abs(hash(text)) & 0xffffffff:08x}",
            "kind": "forbidden",
            "passed": not hits,
            "detail": ("禁止内容未出现" if not hits
                       else f"出现禁止内容 {text!r} 于区段 {sorted(set(hits))}"),
        })
    return out


def grade(
    *,
    outcome: dict,
    candidate: dict,
    reference_ref: str,
    dataset_dir: Path,
    task_id: str,
) -> dict:
    """评分单任务；返回逐项 + 任务级结论。reference 仅在此函数内读取。"""
    reference = load_reference(dataset_dir, reference_ref)
    task_ref = reference.get(task_id)
    if task_ref is None:
        raise GraderConfigError(f"reference 缺少 task {task_id}（ref={reference_ref}）")
    expected_points = task_ref.get("expected_points") or []
    if not isinstance(expected_points, list):
        raise GraderConfigError(f"task {task_id}: expected_points 必须是列表")
    forbidden = task_ref.get("forbidden") or []
    if not isinstance(forbidden, list):
        raise GraderConfigError(f"task {task_id}: forbidden 必须是列表")

    item_results: list[dict] = []
    for point in expected_points:
        item_results.append(_check_point(candidate, point))
    item_results.extend(_check_forbidden(candidate, forbidden))

    compile_ok = bool(outcome.get("run_ok")) and bool(outcome.get("published"))
    checks_ok = all(r["passed"] for r in item_results)
    all_pass = compile_ok and checks_ok
    verdict = "pass" if all_pass else "fail"
    return {
        "grader_version": GRADER_VERSION,
        "task_id": task_id,
        "verdict": verdict,
        "compile_ok": compile_ok,
        "checks_ok": checks_ok,
        "item_results": item_results,
        "outcome": outcome,
    }
