"""Wiki 主题页 API（P6 + P20）。

- GET  /api/wiki                      → 主题目录（普通用户仅 published）
- GET  /api/wiki/{page_id}            → 主题详情（普通用户仅 published + published revision）
- GET  /api/wiki/{page_id}/revisions  → Revision 列表
- GET  /api/wiki/{page_id}/diff/{revision_id} → Diff
- POST /api/wiki/rebuild              → Page 驱动全量重建（不读取 Community/Card）
- POST /api/wiki/{page_id}/preview    → 生成预览 Revision（管理员）
- POST /api/wiki/{page_id}/publish    → 发布指定 revision（管理员）
- POST /api/wiki/{page_id}/archive    → 归档（管理员）
- POST /api/wiki/{page_id}/rollback/{revision_id} → 回滚（管理员）
- POST /api/wiki/{page_id}/approve|reject → 410 废弃
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_shared_engine
from app.core import access_control
from app.core.jwt_utils import get_current_user, is_admin_user
from app.core.knowledge_compiler_v3.wiki_lifecycle import (
    archive_wiki_page,
    diff_wiki_revisions,
    edit_wiki_section_current,
    publish_wiki_revision,
    rollback_wiki,
    set_section_protection_current,
)
from app.core.feature_flags import feature_enabled
from app.models.database import (
    Page,
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSection,
    get_session,
)

router = APIRouter(prefix="/api/wiki", tags=["Wiki 主题"])

logger = logging.getLogger(__name__)

_GONE_REVIEW = "Wiki 不再做事实审核。请使用生成预览 / 发布 / 归档。"

# Page 驱动全量重建任务状态（进程级，不新增表）。同时只能有一个任务。
_page_build_lock = threading.Lock()
_page_build_state: dict = {
    "running": False,
    "processed": 0,
    "total": 0,
    "created": 0,
    "updated": 0,
    "skipped": 0,
    "failed": 0,
    "message": "",
}


class PublishPayload(BaseModel):
    revision_id: str


class SectionUpdatePayload(BaseModel):
    content: str
    category: Optional[str] = None


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _require_edit_wiki(db: Session, current_user: dict, page_id: str) -> WikiPage:
    """编辑类操作：先满足 can_view，再要求 wiki_editor 或 admin（fail closed）。"""
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if page is None:
        raise HTTPException(status_code=404, detail="主题页不存在")
    if not access_control.can_edit_wiki(db, current_user, page):
        raise HTTPException(status_code=403, detail="无权编辑该 Wiki")
    return page


def _require_enabled(db: Session) -> None:
    if not feature_enabled(db, "wiki_topic_enabled"):
        raise HTTPException(status_code=404, detail="Wiki 主题功能未启用")


def _latest_draft_revision(db: Session, page: WikiPage) -> WikiRevision | None:
    return (
        db.query(WikiRevision)
        .filter(
            WikiRevision.wiki_page_id == page.id,
            WikiRevision.status == "draft",
        )
        .order_by(WikiRevision.created_at.desc())
        .first()
    )


def _revision_is_published(db: Session, revision_id: str | None) -> bool:
    if not revision_id:
        return False
    rev = db.query(WikiRevision).filter(WikiRevision.id == revision_id).first()
    return rev is not None and rev.status == "published"


def _serialize_page(db: Session, page: WikiPage, *, admin: bool) -> dict:
    preview = _latest_draft_revision(db, page) if admin else None
    # J-2：正式 Wiki API 不再返回 community_id / community_key（旧 Community 驱动
    # 遗留字段，仅后台兼容只读）。Wiki 标题来自 Page 驱动的真实主题识别。
    return {
        "id": page.id,
        "title": page.title,
        "summary": page.summary or "",
        "status": page.status,
        "locked": bool(page.locked),
        "category": page.category or "未分类",
        "latest_version": page.latest_version,
        "current_revision_id": page.current_revision_id,
        "preview_revision_id": preview.id if preview else None,
        "has_preview": bool(preview),
        "workspace_id": page.workspace_id,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }


def _serialize_revision(rev: WikiRevision) -> dict:
    return {
        "id": rev.id,
        "wiki_page_id": rev.wiki_page_id,
        "parent_revision_id": rev.parent_revision_id,
        "title": rev.title,
        "summary": rev.summary or "",
        "status": rev.status,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


def _visible_wiki_page_ids(db: Session, current_user: dict) -> set[str]:
    """当前用户可见的 WikiPage id 集合。

    Phase B：直接基于 WikiPage.acl_scope 判定。published 状态过滤由调用方
    （list_wiki / _visible_wiki_or_404）负责，权限与状态分离。
    """
    return access_control.get_visible_wiki_page_ids(db, current_user)


def _visible_wiki_or_404(db: Session, current_user: dict, page_id: str) -> WikiPage:
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="主题页不存在")
    admin = is_admin_user(current_user)
    if not admin:
        if page.status != "published" or not _revision_is_published(db, page.current_revision_id):
            raise HTTPException(status_code=404, detail="主题页不存在")
        if page.id not in _visible_wiki_page_ids(db, current_user):
            raise HTTPException(status_code=404, detail="主题页不存在")
        return page
    if page.id not in _visible_wiki_page_ids(db, current_user):
        raise HTTPException(status_code=404, detail="主题页不存在")
    return page


def _sections_payload(
    db: Session,
    revision_id: str,
) -> list[dict]:
    sections = []
    for sec in db.query(WikiSection).filter(
        WikiSection.revision_id == revision_id
    ).order_by(WikiSection.order_index).all():
        sections.append({
            "id": sec.id,
            "section_type": sec.section_type,
            "heading": sec.heading,
            "content": sec.content,
            "locked": bool(sec.locked),
            # V4 Phase I：版本字段（不泄露 Page/Chunk/SourceItem 来源）。
            "version_label": sec.version_label,
            "is_common": bool(sec.is_common),
            "content_origin": sec.content_origin,
            "merge_policy": sec.merge_policy,
            "version_status": sec.version_status,
            "diff_notice": sec.diff_notice,
            "citations": [],
        })
    return sections


def _search_text(db: Session, page: WikiPage) -> str:
    """聚合主题的可搜索文本：标题/摘要/Section 内容。"""
    parts = [page.title, page.summary or ""]
    if page.current_revision_id:
        sections = db.query(WikiSection).filter(
            WikiSection.revision_id == page.current_revision_id
        ).all()
        parts.extend(sec.content or "" for sec in sections)
    return "\n".join(parts)


@router.get("")
def list_wiki(
    q: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """主题目录，支持 q（搜索）、category（分类）、status、workspace_id 过滤，先经 ACL。"""
    _require_enabled(db)
    admin = is_admin_user(current_user)
    visible_ids = _visible_wiki_page_ids(db, current_user)
    if not visible_ids:
        return {"pages": []}
    query = db.query(WikiPage).filter(WikiPage.id.in_(visible_ids)).order_by(WikiPage.updated_at.desc())
    if workspace_id is not None:
        query = query.filter(WikiPage.workspace_id == workspace_id)
    if not admin:
        query = query.filter(WikiPage.status == "published")
    elif status:
        query = query.filter(WikiPage.status == status)
    else:
        query = query.filter(WikiPage.status != "archived")
    pages = query.limit(limit).all()
    if not admin:
        pages = [p for p in pages if _revision_is_published(db, p.current_revision_id)]

    # 搜索：标题/摘要/Section 内容/实体名/引用 Card 标题
    if q:
        needle = q.strip().lower()
        if needle:
            pages = [p for p in pages if needle in _search_text(db, p).lower()]
    if category:
        pages = [p for p in pages if (p.category or "未分类") == category]
    return {"pages": [_serialize_page(db, p, admin=admin) for p in pages]}


def _run_page_build_sync() -> None:
    """Page 驱动的后台全量构建（后台线程内运行，不读取 Card/Community）。

    注意：running=True 已在 _spawn_page_build 持有锁时设置，这里只更新进度/统计。
    """
    global _page_build_state
    _page_build_state.update({
        "processed": 0, "total": 0, "created": 0,
        "updated": 0, "skipped": 0, "failed": 0, "message": "加载原始文档...",
    })

    from app.core.knowledge_compiler_v3.wiki_page_builder import build_wiki_from_pages
    engine = get_shared_engine()
    from app.models.database import get_session as _get_session
    db = _get_session(engine)
    try:
        pages = db.query(Page).filter(Page.content.isnot(None), Page.content != "").all()
        _page_build_state["total"] = len(pages)
        try:
            import asyncio as _asyncio
            stats = _asyncio.run(build_wiki_from_pages(db, pages, commit=True))
        except Exception as exc:  # noqa: BLE001
            logger.exception("page wiki build failed")
            _page_build_state["failed"] += 1
            _page_build_state["message"] = f"构建失败：{exc}"
            return
        _page_build_state.update({
            "processed": len(pages),
            "created": stats.get("created", 0),
            "updated": stats.get("updated", 0),
            "skipped": stats.get("skipped", 0),
            "failed": stats.get("failed", 0),
            "message": "Page 驱动 Wiki 构建完成",
        })
    finally:
        db.close()
        _page_build_state["running"] = False


def _spawn_page_build() -> None:
    # 持有 _page_build_lock 时、启动线程之前设置 running=True，避免连续调用启动两个线程。
    _page_build_state["running"] = True
    threading.Thread(target=_run_page_build_sync, daemon=True, name="wiki-page-build").start()


@router.post("/rebuild")
def rebuild_wiki_topics(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Page 驱动的后台全量构建（V4 Phase C 正式入口）。

    不读取 KnowledgeCommunity / KnowledgeCard；后台执行，不阻塞请求。
    """
    _require_enabled(db)
    _require_admin(current_user)
    with _page_build_lock:
        if _page_build_state["running"]:
            return {"running": True, **_page_build_state}
        _spawn_page_build()
    return {"running": True, **_page_build_state}


