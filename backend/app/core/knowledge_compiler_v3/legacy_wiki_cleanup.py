"""旧 Community 驱动 Wiki 的一次性清理方案（J-2）。

目标：识别并清理「旧 Community/Card 驱动」遗留的 draft Wiki，同时绝不误删
含人工编辑、锁定或保护内容的历史记录。

判定原则（纯函数、幂等、可审计）：

1. 旧 Community 驱动 Wiki 的识别特征（满足任一即视为遗留）：
   - community_id 非空（旧 Community 来源）；
   - community_key 非空（P20 按稳定 key upsert 的旧字段）；
   - 标题匹配内部聚类编号「社区 N」/「社区N」（旧 Community 名称）。

2. 「可清理（auto_generated_only）」：同时满足——
   - status == "draft"（从未发布，普通用户不可见）；
   - page.locked 为 False/None（整页未人工锁定）；
   - 所有 WikiRevision.edit_type 都不是 "manual"（无人工编辑溯源）；
   - 所有 WikiRevision.updated_by 为空（无人工编辑者）；
   - 所有 WikiSection 无 locked=True、content_origin=="manual"、
     merge_policy=="protected"（无人工保护块）。

3. 「必须人工处理（human_content_present）」：是旧 Community 遗留，但存在
   published 状态、整页锁定、或任意 Revision/Section 的人工编辑/保护痕迹，
   一律只报告、不删除。

4. 非旧 Community 遗留（新版 Page 驱动 Wiki）不进入本方案处理范围。

硬约束：
- 默认只做 dry-run（apply=False 时绝不写库）；
- 本模块不直接操作真实 notes.db；只接受显式传入的 engine/session；
- 删除范围最小：只删可清理 Wiki 及其 Revision/Section/VersionSource/Link；
- 幂等：重复运行结果一致，已删除记录下次不再出现，判定不改变。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.database import (
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
)

logger = logging.getLogger(__name__)

# 内部聚类编号标题：社区 1 / 社区1 / 社区 12 等（含全角空格）。
_COMMUNITY_TITLE_RE = re.compile(r"^社区\s*\d+$")

# 人工编辑/保护痕迹字段的可空安全判定。


def _is_true(value) -> bool:
    return value is True


@dataclass
class LegacyWikiVerdict:
    """单个旧 Community Wiki 的清理判定结果。"""

    wiki_page_id: str
    title: str
    status: str
    community_id: str | None
    community_key: str | None
    # 判定结果：auto_generated_only（可清理）/ human_content_present（必须人工处理）
    verdict: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class CleanupReport:
    """整体清理报告（dry-run 或 apply 后返回）。"""

    total_legacy: int = 0
    cleanable: list[LegacyWikiVerdict] = field(default_factory=list)
    human_protected: list[LegacyWikiVerdict] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # apply 时实际删除的 wiki id


def _is_legacy_community_wiki(page: WikiPage) -> bool:
    """判断是否为旧 Community 驱动 Wiki（标题/community_id/community_key）。"""
    if page.community_id or page.community_key:
        return True
    if _COMMUNITY_TITLE_RE.match((page.title or "").strip()):
        return True
    return False


def _human_trace_reasons(db: Session, page: WikiPage) -> list[str]:
    """收集 Wiki 的人工编辑/锁定/保护痕迹，返回结构化原因列表（空 = 无人工内容）。"""
    reasons: list[str] = []
    if page.status != "draft":
        reasons.append(f"status={page.status}")
    if _is_true(page.locked):
        reasons.append("page.locked=true")

    revisions = (
        db.query(WikiRevision)
        .filter(WikiRevision.wiki_page_id == page.id)
        .all()
    )
    rev_ids = [r.id for r in revisions]
    for rev in revisions:
        if rev.edit_type == "manual":
            reasons.append(f"revision.{rev.id}.edit_type=manual")
        if rev.updated_by:
            reasons.append(f"revision.{rev.id}.updated_by={rev.updated_by}")

    if rev_ids:
        sections = (
            db.query(WikiSection)
            .filter(WikiSection.revision_id.in_(rev_ids))
            .all()
        )
        for sec in sections:
            if _is_true(sec.locked):
                reasons.append(f"section.{sec.id}.locked=true")
            if sec.content_origin == "manual":
                reasons.append(f"section.{sec.id}.content_origin=manual")
            if sec.merge_policy == "protected":
                reasons.append(f"section.{sec.id}.merge_policy=protected")

    return reasons


def classify_legacy_wiki(db: Session, page: WikiPage) -> LegacyWikiVerdict:
    """判定单个 Wiki 的清理结论（纯函数，不写库）。"""
    base = LegacyWikiVerdict(
        wiki_page_id=page.id,
        title=page.title or "",
        status=page.status or "",
        community_id=page.community_id,
        community_key=page.community_key,
        verdict="auto_generated_only",
    )
    reasons = _human_trace_reasons(db, page)
    if reasons:
        base.verdict = "human_content_present"
        base.reasons = reasons
    return base


def scan_legacy_wikis(db: Session) -> CleanupReport:
    """扫描全部旧 Community 驱动 Wiki，返回分类报告（只读）。"""
    report = CleanupReport()
    pages = db.query(WikiPage).all()
    legacy = [p for p in pages if _is_legacy_community_wiki(p)]
    report.total_legacy = len(legacy)
    for page in legacy:
        verdict = classify_legacy_wiki(db, page)
        if verdict.verdict == "auto_generated_only":
            report.cleanable.append(verdict)
        else:
            report.human_protected.append(verdict)
    return report


def cleanup_legacy_wikis(db: Session, *, apply: bool = False) -> CleanupReport:
    """执行清理方案。

    - apply=False：只扫描报告（dry-run），绝不写库。
    - apply=True：删除「可清理」的旧 Community Wiki 及其关联记录；
      含人工痕迹的旧 Wiki 一律保留并报告，不删除。

    幂等：删除仅针对 cleanable 列表；再次运行同一输入时 cleanable 中已删除的
    记录不再出现，判定结果稳定不变。
    """
    report = scan_legacy_wikis(db)
    if not apply:
        return report

    for verdict in report.cleanable:
        wiki_id = verdict.wiki_page_id
        page = db.get(WikiPage, wiki_id)
        if page is None:
            continue
        revisions = db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki_id).all()
        rev_ids = [r.id for r in revisions]
        if rev_ids:
            db.query(WikiSection).filter(WikiSection.revision_id.in_(rev_ids)).delete(
                synchronize_session=False
            )
        db.query(WikiVersionSource).filter(WikiVersionSource.wiki_page_id == wiki_id).delete(
            synchronize_session=False
        )
        db.query(WikiLink).filter(
            (WikiLink.source_page_id == wiki_id) | (WikiLink.target_page_id == wiki_id)
        ).delete(synchronize_session=False)
        db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki_id).delete(
            synchronize_session=False
        )
        db.delete(page)
        report.applied.append(wiki_id)

    db.commit()
    return report
