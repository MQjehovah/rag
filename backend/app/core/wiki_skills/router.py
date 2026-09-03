"""Phase 6：Auto Skill Router（确定性信号 + LLM 的自主 Skill 选择）。

Topic Router 与 Skill Router 职责分离：
- Topic Router：判断资料是否值得进入 Wiki / 创建或更新哪篇 / 决定主题。
- Skill Router：判断目标 Wiki 内部使用哪一种内容结构（本文件）。
- Content Skill：决定提取字段 / 生成 IR / 规划 Section / 验证内容。

规则（固定顺序）：
1. 读 Registry 不可变 snapshot；2. 提取确定性信号；3. 过滤候选（default 恒兜底）；
4. 读当前 Wiki skill/version/locked；5. locked → locked；6. 生产只有 default →
   default（不调 LLM）；7. 多候选且新 Wiki：确定性高置信直选；8. 无法确定 → LLM；
9. 严格解析 JSON；10. 高置信 → selected；11. 低置信/非法/超时/未知 → default_fallback；
12. 已有 Wiki 当前 Skill 有效：优势不足 margin → sticky；13. 明显更优 →
   migration_proposed（Phase 6 仍用当前）；14. 返回 SkillDecision。

LLM 请求只含：标题 / 有界摘要 / 确定性信号 / 候选 key+version+label+短 description /
当前 skill+version。不发送：instructions / schema / 完整正文 / ACL / Prompt 原文 /
Token / Secret / 内部路径 / Python runtime 信息。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable
from collections.abc import Mapping

from app.core.wiki_skills import registry as skill_registry
from app.core.wiki_skills.schemas import (
    SkillCandidate,
    SkillContext,
    SkillDecision,
)
from app.core.wiki_skills.signals import extract_signals, signal_weight

logger = logging.getLogger(__name__)


class SkillRouterError(RuntimeError):
    """Skill Router 受控异常（不应使 Wiki 编译失败）。"""


def _cfg(name: str):
    from app.config import settings

    return getattr(settings, name)


def _default_key() -> str:
    return _cfg("wiki_skill_default_key") or "default"


# LLM JSON 严格白名单（出现额外字段 → 安全 fallback）。
_LLM_ALLOWED_FIELDS = frozenset({"skill_key", "skill_version", "confidence", "reason_code"})
# Prompt 中候选 description 的有界长度。
_PROMPT_DESCRIPTION_LIMIT = 200


def _snapshot_has(snap, key: str, version: str) -> bool:
    """基于单一 snapshot 的精确存在性判定（不触碰全局 Registry）。"""
    info = snap.get(key)
    if not info:
        return False
    versions = info.get("versions") or {}
    return version in versions


def _build_prompt(
    context: SkillContext, signals, candidates: list[SkillCandidate], snap=None
) -> str:
    """构造 Skill Router LLM 请求（严格白名单内容 + 严格 JSON 输出指令）。

    候选信息只允许 key/version/label/有界 description（取自同一 snapshot）；
    不发送 instructions / schema / 完整正文 / ACL / Secret / Token / 内部路径。
    """
    summary_chars = int(_cfg("wiki_skill_router_summary_chars") or 4000)
    parts: list[str] = []
    parts.append("请从候选 Skill 中为以下 Wiki 内容选择最合适的内容结构 Skill。")
    parts.append("Wiki 标题：" + (context.title or "")[:512])
    summaries = []
    for s in context.source_summaries:
        summaries.append(str(s.get("summary", "")) if isinstance(s, Mapping) else "")
    blob = "\n".join(summaries)
    parts.append("内容摘要：" + (blob[:summary_chars] if len(blob) > summary_chars else blob))
    signal_text = ", ".join(
        f"{s.signal_type}={s.value[:64]}" for s in signals[: int(_cfg("wiki_skill_router_max_candidates") or 5) * 3]
    )
    parts.append("检测信号：" + (signal_text or "无"))
    parts.append("候选 Skill：")
    for c in candidates:
        line = f"- {c.skill_key} (version {c.skill_version})"
        if snap is not None:
            info = snap.get(c.skill_key)
            if info is not None:
                desc = (info.get("versions") or {}).get(c.skill_version)
                if isinstance(desc, Mapping):
                    label = str(desc.get("label") or "")
                    description = str(desc.get("description") or "")
                    extra = []
                    if label:
                        extra.append(f"label={label[:64]}")
                    if description:
                        extra.append(
                            "description=" + description[:_PROMPT_DESCRIPTION_LIMIT]
                        )
                    if extra:
                        line += " " + "; ".join(extra)
        parts.append(line)
    if context.current_skill:
        parts.append(f"当前 Skill：{context.current_skill} (version {context.current_version or ''})")
    else:
        parts.append("当前 Skill：无")
    parts.append(
        "只返回严格 JSON，禁止其它文本："
        '{"skill_key": "...", "skill_version": "...", "confidence": 0.0, "reason_code": "..."}'
    )
    return "\n".join(parts)


def _select_default(snapshot: Mapping) -> tuple[str, str] | None:
    """返回 default active (key, version)；缺失 → None（fail closed）。"""
    info = snapshot.get(_default_key())
    if not info:
        return None
    active = info.get("active_version")
    if active is None:
        return None
    return _default_key(), active


def _only_default_registered(snapshot: Mapping) -> bool:
    """生产只有 default 判定（其余 key 均未注册）。"""
    others = [k for k in snapshot.keys() if k != _default_key()]
    return not others and _default_key() in snapshot


def _filter_candidates(
    snapshot: Mapping,
    signal_types: set[str],
    max_candidates: int,
) -> list[SkillCandidate]:
    """候选过滤（排序确定，不依赖注册顺序；default 恒保留）。

    - 非 default 候选须命中声明信号；
    - default 缺失/无 active → fail closed（返回空，route 走 NO_CANDIDATES）；
    - 上限：max_candidates 最小按 1；上限 ==1 → 只保留 default；
      上限 >1 → 最多保留 (上限-1) 个非 default，再追加 default；
      最终候选总数不超过上限。
    """
    cap = max(int(max_candidates or 1), 1)
    non_default: list[SkillCandidate] = []
    default_candidate: SkillCandidate | None = None
    for key in sorted(snapshot.keys()):
        info = snapshot[key]
        active = info.get("active_version")
        if active is None:
            continue
        desc = (info.get("versions") or {}).get(active)
        if not desc:
            continue
        declared = set(desc.get("applicability_signals") or ())
        matched = sorted(declared & signal_types)
        is_default = key == _default_key()
        if not is_default and not matched:
            continue  # 未命中声明信号：排除（default 除外）
        strength = 0.0
        for st in matched:
            strength += signal_weight(st)
        if is_default:
            # default 声明 generic_text：命中即保留最低确定性分，恒可兜底。
            det_score = 0.2 if "generic_text" in matched else 0.1
        else:
            # 确定性分 = 命中信号平均强度（≥2 个高权重特异信号 → ~1.0）。
            det_score = strength / len(matched) if matched else 0.0
        cand = SkillCandidate(
            skill_key=key,
            skill_version=active,
            deterministic_score=det_score,
            combined_score=det_score,
            matched_signals=tuple(matched),
        )
        if is_default:
            default_candidate = cand
        else:
            non_default.append(cand)
    # default 不可用 → fail closed（不允许非 default 独裁兜底）。
    if default_candidate is None:
        return []
    non_default.sort(key=lambda c: (-c.combined_score, c.skill_key))
    if cap <= 1:
        return [default_candidate]
    return non_default[: cap - 1] + [default_candidate]


def _decision_not_applicable(context: SkillContext, reason_code: str) -> SkillDecision:
    return SkillDecision(
        target_key=context.target_key,
        wiki_page_id=context.wiki_page_id,
        selected_skill=None,
        selected_version=None,
        selected_by="auto",
        confidence=0.0,
        status="not_applicable",
        reason_code=reason_code,
        previous_skill=context.current_skill,
        previous_version=context.current_version,
        locked=context.skill_locked,
    )


def _decision_default(
    context: SkillContext,
    version: str,
    *,
    selected_by: str,
    status: str,
    reason_code: str,
    candidates: tuple[dict, ...] = (),
    confidence: float = 1.0,
) -> SkillDecision:
    return SkillDecision(
        target_key=context.target_key,
        wiki_page_id=context.wiki_page_id,
        selected_skill=_default_key(),
        selected_version=version,
        selected_by=selected_by,
        confidence=confidence,
        status=status,
        reason_code=reason_code,
        matched_signals=("generic_text",),
        candidates=candidates,
        previous_skill=context.current_skill,
        previous_version=context.current_version,
        locked=context.skill_locked,
    )


def _parse_llm_json(raw: Any) -> dict | None:
    """严格解析 LLM 返回。

    - dict 或 JSON 字符串均可；非法返回 None，禁止自由文本猜 JSON；
    - 只允许字段 {skill_key, skill_version, confidence, reason_code}；
    - 额外字段 / 缺失必要字段 / 类型错误 / 非法置信度 → None（安全 fallback）。
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not isinstance(data, dict):
        return None
    unknown = set(data.keys()) - _LLM_ALLOWED_FIELDS
    if unknown:
        return None
    if not isinstance(data.get("skill_key"), str) or not data["skill_key"].strip():
        return None
    if not isinstance(data.get("skill_version"), str) or not data["skill_version"].strip():
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None
    reason_code = data.get("reason_code")
    if reason_code is not None and not isinstance(reason_code, str):
        return None
    return {
        "skill_key": data["skill_key"].strip(),
        "skill_version": data["skill_version"].strip(),
        "confidence": confidence,
        "reason_code": (reason_code or "").strip(),
    }