@router.get("/rebuild-status")
def rebuild_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回 Page 驱动构建任务状态。"""
    _require_enabled(db)
    _require_admin(current_user)
    return {"running": _page_build_state["running"], **_page_build_state}


@router.post("/refresh-dirty")
def refresh_dirty(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Page 驱动的 dirty 刷新（V4 Phase C 正式入口）。

    只扫描并提交 Page/Wiki 后台任务，立即返回 202，不在 HTTP 请求里等待 LLM。
    """
    _require_enabled(db)
    _require_admin(current_user)
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import (
        recover_dirty_pages,
        schedule_wiki_rebuild,
        get_refresh_status,
    )

    pages_result = recover_dirty_pages()

    # 提交 dirty Wiki 后台任务（不 await LLM）
    from app.models.database import WikiPage
    dirty_wikis = db.query(WikiPage.id).filter(WikiPage.dirty.is_(True)).all()
    submitted_wikis = 0
    rejected_wikis = 0
    for (wiki_id,) in dirty_wikis:
        if schedule_wiki_rebuild(wiki_id):
            submitted_wikis += 1
        else:
            rejected_wikis += 1

    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content={
        "message": "已提交后台刷新",
        "dirty_pages_submitted": pages_result.get("submitted", 0),
        "dirty_pages_rejected": pages_result.get("rejected", 0),
        "dirty_wikis_submitted": submitted_wikis,
        "dirty_wikis_rejected": rejected_wikis,
        "pending": get_refresh_status(),
    })


