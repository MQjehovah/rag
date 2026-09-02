"""检索 trace 记录（P4-BE-11，V3 计划 9.1）。

记录一次检索的完整生命周期：每路召回数量/候选、融合分数、重排结果、
最终引用。用于诊断召回损失、融合效果、ACL 误杀。

纯数据结构：RetrievalTrace，由 pipeline 填充，可序列化。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from app.core.retrieval.candidates import RetrievalCandidate


@dataclass
class TraceStage:
    """某一阶段的记录。"""
    name: str
    candidate_count: int = 0
    candidates: list[dict] = field(default_factory=list)  # 简化摘要：type/id/score


@dataclass
class RetrievalTrace:
    """一次检索的完整 trace。"""
    question: str = ""
    intent: str = ""
    stages: list[TraceStage] = field(default_factory=list)
    embedding_used: bool = False
    reranker_used: bool = False
    degraded: list[str] = field(default_factory=list)

    def record(self, name: str, candidates: list[RetrievalCandidate]) -> None:
        """记录一个阶段（每路召回 / 融合 / 重排 / 最终引用）。"""
        self.stages.append(TraceStage(
            name=name,
            candidate_count=len(candidates),
            candidates=[
                {
                    "candidate_type": c.candidate_type,
                    "candidate_id": c.candidate_id,
                    "fused_score": round(c.fused_score, 6) if c.fused_score else None,
                    "sparse_score": c.sparse_score,
                    "sources": c.sources,
                }
                for c in candidates
            ],
        ))

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """诊断摘要：各阶段候选数变化。"""
        return {
            "question": self.question,
            "intent": self.intent,
            "embedding_used": self.embedding_used,
            "reranker_used": self.reranker_used,
            "degraded": list(self.degraded),
            "stages": [
                {"name": s.name, "count": s.candidate_count}
                for s in self.stages
            ],
        }
