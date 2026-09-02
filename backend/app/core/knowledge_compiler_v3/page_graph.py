"""Page/Chunk/Evidence 驱动的实体关系图 + Community 聚类（V4 Phase D-3）。

替代旧 Card 驱动图谱（card_graph.py），不依赖 KnowledgeCard、KnowledgeCardBlock、
CardGraphRelation、CardEntityLink、KnowledgeObject / 旧 KO。

流程（V4 计划 4.1，有界 + 只读）：
    seed 命中 Chunk → 抽实体名 → 有界定位（含该实体名的 Chunk / active Evidence）
        → 实体关系图 → 单层 Louvain 聚类 → Community（纯内存）
        → 定位成员 Page/Chunk。

关键约束（封板）：
- **只读**：不调用 resolve_entity（其会 add/flush 写库）。实体 id 用确定性 key
  `entity_type:normalize_name(name)`，不新增 CanonicalEntity / EntityAlias。
- **有界**：不加载整个权限域全部 PageChunk；只加载与 seed 实体名匹配的 Chunk，
  top_k 在 SQL 层生效。
- **Evidence 真实接入**：active EvidenceItem（source_page_id 可见、content 非空）
  作为 page 级实体来源，帮助发现成员 Page（不直接产出 Chunk，扩展时从成员页取）。
- ACL：用规范化 key（company / admin / group:<组名>）严格隔离，None/__public__ 同属
  company；无法确定域 fail closed。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core import access_control
from app.core.knowledge_compiler_v3.entity_patterns import ENTITY_PATTERNS
from app.core.knowledge_compiler_v3.graph_clustering import (
    louvain_partition,
    _merge_and_split,
)
from app.core.knowledge_compiler_v3.relation_extractor import extract_relations_from_claim
from app.core.text_normalization import normalize_text
from app.models.database import EvidenceItem, Notebook, Page, PageChunk

logger = logging.getLogger(__name__)

LARGE_SPLIT_DEFAULT = 48


@dataclass
class PageCommunity:
    """单个语义 Community（Page/Chunk/Evidence 驱动，纯内存）。"""
    entity_ids: list[str]
    member_page_ids: list[str]
    member_chunk_ids: list[str]
    acl_scope: str  # company / admin / group:<组名>

    @property
    def fingerprint(self) -> tuple:
        """稳定排序 key（跨进程一致）。"""
        return (
            self.acl_scope,
            tuple(self.entity_ids),
            tuple(self.member_page_ids),
        )


def _extract_entities(text: str) -> list[tuple[str, str]]:
    """从文本抽实体 [(entity_type, name)]（NFKC 归一化后去重）。"""
    text = normalize_text(text or "")
    entities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity_type, patterns in ENTITY_PATTERNS:
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                name = m.group(0).strip()
                key = (entity_type, name)
                if not name or key in seen:
                    continue
                seen.add(key)
                entities.append(key)
    return entities


def normalize_name(name: str) -> str:
    """实体名字符串归一化（NFKC 小写去连字符/下划线/空白，确定性）。"""
    s = normalize_text(name or "").strip().lower()
    return re.sub(r"[-\s_]+", "", s)


def _entity_key(entity_type: str, name: str) -> str:
    """确定性只读实体 id（不写库）。"""
    return f"{entity_type}:{normalize_name(name)}"


def _scope_key(scope: access_control.AccessScope) -> str:
    """把 AccessScope 归一为 ACL 域 key，严格隔离（J-1 支持多组）。"""
    if scope.kind == "group":
        return "group:" + ",".join(sorted(scope.groups))
    return scope.kind  # company / admin


def _page_scope(db: Session, page_ids: set[str]) -> dict[str, str]:
    """page_id -> ACL 域 key（严格隔离，J-1 多组）。孤儿/无 notebook fail closed。"""
    if not page_ids:
        return {}
    rows = db.query(Page.id, Page.notebook_id).filter(Page.id.in_(page_ids)).all()
    nb_ids = {nb for _, nb in rows if nb}
    nb_scope: dict[str, access_control.AccessScope] = {}
    if nb_ids:
        for nid in nb_ids:
            notebook = db.get(Notebook, nid)
            if notebook is not None:
                nb_scope[nid] = access_control.scope_from_notebook(db, notebook)
    result: dict[str, str] = {}
    for pid, nb in rows:
        if not nb or nb not in nb_scope:
            continue  # 无法确定域 → fail closed
        scope = nb_scope[nb]
        if scope.kind == access_control.SCOPE_UNKNOWN:
            continue  # 无法确定域 → fail closed
        result[pid] = _scope_key(scope)
    return result


def _bounded_chunks(
    db: Session,
    visible_page_ids: set[str],
    names: set[str],
    seed_chunk_ids: set[str],
    top_k: int,
) -> list[PageChunk]:
    """有界定位：只加载含 seed 实体名的 Chunk（排除 seed chunk），top_k 在 SQL 层生效。

    确定性：names 遍历 sorted；SQL 在 limit 前 order_by（不依赖数据库默认顺序）。
    """
    out: dict[str, PageChunk] = {}
    for name in sorted(names):
        q = (
            db.query(PageChunk)
            .filter(PageChunk.page_id.in_(visible_page_ids))
        )
        if seed_chunk_ids:
            q = q.filter(~PageChunk.id.in_(seed_chunk_ids))
        q = (
            q.filter(PageChunk.content.contains(name))
            .order_by(PageChunk.page_id, PageChunk.chunk_index, PageChunk.id)
            .limit(top_k)
        )
        for c in q.all():
            out[c.id] = c
    return [out[cid] for cid in sorted(out)]


def _bounded_evidence(
    db: Session,
    visible_page_ids: set[str],
    names: set[str],
    top_k: int,
) -> dict[str, list[tuple[str, str]]]:
    """有界定位 active EvidenceItem：page 级实体来源（只读，确定性）。"""
    result: dict[str, list[tuple[str, str]]] = {}
    for name in sorted(names):
        rows = (
            db.query(EvidenceItem.source_page_id, EvidenceItem.content)
            .filter(
                EvidenceItem.status == "active",
                EvidenceItem.source_page_id.in_(visible_page_ids),
                EvidenceItem.content.contains(name),
            )
            .order_by(EvidenceItem.source_page_id, EvidenceItem.id)
            .limit(top_k)
            .all()
        )
        for source_page_id, content in rows:
            if not source_page_id:
                continue
            result.setdefault(source_page_id, []).extend(_extract_entities(content or ""))
    # 去重（保持稳定顺序）
    for pid in sorted(result):
        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for ent in result[pid]:
            if ent in seen:
                continue
            seen.add(ent)
            deduped.append(ent)
        result[pid] = deduped
    return result


def build_page_communities(
    db: Session,
    visible_page_ids: set[str],
    *,
    seed_hits: list | None = None,
    top_k: int = 20,
    algorithm: str = "louvain",
) -> list[PageCommunity]:
    """从可见 Page/Chunk/Evidence 构建语义 Community（纯内存、只读、有界）。

    seed_hits：问答主链传入 RawChunkHit 列表，据此做有界定位；None 时用于
    测试/预构建（此时退化为加载可见域内全部非空 chunk，仅限非问答路径）。
    """
    if not visible_page_ids:
        return []

    scope_of = _page_scope(db, visible_page_ids)
    if not scope_of:
        return []

    seed_items: list[tuple[str, str, str, str]] = []  # (chunk_id, page_id, content, scope)
    names: set[str] = set()
    if seed_hits:
        for h in seed_hits:
            scope = scope_of.get(h.page_id)
            if not scope:
                continue
            seed_items.append((h.chunk_id, h.page_id, h.content, scope))
            names.update(name for _etype, name in _extract_entities(h.content))

    seed_chunk_ids = {cid for cid, *_ in seed_items}

    if seed_hits is not None:
        # 有界：只加载与 seed 实体名匹配的 chunk（不加载整个权限域）
        matched_chunks = _bounded_chunks(db, visible_page_ids, names, seed_chunk_ids, top_k)
    else:
        # 预构建/测试路径：加载可见域全部非空 chunk（问答主链不进入此分支）
        matched_chunks = (
            db.query(PageChunk)
            .filter(PageChunk.page_id.in_(visible_page_ids))
            .order_by(PageChunk.page_id, PageChunk.chunk_index)
            .all()
        )
        matched_chunks = [c for c in matched_chunks if (c.content or "").strip()]

    # 有界 Evidence：page 级实体来源（真实接入，只读）
    evidence_by_page = _bounded_evidence(db, visible_page_ids, names, top_k) if names else {}

    # 统一 chunk 项：(chunk_id, page_id, content, scope)
    chunk_items: list[tuple[str, str, str, str]] = list(seed_items)
    for c in matched_chunks:
        scope = scope_of.get(c.page_id)
        if not scope:
            continue
        chunk_items.append((c.id, c.page_id, c.content, scope))

    # 聚类数据结构（只读，确定性实体 key）
    scope_entities: dict[str, dict[str, dict]] = {}
    scope_edges: dict[str, dict[tuple[str, str], float]] = {}

    for chunk_id, page_id, content, scope in chunk_items:
        ents = _extract_entities(content)
        if not ents:
            continue
        entity_keys = [_entity_key(et, name) for et, name in ents]
        for ekey in entity_keys:
            bucket = scope_entities.setdefault(scope, {}).setdefault(ekey, {
                "entity_id": ekey,
                "pages": set(),
                "chunks": set(),
            })
            bucket["pages"].add(page_id)
            bucket["chunks"].add(chunk_id)

        rels = extract_relations_from_claim(content, ents)
        edges = scope_edges.setdefault(scope, {})
        for rel in rels:
            src = _entity_key(rel.source_type, rel.source_entity)
            tgt = _entity_key(rel.target_type, rel.target_entity)
            if src == tgt:
                continue
            key = tuple(sorted((src, tgt)))
            edges[key] = edges.get(key, 0.0) + float(rel.confidence or 0.8)

    # Evidence 实体并入 page 级（无 chunk，仅让 page 进入社区）
    for page_id, ents in evidence_by_page.items():
        scope = scope_of.get(page_id)
        if not scope:
            continue
        for etype, name in ents:
            ekey = _entity_key(etype, name)
            bucket = scope_entities.setdefault(scope, {}).setdefault(ekey, {
                "entity_id": ekey,
                "pages": set(),
                "chunks": set(),
            })
            bucket["pages"].add(page_id)

    communities: list[PageCommunity] = []
    for scope in sorted(scope_entities):
        entities = scope_entities[scope]
        entity_ids = set(entities.keys())
        if not entity_ids:
            continue

        weighted = sorted((a, b, w) for (a, b), w in scope_edges.get(scope, {}).items())
        undirected = [(a, b) for a, b, _ in weighted]

        if algorithm == "louvain" and weighted:
            try:
                clusters = louvain_partition(entity_ids, weighted)
            except Exception:  # noqa: BLE001
                clusters = _connected_components_fallback(entity_ids, undirected)
        else:
            clusters = _connected_components_fallback(entity_ids, undirected)
        clusters = _merge_and_split(clusters, weighted, LARGE_SPLIT_DEFAULT)

        for component in clusters:
            if not component:
                continue
            member_pages: set[str] = set()
            member_chunks: set[str] = set()
            for eid in component:
                bucket = entities.get(eid)
                if bucket is None:
                    continue
                member_pages.update(bucket["pages"])
                member_chunks.update(bucket["chunks"])
            if not member_pages:
                continue
            communities.append(PageCommunity(
                entity_ids=sorted(component),
                member_page_ids=sorted(member_pages),
                member_chunk_ids=sorted(member_chunks),
                acl_scope=scope,
            ))

    communities.sort(key=lambda c: c.fingerprint)
    return communities


def _connected_components_fallback(
    entity_ids: set[str],
    edges: list[tuple[str, str]],
) -> list[set[str]]:
    """降级：连通分量。"""
    parent = {e: e for e in entity_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in edges:
        if a in parent and b in parent:
            union(a, b)
    groups: dict[str, set[str]] = {}
    for e in entity_ids:
        groups.setdefault(find(e), set()).add(e)
    return list(groups.values())
