"""V4 实体关系图谱增量构建器（J-3 封板）。

把 Page/PageChunk/Evidence/Wiki 正文 → 实体节点 → 带关系名的边 → 追溯证据映射
→ Community 分组，持久化到 v4_graph_* 表。

不依赖任何 Card/KO/旧图谱表；不写 CanonicalEntity / EntityAlias / 旧 Community。

封板关键设计：
- 实体 id = `{scope_key}|{entity_type}:{normalized_name}`（确定性、scope 隔离）。
- 关系 id = `{source_id}|{relation_type}|{target_id}|{version_label}`。
- evidence_count 由当前有效 provenance 重算（绝不 Python 旧值 +1）。
- provenance 幂等：同一 (relation, page, chunk, evidence) 或 (relation, wiki, revision,
  section) 只写一次，重复抽取不触发唯一约束异常。
- 按来源增量替换：先删该 Page/Wiki 的 provenance，写入当前实体/关系，再清理无
  provenance 的孤立关系/实体，重算 count，最后有界重建 Community。
- 有界：只扫描目标 Page/Wiki 的 Chunk/Evidence/Section；Community 重建只用受影响
  实体集合及其相邻关系，不 `.all()` 扫描整个 scope。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core import access_control
from app.core.knowledge_compiler_v3 import versioning
from app.core.knowledge_compiler_v3.entity_patterns import ENTITY_PATTERNS
from app.core.knowledge_compiler_v3.relation_extractor import extract_relations_from_claim
from app.core.text_normalization import normalize_text
from app.models.database import (
    EvidenceItem,
    Notebook,
    Page,
    PageChunk,
    V4GraphCommunity,
    V4GraphEntity,
    V4GraphEntityEvidence,
    V4GraphRelation,
    V4GraphRelationEvidence,
    WikiPage,
    WikiRevision,
    WikiSection,
)

logger = logging.getLogger(__name__)

PARAMETER_RELATION_TYPES = {
    "容量": "capacity",
    "电压": "voltage",
    "电流": "current",
    "功率": "power",
    "扭矩": "torque",
    "转速": "speed",
}


def normalize_name(name: str) -> str:
    """实体名归一化（NFKC 小写去连字符/下划线/空白，确定性）。"""
    s = normalize_text(name or "").strip().lower()
    return re.sub(r"[-\s_]+", "", s)


def _extract_entities(text: str) -> list[tuple[str, str]]:
    """从文本抽取实体 [(entity_type, name)]（确定性，NFKC 后去重保序）。"""
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


def _scope_key(scope: access_control.AccessScope) -> str:
    if scope.kind == "group":
        return "group:" + ",".join(sorted(scope.groups))
    return scope.kind


def _page_scope(db: Session, page: Page) -> access_control.AccessScope | None:
    if page is None or not page.notebook_id:
        return None
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return None
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        return None
    return scope


def _entity_id(scope: str, entity_type: str, name: str) -> str:
    return f"{scope}|{entity_type}:{normalize_name(name)}"


def _stable_hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 参数关系提取
# ---------------------------------------------------------------------------

def extract_parameter_relations(
    text: str,
    entities: list[tuple[str, str]],
) -> list[dict]:
    """从单条 Claim/Chunk 提取参数关系：`<实体> <参数词> <数值>`。

    仅当参数值与一个实体出现在同一 Claim 内才建立。
    """
    text = normalize_text(text or "")
    results: list[dict] = []
    for param_word, rel_type in PARAMETER_RELATION_TYPES.items():
        pattern = re.compile(
            rf"{re.escape(param_word)}\s*(?:要求\s*(?:为|是)?\s*)?"
            rf"(\d+(?:\.\d+)?)\s*([A-Za-zμΩ%]+)?"
        )
        for m in pattern.finditer(text):
            value = m.group(1)
            unit = m.group(2) or ""
            target_value = f"{value}{unit}"
            left_text = text[: m.start()]
            left_entities = [e for e in entities if normalize_name(e[1]) in normalize_text(left_text)]
            if not left_entities:
                continue
            src = max(left_entities, key=lambda e: left_text.rfind(e[1]))
            results.append({
                "source_entity": src[1],
                "source_type": src[0],
                "relation_type": rel_type,
                "target_value": target_value,
                "target_type": "parameter",
            })
    return results


# ---------------------------------------------------------------------------
# 版本族推导（关系级，per-family latest）
# ---------------------------------------------------------------------------

def _relation_version_family(
    source_type: str,
    source_name: str,
    target_type: str,
    target_name: str,
    evidence_entities: list[tuple[str, str]],
) -> str | None:
    """按「当前关系所在证据」推导 version_family（关系级，fail closed）。

    - 关系 source/target 中明确含一个 product → 该产品。
    - 关系两端是两个不同 product → None（无法归属，fail closed）。
    - 否则当前证据中唯一 product → 该产品。
    - 证据中多个 product 且关系两端无 product → None。
    - 无 product 时用关系两端唯一 system/component 兜底。
    - 仍无法唯一确定 → None（不参与 per-family latest，只允许 common）。
    """
    def _norm_set(iterable):
        return {normalize_name(n) for n in iterable if n and normalize_name(n)}

    rel_products = _norm_set(
        n for t, n in ((source_type, source_name), (target_type, target_name)) if t == "product"
    )
    if len(rel_products) == 1:
        return rel_products.pop()
    if len(rel_products) > 1:
        return None  # 关系两端不同 product，无法归属

    evidence_products = _norm_set(n for t, n in evidence_entities if t == "product")
    if len(evidence_products) == 1:
        return evidence_products.pop()
    if len(evidence_products) > 1:
        return None

    rel_sys = _norm_set(
        n for t, n in ((source_type, source_name), (target_type, target_name))
        if t in ("system", "component")
    )
    if len(rel_sys) == 1:
        return rel_sys.pop()
    evidence_sys = _norm_set(n for t, n in evidence_entities if t in ("system", "component"))
    if len(evidence_sys) == 1:
        return evidence_sys.pop()
    return None


def _page_default_version(page: Page) -> dict:
    """Page 标题/路径的默认版本（不含 content，content 可能多版本）。"""
    det = versioning.detect_product_version(
        structured_version=None,
        title=page.title,
        source_path=page.source_path,
        content=None,
    )
    return {"version_label": det.version_label, "version_status": det.version_status}


def _evidence_version(page: Page, text: str) -> dict:
    """证据粒度版本：当前 Chunk/Evidence 正文为第一依据，正文无版本才回退标题/路径。

    - 证据正文明确唯一版本 → confirmed 用该版本（优先级最高，覆盖标题/路径）。
    - 证据正文多个版本 → ambiguous（绝不回退标题/路径）。
    - 证据正文无版本 → 回退 Page 标题/路径默认版本。
    """
    text = text or ""
    # 1. 先只看正文，提取版本标签（复用 Phase I extract_version_labels）。
    content_labels = versioning.extract_version_labels(text)
    unique = list(dict.fromkeys(content_labels))
    if len(unique) > 1:
        return {"version_label": versioning.UNVERSIONED_LABEL, "version_status": versioning.VERSION_STATUS_AMBIGUOUS}
    if len(unique) == 1:
        label = versioning.normalize_version_label(unique[0])
        return {"version_label": label, "version_status": versioning.VERSION_STATUS_CONFIRMED}
    # 2. 正文无版本 → 回退 Page 标题/路径默认版本。
    det = versioning.detect_product_version(
        structured_version=None,
        title=page.title,
        source_path=page.source_path,
        content=None,
    )
    return {"version_label": det.version_label, "version_status": det.version_status}


# ---------------------------------------------------------------------------
# provenance 幂等写入
# ---------------------------------------------------------------------------

def _relation_id(source_id: str, relation_type: str, target_id: str, version_family: str | None, version_label: str) -> str:
    # J-3 封板：relation 唯一性包含 version_family，不同 family 相同端点/关系/版本不合并。
    return "|".join([source_id, relation_type, target_id, version_family or "", version_label])


def _add_entity_evidence(
    db: Session,
    entity_id: str,
    *,
    page_id: str | None = None,
    chunk_id: str | None = None,
    evidence_id: str | None = None,
    wiki_page_id: str | None = None,
    revision_id: str | None = None,
    section_id: str | None = None,
) -> None:
    eid = _stable_hash(
        entity_id, page_id or "", chunk_id or "", evidence_id or "",
        wiki_page_id or "", revision_id or "", section_id or "",
    )
    if db.get(V4GraphEntityEvidence, eid) is None:
        db.add(V4GraphEntityEvidence(
            id=eid,
            entity_id=entity_id,
            page_id=page_id,
            chunk_id=chunk_id,
            evidence_id=evidence_id,
            wiki_page_id=wiki_page_id,
            revision_id=revision_id,
            section_id=section_id,
        ))


def _add_relation(
    db: Session,
    source_id: str,
    target_id: str,
    relation_type: str,
    version_label: str,
    version_status: str,
    version_family: str | None,
    scope: str,
    *,
    page_id: str | None = None,
    chunk_id: str | None = None,
    evidence_id: str | None = None,
    wiki_page_id: str | None = None,
    revision_id: str | None = None,
    section_id: str | None = None,
) -> str | None:
    if source_id == target_id:
        return None
    rid = _relation_id(source_id, relation_type, target_id, version_family, version_label)
    rel = db.get(V4GraphRelation, rid)
    if rel is None:
        rel = V4GraphRelation(
            id=rid,
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            version_label=version_label,
            version_status=version_status,
            version_family=version_family,
            evidence_count=0,  # 由 _recount_evidence 重算
            acl_scope=scope,
        )
        db.add(rel)
    elif rel.version_family is None and version_family:
        rel.version_family = version_family

    peid = _stable_hash(
        rid, page_id or "", chunk_id or "", evidence_id or "",
        wiki_page_id or "", revision_id or "", section_id or "",
    )
    if db.get(V4GraphRelationEvidence, peid) is None:
        db.add(V4GraphRelationEvidence(
            id=peid,
            relation_id=rid,
            page_id=page_id,
            chunk_id=chunk_id,
            evidence_id=evidence_id,
            wiki_page_id=wiki_page_id,
            revision_id=revision_id,
            section_id=section_id,
        ))
    return rid


def _upsert_entity(
    db: Session,
    scope: str,
    entity_type: str,
    name: str,
) -> V4GraphEntity:
    eid = _entity_id(scope, entity_type, name)
    ent = db.get(V4GraphEntity, eid)
    if ent is None:
        ent = V4GraphEntity(
            id=eid,
            entity_type=entity_type,
            normalized_name=normalize_name(name),
            display_name=name,
            acl_scope=scope,
            version_label=versioning.IS_COMMON_LABEL,
            version_status="confirmed",
        )
        db.add(ent)
    return ent


# ---------------------------------------------------------------------------
# 增量重建（Page）
# ---------------------------------------------------------------------------

def rebuild_page_graph(db: Session, page_id: str, *, commit: bool = True) -> dict:
    """按 Page 增量重建实体/关系（幂等）。

    事务边界（单 Page）：
    1. 删除该 Page 旧的关系/实体 provenance；
    2. 从该 Page 的 Chunk 与 Evidence 抽取实体/关系并写入；
    3. 清理无 provenance 的孤立关系/实体 + 重算 evidence_count；
    4. 有界重建受影响 Community。
    """
    page = db.get(Page, page_id)
    if page is None:
        return {"page_id": page_id, "status": "deleted"}

    scope = _page_scope(db, page)
    if scope is None:
        remove_page_graph(db, page_id, commit=commit)
        return {"page_id": page_id, "status": "no_scope"}

    # 失效远程 Page：只清理，不重写（fail closed）。
    if not _page_eligible(db, page):
        remove_page_graph(db, page_id, commit=commit)
        return {"page_id": page_id, "status": "ineligible_remote"}

    scope_key = _scope_key(scope)

    # 1. 删除该 Page 旧 provenance，收集受影响关系 id
    affected_rel_ids = _clear_page_provenance(db, page_id)

    # 2. 抽取该 Page 的 Chunk（稳定排序，有界：仅该 Page）
    chunks = (
        db.query(PageChunk)
        .filter(PageChunk.page_id == page_id)
        .order_by(PageChunk.chunk_index, PageChunk.id)
        .all()
    )
    chunk_texts = [(c.id, c.content or "") for c in chunks if (c.content or "").strip()]

    evidences = (
        db.query(EvidenceItem)
        .filter(EvidenceItem.source_page_id == page_id, EvidenceItem.status == "active")
        .order_by(EvidenceItem.id)
        .all()
    )

    # 受影响实体（本 Page 涉及）
    affected_entity_ids: set[str] = set()

    def _upsert_and_track(entity_type: str, name: str) -> str:
        eid = _entity_id(scope_key, entity_type, name)
        _upsert_entity(db, scope_key, entity_type, name)
        affected_entity_ids.add(eid)
        return eid

    def _rel_version(page: Page, text: str) -> tuple[str, str]:
        """证据粒度版本：Chunk/Evidence 内容明确版本优先，ambiguous 不写成 unversioned。"""
        ev = _evidence_version(page, text)
        if ev["version_status"] == versioning.VERSION_STATUS_AMBIGUOUS:
            return versioning.UNVERSIONED_LABEL, versioning.VERSION_STATUS_AMBIGUOUS
        if ev["version_status"] == versioning.VERSION_STATUS_CONFIRMED:
            return ev["version_label"], versioning.VERSION_STATUS_CONFIRMED
        return versioning.UNVERSIONED_LABEL, versioning.VERSION_STATUS_UNVERSIONED

    # 从 Chunk 抽取（关系级 family + 证据级 version）
    for chunk_id, text in chunk_texts:
        ents = _extract_entities(text)
        for etype, name in ents:
            eid = _upsert_and_track(etype, name)
            _add_entity_evidence(db, eid, page_id=page_id, chunk_id=chunk_id)
        rels = extract_relations_from_claim(text, ents)
        for rel in rels:
            sid = _upsert_and_track(rel.source_type, rel.source_entity)
            tid = _upsert_and_track(rel.target_type, rel.target_entity)
            family = _relation_version_family(rel.source_type, rel.source_entity, rel.target_type, rel.target_entity, ents)
            rv, rstatus = _rel_version(page, text)
            _add_relation(db, sid, tid, rel.relation_type, rv, rstatus, family, scope_key,
                          page_id=page_id, chunk_id=chunk_id)
        for pr in extract_parameter_relations(text, ents):
            sid = _upsert_and_track(pr["source_type"], pr["source_entity"])
            tid = _upsert_and_track("parameter", pr["target_value"])
            _add_entity_evidence(db, sid, page_id=page_id, chunk_id=chunk_id)
            _add_entity_evidence(db, tid, page_id=page_id, chunk_id=chunk_id)
            family = _relation_version_family(pr["source_type"], pr["source_entity"], "parameter", pr["target_value"], ents)
            rv, rstatus = _rel_version(page, text)
            _add_relation(db, sid, tid, pr["relation_type"], rv, rstatus, family, scope_key,
                          page_id=page_id, chunk_id=chunk_id)

    # 从 active Evidence 抽取（关系级 family + 证据级 version）
    for ev in evidences:
        ents = _extract_entities(ev.content or "")
        for etype, name in ents:
            eid = _upsert_and_track(etype, name)
            _add_entity_evidence(db, eid, page_id=page_id, evidence_id=ev.id)
        rels = extract_relations_from_claim(ev.content or "", ents)
        for rel in rels:
            sid = _upsert_and_track(rel.source_type, rel.source_entity)
            tid = _upsert_and_track(rel.target_type, rel.target_entity)
            family = _relation_version_family(rel.source_type, rel.source_entity, rel.target_type, rel.target_entity, ents)
            rv, rstatus = _rel_version(page, ev.content or "")
            _add_relation(db, sid, tid, rel.relation_type, rv, rstatus, family, scope_key,
                          page_id=page_id, evidence_id=ev.id)

    db.flush()

    # 3. 清理无 provenance 的孤立关系/实体 + 重算 count
    all_affected_rel_ids = set(affected_rel_ids)
    all_affected_rel_ids |= {r[0] for r in db.query(V4GraphRelationEvidence.relation_id)
                             .filter(V4GraphRelationEvidence.page_id == page_id).all()}
    _prune_orphans(db, scope_key, affected_entity_ids)
    _recount_evidence(db, all_affected_rel_ids)

    # 4. 有界重建受影响 Community
    _rebuild_communities(db, scope_key, affected_entity_ids)

    if commit:
        db.commit()
    return {"page_id": page_id, "status": "rebuilt", "scope": scope_key}


def _page_eligible(db: Session, page: Page) -> bool:
    """远程 Page 必须存在同 Connector active SourceItem；否则视为失效，只清理不重写。"""
    if not page.source_type:
        return True  # 手工 Page 不要求 SourceItem
    from app.models.database import SourceConnection, SourceItem
    rows = (
        db.query(SourceItem.id)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(
            SourceItem.page_id == page.id,
            SourceItem.state == "active",
            SourceConnection.connector_key == page.source_type,
        )
        .first()
    )
    return rows is not None


# ---------------------------------------------------------------------------
# 增量重建（Wiki）
# ---------------------------------------------------------------------------

def rebuild_wiki_graph(db: Session, wiki_page_id: str, *, commit: bool = True) -> dict:
    """按 Wiki 当前有效 Revision 重建实体/关系 provenance（幂等）。

    只读当前有效 Revision（published）的 Section（common + 版本块 + 人工内容）。
    旧 Revision 的 provenance 立即失效。
    """
    wiki = db.get(WikiPage, wiki_page_id)
    if wiki is None:
        return {"wiki_page_id": wiki_page_id, "status": "deleted"}

    scope = access_control.scope_from_acl(wiki.acl_scope)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        remove_wiki_graph(db, wiki_page_id, commit=commit)
        return {"wiki_page_id": wiki_page_id, "status": "no_scope"}
    scope_key = _scope_key(scope)

    # 1. 删除该 Wiki 旧 provenance
    affected_rel_ids = _clear_wiki_provenance(db, wiki_page_id)

    # 2. 读取当前有效 Revision
    rev = _valid_current_revision(db, wiki)
    if rev is None:
        db.flush()
        _prune_orphans(db, scope_key, set())
        _recount_evidence(db, set(affected_rel_ids))
        _rebuild_communities(db, scope_key, set())
        if commit:
            db.commit()
        return {"wiki_page_id": wiki_page_id, "status": "no_valid_revision"}

    sections = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == rev.id)
        .order_by(WikiSection.order_index)
        .all()
    )
    if not sections:
        db.flush()
        _prune_orphans(db, scope_key, set())
        _recount_evidence(db, set(affected_rel_ids))
        _rebuild_communities(db, scope_key, set())
        if commit:
            db.commit()
        return {"wiki_page_id": wiki_page_id, "status": "no_sections"}

    # Wiki 版本族：用 wiki title 实体（作为 section 无 product 时的兜底）
    title_ents = _extract_entities(wiki.title or "")

    affected_entity_ids: set[str] = set()

    def _upsert_and_track(entity_type: str, name: str) -> str:
        eid = _entity_id(scope_key, entity_type, name)
        _upsert_entity(db, scope_key, entity_type, name)
        affected_entity_ids.add(eid)
        return eid

    for sec in sections:
        content = sec.content or ""
        ents = _extract_entities(content)
        for etype, name in ents:
            eid = _upsert_and_track(etype, name)
            _add_entity_evidence(db, eid, wiki_page_id=wiki_page_id, revision_id=rev.id, section_id=sec.id)
        rels = extract_relations_from_claim(content, ents)
        for rel in rels:
            sid = _upsert_and_track(rel.source_type, rel.source_entity)
            tid = _upsert_and_track(rel.target_type, rel.target_entity)
            # Wiki Section 版本：直接复用 section.version_label/version_status/is_common
            vlabel = sec.version_label or versioning.UNVERSIONED_LABEL
            vstatus = sec.version_status or versioning.VERSION_STATUS_UNVERSIONED
            # 关系级 family：优先关系两端 product，兜底 section 实体 + wiki title
            family = _relation_version_family(rel.source_type, rel.source_entity, rel.target_type, rel.target_entity, ents)
            if family is None:
                family = _relation_version_family(rel.source_type, rel.source_entity, rel.target_type, rel.target_entity, title_ents)
            _add_relation(db, sid, tid, rel.relation_type, vlabel, vstatus, family, scope_key,
                          wiki_page_id=wiki_page_id, revision_id=rev.id, section_id=sec.id)

    db.flush()

    all_affected_rel_ids = set(affected_rel_ids)
    all_affected_rel_ids |= {r[0] for r in db.query(V4GraphRelationEvidence.relation_id)
                             .filter(V4GraphRelationEvidence.wiki_page_id == wiki_page_id).all()}
    _prune_orphans(db, scope_key, affected_entity_ids)
    _recount_evidence(db, all_affected_rel_ids)
    _rebuild_communities(db, scope_key, affected_entity_ids)

    if commit:
        db.commit()
    return {"wiki_page_id": wiki_page_id, "status": "rebuilt", "scope": scope_key}


def _valid_current_revision(db: Session, wiki: WikiPage) -> WikiRevision | None:
    if not wiki.current_revision_id:
        return None
    return (
        db.query(WikiRevision)
        .filter(
            WikiRevision.id == wiki.current_revision_id,
            WikiRevision.wiki_page_id == wiki.id,
            WikiRevision.status == "published",
        )
        .first()
    )


# ---------------------------------------------------------------------------
# 删除/失效
# ---------------------------------------------------------------------------

def _clear_page_provenance(db: Session, page_id: str) -> list[str]:
    """删除该 Page 的关系/实体 provenance，返回受影响关系 id（删除前）。"""
    affected = [r[0] for r in db.query(V4GraphRelationEvidence.relation_id)
                .filter(V4GraphRelationEvidence.page_id == page_id).all()]
    db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.page_id == page_id).delete(synchronize_session=False)
    db.query(V4GraphEntityEvidence).filter(V4GraphEntityEvidence.page_id == page_id).delete(synchronize_session=False)
    return affected


def _clear_wiki_provenance(db: Session, wiki_page_id: str) -> list[str]:
    affected = [r[0] for r in db.query(V4GraphRelationEvidence.relation_id)
                .filter(V4GraphRelationEvidence.wiki_page_id == wiki_page_id).all()]
    db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.wiki_page_id == wiki_page_id).delete(synchronize_session=False)
    db.query(V4GraphEntityEvidence).filter(V4GraphEntityEvidence.wiki_page_id == wiki_page_id).delete(synchronize_session=False)
    return affected


def _collect_affected_community_keys(
    db: Session,
    scope_key: str,
    entity_ids: set[str],
    relation_ids: set[str],
) -> set[str]:
    """收集受影响实体/关系所属的旧 community key（删除/新增前）。"""
    keys: set[str] = set()
    if entity_ids:
        for chunk in _chunks(sorted(entity_ids), 500):
            rows = db.query(V4GraphEntity.community_key).filter(
                V4GraphEntity.id.in_(chunk), V4GraphEntity.acl_scope == scope_key
            ).all()
            keys.update(r[0] for r in rows if r[0])
    if relation_ids:
        for chunk in _chunks(sorted(relation_ids), 500):
            rels = db.query(V4GraphRelation.source_id, V4GraphRelation.target_id).filter(
                V4GraphRelation.id.in_(chunk), V4GraphRelation.acl_scope == scope_key
            ).all()
            for src, tgt in rels:
                for eid in (src, tgt):
                    ent = db.get(V4GraphEntity, eid)
                    if ent is not None and ent.community_key:
                        keys.add(ent.community_key)
    return keys


def remove_page_graph(db: Session, page_id: str, *, commit: bool = True) -> dict:
    """Page 删除/SourceItem skipped/NEEDS_REASSIGN/权限变化时立即失效其图谱证据。

    删除前记录受影响实体/关系/旧 community，删除后基于「受影响旧 community 全部
    成员」重算连通分量，支持桥接边删除后的拆分与实体失去全部 provenance 后的删除。
    """
    scope_keys: set[str] = set()
    ent_rows = db.query(V4GraphEntityEvidence.entity_id).filter(
        V4GraphEntityEvidence.page_id == page_id).all()
    affected_entities = {r[0] for r in ent_rows}
    for eid in affected_entities:
        ent = db.get(V4GraphEntity, eid)
        if ent is not None:
            scope_keys.add(ent.acl_scope)
    rel_rows = db.query(V4GraphRelationEvidence.relation_id).filter(
        V4GraphRelationEvidence.page_id == page_id).all()
    affected_rels = {r[0] for r in rel_rows}
    for rid in affected_rels:
        rel = db.get(V4GraphRelation, rid)
        if rel is not None:
            scope_keys.add(rel.acl_scope)

    # 删除前记录旧 community（用于删除后拆分/合并）
    old_community_keys: dict[str, set[str]] = {}
    for scope_key in scope_keys:
        old_community_keys[scope_key] = _collect_affected_community_keys(
            db, scope_key, affected_entities, affected_rels
        )

    _clear_page_provenance(db, page_id)
    db.flush()
    for scope_key in scope_keys:
        if scope_key:
            _prune_orphans(db, scope_key, affected_entities)
            _recount_evidence(db, affected_rels)
            _rebuild_affected_communities(db, scope_key, affected_entities, old_community_keys.get(scope_key, set()))
    if commit:
        db.commit()
    return {"page_id": page_id, "status": "removed", "scopes": sorted(scope_keys)}


def remove_wiki_graph(db: Session, wiki_page_id: str, *, commit: bool = True) -> dict:
    """Wiki 归档/删除/回滚后失效其图谱 provenance。"""
    scope_keys: set[str] = set()
    ent_rows = db.query(V4GraphEntityEvidence.entity_id).filter(
        V4GraphEntityEvidence.wiki_page_id == wiki_page_id).all()
    affected_entities = {r[0] for r in ent_rows}
    for eid in affected_entities:
        ent = db.get(V4GraphEntity, eid)
        if ent is not None:
            scope_keys.add(ent.acl_scope)
    rel_rows = db.query(V4GraphRelationEvidence.relation_id).filter(
        V4GraphRelationEvidence.wiki_page_id == wiki_page_id).all()
    affected_rels = {r[0] for r in rel_rows}
    for rid in affected_rels:
        rel = db.get(V4GraphRelation, rid)
        if rel is not None:
            scope_keys.add(rel.acl_scope)

    old_community_keys: dict[str, set[str]] = {}
    for scope_key in scope_keys:
        old_community_keys[scope_key] = _collect_affected_community_keys(
            db, scope_key, affected_entities, affected_rels
        )

    _clear_wiki_provenance(db, wiki_page_id)
    db.flush()
    for scope_key in scope_keys:
        if scope_key:
            _prune_orphans(db, scope_key, affected_entities)
            _recount_evidence(db, affected_rels)
            _rebuild_affected_communities(db, scope_key, affected_entities, old_community_keys.get(scope_key, set()))
    if commit:
        db.commit()
    return {"wiki_page_id": wiki_page_id, "status": "removed", "scopes": sorted(scope_keys)}


# ---------------------------------------------------------------------------
# 清理孤立 + evidence_count 重算
# ---------------------------------------------------------------------------

def _prune_orphans(db: Session, scope_key: str, affected_entity_ids: set[str]) -> None:
    """删除无任何 provenance 的孤立关系与实体（有界：优先受影响实体 + 无证据关系）。

    1. 删除无 provenance 的关系。
    2. 删除无 provenance 的实体（优先受影响实体集合，再兜底扫描 scope 无证据实体）。
    """
    # 1. 无 provenance 的关系 → 删除
    orphan_rels = db.query(V4GraphRelation.id).outerjoin(
        V4GraphRelationEvidence,
        V4GraphRelationEvidence.relation_id == V4GraphRelation.id,
    ).filter(
        V4GraphRelation.acl_scope == scope_key,
        V4GraphRelationEvidence.id.is_(None),
    ).all()
    for (rid,) in orphan_rels:
        db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.relation_id == rid).delete(synchronize_session=False)
        db.query(V4GraphRelation).filter(V4GraphRelation.id == rid).delete(synchronize_session=False)

    # 2. 无 provenance 的实体 → 删除（有界：只检查 scope 内实体，但用 EXISTS 反查）
    #    优先检查受影响实体，再兜底 scope 全量（受 scope 实体数量约束，非 chunk 全量）。
    candidate_ids = set(affected_entity_ids)
    if not candidate_ids:
        candidate_ids = {r[0] for r in db.query(V4GraphEntity.id).filter(V4GraphEntity.acl_scope == scope_key).all()}
    for eid in sorted(candidate_ids):
        has_ev = db.query(V4GraphEntityEvidence.id).filter(V4GraphEntityEvidence.entity_id == eid).first()
        if has_ev is not None:
            continue
        rel_ids = [r[0] for r in db.query(V4GraphRelation.id).filter(
            (V4GraphRelation.source_id == eid) | (V4GraphRelation.target_id == eid)).all()]
        if rel_ids:
            db.query(V4GraphRelationEvidence).filter(V4GraphRelationEvidence.relation_id.in_(rel_ids)).delete(synchronize_session=False)
            db.query(V4GraphRelation).filter(V4GraphRelation.id.in_(rel_ids)).delete(synchronize_session=False)
        db.query(V4GraphEntity).filter(V4GraphEntity.id == eid).delete(synchronize_session=False)


def _recount_evidence(db: Session, relation_ids: set[str]) -> None:
    """按当前有效 provenance 重算 evidence_count（绝不 Python +1）。"""
    if not relation_ids:
        return
    for chunk in _chunks(sorted(relation_ids), 500):
        rows = (
            db.query(V4GraphRelationEvidence.relation_id)
            .filter(V4GraphRelationEvidence.relation_id.in_(chunk))
            .all()
        )
        counts: dict[str, int] = {}
        for (rid,) in rows:
            counts[rid] = counts.get(rid, 0) + 1
        rels = db.query(V4GraphRelation).filter(V4GraphRelation.id.in_(chunk)).all()
        for rel in rels:
            rel.evidence_count = counts.get(rel.id, 0)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# 有界 Community 重建（拆分 + 合并）
# ---------------------------------------------------------------------------

def _rebuild_communities(db: Session, scope_key: str, affected_entity_ids: set[str]) -> None:
    """重建受影响 Community（新增边场景：合并两个社区）。

    复用 _rebuild_affected_communities：新增边连接两个 Community 时，受影响实体
    与旧 community 成员一起重算连通分量，产生合并。
    """
    if not affected_entity_ids:
        _cleanup_empty_communities(db, scope_key)
        return
    # 新增边场景：受影响实体的旧 community 也需纳入重算（合并）。
    old_keys = _collect_affected_community_keys(db, scope_key, affected_entity_ids, set())
    _rebuild_affected_communities(db, scope_key, affected_entity_ids, old_keys)


def _rebuild_affected_communities(
    db: Session,
    scope_key: str,
    affected_entity_ids: set[str],
    old_community_keys: set[str],
) -> None:
    """基于「受影响旧 Community 全部成员 + 受影响实体」重算连通分量。

    支持：新增边合并、桥接边删除拆分、实体失去全部 provenance 删除。
    未受影响 Community 不改动（只重算受影响旧 community 成员集合）。
    """
    # 1. 受影响旧 community 的全部成员（有界：按 community_key 精确查询，不扫 scope）
    member_entities: set[str] = set(affected_entity_ids)
    for ckey in sorted(old_community_keys):
        rows = db.query(V4GraphEntity.id).filter(V4GraphEntity.community_key == ckey).all()
        member_entities.update(r[0] for r in rows)

    # 2. 这些成员之间的所有当前关系（有界：只查这些实体的边，不扫 scope）
    if not member_entities:
        _cleanup_empty_communities(db, scope_key)
        return
    member_list = sorted(member_entities)
    relations = (
        db.query(V4GraphRelation)
        .filter(
            V4GraphRelation.acl_scope == scope_key,
            (V4GraphRelation.source_id.in_(member_list)) | (V4GraphRelation.target_id.in_(member_list)),
        )
        .order_by(V4GraphRelation.id)
        .all()
    )

    # 3. 收集这些关系两端涉及的全部实体（可能含新邻居，用于合并）
    subgraph_entities: set[str] = set(member_entities)
    for r in relations:
        subgraph_entities.add(r.source_id)
        subgraph_entities.add(r.target_id)

    # 4. 在子图上算连通分量（确定性）
    edges = set()
    for r in relations:
        if r.source_id != r.target_id:
            edges.add(tuple(sorted((r.source_id, r.target_id))))
    components = _connected_components(subgraph_entities, sorted(edges))

    # 5. 实体 map（只加载子图实体）
    entity_map = {e.id: e for e in db.query(V4GraphEntity).filter(V4GraphEntity.id.in_(sorted(subgraph_entities))).all()}

    # 6. 写入 community 归属
    for comp in components:
        if not comp:
            continue
        rep_id = sorted(comp)[0]
        rep = entity_map.get(rep_id)
        if rep is None:
            continue
        ckey = f"{scope_key}|{rep.normalized_name}"
        community = db.get(V4GraphCommunity, ckey)
        if community is None:
            community = V4GraphCommunity(key=ckey, display_name=rep.display_name, acl_scope=scope_key)
            db.add(community)
        else:
            community.display_name = rep.display_name
        for eid in comp:
            ent = entity_map.get(eid)
            if ent is not None:
                ent.community_key = ckey

    _cleanup_empty_communities(db, scope_key)


def _cleanup_empty_communities(db: Session, scope_key: str) -> None:
    """删除无实体归属的 community（有界：只检查该 scope 的 community）。"""
    comms = db.query(V4GraphCommunity).filter(V4GraphCommunity.acl_scope == scope_key).all()
    for com in comms:
        has_entity = db.query(V4GraphEntity.id).filter(V4GraphEntity.community_key == com.key).first()
        if has_entity is None:
            db.delete(com)


def _connected_components(entity_ids: set[str], edges: list[tuple[str, str]]) -> list[set[str]]:
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
    for e in sorted(entity_ids):
        groups.setdefault(find(e), set()).add(e)
    return sorted([g for g in groups.values()], key=lambda s: tuple(sorted(s)))