def _llm_select(
    context: SkillContext,
    signals,
    candidates: list[SkillCandidate],
    llm_runner: Callable,
    snap: Mapping,
) -> SkillDecision:
    """LLM 高置信选择。

    LLM 只能选择本次 candidates 中存在的精确 (skill_key, skill_version)；
    即使 Skill 在全局 Registry 已注册，只要未进入本次候选 → 拒绝并 fallback
    default（不得用“全局已注册”代替“本次允许候选”）。
    任何异常/非法 → default_fallback（不得使编译失败）。
    """
    prompt = _build_prompt(context, signals, candidates, snap=snap)
    try:
        result = llm_runner(
            [{"role": "user", "content": prompt}],
            context="wiki-skill-route",
        )
    except TimeoutError:
        logger.warning("skill router llm timeout target=%s", context.target_key)
        return _llm_fallback(context, candidates, "LLM_TIMEOUT")
    except Exception:  # noqa: BLE001
        logger.warning("skill router llm error target=%s", context.target_key)
        return _llm_fallback(context, candidates, "LLM_ERROR")
    parsed = _parse_llm_json(result)
    if parsed is None:
        return _llm_fallback(context, candidates, "LLM_INVALID_RESPONSE")
    key, version = parsed["skill_key"], parsed["skill_version"]
    allowed = {(c.skill_key, c.skill_version) for c in candidates}
    if (key, version) not in allowed:
        # 严格候选边界：LLM 不得越过本次 candidates。
        if not _snapshot_has(snap, key, version):
            if key not in snap:
                return _llm_fallback(context, candidates, "LLM_UNKNOWN_SKILL")
            return _llm_fallback(context, candidates, "LLM_UNKNOWN_VERSION")
        return _llm_fallback(context, candidates, "LLM_OUT_OF_CANDIDATES")
    threshold = float(_cfg("wiki_skill_auto_threshold") or 0.80)
    confidence = parsed["confidence"]
    if confidence >= threshold:
        matched: tuple[str, ...] = ()
        for c in candidates:
            if c.skill_key == key:
                matched = c.matched_signals
                break
        return SkillDecision(
            target_key=context.target_key,
            wiki_page_id=context.wiki_page_id,
            selected_skill=key,
            selected_version=version,
            selected_by="auto",
            confidence=confidence,
            status="selected",
            reason_code=parsed["reason_code"] or "LLM_HIGH_CONFIDENCE",
            matched_signals=matched,
            candidates=tuple(c.to_dict() for c in candidates),
            previous_skill=context.current_skill,
            previous_version=context.current_version,
            locked=context.skill_locked,
        )
    # 低置信 → default fallback。
    return _llm_fallback(context, candidates, "LOW_LLM_CONFIDENCE")


