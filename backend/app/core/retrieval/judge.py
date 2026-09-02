"""知识审判层（W7 T7.1/T7.2）。

回答前对候选知识卡片 做 5 项检查：时效 / 范围 / 冲突 / 权威 / 证据。
审判不通过时，必须显式报告"为什么不能确定答案"。

全部检查器为纯函数（输入 list[RetrievedKnowledge] + user_context，不碰 DB），便于测试。
authority 依赖 RetrievedKnowledge.source_mime_type（检索时已 JOIN pages 取到）。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from app.core.retrieval.sources import RetrievedKnowledge

# authority 等级：rank 越高越权威。
AUTHORITY_RANK: dict[str, int] = {
    "application/pdf": 5,
    "text/markdown": 4,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 3,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 2,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": 1,
    "text/plain": 1,
}
# rank < 该值视为低权威（ppt/xlsx/txt）。
LOW_AUTHORITY_MAX = 3

# 需要证据支撑的知识卡片类型。
EVIDENCE_REQUIRED_TYPES = {"procedure", "diagnostic", "rule"}

# 冲突判定：同 title+type 分组内，body 相似度低于该阈值视为冲突。
CONFLICT_SIM_THRESHOLD = 0.5
MIN_RESULTS_FOR_CONFLICT = 2


def _normalize(text: str) -> str:
    """归一化：去空白、小写、去标点，用于标题/正文比较。"""
    return re.sub(r"[^\w一-鿿]", "", (text or "").lower())


def _bigrams(text: str) -> set:
    """归一化文本的相邻字符 bigram 集合。对局部差异（如数字 530/535）敏感。"""
    norm = _normalize(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _jaccard(a: str, b: str) -> float:
    """基于字符 bigram 集合的 Jaccard 相似度。"""
    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _numbers(text: str) -> set:
    """提取正文中的数字 token（含小数）。用于检测数值差异（530 vs 535）。"""
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


class KnowledgeJudge:
    """知识审判器。judge() 返回 {passed, checks, advice, reasons}。"""

    def judge(self, results: list[RetrievedKnowledge], user_context: Optional[dict] = None) -> dict:
        if not results:
            # 空候选 = 无答案，判不通过，使该查询进入债务识别（W12 修复）
            return {
                "passed": False,
                "checks": {},
                "advice": "",
                "reasons": ["未检索到可用的已发布知识卡片"],
                "gap": derive_gap({
                    "passed": False,
                    "reasons": ["未检索到可用的已发布知识卡片"],
                    "checks": {},
                }, results),
            }
        checks = {
            "freshness": self._check_freshness(results),
            "scope": self._check_scope(results, user_context),
            "conflict": self._check_conflict(results),
            "authority": self._check_authority(results),
            "evidence": self._check_evidence(results),
        }
        passed = all(c["passed"] for c in checks.values())
        reasons = [c["detail"] for c in checks.values() if not c["passed"]]
        return {
            "passed": passed,
            "checks": checks,
            "advice": self._build_advice(results),
            "reasons": reasons,
            "gap": derive_gap({"passed": passed, "reasons": reasons, "checks": checks}, results),
        }

    # ---------- 5 个检查器 ----------

    def _check_freshness(self, results: list[RetrievedKnowledge]) -> dict:
        """时效：卡片是否在 valid_to 之前；是否被 superseded。"""
        if not results:
            return {"passed": True, "detail": "无候选知识卡片"}
        now = datetime.now()
        expired = [k for k in results if k.valid_to and self._parse_dt(k.valid_to) < now]
        if expired:
            ids = ", ".join(k.card_id[:8] for k in expired)
            return {"passed": False, "detail": f"存在已过期知识卡片（valid_to 已过）：{ids}"}
        superseded = [k for k in results if k.superseded_by]
        if superseded:
            ids = ", ".join(k.card_id[:8] for k in superseded)
            return {"passed": False, "detail": f"存在已被替代的知识卡片（superseded）：{ids}"}
        return {"passed": True, "detail": "所有知识卡片均在有效期内"}

    def _check_scope(self, results: list[RetrievedKnowledge], user_context: Optional[dict]) -> dict:
        """范围：用户产品/版本是否在卡片 scope 内。空 scope 视为通配。"""
        if not user_context:
            return {"passed": True, "detail": "未提供用户上下文，跳过范围检查"}
        product = (user_context.get("product") or "").strip()
        version = (user_context.get("version") or "").strip()
        if not product and not version:
            return {"passed": True, "detail": "用户上下文无产品/版本，跳过范围检查"}

        mismatches = []
        for k in results:
            scope = k.scope or {}
            if product and scope.get("product") and scope["product"] != product:
                mismatches.append(k.card_id[:8])
                continue
            if version and scope.get("version") and scope["version"] != version:
                mismatches.append(k.card_id[:8])
        if mismatches:
            return {"passed": False, "detail": f"存在 scope 不匹配的知识卡片：{', '.join(mismatches)}"}
        return {"passed": True, "detail": "所有知识卡片 scope 匹配"}

    def _check_conflict(self, results: list[RetrievedKnowledge]) -> dict:
        """冲突：同 title+type 分组内 body 相似度低 → 结论冲突。"""
        if len(results) < MIN_RESULTS_FOR_CONFLICT:
            return {"passed": True, "detail": "候选不足 2 个，无法判冲突"}
        groups: dict[tuple, list[RetrievedKnowledge]] = {}
        for k in results:
            key = (_normalize(k.title), k.type)
            groups.setdefault(key, []).append(k)
        conflicts = []
        for key, members in groups.items():
            if len(members) < MIN_RESULTS_FOR_CONFLICT:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    # 数值 token 差异是最强冲突信号（驱动 530 vs 535）
                    num_a, num_b = _numbers(a.body), _numbers(b.body)
                    if num_a and num_b and num_a != num_b:
                        conflicts.append((a.card_id, b.card_id))
                        continue
                    sim = _jaccard(a.body, b.body)
                    if sim < CONFLICT_SIM_THRESHOLD:
                        conflicts.append((a.card_id, b.card_id))
        if conflicts:
            pairs = "; ".join(f"{a[:8]} vs {b[:8]}" for a, b in conflicts[:3])
            return {"passed": False, "detail": f"发现 {len(conflicts)} 组结论冲突：{pairs}"}
        return {"passed": True, "detail": "无结论冲突"}

    def _check_authority(self, results: list[RetrievedKnowledge]) -> dict:
        """权威：来源文档类型 rank。低权威（ppt/xlsx/txt）判不过。"""
        if not results:
            return {"passed": True, "detail": "无候选知识卡片"}
        low = [
            k for k in results
            if k.source_mime_type
            and AUTHORITY_RANK.get(k.source_mime_type, 0) < LOW_AUTHORITY_MAX
        ]
        if low:
            types = {k.source_mime_type for k in low}
            return {"passed": False, "detail": f"存在低权威来源知识卡片（{', '.join(t for t in types if t)}）"}
        return {"passed": True, "detail": "所有来源文档权威等级足够"}

    def _check_evidence(self, results: list[RetrievedKnowledge]) -> dict:
        """证据：需证据类型（procedure/diagnostic/rule）必须有关联 evidence。"""
        if not results:
            return {"passed": True, "detail": "无候选知识卡片"}
        missing = [
            k for k in results
            if k.type in EVIDENCE_REQUIRED_TYPES and not (k.evidence_ids or [])
        ]
        if missing:
            ids = ", ".join(k.card_id[:8] for k in missing[:5])
            return {"passed": False, "detail": f"存在缺证据的知识卡片（{', '.join(sorted({k.type for k in missing}))}）：{ids}"}
        return {"passed": True, "detail": "所有知识卡片均有证据支撑或无需证据"}

    # ---------- 辅助 ----------

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return datetime.min

    @staticmethod
    def _build_advice(results: list[RetrievedKnowledge]) -> str:
        """拒答模板（T7.2）。返回空串表示无需拒答。"""
        groups: dict[tuple, list[RetrievedKnowledge]] = {}
        for k in results:
            key = (_normalize(k.title), k.type)
            groups.setdefault(key, []).append(k)
        conflict_members = [
            members for members in groups.values()
            if len(members) >= MIN_RESULTS_FOR_CONFLICT
        ]
        if not conflict_members:
            return ""
        members = conflict_members[0]
        parts = [
            f"[卡片 #{m.card_id[:8]} 来自《{m.title or '未知来源'}》说 {m.body[:40]}...]"
            for m in members[:2]
        ]
        return (
            f"当前发现 {len(members)} 份知识存在冲突："
            + "、".join(parts)
            + "，建议由平台确认后更新。"
        )


def derive_gap(judgment: dict, results: list[RetrievedKnowledge] | None = None) -> dict | None:
    """从 judge 结果推导结构化 gap（P4-BE-09）。

    返回 None = 无明确缺口（passed 或缺口不可补充检索定位）。
    返回 dict = {gap_type, reason, query_hint, supplement}：
    - supplement=True 表示值得用 query_hint 做一次补充检索（P4-BE-10）。
    - supplement=False 表示缺口不是检索能解决的（如 scope 不匹配是用户上下文问题）。

    映射：
    - 空候选 → missing_knowledge（换更宽泛查询词）
    - evidence 检查不过 → missing_evidence（找证据来源）
    - conflict 检查不过 → conflict（找更多同主题确认）
    - freshness 检查不过 → outdated（找替代/更新版本）
    - scope 检查不过 → scope_mismatch（非检索可解决，supplement=False）
    - authority 检查不过 → 无 gap（来源权威问题，不是知识缺口）
    """
    results = results or []
    if judgment.get("passed"):
        return None
    if not results:
        return {
            "gap_type": "missing_knowledge",
            "reason": "未检索到可用的已发布知识卡片",
            "query_hint": "",  # 由 supplement 用原始问题换词构造
            "supplement": True,
        }

    checks = judgment.get("checks") or {}
    # 优先级：evidence > conflict > freshness > scope（更具体/更可补充的优先）
    if not checks.get("evidence", {}).get("passed", True):
        return {
            "gap_type": "missing_evidence",
            "reason": checks["evidence"].get("detail", "缺证据"),
            "query_hint": "证据 来源 依据",
            "supplement": True,
        }
    if not checks.get("conflict", {}).get("passed", True):
        return {
            "gap_type": "conflict",
            "reason": checks["conflict"].get("detail", "结论冲突"),
            "query_hint": "对比 差异 版本",
            "supplement": True,
        }
    if not checks.get("freshness", {}).get("passed", True):
        return {
            "gap_type": "outdated",
            "reason": checks["freshness"].get("detail", "知识过期"),
            "query_hint": "最新 更新 版本",
            "supplement": True,
        }
    if not checks.get("scope", {}).get("passed", True):
        return {
            "gap_type": "scope_mismatch",
            "reason": checks["scope"].get("detail", "scope 不匹配"),
            "query_hint": "",
            "supplement": False,  # 用户上下文问题，补充检索无益
        }
    return None


def build_supplement_query(original_question: str, gap: dict) -> str:
    """根据 gap 构造一次补充查询（P4-BE-09 后半）。"""
    query_hint = gap.get("query_hint") or ""
    if not query_hint:
        return original_question
    # 去掉原问题里的具体型号/编号，避免过度约束补充检索
    import re as _re
    widened = _re.sub(r"\b(Titan\s*\d{3,}|T-\d{3,}|[A-Z]{2,}\d+)\b", "", original_question, flags=_re.IGNORECASE)
    widened = _re.sub(r"\s+", " ", widened).strip()
    base = widened or original_question
    return f"{base} {query_hint}".strip()
