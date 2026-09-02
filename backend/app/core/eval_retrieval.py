"""P4 检索评测 harness（V3 计划 9.4）。

加载评测集，跑检索，计算 Recall@10 / 引用准确率。

指标定义：
- Recall@10：answerable 用例中，检索 top-10 命中期望（关键词或来源片段）的比例
- Citation 准确率：命中的用例中，来源正确匹配的比例

检索函数依赖注入，当前用 BM25 路（外部 Dense 服务恢复后接入完整多路）。
纯函数 + 依赖注入，不依赖具体检索实现。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.retrieval.candidates import RetrievalCandidate

logger = logging.getLogger(__name__)

# 检索函数：(question) -> list[RetrievalCandidate]
RetrieveFn = Callable[[str], list[RetrievalCandidate]]
# page 标题解析：(source_page_id) -> str
PageTitleResolver = Callable[[str], str]


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    answerable: bool
    keyword_hit: bool
    source_hit: bool
    retrieved_count: int
    top_contents: list[str]
    source_required: bool = False

    @property
    def recall_hit(self) -> bool:
        """Prefer the expected source id/title; use keywords only without labels."""
        hit = self.source_hit if self.source_required else self.keyword_hit
        return self.answerable and hit


@dataclass
class EvalReport:
    results: list[EvalCaseResult]
    recall_at_10: float = 0.0
    citation_accuracy: float = 0.0

    def summary(self) -> dict:
        return {
            "total_cases": len(self.results),
            "answerable": sum(1 for r in self.results if r.answerable),
            "recall_hit": sum(1 for r in self.results if r.recall_hit),
            "recall_at_10": round(self.recall_at_10, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
        }


def load_eval_set(path: Path | str) -> dict:
    """加载评测集 JSON。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


def evaluate_cases(
    eval_set: dict,
    retrieve_fn: RetrieveFn,
    page_title_resolver: PageTitleResolver | None = None,
    top_k: int = 10,
) -> EvalReport:
    """跑评测集，返回报告。

    Args:
        eval_set: 评测集 dict（含 cases）。
        retrieve_fn: 检索函数，返回 RetrievalCandidate 列表。
        page_title_resolver: page_id → 标题（用于 source 匹配），缺省跳过 source 检查。
        top_k: 取前 N 个候选。
    """
    results: list[EvalCaseResult] = []
    for case in eval_set.get("cases", []):
        case_id = case["id"]
        category = case["category"]
        answerable = bool(case.get("answerable", True))
        keywords = case.get("expected_keywords") or []
        source_fragment = case.get("expected_source_fragment")

        try:
            candidates = retrieve_fn(case["question"])[:top_k]
        except Exception as exc:
            logger.warning(f"case {case_id} retrieve failed: {exc}")
            candidates = []

        contents = [c.content or "" for c in candidates]
        # 关键词命中：任一关键词出现在任一候选内容
        keyword_hit = any(
            kw.lower() in " ".join(contents).lower()
            for kw in keywords
        ) if keywords else False

        # 来源命中：任一候选的来源页标题含 source_fragment
        source_hit = False
        if source_fragment and page_title_resolver:
            for cand in candidates:
                if not cand.source_page_id:
                    continue
                title = page_title_resolver(cand.source_page_id)
                if source_fragment.lower() in (title or "").lower():
                    source_hit = True
                    break

        results.append(EvalCaseResult(
            case_id=case_id,
            category=category,
            answerable=answerable,
            keyword_hit=keyword_hit,
            source_hit=source_hit,
            retrieved_count=len(candidates),
            top_contents=contents[:3],
            source_required=bool(source_fragment and page_title_resolver),
        ))

    # 计算指标
    answerable = [r for r in results if r.answerable]
    recall_hit = [r for r in answerable if r.recall_hit]
    recall = len(recall_hit) / len(answerable) if answerable else 0.0

    # 引用准确率：命中（关键词）的用例中来源也命中的比例
    keyword_hits = [r for r in results if r.keyword_hit]
    source_among_hits = [r for r in keyword_hits if r.source_hit]
    citation = len(source_among_hits) / len(keyword_hits) if keyword_hits else 0.0

    return EvalReport(
        results=results,
        recall_at_10=recall,
        citation_accuracy=citation,
    )


def build_page_title_resolver(db) -> PageTitleResolver:
    """构造 page_id → 标题解析器（从 DB 加载 page 标题映射）。"""
    from sqlalchemy import text

    rows = db.execute(text("SELECT id, title FROM pages")).fetchall()
    mapping = {row[0]: row[1] for row in rows}
    return lambda pid: mapping.get(pid, "")