def _llm_fallback(
    context: SkillContext, candidates: list[SkillCandidate], reason_code: str
) -> SkillDecision:
    """LLM 路径失败统一收敛 default_fallback（不选 unknown skill）。"""
    default_version = None
    for c in candidates:
        if c.skill_key == _default_key():
            default_version = c.skill_version
    if default_version is None:
        return _decision_not_applicable(context, "NO_DEFAULT_AVAILABLE")
    return _decision_default(
        context,
        default_version,
        selected_by="default_fallback",
        status="fallback",
        reason_code=reason_code,
        candidates=tuple(c.to_dict() for c in candidates),
        confidence=0.0,
    )


def route(
    context: SkillContext,
    *,
    snapshot: Mapping | None = None,
    llm_runner: Callable | None = None,
) -> SkillDecision:
    """执行固定顺序 Skill 路由，返回 SkillDecision（绝不因内部异常让编译失败）。

    一次路由只使用同一个 snapshot（调用方传入或本次读取一次 Registry），
    locked/sticky/migration/LLM 返回校验全部基于该 snapshot 与本次候选列表；
    不在路由过程中重新读取全局 Registry。
    """
    snap = snapshot if snapshot is not None else skill_registry.snapshot()
    signals = extract_signals(
        context.title,
        "\n".join(
            str(s.get("summary", "")) for s in context.source_summaries if isinstance(s, Mapping)
        ),
        context.content_kind,
    )
    signal_types = {s.signal_type for s in signals}
    max_candidates = int(_cfg("wiki_skill_router_max_candidates") or 5)
    candidates = _filter_candidates(snap, signal_types, max_candidates)
    if not candidates:
        return _decision_not_applicable(context, "NO_CANDIDATES")

    cur_key = context.current_skill
    cur_version = context.current_version
    # —— locked：强制保持锁定 Skill（基于本次 snapshot 精确解析）——
    if context.skill_locked:
        if cur_key and cur_version and _snapshot_has(snap, cur_key, cur_version):
            return SkillDecision(
                target_key=context.target_key,
                wiki_page_id=context.wiki_page_id,
                selected_skill=cur_key,
                selected_version=cur_version,
                selected_by="locked",
                confidence=1.0,
                status="locked",
                reason_code="SKILL_LOCKED",
                candidates=tuple(c.to_dict() for c in candidates),
                previous_skill=cur_key,
                previous_version=cur_version,
                locked=True,
            )
        default_ver = _select_default(snap)
        if default_ver is None:
            return _decision_not_applicable(context, "NO_DEFAULT_AVAILABLE")
        return _decision_default(
            context, default_ver[1],
            selected_by="default_fallback", status="fallback",
            reason_code="LOCKED_SKILL_MISSING",
            candidates=tuple(c.to_dict() for c in candidates),
        )

    # —— 生产只有 default：直接选 default，不调 LLM ——
    if _only_default_registered(snap):
        default_ver = _select_default(snap)
        if default_ver is None:
            return _decision_not_applicable(context, "NO_DEFAULT_AVAILABLE")
        # 已有 Wiki 精确沿用当前 default 版本（不自动升级）。
        if cur_key == _default_key() and cur_version and \
                _snapshot_has(snap, cur_key, cur_version):
            return SkillDecision(
                target_key=context.target_key,
                wiki_page_id=context.wiki_page_id,
                selected_skill=cur_key,
                selected_version=cur_version,
                selected_by="sticky",
                confidence=1.0,
                status="sticky",
                reason_code="SKILL_STICKY_CURRENT",
                candidates=tuple(c.to_dict() for c in candidates),
                previous_skill=cur_key,
                previous_version=cur_version,
                locked=False,
            )
        return _decision_default(
            context, default_ver[1],
            selected_by="default_fallback", status="selected",
            reason_code="ONLY_DEFAULT_AVAILABLE",
            candidates=tuple(c.to_dict() for c in candidates),
        )

    # —— 已有 Wiki 当前 Skill 在 snapshot 中存在：黏性/迁移建议 ——
    if cur_key and cur_version and _snapshot_has(snap, cur_key, cur_version):
        best = candidates[0] if candidates else None
        current_matched = next(
            (c for c in candidates if c.skill_key == cur_key and c.skill_version == cur_version),
            None,
        )
        # 当前精确版本未进入本次候选 → 当前匹配分按 0 处理。
        current_score = (
            current_matched.combined_score if current_matched is not None else 0.0
        )
        if best is not None and current_matched is not None and \
                best.skill_key == cur_key and best.skill_version == cur_version:
            # 当前 Skill 即最优候选 → 沿用（sticky）。
            return SkillDecision(
                target_key=context.target_key,
                wiki_page_id=context.wiki_page_id,
                selected_skill=cur_key,
                selected_version=cur_version,
                selected_by="sticky",
                confidence=1.0,
                status="sticky",
                reason_code="SKILL_STICKY_CURRENT",
                candidates=tuple(c.to_dict() for c in candidates),
                previous_skill=cur_key,
                previous_version=cur_version,
                locked=False,
            )
        margin = float(_cfg("wiki_skill_switch_margin") or 0.15)
        if best is not None and best.combined_score - current_score >= margin:
            # 新候选（相对当前 0 分或命中分）明显更优 → migration_proposed
            # （Phase 6 仍沿用当前 Skill，不实际切换）。
            return SkillDecision(
                target_key=context.target_key,
                wiki_page_id=context.wiki_page_id,
                selected_skill=cur_key,
                selected_version=cur_version,
                selected_by="sticky",
                confidence=current_score,
                status="migration_proposed",
                reason_code="MIGRATION_PROPOSED",
                candidates=tuple(c.to_dict() for c in candidates),
                previous_skill=cur_key,
                previous_version=cur_version,
                locked=False,
            )
        # 优势不足 → 保持当前（sticky）。
        return SkillDecision(
            target_key=context.target_key,
            wiki_page_id=context.wiki_page_id,
            selected_skill=cur_key,
            selected_version=cur_version,
            selected_by="sticky",
            confidence=current_score,
            status="sticky",
            reason_code="SKILL_STICKY_MARGIN",
            candidates=tuple(c.to_dict() for c in candidates),
            previous_skill=cur_key,
            previous_version=cur_version,
            locked=False,
        )

    # —— 新 Wiki（无当前 Skill）：确定性高置信直选；否则 LLM ——
    threshold = float(_cfg("wiki_skill_auto_threshold") or 0.80)
    best = candidates[0] if candidates else None
    # 排除 default 的确定性直选（default 只作兜底，不靠确定性高分替代真候选判定，
    # 除非只 default —— 上面已早退）。
    real_best = next((c for c in candidates if c.skill_key != _default_key()), None)
    if real_best is not None and real_best.combined_score >= threshold:
        return SkillDecision(
            target_key=context.target_key,
            wiki_page_id=context.wiki_page_id,
            selected_skill=real_best.skill_key,
            selected_version=real_best.skill_version,
            selected_by="auto",
            confidence=real_best.combined_score,
            status="selected",
            reason_code="DETERMINISTIC_HIGH_CONFIDENCE",
            matched_signals=real_best.matched_signals,
            candidates=tuple(c.to_dict() for c in candidates),
            previous_skill=cur_key,
            previous_version=cur_version,
            locked=False,
        )
    if llm_runner is None:
        # 无 LLM 可用（测试未注入 / 生产降级）→ 安全 fallback default。
        default_ver = _select_default(snap)
        if default_ver is None:
            return _decision_not_applicable(context, "NO_DEFAULT_AVAILABLE")
        return _decision_default(
            context, default_ver[1],
            selected_by="default_fallback", status="fallback",
            reason_code="NO_LLM_ROUTER",
            candidates=tuple(c.to_dict() for c in candidates),
        )
    return _llm_select(context, signals, candidates, llm_runner, snap)
