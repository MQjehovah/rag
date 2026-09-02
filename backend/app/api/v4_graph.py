"""V4 实体关系图谱 API（J-3 封板）。

真正的局部实体关系子图。所有查询遵循：ACL provenance 子查询 → 过滤 → 稳定排序
→ SQL limit → all，禁止先加载全部授权实体/关系再 Python 截断。

端点：
- GET /api/v4/graph/subgraph     → 有界局部子图（搜索/类型/版本/community/深度/上限/邻居）。
- GET /api/v4/graph/communities  → Community 分组列表。
- GET /api/v4/graph/search       → 实体关键词搜索提示（有界）。
- GET /api/v4/graph/facets       → 授权版本/类型/community facets（前端筛选下拉）。

普通响应不泄露 Page/Chunk/Evidence ID、正文、文件名、Notebook、权限组或 scope。
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.jwt_utils import get_current_user
from app.core.knowledge_compiler_v3 import versioning
from app.models.database import (
    V4GraphCommunity,
    V4GraphEntity,
    V4GraphEntityEvidence,
    V4GraphRelation,
    V4GraphRelationEvidence,
    WikiPage,
)

router = APIRouter(prefix="/api/v4/graph", tags=["V4 图谱"])

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
MAX_NODES = 200
MAX_EDGES = 400
MAX_SEARCH_RESULTS = 50
MAX_FACETS = 200
MAX_COMMUNITIES = 100
# family/version distinct 查询的硬上限：超过则 fail closed（不错误声称 latest）。
MAX_FAMILY_VERSIONS = 500
# related Wiki 批量查询的行数硬上限（每节点最终最多 5 条，总行数封顶，不无界 .all()）。
MAX_RELATED_WIKI_ROWS = 1000

_IS_COMMON = versioning.IS_COMMON_LABEL
_UNVERSIONED = versioning.UNVERSIONED_LABEL


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _visible_page_ids(db: Session, current_user: dict) -> set[str]:
    override = current_user.get("_scope_override") if isinstance(current_user, dict) else None
    if override:
        return access_control.get_scoped_page_ids(db, override)
    return access_control.get_visible_page_ids(db, current_user)


def _visible_wiki_ids(db: Session, current_user: dict) -> set[str]:
    override = current_user.get("_scope_override") if isinstance(current_user, dict) else None
    if override:
        return access_control.get_scoped_wiki_page_ids(db, override)
    return access_control.get_visible_wiki_page_ids(db, current_user)


# ---------------------------------------------------------------------------
# SQL 层授权 provenance 子查询（复用）
# ---------------------------------------------------------------------------

def _evidence_conds(visible_pages: set[str], visible_wikis: set[str]):
    conds = []
    if visible_pages:
        conds.append(V4GraphRelationEvidence.page_id.in_(visible_pages))
    if visible_wikis:
        conds.append(V4GraphRelationEvidence.wiki_page_id.in_(visible_wikis))
    return conds


def _authorized_relation_ids_subq(db: Session, visible_pages: set[str], visible_wikis: set[str]):
    conds = _evidence_conds(visible_pages, visible_wikis)
    if not conds:
        return None
    return (
        db.query(V4GraphRelationEvidence.relation_id)
        .filter(or_(*conds))
        .distinct()
        .subquery()
    )


def _authorized_entity_ids_subq(db: Session, visible_pages: set[str], visible_wikis: set[str]):
    conds = []
    if visible_pages:
        conds.append(V4GraphEntityEvidence.page_id.in_(visible_pages))
    if visible_wikis:
        conds.append(V4GraphEntityEvidence.wiki_page_id.in_(visible_wikis))
    if not conds:
        return None
    return (
        db.query(V4GraphEntityEvidence.entity_id)
        .filter(or_(*conds))
        .distinct()
        .subquery()
    )


# ---------------------------------------------------------------------------
# version_family per-family latest（纯函数）
# ---------------------------------------------------------------------------

def _family_allowed_labels(families: dict[str, set[str]], version_param: str | None) -> dict[str, set[str]]:
    """按 version_family 分别计算允许的 version_label。common 恒允许。

    family=""（version_family=None，无法确定唯一版本族）：不参与 latest，只允许
    common + unversioned。
    """
    result: dict[str, set[str]] = {}
    for family, labels in families.items():
        if family == "":
            result[family] = {_IS_COMMON, _UNVERSIONED}
            continue
        if version_param is None or not version_param.strip():
            comparable = [l for l in labels if l not in (_IS_COMMON, _UNVERSIONED)]
            latest = versioning.latest_version(comparable) if comparable else None
            if latest:
                result[family] = {_IS_COMMON, latest}
            else:
                result[family] = {_IS_COMMON, _UNVERSIONED}
        else:
            parts = [p.strip() for p in version_param.split(",") if p.strip()]
            if len(parts) > 1:
                result[family] = {_IS_COMMON}
            else:
                label = versioning.normalize_version_label(parts[0])
                result[family] = {_IS_COMMON, label}
    return result


def _allowed_versions_for_candidates(db: Session, rel_subq, version_param: str | None) -> dict[str, set[str]] | None:
    """针对授权关系候选计算 per-family allowed labels（SQL distinct + 硬上限）。

    超过 MAX_FAMILY_VERSIONS → 返回 None（fail closed，调用方返回空图），
    绝不从不完整集合上错误声称 latest。
    """
    rows = (
        db.query(V4GraphRelation.version_family, V4GraphRelation.version_label)
        .filter(V4GraphRelation.id.in_(rel_subq.select()))
        .distinct()
        .order_by(V4GraphRelation.version_family, V4GraphRelation.version_label)
        .limit(MAX_FAMILY_VERSIONS + 1)
        .all()
    )
    if len(rows) > MAX_FAMILY_VERSIONS:
        return None  # fail closed：超出硬上限，不错误计算 latest
    families: dict[str, set[str]] = {}
    for family, label in rows:
        if not label:
            continue
        families.setdefault(family or "", set()).add(label)
    return _family_allowed_labels(families, version_param)


# ---------------------------------------------------------------------------
# Community hash 映射
# ---------------------------------------------------------------------------

def _resolve_community(community_hash: str, communities: list[V4GraphCommunity]) -> str | None:
    for c in communities:
        if _stable_hash("c", c.key) == community_hash:
            return c.key
    return None


def _community_views(communities: list[V4GraphCommunity]) -> list[dict]:
    seen: dict[str, dict] = {}
    for com in communities:
        seen[com.key] = {"display_name": com.display_name, "color_index": _stable_hash("color", com.key)[:2]}
    return [
        {"key": _stable_hash("c", k), "display_name": v["display_name"], "color_index": v["color_index"]}
        for k, v in sorted(seen.items(), key=lambda kv: kv[1]["display_name"])
    ]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.get("/subgraph")
def subgraph(
    q: str | None = None,
    entity_type: str | None = None,
    version: str | None = None,
    community: str | None = None,
    depth: int = Query(1, ge=1),
    limit: int = Query(80, ge=1),
    focus: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    depth = min(depth, MAX_DEPTH)
    limit = min(limit, MAX_NODES)

    visible_pages = _visible_page_ids(db, current_user)
    visible_wikis = _visible_wiki_ids(db, current_user)
    if not visible_pages and not visible_wikis:
        return _empty_subgraph()

    ent_subq = _authorized_entity_ids_subq(db, visible_pages, visible_wikis)
    rel_subq = _authorized_relation_ids_subq(db, visible_pages, visible_wikis)
    if ent_subq is None or rel_subq is None:
        return _empty_subgraph()

    all_communities = _authorized_communities(db, ent_subq)
    community_key: str | None = None
    if community:
        community_key = _resolve_community(community.strip(), all_communities)
        if community_key is None:
            return _empty_subgraph()

    # per-family allowed versions（SQL distinct + 硬上限；超限 fail closed）
    allowed_by_family = _allowed_versions_for_candidates(db, rel_subq, version)
    if allowed_by_family is None:
        return _empty_subgraph()

    # 构造 family/version 的 SQL 过滤条件（可复用）
    family_conds = []
    for family, allowed in allowed_by_family.items():
        if family == "":
            family_conds.append(and_(V4GraphRelation.version_family.is_(None), V4GraphRelation.version_label.in_(allowed)))
        else:
            family_conds.append(and_(V4GraphRelation.version_family == family, V4GraphRelation.version_label.in_(allowed)))
    if not family_conds:
        return _empty_subgraph()

    # 授权关系查询：ACL + family/version + 稳定排序 + SQL LIMIT（禁止无界加载）
    rel_query = (
        db.query(V4GraphRelation)
        .filter(V4GraphRelation.id.in_(rel_subq.select()))
        .filter(or_(*family_conds))
    )
    if community_key:
        # 关系两端实体必须属于该 community
        community_entity_subq = (
            db.query(V4GraphEntity.id)
            .filter(V4GraphEntity.community_key == community_key)
            .subquery()
        )
        rel_query = rel_query.filter(or_(
            V4GraphRelation.source_id.in_(community_entity_subq.select()),
            V4GraphRelation.target_id.in_(community_entity_subq.select()),
        ))

    rel_rows = rel_query.order_by(V4GraphRelation.id).limit(MAX_EDGES + 1).all()
    rel_truncated = len(rel_rows) > MAX_EDGES
    rel_rows = rel_rows[:MAX_EDGES]

    # 从候选关系收集两端实体 id
    candidate_eids: set[str] = set()
    for r in rel_rows:
        candidate_eids.add(r.source_id)
        candidate_eids.add(r.target_id)
    if not candidate_eids:
        return _empty_subgraph()

    # 实体 SQL 层过滤（授权 + 候选 + type + keyword + community）+ limit
    ent_query = (
        db.query(V4GraphEntity)
        .filter(V4GraphEntity.id.in_(ent_subq.select()))
        .filter(V4GraphEntity.id.in_(candidate_eids))
    )
    if entity_type:
        ent_query = ent_query.filter(V4GraphEntity.entity_type == entity_type)
    if q and q.strip():
        needle = _norm(q)
        ent_query = ent_query.filter(or_(
            V4GraphEntity.normalized_name.contains(needle),
            V4GraphEntity.display_name.contains(needle),
        ))
    if community_key:
        ent_query = ent_query.filter(V4GraphEntity.community_key == community_key)

    node_limit = limit
    ent_rows = ent_query.order_by(V4GraphEntity.id).limit(node_limit + 1).all()
    truncated = rel_truncated or (len(ent_rows) > node_limit)
    ent_rows = ent_rows[:node_limit]

    entities: dict[str, V4GraphEntity] = {e.id: e for e in ent_rows}
    node_set = set(entities.keys())

    # 仅保留两端都在返回实体集合内的关系
    final_rel_ids = [r.id for r in rel_rows if r.source_id in node_set and r.target_id in node_set]

    # focus BFS：每层 SQL 查询 frontier 相邻授权关系（不预加载整图）
    if focus:
        focus_eid = _resolve_focus_entity(focus, entities)
        if focus_eid is None:
            return _empty_subgraph()
        final_rel_ids, node_set, truncated = _focus_bfs_sql(
            db, focus_eid, entities, rel_subq, family_conds, community_key,
            depth, limit, truncated,
        )
        entities = {eid: entities[eid] for eid in node_set if eid in entities}

    final_rel_ids_sorted = sorted(final_rel_ids)[:MAX_EDGES]
    if len(final_rel_ids) > len(final_rel_ids_sorted):
        truncated = True

    # 批量 evidence_count（GROUP BY 一次）+ conflict（批量）
    ev_counts = _batch_evidence_counts(db, final_rel_ids_sorted, visible_pages, visible_wikis)
    conflicts = _batch_conflicts(db, final_rel_ids_sorted, visible_pages, visible_wikis)

    # 批量 related_wiki（一次查询所有节点，内存分组）
    related_wikis = _batch_related_wikis(db, list(entities.values()), visible_wikis)

    community_keys = sorted({e.community_key for e in entities.values() if e.community_key})
    communities = (
        db.query(V4GraphCommunity).filter(V4GraphCommunity.key.in_(community_keys)).all()
    ) if community_keys else []

    def node_id(eid: str) -> str:
        return _stable_hash("n", eid)

    node_view = []
    for eid in sorted(entities.keys()):
        ent = entities[eid]
        node_view.append({
            "id": node_id(eid),
            "display_name": ent.display_name,
            "entity_type": ent.entity_type,
            "version_status": ent.version_status or _UNVERSIONED,
            "community": (
                {"key": _stable_hash("c", ent.community_key), "display_name": ent.community_key.split("|", 1)[-1]}
                if ent.community_key else None
            ),
            "related_wiki": related_wikis.get(ent.normalized_name, []),
        })

    edge_view = []
    rel_by_id = {r.id: r for r in rel_rows}
    for rid in final_rel_ids_sorted:
        r = rel_by_id[rid]
        edge_view.append({
            "source": node_id(r.source_id),
            "target": node_id(r.target_id),
            "relation_label": r.relation_type,
            "version_label": r.version_label,
            "version_status": r.version_status or _UNVERSIONED,
            "evidence_count": ev_counts.get(rid, 0),
            "conflict": conflicts.get(rid, False),
        })

    return {
        "nodes": node_view,
        "edges": edge_view,
        "communities": _community_views(communities),
        "total_nodes": len(node_view),
        "total_edges": len(edge_view),
        "truncated": truncated,
    }


@router.get("/communities")
def list_communities(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    visible_pages = _visible_page_ids(db, current_user)
    visible_wikis = _visible_wiki_ids(db, current_user)
    if not visible_pages and not visible_wikis:
        return {"communities": []}
    ent_subq = _authorized_entity_ids_subq(db, visible_pages, visible_wikis)
    if ent_subq is None:
        return {"communities": []}
    communities = _authorized_communities(db, ent_subq)
    return {"communities": _community_views(communities)}


@router.get("/facets")
def facets(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    visible_pages = _visible_page_ids(db, current_user)
    visible_wikis = _visible_wiki_ids(db, current_user)
    if not visible_pages and not visible_wikis:
        return {"versions": [], "entity_types": [], "communities": []}

    ent_subq = _authorized_entity_ids_subq(db, visible_pages, visible_wikis)
    rel_subq = _authorized_relation_ids_subq(db, visible_pages, visible_wikis)

    types: list[str] = []
    if ent_subq is not None:
        types = [
            r[0] for r in db.query(V4GraphEntity.entity_type)
            .filter(V4GraphEntity.id.in_(ent_subq.select()))
            .distinct()
            .order_by(V4GraphEntity.entity_type)
            .limit(MAX_FACETS + 1)
            .all()
        ]

    versions: list[str] = []
    if rel_subq is not None:
        versions = [
            r[0] for r in db.query(V4GraphRelation.version_label)
            .filter(V4GraphRelation.id.in_(rel_subq.select()))
            .filter(V4GraphRelation.version_label.notin_([_IS_COMMON, _UNVERSIONED]))
            .distinct()
            .order_by(V4GraphRelation.version_label)
            .limit(MAX_FACETS + 1)
            .all()
        ]

    communities = _authorized_communities(db, ent_subq) if ent_subq is not None else []

    return {
        "versions": sorted(versions)[:MAX_FACETS],
        "entity_types": sorted(types)[:MAX_FACETS],
        "communities": _community_views(communities)[:MAX_FACETS],
    }


@router.get("/search")
def search_entities(
    q: str = Query(..., min_length=1),
    entity_type: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    visible_pages = _visible_page_ids(db, current_user)
    visible_wikis = _visible_wiki_ids(db, current_user)
    if not visible_pages and not visible_wikis:
        return {"entities": []}
    ent_subq = _authorized_entity_ids_subq(db, visible_pages, visible_wikis)
    if ent_subq is None:
        return {"entities": []}

    needle = _norm(q)
    query = (
        db.query(V4GraphEntity)
        .filter(V4GraphEntity.id.in_(ent_subq.select()))
        .filter(or_(V4GraphEntity.normalized_name.contains(needle), V4GraphEntity.display_name.contains(needle)))
    )
    if entity_type:
        query = query.filter(V4GraphEntity.entity_type == entity_type)
    rows = query.order_by(V4GraphEntity.display_name).limit(MAX_SEARCH_RESULTS + 1).all()
    rows = rows[:MAX_SEARCH_RESULTS]
    return {
        "entities": [
            {"id": _stable_hash("n", e.id), "display_name": e.display_name, "entity_type": e.entity_type}
            for e in rows
        ]
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _empty_subgraph() -> dict:
    return {"nodes": [], "edges": [], "communities": [], "total_nodes": 0, "total_edges": 0, "truncated": False}


def _authorized_communities(db: Session, ent_subq) -> list[V4GraphCommunity]:
    """只查询含授权节点的 community（SQL 层 distinct + limit）。"""
    keys = [
        r[0] for r in db.query(V4GraphEntity.community_key)
        .filter(V4GraphEntity.id.in_(ent_subq.select()))
        .filter(V4GraphEntity.community_key.isnot(None))
        .distinct()
        .order_by(V4GraphEntity.community_key)
        .limit(MAX_COMMUNITIES + 1)
        .all()
    ]
    if not keys:
        return []
    return (
        db.query(V4GraphCommunity)
        .filter(V4GraphCommunity.key.in_(keys))
        .order_by(V4GraphCommunity.display_name)
        .all()
    )


def _resolve_focus_entity(focus: str, entities: dict[str, V4GraphEntity]) -> str | None:
    for eid in entities:
        if _stable_hash("n", eid) == focus:
            return eid
    return None


def _focus_bfs_sql(
    db: Session,
    focus_eid: str,
    entities: dict[str, V4GraphEntity],
    rel_subq,
    family_conds: list,
    community_key: str | None,
    depth: int,
    limit: int,
    truncated: bool,
):
    """有界 BFS：每层只查询当前 frontier 相邻的授权关系（SQL limit），不预加载整图。"""
    node_set: set[str] = {focus_eid}
    frontier: set[str] = {focus_eid}
    frontier_rels: dict[str, V4GraphRelation] = {}

    for _ in range(depth):
        if len(node_set) >= limit:
            truncated = True
            break
        # 每层 SQL：只查 frontier 相邻授权关系（有 limit）
        layer_rel_query = (
            db.query(V4GraphRelation)
            .filter(V4GraphRelation.id.in_(rel_subq.select()))
            .filter(or_(*family_conds))
            .filter(or_(
                V4GraphRelation.source_id.in_(frontier),
                V4GraphRelation.target_id.in_(frontier),
            ))
        )
        if community_key:
            community_entity_subq = (
                db.query(V4GraphEntity.id)
                .filter(V4GraphEntity.community_key == community_key)
                .subquery()
            )
            layer_rel_query = layer_rel_query.filter(or_(
                V4GraphRelation.source_id.in_(community_entity_subq.select()),
                V4GraphRelation.target_id.in_(community_entity_subq.select()),
            ))
        layer_rels = layer_rel_query.order_by(V4GraphRelation.id).limit(MAX_EDGES + 1).all()

        next_frontier: set[str] = set()
        for r in layer_rels:
            frontier_rels[r.id] = r
            if r.source_id in frontier and r.target_id not in node_set:
                next_frontier.add(r.target_id)
            if r.target_id in frontier and r.source_id not in node_set:
                next_frontier.add(r.source_id)
        if not next_frontier:
            break
        node_set |= next_frontier
        frontier = next_frontier

    if len(node_set) > limit:
        node_set = set(sorted(node_set)[:limit])
        truncated = True
    final_rel_ids = [
        rid for rid, r in frontier_rels.items()
        if r.source_id in node_set and r.target_id in node_set
    ]
    return final_rel_ids, node_set, truncated


def _batch_evidence_counts(db: Session, rel_ids: list[str], visible_pages: set[str], visible_wikis: set[str]) -> dict[str, int]:
    """批量计算每条边的授权 evidence_count（一次 GROUP BY，不 N+1）。"""
    if not rel_ids:
        return {}
    conds = _evidence_conds(visible_pages, visible_wikis)
    if not conds:
        return {rid: 0 for rid in rel_ids}
    rows = (
        db.query(
            V4GraphRelationEvidence.relation_id,
            func.count(V4GraphRelationEvidence.id),
        )
        .filter(V4GraphRelationEvidence.relation_id.in_(rel_ids), or_(*conds))
        .group_by(V4GraphRelationEvidence.relation_id)
        .all()
    )
    counts: dict[str, int] = {rid: 0 for rid in rel_ids}
    for rid, cnt in rows:
        counts[rid] = cnt
    return counts


def _batch_conflicts(db: Session, rel_ids: list[str], visible_pages: set[str], visible_wikis: set[str]) -> dict[str, bool]:
    """批量计算 conflict（同 source/target/type 存在多个授权版本），不 N+1。

    一次查询：候选三元组 (source, target, type) 的授权 version 分组计数。
    """
    result: dict[str, bool] = {}
    if not rel_ids:
        return result
    conds = _evidence_conds(visible_pages, visible_wikis)
    if not conds:
        return {rid: False for rid in rel_ids}

    # 候选三元组
    triples = []
    seen_triples = set()
    rel_map: dict[str, tuple] = {}
    for rid in rel_ids:
        # 需要 source/target/type，先查关系（候选关系已在 rel_rows 加载，这里重新查一次固定次数）
        # 为避免重复查询，直接通过关系表批量取
        pass
    # 一次取候选关系
    rels = db.query(V4GraphRelation).filter(V4GraphRelation.id.in_(rel_ids)).all()
    for r in rels:
        rel_map[r.id] = r
        key = (r.source_id, r.target_id, r.relation_type)
        if key not in seen_triples:
            seen_triples.add(key)
            triples.append(key)

    if not triples:
        return {rid: False for rid in rel_ids}

    # 一次 GROUP BY：按 (source, target, type) 统计授权 distinct version
    from sqlalchemy import tuple_ as sa_tuple
    authed_subq = db.query(V4GraphRelationEvidence.relation_id).filter(or_(*conds)).distinct().subquery()

    # 用 OR 条件构造候选三元组过滤（固定一次 SQL）
    triple_conds = [
        and_(
            V4GraphRelation.source_id == src,
            V4GraphRelation.target_id == tgt,
            V4GraphRelation.relation_type == rtype,
        )
        for (src, tgt, rtype) in triples
    ]
    rows = (
        db.query(
            V4GraphRelation.source_id,
            V4GraphRelation.target_id,
            V4GraphRelation.relation_type,
            func.count(func.distinct(V4GraphRelation.version_label)),
        )
        .filter(V4GraphRelation.id.in_(authed_subq.select()))
        .filter(V4GraphRelation.version_label.notin_([_IS_COMMON, None]))
        .filter(or_(*triple_conds))
        .group_by(
            V4GraphRelation.source_id,
            V4GraphRelation.target_id,
            V4GraphRelation.relation_type,
        )
        .all()
    )
    conflict_triples = {
        (src, tgt, rtype) for (src, tgt, rtype, cnt) in rows if cnt > 1
    }
    for rid in rel_ids:
        r = rel_map.get(rid)
        if r is None:
            result[rid] = False
            continue
        result[rid] = (r.source_id, r.target_id, r.relation_type) in conflict_triples
    return result


def _batch_related_wikis(db: Session, entities: list[V4GraphEntity], visible_wikis: set[str]) -> dict[str, list[dict]]:
    """批量计算每个实体的相关 Wiki（一次查询所有节点，内存分组，不 N+1）。"""
    result: dict[str, list[dict]] = {}
    if not visible_wikis:
        return result
    names = sorted({e.normalized_name for e in entities if e.normalized_name})
    if not names:
        return result
    # 一次查询：任一节点名命中 title/summary 的授权 Wiki（SQL LIMIT 硬上限，
    # 每节点最多取 5 条由内存分组后截断，不无界 .all()）。
    name_conds = [or_(WikiPage.title.contains(n), WikiPage.summary.contains(n)) for n in names]
    rows = (
        db.query(WikiPage.id, WikiPage.title, WikiPage.summary)
        .filter(WikiPage.id.in_(visible_wikis), WikiPage.status == "published")
        .filter(or_(*name_conds))
        .order_by(WikiPage.title)
        .limit(MAX_RELATED_WIKI_ROWS)
        .all()
    )
    # 内存按实体名分组（每个 name 匹配 title/summary）
    for name in names:
        matched = []
        for pid, title, summary in rows:
            if name in _norm(title) or name in _norm(summary or ""):
                matched.append({"id": pid, "title": title})
            if len(matched) >= 5:
                break
        if matched:
            result[name] = matched
    return result
