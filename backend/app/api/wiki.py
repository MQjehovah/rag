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
- GET  /api/wiki/{wiki_id}/revisions/{revision_id}/sections/{section_id}/evidence
      → Section Evidence 追溯（只读；Phase 8C）
- GET  /api/wiki/{wiki_id}/diagnostics   → 编辑者只读诊断（只读；Phase 8C；?revision_id= 绑定查看目标 Revision）

"""
from __future__ import annotations

import json
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
from app.core.wiki_skills.api_reference.display import section_api_view
from app.core.wiki_skills.schemas import SELECTED_BY_VALUES
from app.models.database import (
    EvidenceItem,
    Page,
    WikiLink,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
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


def _pipeline_enabled() -> bool:
    """统一 DB-first kill switch 门（scheduler / API / 删除入口共用同一读取函数）。

    返回 True = wiki.default 编译启用（可建 queued run）；False = kill（暂停编译，
    不建 run、保持 dirty、绝不回退 legacy builder）。
    """
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import (
        _pipeline_kill_switch_enabled,
    )
    return _pipeline_kill_switch_enabled()


def _enqueue_rebuild_batches(db: Session) -> dict:
    """Page 驱动全量重建入队：按 workspace 分组 → 每 workspace 建一个 batch_rebuild
    CompileRun（queued run 由 wiki.default worker 泵执行）。

    - 加载全部非空 Page，用 page_workspace_id（只读，勿 auto-create）分组；
    - 每组调 wiki_default.create_batch_run 后统一 commit；
    - kill off：不建 run，返回 skipped=True（不偷偷回退 legacy builder）。
    """
    if not _pipeline_enabled():
        return {
            "skipped": True, "workspaces": 0, "runs": 0, "pages": 0, "unbound": 0,
            "message": "kill switch off：跳过",
        }
    from app.core.wiki_workspace.routing import page_workspace_id
    from app.core.wiki_pipeline.pipelines.wiki_default import create_batch_run

    pages = db.query(Page).filter(Page.content.isnot(None), Page.content != "").all()
    groups: dict[str, list[str]] = {}
    unbound = 0
    for p in pages:
        ws = page_workspace_id(db, p)
        if ws:
            groups.setdefault(ws, []).append(p.id)
        else:
            unbound += 1
    run_count = 0
    for ws_id, ids in sorted(groups.items()):
        create_batch_run(db, workspace_id=ws_id, page_ids=ids)
        run_count += 1
    db.commit()
    total_pages = sum(len(ids) for ids in groups.values())
    return {
        "skipped": False, "workspaces": len(groups), "runs": run_count,
        "pages": total_pages, "unbound": unbound,
        "message": f"已提交 {run_count} 个 workspace batch 编译任务（共 {total_pages} 页）",
    }


def _refresh_dirty_wikis_scheduled(db: Session) -> dict:
    """dirty Wiki → schedule_wiki_rebuild（内部已 DB-first kill 门控、建 manual_rebuild run）。

    kill off：提交数 0、message 说明；不调 legacy builder。返回汇总统计 dict。
    """
    if not _pipeline_enabled():
        return {
            "skipped": True, "submitted": 0, "rejected": 0,
            "message": "kill switch off：跳过（保持 dirty）",
        }
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_wiki_rebuild

    dirty_wikis = db.query(WikiPage.id).filter(WikiPage.dirty.is_(True)).all()
    submitted = 0
    rejected = 0
    for (wiki_id,) in dirty_wikis:
        if schedule_wiki_rebuild(wiki_id):
            submitted += 1
        else:
            rejected += 1
    return {
        "skipped": False, "submitted": submitted, "rejected": rejected,
        "message": "Page 驱动 dirty 刷新已提交",
    }


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
        section_role, display = section_api_view(
            sec.structure_json, sec.content or "",
            content_origin=sec.content_origin,
            merge_policy=sec.merge_policy,
            locked=sec.locked,
            validation_status=sec.validation_status,
        )
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
            # Phase 8B：展示 DTO 增量字段（合法时受限 DTO；否则 null）。
            "section_role": section_role,
            "display": display,
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
    """Page 驱动的后台全量构建（Phase 5.2：每 workspace 建 batch_rebuild CompileRun）。

    只把任务落为 DB queued run（由 wiki.default worker 泵执行），不调 LLM、不调任何
    legacy builder。running=True 已在 _spawn_page_build 持有锁时设置，这里只更新
    进度/统计。kill off：不建 run、更新 message 后返回（不偷偷回退 legacy）。
    """
    global _page_build_state
    _page_build_state.update({
        "processed": 0, "total": 0, "created": 0,
        "updated": 0, "skipped": 0, "failed": 0, "message": "加载原始文档...",
    })

    engine = get_shared_engine()
    from app.models.database import get_session as _get_session
    db = _get_session(engine)
    try:
        result = _enqueue_rebuild_batches(db)
        _page_build_state.update({
            "processed": result.get("pages", 0),
            "total": result.get("pages", 0),
            "created": result.get("runs", 0),
            "skipped": result.get("unbound", 0),
            "updated": 0,
            "failed": 0,
            "message": result.get("message", ""),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("page wiki batch enqueue failed")
        _page_build_state["failed"] += 1
        _page_build_state["message"] = f"batch 入队失败：{exc}"
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
def refresh_page_dirty(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Page 驱动的 dirty 刷新（V4 Phase C 正式入口，Phase 5.2 单轨 CompileRun）。

    扫描 WikiPage.dirty=true → 每个 dirty Wiki 调 schedule_wiki_rebuild（内部 DB-first
    kill switch 门控、建 manual_rebuild queued run 由 worker 泵消费）。kill off →
    提交数 0 且保持 dirty；不再调用 legacy refresh_dirty_wikis。
    """
    _require_enabled(db)
    _require_admin(current_user)
    result = _refresh_dirty_wikis_scheduled(db)
    return {"message": result["message"], **result}


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


