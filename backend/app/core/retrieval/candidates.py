"""统一检索候选模型与 Retriever 接口（P4-BE-01，V3 计划 9.2）。

统一候选模型 RetrievalCandidate：把不同来源（Chunk/CardBlock/Evidence/
Entity/Community）的召回结果归一为同一结构，供融合、重排、引用统一处理。

Retriever 接口：抽象多路召回，每路实现返回该路的候选列表 + 排名信息。
dense/sparse/graph 分数独立保存，融合阶段（fusion.py）再决定如何合并。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class CandidateType(str):
    CHUNK = "chunk"
    CARD_BLOCK = "card_block"
    EVIDENCE = "evidence"
    ENTITY = "entity"
    COMMUNITY = "community"


@dataclass
class RetrievalCandidate:
    """统一候选（V3 计划 9.2）。"""
    candidate_type: str                 # chunk/card_block/evidence/entity/community
    candidate_id: str                   # 候选在来源表中的 id
    content: str                        # 候选正文（供 judge/answer 使用）
    parent_card_id: str | None = None   # CardBlock 所属 card
    source_page_id: str | None = None   # 来源 page（ACL 过滤用）
    evidence_ids: list[str] = field(default_factory=list)
    acl_scope: str | None = None        # 可见范围（JSON 或 None=公开）

    # 各路独立分数（融合前保留原始值，供诊断 trace）
    dense_score: float | None = None
    sparse_score: float | None = None
    graph_score: float | None = None
    rerank_score: float | None = None

    # 融合后的最终分
    fused_score: float = 0.0
    # 命中该候选的路名（trace 用）
    sources: list[str] = field(default_factory=list)

    def with_source(self, source: str) -> "RetrievalCandidate":
        """记录命中路（链式）。"""
        if source not in self.sources:
            self.sources.append(source)
        return self


class Retriever(Protocol):
    """统一 Retriever 接口：一次查询返回该路的候选列表。

    每路实现负责自己的召回逻辑（dense/sparse/graph），返回带独立分数的
    RetrievalCandidate 列表。融合与 ACL 由 pipeline 统一处理，不在路内做。
    """

    name: str

    def retrieve(self, db, question: str, *, top_k: int = 20) -> list[RetrievalCandidate]:
        """执行召回，返回该路候选（已按本路分数降序）。"""
        ...
