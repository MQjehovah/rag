"""Wiki 生命周期：预览/发布/归档/Diff/回滚 + dirty + locked（P6 + P20）。

- 新 Revision 不覆盖已发布内容，只产生 preview。
- 发布/回滚/Diff/locked Section 保留。
- P20 废弃 approve/reject 事实审核，改为 archive_wiki_page。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.knowledge_compiler_v3 import versioning
from app.models.database import (
    WikiPage,
    WikiRevision,
    WikiSection,
)

logger = logging.getLogger(__name__)


@dataclass
class WikiSectionChange:
    section_type: str
    heading: str
    kind: str  # added/removed/changed
    old_content: str = ""
    new_content: str = ""


@dataclass
class WikiDiff:
    added_sections: list[WikiSectionChange] = field(default_factory=list)
    removed_sections: list[WikiSectionChange] = field(default_factory=list)
    changed_sections: list[WikiSectionChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added_sections or self.removed_sections or self.changed_sections)


def publish_wiki_revision(db: Session, wiki_page_id: str, revision_id: str) -> list[str]:
    """发布指定 Revision，旧 published revision → superseded（P6-BE-06，原子）。"""
    page = db.query(WikiPage).filter(WikiPage.id == wiki_page_id).first()
    if page is None:
        raise ValueError(f"wiki page 不存在: {wiki_page_id}")
    target = db.query(WikiRevision).filter(WikiRevision.id == revision_id).first()
    if target is None:
        raise ValueError(f"revision 不存在: {revision_id}")

    superseded = []
    try:
        old_published = (
            db.query(WikiRevision)
            .filter(
                WikiRevision.wiki_page_id == wiki_page_id,
                WikiRevision.status == "published",
                WikiRevision.id != revision_id,
            )
            .all()
        )
        for old in old_published:
            old.status = "superseded"
            superseded.append(old.id)

        target.status = "published"
        page.status = "published"
        page.current_revision_id = revision_id
        page.title = target.title
        page.summary = target.summary or ""
        page.source_hash = target.source_hash
        # V4 Phase I：发布/回滚后按目标 Revision 的版本块重算 latest_version，
        # 保证版本结构与 latest 一致回滚。
        labels = [
            r[0] for r in db.query(WikiSection.version_label)
            .filter(WikiSection.revision_id == revision_id).all()
            if r[0] and r[0] not in (versioning.IS_COMMON_LABEL, versioning.UNVERSIONED_LABEL)
        ]
        page.latest_version = versioning.latest_version(labels)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return superseded


def rollback_wiki(db: Session, wiki_page_id: str, revision_id: str) -> None:
    """回滚到指定 Revision（P6-BE-06）：将该 revision 重新发布。"""
    publish_wiki_revision(db, wiki_page_id, revision_id)


def archive_wiki_page(db: Session, wiki_page_id: str) -> WikiPage:
    """归档主题页，不删除 Revision。"""
    page = db.query(WikiPage).filter(WikiPage.id == wiki_page_id).first()
    if page is None:
        raise ValueError(f"wiki page 不存在: {wiki_page_id}")
    page.status = "archived"
    db.commit()
    return page


def _load_sections(db: Session, revision_id: str) -> dict[str, WikiSection]:
    sections = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == revision_id)
        .all()
    )
    return {s.section_type: s for s in sections}


def diff_wiki_revisions(db: Session, old_revision_id: str, new_revision_id: str) -> WikiDiff:
    """对比两个 Revision 的 Sections（P6-BE-06）。"""
    diff = WikiDiff()
    old_sections = _load_sections(db, old_revision_id)
    new_sections = _load_sections(db, new_revision_id)

    for stype, ns in new_sections.items():
        if stype not in old_sections:
            diff.added_sections.append(WikiSectionChange(
                section_type=stype, heading=ns.heading or "", kind="added",
                new_content=ns.content or "",
            ))
        elif old_sections[stype].content != ns.content:
            diff.changed_sections.append(WikiSectionChange(
                section_type=stype, heading=ns.heading or "", kind="changed",
                old_content=old_sections[stype].content or "",
                new_content=ns.content or "",
            ))

    for stype, os in old_sections.items():
        if stype not in new_sections:
            diff.removed_sections.append(WikiSectionChange(
                section_type=stype, heading=os.heading or "", kind="removed",
                old_content=os.content or "",
            ))
    return diff


def update_locked_section(
    db: Session,
    section_id: str,
    new_content: str,
) -> bool:
    """更新 Section 内容；locked 时返回 False（只能提交建议，P6-BE-07）。"""
    section = db.query(WikiSection).filter(WikiSection.id == section_id).first()
    if section is None:
        raise ValueError(f"section 不存在: {section_id}")
    if section.locked:
        return False  # 锁定 → 只能提交建议，不直接改
    section.content = new_content
    db.commit()
    return True


def edit_wiki_section_current(
    db: Session,
    page: WikiPage,
    section_id: str,
    new_content: str,
    updated_by: str,
    *,
    category: str | None = None,
) -> WikiRevision:
    """编辑当前 Wiki 的某个 Section（V4 Phase C：立即生效）。

    流程（不直接修改历史 Revision）：
    1. 校验目标 Section 属于当前 Revision。
    2. 复制当前 Revision 与全部 Sections。
    3. 修改目标 Section（锁定 + edit_type=manual + updated_by）。
    4. 同步 summary（若改的是 summary Section）与 category（若提供）。
    5. 重算 Revision 内容哈希。
    6. 原子切换 current_revision_id → 修改立即生效。
    7. 保留旧 Revision 供回滚。
    """
    cur_rev_id = page.current_revision_id
    if not cur_rev_id:
        raise ValueError("Wiki 尚无当前版本")

    cur_section = db.query(WikiSection).filter(WikiSection.id == section_id).first()
    if cur_section is None or cur_section.revision_id != cur_rev_id:
        raise ValueError("Section 不存在或不属于当前版本")

    cur_rev = db.query(WikiRevision).filter(WikiRevision.id == cur_rev_id).first()
    if cur_rev is None:
        raise ValueError("当前版本不存在")

    # 复制 Revision
    new_rev = WikiRevision(
        id=str(uuid.uuid4()),
        wiki_page_id=page.id,
        parent_revision_id=cur_rev.id,
        title=page.title,
        summary=cur_rev.summary,
        source_hash=cur_rev.source_hash,
        status="published",  # 立即生效（兼容状态语义，不再要求人工发布）
        edit_type="manual",
        updated_by=updated_by,
    )
    db.add(new_rev)
    db.flush()

    # 复制 Sections，并定位目标 Section
    new_target: WikiSection | None = None
    old_sections = db.query(WikiSection).filter(WikiSection.revision_id == cur_rev_id).all()
    for sec in old_sections:
        new_sec = WikiSection(
            id=str(uuid.uuid4()),
            revision_id=new_rev.id,
            section_type=sec.section_type,
            heading=sec.heading,
            content=sec.content,
            order_index=sec.order_index,
            locked=bool(sec.locked),
            version_label=sec.version_label,
            version_sort_key=sec.version_sort_key,
            is_common=sec.is_common,
            content_origin=sec.content_origin,
            merge_policy=sec.merge_policy,
            version_confidence=sec.version_confidence,
            version_status=sec.version_status,
            diff_notice=sec.diff_notice,
        )
        db.add(new_sec)
        if sec.id == section_id:
            new_target = new_sec

    if new_target is None:
        db.rollback()
        raise ValueError("目标 Section 未找到")

    new_target.content = new_content
    new_target.locked = True  # 人工修改后默认锁定，防止自动刷新覆盖
    # V4 Phase I：人工保护粒度 = 对应版本块；编辑者改的是哪个块，只保护那个块。
    new_target.content_origin = "manual"
    new_target.merge_policy = "protected"

    # 同步 summary：若修改的是 summary Section，同步 Revision.summary 与 Page.summary
    new_sections = db.query(WikiSection).filter(WikiSection.revision_id == new_rev.id).all()
    if new_target.section_type == "summary":
        new_rev.summary = new_content
        page.summary = new_content

    # 同步 category（人工编辑分类立即生效并进入版本记录）
    if category is not None:
        page.category = category or None

    # 重算当前 Revision 内容哈希（基于全部新 Sections，而非复用旧 source_hash）
    new_rev.source_hash = _revision_content_hash(new_sections)

    page.current_revision_id = new_rev.id
    db.commit()
    return new_rev


def _revision_content_hash(sections: list[WikiSection]) -> str:
    """根据全部 Sections 计算稳定哈希（修改正文/摘要均能反映）。"""
    payload = [
        {"section_type": s.section_type, "content": s.content or "", "locked": bool(s.locked)}
        for s in sections
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def set_section_protection_current(
    db: Session,
    page: WikiPage,
    section_id: str,
    *,
    protected: bool,
    updated_by: str,
) -> WikiRevision:
    """设置当前 published Revision 的某个 Section（版本块）的保护状态。

    V4 Phase I 修复：原 lock/unlock 只允许 Draft，导致无法解除当前 published
    版本块的锁定。这里复用 edit_wiki_section_current 的「复制 Revision → 原子切换」
    语义，只改 merge_policy/locked/content_origin，不动正文。

    - protected=True：merge_policy='protected'，locked=True，content_origin='manual'。
    - protected=False：merge_policy='auto'，locked=False，content_origin 保持 auto。
      解除后该版本块可接受后续自动刷新。
    保留旧 Revision 供回滚。
    """
    cur_rev_id = page.current_revision_id
    if not cur_rev_id:
        raise ValueError("Wiki 尚无当前版本")

    cur_section = db.query(WikiSection).filter(WikiSection.id == section_id).first()
    if cur_section is None or cur_section.revision_id != cur_rev_id:
        raise ValueError("Section 不存在或不属于当前版本")

    cur_rev = db.query(WikiRevision).filter(WikiRevision.id == cur_rev_id).first()
    if cur_rev is None:
        raise ValueError("当前版本不存在")

    new_rev = WikiRevision(
        id=str(uuid.uuid4()),
        wiki_page_id=page.id,
        parent_revision_id=cur_rev.id,
        title=page.title,
        summary=cur_rev.summary,
        source_hash=cur_rev.source_hash,
        status="published",
        edit_type="manual",
        updated_by=updated_by,
    )
    db.add(new_rev)
    db.flush()

    new_target: WikiSection | None = None
    for sec in db.query(WikiSection).filter(WikiSection.revision_id == cur_rev_id).all():
        new_sec = WikiSection(
            id=str(uuid.uuid4()),
            revision_id=new_rev.id,
            section_type=sec.section_type,
            heading=sec.heading,
            content=sec.content,
            order_index=sec.order_index,
            locked=bool(sec.locked),
            version_label=sec.version_label,
            version_sort_key=sec.version_sort_key,
            is_common=sec.is_common,
            content_origin=sec.content_origin,
            merge_policy=sec.merge_policy,
            version_confidence=sec.version_confidence,
            version_status=sec.version_status,
            diff_notice=sec.diff_notice,
        )
        db.add(new_sec)
        if sec.id == section_id:
            new_target = new_sec

    if new_target is None:
        db.rollback()
        raise ValueError("目标 Section 未找到")

    if protected:
        new_target.locked = True
        new_target.content_origin = "manual"
        new_target.merge_policy = "protected"
    else:
        new_target.locked = False
        new_target.merge_policy = "auto"
        # 解除保护后 origin 回到 auto（内容本身仍保留，但可被刷新合并）。
        new_target.content_origin = "auto"

    new_sections = db.query(WikiSection).filter(WikiSection.revision_id == new_rev.id).all()
    new_rev.source_hash = _revision_content_hash(new_sections)
    page.current_revision_id = new_rev.id
    db.commit()
    return new_rev