@router.get("/refresh-status")
def refresh_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """查询后台刷新状态，供前端轮询。"""
    _require_enabled(db)
    _require_admin(current_user)
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import get_refresh_status
    return get_refresh_status()


@router.post("/refresh-page-dirty")
async def refresh_page_dirty(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Page 驱动的 dirty 刷新（V4 Phase C 正式入口）。

    扫描 WikiPage.dirty=true → 读取 source_page_ids → 加载 Page → 按权限域
    校验 → 用 Page 驱动构建器刷新 → 成功 dirty=false，失败继续 dirty=true。
    不读取 Card / Community。
    """
    _require_enabled(db)
    _require_admin(current_user)
    from app.core.knowledge_compiler_v3.wiki_page_builder import refresh_dirty_wikis

    result = await refresh_dirty_wikis(db, commit=True)
    return {"message": "Page 驱动 dirty 刷新完成", **result}


@router.get("/{page_id}")
def get_wiki(
    page_id: str,
    preview: bool = Query(False),
    revision_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    page = _visible_wiki_or_404(db, current_user, page_id)
    admin = is_admin_user(current_user)
    data = _serialize_page(db, page, admin=admin)

    view_revision_id = page.current_revision_id
    if admin and revision_id:
        target = db.query(WikiRevision).filter(
            WikiRevision.id == revision_id,
            WikiRevision.wiki_page_id == page.id,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="Revision 不存在")
        view_revision_id = target.id
    elif admin and preview:
        draft = _latest_draft_revision(db, page)
        if draft:
            view_revision_id = draft.id

    data["sections"] = (
        _sections_payload(db, view_revision_id)
        if view_revision_id else []
    )
    data["viewing_revision_id"] = view_revision_id
    data["related_topics"] = []
    data["related_topic_ids"] = []
    return data


@router.get("/{page_id}/revisions")
def list_revisions(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    _visible_wiki_or_404(db, current_user, page_id)
    query = db.query(WikiRevision).filter(WikiRevision.wiki_page_id == page_id)
    if not is_admin_user(current_user):
        query = query.filter(WikiRevision.status == "published")
    revs = query.order_by(WikiRevision.created_at.desc()).all()
    return {"revisions": [_serialize_revision(r) for r in revs]}


@router.get("/{page_id}/diff/{revision_id}")
def get_diff(
    page_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    page = _visible_wiki_or_404(db, current_user, page_id)
    if not page.current_revision_id:
        raise HTTPException(status_code=404, detail="主题页或当前 revision 不存在")
    if not is_admin_user(current_user):
        if not _revision_is_published(db, revision_id):
            raise HTTPException(status_code=404, detail="Revision 不存在")
    diff = diff_wiki_revisions(db, page.current_revision_id, revision_id)
    return {
        "added": [s.__dict__ for s in diff.added_sections],
        "removed": [s.__dict__ for s in diff.removed_sections],
        "changed": [s.__dict__ for s in diff.changed_sections],
    }


@router.post("/{page_id}/approve")
def approve_wiki(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    _require_admin(current_user)
    raise HTTPException(status_code=410, detail=_GONE_REVIEW)


@router.post("/{page_id}/reject")
def reject_wiki(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    _require_admin(current_user)
    raise HTTPException(status_code=410, detail=_GONE_REVIEW)


@router.post("/{page_id}/preview")
def preview_wiki(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Phase H：旧 Community/Card 驱动预览已退出，统一返回 410。"""
    _require_enabled(db)
    _require_admin(current_user)
    raise HTTPException(status_code=410, detail=_GONE_REVIEW)


@router.post("/{page_id}/archive")
def archive_wiki(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    _require_admin(current_user)
    try:
        page = archive_wiki_page(db, page_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # V4 Phase J-3：归档后移除该 Wiki 图谱 provenance（立即失效）。
    try:
        from app.core.knowledge_compiler_v3.v4_graph_builder import remove_wiki_graph
        remove_wiki_graph(db, page_id, commit=False)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return {"message": "主题页已归档", "page": _serialize_page(db, page, admin=True)}


@router.post("/{page_id}/publish")
def publish_wiki(
    page_id: str,
    payload: PublishPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    _require_admin(current_user)
    try:
        superseded = publish_wiki_revision(db, page_id, payload.revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # V4 Phase F：Wiki 发布后，同 scope 债务有界重验证（携带真实生效内容）。
    _notify_wiki_changed(db, page_id)
    return {"message": "Revision 已发布", "superseded": superseded}


@router.post("/{page_id}/rollback/{revision_id}")
def rollback(
    page_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    # V4 Phase B：回滚 = 把目标 Revision 重新发布到有编辑权的 Wiki。
    # 不涉及 Phase C 数据结构，改为 can_edit_wiki（wiki_editor/admin 可回滚自己有权编辑的 Wiki）。
    _require_edit_wiki(db, current_user, page_id)
    try:
        rollback_wiki(db, page_id, revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # V4 Phase F：Wiki 回滚后，同 scope 债务有界重验证（携带真实生效内容）。
    _notify_wiki_changed(db, page_id)
    return {"message": "已回滚"}


def _notify_wiki_changed(db: Session, page_id: str) -> None:
    """V4 Phase F/J-3：Wiki 内容变化后，读取真实生效内容并触发债务重验证 + 图谱重建。"""
    try:
        from app.core.retrieval.debt_service import notify_knowledge_changed_for_wiki
        from app.core.retrieval.wiki_retriever import _valid_current_revision, _wiki_content
        page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
        if page is None:
            return
        rev = _valid_current_revision(db, page)
        content = _wiki_content(db, page, rev) if rev is not None else ""
        notify_knowledge_changed_for_wiki(db, page.acl_scope, content)
    except Exception:  # noqa: BLE001
        pass
    # V4 Phase J-3：Wiki 发布/回滚后，异步重建该 Wiki 的图谱 provenance（幂等）。
    try:
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_wiki_graph_rebuild
        schedule_wiki_graph_rebuild(page_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph rebuild schedule failed wiki=%s: %s", page_id, exc)


def _draft_section_or_404(
    db: Session,
    page_id: str,
    revision_id: str,
    section_id: str,
) -> WikiSection:
    """校验 section 属于该 page 的 revision，且 revision 为 draft（可编辑）。"""
    section = db.query(WikiSection).filter(WikiSection.id == section_id).first()
    if section is None:
        raise HTTPException(status_code=404, detail="Section 不存在")
    revision = db.query(WikiRevision).filter(WikiRevision.id == revision_id).first()
    if revision is None or revision.wiki_page_id != page_id:
        raise HTTPException(status_code=404, detail="Revision 不存在或不属于该主题")
    if section.revision_id != revision_id:
        raise HTTPException(status_code=404, detail="Section 不属于该 Revision")
    if revision.status != "draft":
        raise HTTPException(status_code=409, detail="仅 Draft 版本可编辑")
    return section


@router.patch("/{page_id}/revisions/{revision_id}/sections/{section_id}")
def update_section(
    page_id: str,
    revision_id: str,
    section_id: str,
    payload: SectionUpdatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """编辑 Section 内容（V4 Phase C：编辑当前版本立即生效）。

    - 若目标 Section 属于当前 Revision：复制 → 修改 → 原子切换 current_revision_id，
      立即生效，保留旧 Revision 供回滚，标记 manual + locked + updated_by。
    - 否则（历史 draft 预览）保持旧逻辑，仅允许 Draft。
    """
    _require_enabled(db)
    page = _require_edit_wiki(db, current_user, page_id)
    updated_by = current_user.get("username") or current_user.get("id")

    # 编辑当前有效版本：立即生效路径
    if page.current_revision_id and revision_id == page.current_revision_id:
        try:
            edit_wiki_section_current(db, page, section_id, payload.content, updated_by, category=payload.category)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        # V4 Phase F：Wiki 人工编辑后，同 scope 债务有界重验证（携带真实生效内容）。
        try:
            from app.core.retrieval.debt_service import notify_knowledge_changed_for_wiki
            notify_knowledge_changed_for_wiki(db, page.acl_scope, payload.content)
        except Exception:  # noqa: BLE001
            pass
        # V4 Phase J-3：人工编辑后异步重建该 Wiki 图谱 provenance（幂等）。
        try:
            from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_wiki_graph_rebuild
            schedule_wiki_graph_rebuild(page_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph rebuild schedule failed wiki=%s: %s", page_id, exc)
        return {"message": "Section 已保存并生效", "locked": True}

    # 历史 draft 预览编辑（兼容旧行为）
    section = _draft_section_or_404(db, page_id, revision_id, section_id)
    section.content = payload.content
    section.locked = True
    revision = db.query(WikiRevision).filter(WikiRevision.id == revision_id).first()
    revision.source_hash = f"manual:{uuid.uuid4().hex}"
    logger.info(
        "wiki section edited",
        extra={"page_id": page_id, "revision_id": revision_id, "section_id": section_id, "updated_by": updated_by},
    )
    db.commit()
    return {"message": "Section 已保存", "locked": True}


@router.post("/{page_id}/revisions/{revision_id}/sections/{section_id}/lock")
def lock_section(
    page_id: str,
    revision_id: str,
    section_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    page = _require_edit_wiki(db, current_user, page_id)
    updated_by = current_user.get("username") or current_user.get("id")

    # V4 Phase I：当前 published 版本块可直接保护（不再仅限 draft）。
    if page.current_revision_id and revision_id == page.current_revision_id:
        try:
            set_section_protection_current(db, page, section_id, protected=True, updated_by=updated_by)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"message": "Section 已锁定", "locked": True, "merge_policy": "protected"}

    section = _draft_section_or_404(db, page_id, revision_id, section_id)
    section.locked = True
    db.commit()
    return {"message": "Section 已锁定", "locked": True}


@router.post("/{page_id}/revisions/{revision_id}/sections/{section_id}/unlock")
def unlock_section(
    page_id: str,
    revision_id: str,
    section_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_enabled(db)
    page = _require_edit_wiki(db, current_user, page_id)
    updated_by = current_user.get("username") or current_user.get("id")

    # V4 Phase I：当前 published 版本块可解除保护，解除后可接受自动刷新。
    if page.current_revision_id and revision_id == page.current_revision_id:
        try:
            set_section_protection_current(db, page, section_id, protected=False, updated_by=updated_by)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"message": "Section 已解锁", "locked": False, "merge_policy": "auto"}

    section = _draft_section_or_404(db, page_id, revision_id, section_id)
    section.locked = False
    db.commit()
    return {"message": "Section 已解锁", "locked": False}