# ---------------------------------------------------------------------------
# Phase 8C：Section Evidence 追溯 + 编辑者只读诊断（全部只读，绝不 flush/commit）
# ---------------------------------------------------------------------------

_EVIDENCE_CONTENT_LIMIT = 2000

# Skill decision reason_code 受控回放集合（服务端可产生的全部 code）；其余一律 "unknown"。
_CONTROLLED_REASON_CODES = frozenset({
    "MANUAL_OVERRIDE", "MANUAL_UNLOCK",
    "SKILL_LOCKED", "LOCKED_SKILL_MISSING", "SKILL_STICKY_CURRENT",
    "SKILL_STICKY_MARGIN", "ONLY_DEFAULT_AVAILABLE", "MIGRATION_PROPOSED",
    "DETERMINISTIC_HIGH_CONFIDENCE", "NO_LLM_ROUTER",
    "LLM_TIMEOUT", "LLM_ERROR", "LLM_INVALID_RESPONSE", "LLM_UNKNOWN_SKILL",
    "LLM_UNKNOWN_VERSION", "LLM_OUT_OF_CANDIDATES", "LOW_LLM_CONFIDENCE",
    "LLM_HIGH_CONFIDENCE", "NO_CANDIDATES", "NO_DEFAULT_AVAILABLE",
    "SKILL_NOT_APPLICABLE", "SKILL_ROUTE_ERROR", "SKILL_MATCH_FALLBACK",
    "MIGRATION_APPLIED",
})


def _parse_evidence_locator(locator_json: str | None) -> dict:
    """locator 白名单解析：仅 page_number(int)/heading/image_id/content_type。

    解析失败或类型不符 → 对应字段 null；绝不返回 chunk_id/bbox 等内部字段。
    """
    out = {"page_number": None, "heading": None, "image_id": None, "content_type": None}
    try:
        parsed = json.loads(locator_json or "{}")
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        page_number = parsed.get("page_number")
        if isinstance(page_number, int) and not isinstance(page_number, bool):
            out["page_number"] = page_number
        for key in ("heading", "image_id", "content_type"):
            value = parsed.get(key)
            if isinstance(value, str):
                out[key] = value
    return out


def _clip_evidence_content(content: str | None) -> tuple[str, bool]:
    """Evidence 正文截断到 2000 字符；截断时置标记。"""
    text = content or ""
    if len(text) <= _EVIDENCE_CONTENT_LIMIT:
        return text, False
    return text[:_EVIDENCE_CONTENT_LIMIT], True


def _evidence_state(status: str | None, hash_matches: bool) -> str:
    """Evidence state 派生（契约 §1）。未知状态 fail-closed 为 unknown。"""
    if status == "active":
        return "active_current" if hash_matches else "changed"
    if status == "stale":
        return "stale"
    if status == "rejected":
        return "rejected"
    return "unknown"


@router.get(
    "/{page_id}/revisions/{revision_id}/sections/{section_id}/evidence"
)
def get_section_evidence(
    page_id: str,
    revision_id: str,
    section_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Section Evidence 追溯（只读，Phase 8C）。

    鉴权链（任一不满足/不存在 → 统一 404）：
    1. Wiki 可见（对齐 _visible_wiki 语义）；
    2. Revision 属于该 Wiki（普通读者仅 page.current_revision_id 且 published；
       admin 可看该 Wiki 任意 revision）；
    3. Section 属于该 Revision；
    4. Evidence 由该 Section 的 Binding 引用；
    5. Evidence.source_page_id ∈ 授权 Page 集合（不可见不返回、不计 total）。
    """
    _require_enabled(db)
    page = _visible_wiki_or_404(db, current_user, page_id)
    admin = is_admin_user(current_user)

    revision = db.query(WikiRevision).filter(
        WikiRevision.id == revision_id,
        WikiRevision.wiki_page_id == page.id,
    ).first()
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision 不存在")
    if not admin:
        if page.current_revision_id != revision_id or revision.status != "published":
            raise HTTPException(status_code=404, detail="Revision 不存在")

    section = db.query(WikiSection).filter(WikiSection.id == section_id).first()
    if section is None or section.revision_id != revision_id:
        raise HTTPException(status_code=404, detail="Section 不存在")

    visible_page_ids = access_control.get_visible_page_ids(db, current_user)

    bindings = (
        db.query(WikiSectionEvidenceBinding)
        .filter(WikiSectionEvidenceBinding.section_id == section.id)
        .order_by(
            WikiSectionEvidenceBinding.created_at,
            WikiSectionEvidenceBinding.id,
        )
        .all()
    )
    # 授权后聚合：同 evidence 多条 binding → 单 item，bindings 稳定排序。
    order: list[str] = []
    bindings_by_ev: dict[str, list[WikiSectionEvidenceBinding]] = {}
    evidence_rows: dict[str, EvidenceItem] = {}
    titles: dict = {}
    if bindings:
        evidence_rows = {
            e.id: e
            for e in db.query(EvidenceItem)
            .filter(EvidenceItem.id.in_([b.evidence_id for b in bindings]))
            .all()
        }
        visible_src_ids = {
            e.source_page_id for e in evidence_rows.values()
            if e.source_page_id in visible_page_ids
        }
        if visible_src_ids:
            titles = dict(
                db.query(Page.id, Page.title)
                .filter(Page.id.in_(visible_src_ids))
                .all()
            )
        for b in bindings:
            ev = evidence_rows.get(b.evidence_id)
            if ev is None:
                continue  # Evidence 物理不存在：不伪造记录
            if ev.source_page_id not in visible_page_ids:
                continue  # 来源不可见：不返回、不计入 total
            if b.evidence_id not in bindings_by_ev:
                bindings_by_ev[b.evidence_id] = []
                order.append(b.evidence_id)
            bindings_by_ev[b.evidence_id].append(b)

    total = len(order)
    items = []
    for eid in order[offset:offset + limit]:
        ev = evidence_rows[eid]
        rows = bindings_by_ev[eid]
        content, truncated = _clip_evidence_content(ev.content)
        # 同 evidence 全部 binding 快照 hash 都与当前 Evidence 内容一致才为 true。
        hash_matches = bool(ev.content_hash) and all(
            r.evidence_content_hash == ev.content_hash for r in rows
        )
        items.append({
            "evidence_id": ev.id,
            "evidence_type": ev.evidence_type,
            "status": ev.status,
            "hash_matches": hash_matches,
            "state": _evidence_state(ev.status, hash_matches),
            "content": content,
            "content_truncated": truncated,
            "locator": _parse_evidence_locator(ev.locator_json),
            "source_display_name": titles.get(ev.source_page_id) or "",
            "bindings": [
                {"field_path": r.field_path, "usage_type": r.usage_type}
                for r in rows
            ],
        })

    return {
        "wiki_id": page.id,
        "revision_id": revision_id,
        "section_id": section.id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def _parse_skill_decision(page: WikiPage) -> dict | None:
    """安全读取 skill_decision_json（仅作受控字段回放；失败/非对象 → None）。"""
    raw = page.skill_decision_json
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _skill_selected_by(page: WikiPage, decision: dict | None) -> str | None:
    """受限「选择方式」方法枚举（方法，不是操作者姓名）。

    读取真实存储：优先 page.skill_selected_by；为空时回退 decision.selected_by。
    值 ∈ SELECTED_BY_VALUES 则原样（auto/manual/migration/default_fallback/
    locked/sticky），否则 None（不折叠成 auto/manual/none）。
    """
    method = page.skill_selected_by
    if not method and decision:
        raw = decision.get("selected_by")
        method = raw if isinstance(raw, str) and raw else None
    if not method:
        return None
    return method if method in SELECTED_BY_VALUES else None


def _skill_display_name(key: str | None, version: str | None) -> str | None:
    """display_name 尽量经既有 Skill Registry 解析；无法解析 → None。"""
    if not key:
        return None
    try:
        from app.core.wiki_skills import registry as skill_registry

        info = skill_registry.snapshot().get(key)
        if not info:
            return None
        versions = info.get("versions") or {}
        desc = None
        if version and version in versions:
            desc = versions[version]
        if desc is None:
            active = info.get("active_version")
            if active and active in versions:
                desc = versions[active]
        if desc is None and versions:
            try:
                first = next(iter(versions))
            except StopIteration:  # pragma: no cover
                first = None
            if first and first in versions:
                desc = versions[first]
        if desc is not None and hasattr(desc, "get"):
            label = desc.get("label")
            return label if isinstance(label, str) and label else None
        return None
    except Exception:  # noqa: BLE001 - 展示字段尽力而为，绝不外泄内部错误
        return None


def _skill_diagnostics(page: WikiPage) -> dict:
    """Skill 只读诊断（只回放受控字段；不返回 decision JSON/Prompt/候选/原始 reason）。"""
    decision = _parse_skill_decision(page)
    key = page.content_skill
    if not key and decision:
        selected = decision.get("selected_skill")
        key = selected if isinstance(selected, str) and selected else None
    version = page.skill_version
    if not version and decision:
        selected_version = decision.get("selected_version")
        version = (
            selected_version
            if isinstance(selected_version, str) and selected_version
            else None
        )
    reason = ""
    if decision:
        raw = decision.get("reason_code")
        reason = raw if isinstance(raw, str) and raw in _CONTROLLED_REASON_CODES else ""
    return {
        "key": key,
        "display_name": _skill_display_name(key, version),
        "version": version,
        "selected_by": _skill_selected_by(page, decision),
        "locked": bool(page.skill_locked),
        "reason_code": reason or "unknown",
    }


def _validation_payload(db: Session, revision_id: str | None) -> dict:
    """validation 摘要（取授权后的目标查看 revision 的 sections）。

    任一 fail→fail；否则任一 NULL/unknown→unknown；全 pass→pass；无 section → unknown。
    """
    sections: list[dict] = []
    if revision_id:
        rows = (
            db.query(WikiSection)
            .filter(WikiSection.revision_id == revision_id)
            .order_by(WikiSection.order_index, WikiSection.id)
            .all()
        )
        for sec in rows:
            heading = (sec.heading or "").strip() or (sec.section_type or "")
            vs = sec.validation_status
            sections.append({
                "heading": heading,
                "validation_status": vs if vs in ("pass", "fail") else "unknown",
            })
    if not sections:
        summary = "unknown"
    else:
        values = [s["validation_status"] for s in sections]
        if "fail" in values:
            summary = "fail"
        elif any(v == "unknown" for v in values):
            summary = "unknown"
        else:
            summary = "pass"
    return {"summary": summary, "sections": sections}


def _resolve_diagnostics_revision(
    db: Session,
    page: WikiPage,
    current_user: dict,
    revision_id: str | None,
) -> str:
    """解析 diagnostics 查看目标 Revision（统一 404，不区分不存在/无权/不属于）。

    - 缺省 = page.current_revision_id；
    - Revision 必须属于该 Wiki；
    - 可见性沿用 Wiki 详情规则：非 admin（含 wiki_editor）仅当前 published；
      admin 可查看属于该 Wiki 的任意 Revision。
    """
    view_id = revision_id or page.current_revision_id
    if not view_id:
        raise HTTPException(status_code=404, detail="Revision 不存在")
    target = (
        db.query(WikiRevision)
        .filter(
            WikiRevision.id == view_id,
            WikiRevision.wiki_page_id == page.id,
        )
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Revision 不存在")
    if not is_admin_user(current_user):
        if page.current_revision_id != view_id or target.status != "published":
            raise HTTPException(status_code=404, detail="Revision 不存在")
    return target.id


@router.get("/{page_id}/diagnostics")
def get_wiki_diagnostics(
    page_id: str,
    revision_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """编辑者只读诊断（Phase 8C）：admin 或可编辑（wiki_editor）用户。

    - Wiki 不可见 → 404（对齐 Wiki 详情规则：非 admin 仅 published + ACL 可见）；
      可见但无编辑权（普通读者）→ 403。
    - 可选 query `revision_id`：校验 Revision 属于该 Wiki 并按详情规则授权
      （非 admin 仅 current published；admin 任意）。validation.sections 取该
      目标 Revision；顶层返回实际查看的 `revision_id`。skill 摘要恒为当前 Wiki 配置。
    """
    _require_enabled(db)
    page = _visible_wiki_or_404(db, current_user, page_id)
    if not access_control.can_edit_wiki(db, current_user, page):
        raise HTTPException(status_code=403, detail="无权编辑该 Wiki")
    view_revision_id = _resolve_diagnostics_revision(db, page, current_user, revision_id)
    return {
        "wiki_id": page.id,
        "editable": True,
        "is_current_wiki_config": True,
        "revision_id": view_revision_id,
        "skill": _skill_diagnostics(page),
        "validation": _validation_payload(db, view_revision_id),
    }
