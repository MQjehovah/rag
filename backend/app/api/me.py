"""当前用户的个人数据接口: 最近访问 / 收藏 / 模板 / 聊天记录。

服务端按 current_user["id"] 隔离(替代前端 localStorage):
一律不接受客户端传入 user_id;按 id 的 UPDATE/DELETE 必须带
user_id == 当前用户 条件,不命中即 404;未登录经 get_current_user 401。
"""
import json
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.search_common import page_visible_condition
from app.core.jwt_utils import get_current_user
from app.models.database import (
    Page,
    User,
    UserChatMessage,
    UserPageFavorite,
    UserRecentPage,
    UserTemplate,
    UserTemplateRevision,
)

router = APIRouter(prefix="/api/me", tags=["用户数据"])

RECENT_LIMIT_DEFAULT = 5
RECENT_LIMIT_MAX = 50
CHAT_LIMIT_DEFAULT = 200
CHAT_LIMIT_MAX = 500
TEMPLATE_NAME_MAX = 128
TEMPLATE_CONTENT_MAX = 200_000
TEMPLATE_VISIBILITIES = ("private", "public")
# 每模板保留的最近修订数(超出删最旧)
TEMPLATE_REVISION_LIMIT = 20
# 修订列表返回的内容预览截断长度
TEMPLATE_REVISION_PREVIEW_MAX = 500
CHAT_CONTENT_MAX = 100_000
CHAT_SOURCES_MAX = 200_000
CHAT_ROLES = ("user", "assistant")


class PageIdBody(BaseModel):
    page_id: str = ""


class TemplateBody(BaseModel):
    name: str = ""
    content: str = ""
    # private(默认, 仅本人) | public(共享给所有用户只读); PUT 缺省=None 表示不改可见性
    visibility: Optional[str] = None


class ChatMessageBody(BaseModel):
    role: str = ""
    content: str = ""
    sources: Optional[Any] = None


def _clamp_limit(raw: int, default: int, maximum: int) -> int:
    """limit 归一:非法值取默认,并夹取到 1..maximum。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _get_visible_page_or_404(db: Session, current_user, page_id: str) -> Page:
    """页面存在、未删、可见(admin 恒真)才放行,否则 404(不泄露存在性)。"""
    page = (
        db.query(Page)
        .filter(
            Page.id == page_id,
            Page.deleted_at.is_(None),
            page_visible_condition(current_user),
        )
        .first()
    )
    if page is None:
        raise HTTPException(status_code=404, detail="笔记不存在或不可见")
    return page


def _page_items(db: Session, current_user, model, order_col, limit: Optional[int] = None) -> list:
    """用户页面关联(最近/收藏) join Page,过滤回收站与不可见页,输出 {id,title,icon}。"""
    query = (
        db.query(Page.id, Page.title, Page.icon)
        .join(model, model.page_id == Page.id)
        .filter(
            model.user_id == current_user["id"],
            Page.deleted_at.is_(None),
            page_visible_condition(current_user),
        )
        .order_by(order_col.desc(), Page.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return [
        {"id": pid, "title": title or "无标题", "icon": icon or ""}
        for pid, title, icon in query.all()
    ]


def _template_out(t: UserTemplate, owner: str = "", is_mine: bool = True) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "content": t.content or "",
        "visibility": t.visibility or "private",
        "owner": owner,
        "owner_id": t.user_id,
        "is_mine": is_mine,
        "updated_at": t.updated_at,
    }


def _parse_sources(raw: Optional[str]) -> list:
    """sources JSON 文本 → list;解析失败回 []。"""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _chat_out(m: UserChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content or "",
        "sources": _parse_sources(m.sources),
        "created_at": m.created_at,
    }


# ---------------- 最近访问 ----------------

@router.get("/recent-pages")
def list_recent_pages(limit: int = RECENT_LIMIT_DEFAULT, db: Session = Depends(get_db),
                      current_user=Depends(get_current_user)):
    limit = _clamp_limit(limit, RECENT_LIMIT_DEFAULT, RECENT_LIMIT_MAX)
    return {"items": _page_items(db, current_user, UserRecentPage, UserRecentPage.visited_at, limit)}


@router.post("/recent-pages")
def touch_recent_page(body: PageIdBody, db: Session = Depends(get_db),
                      current_user=Depends(get_current_user)):
    """记录访问(upsert):存在则仅刷新 visited_at。"""
    page = _get_visible_page_or_404(db, current_user, (body.page_id or "").strip())
    user_id = current_user["id"]
    row = (
        db.query(UserRecentPage)
        .filter(UserRecentPage.user_id == user_id, UserRecentPage.page_id == page.id)
        .first()
    )
    now = datetime.now()
    if row is None:
        db.add(UserRecentPage(id=str(uuid.uuid4()), user_id=user_id, page_id=page.id, visited_at=now))
        try:
            db.commit()
        except IntegrityError:
            # 并发下另一请求已插入同一 (user, page): 回滚后按幂等刷新 visited_at
            db.rollback()
            db.query(UserRecentPage).filter(
                UserRecentPage.user_id == user_id, UserRecentPage.page_id == page.id
            ).update({UserRecentPage.visited_at: now}, synchronize_session=False)
            db.commit()
    else:
        row.visited_at = now
        db.commit()
    return {"ok": True}


@router.delete("/recent-pages")
def clear_recent_pages(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    db.query(UserRecentPage).filter(UserRecentPage.user_id == current_user["id"]).delete(
        synchronize_session=False
    )
    db.commit()
    return {"ok": True}


# ---------------- 收藏 ----------------

@router.get("/favorites")
def list_favorites(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return {"items": _page_items(db, current_user, UserPageFavorite, UserPageFavorite.created_at)}


@router.post("/favorites")
def add_favorite(body: PageIdBody, db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    """收藏(幂等):已存在不报错。"""
    page = _get_visible_page_or_404(db, current_user, (body.page_id or "").strip())
    user_id = current_user["id"]
    exists = (
        db.query(UserPageFavorite.id)
        .filter(UserPageFavorite.user_id == user_id, UserPageFavorite.page_id == page.id)
        .first()
    )
    if exists is None:
        db.add(UserPageFavorite(id=str(uuid.uuid4()), user_id=user_id, page_id=page.id))
        try:
            db.commit()
        except IntegrityError:
            # 并发下另一请求已收藏: 回滚后按幂等成功处理
            db.rollback()
    return {"ok": True}


@router.delete("/favorites")
def clear_favorites(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    db.query(UserPageFavorite).filter(UserPageFavorite.user_id == current_user["id"]).delete(
        synchronize_session=False
    )
    db.commit()
    return {"ok": True}


@router.delete("/favorites/{page_id}")
def remove_favorite(page_id: str, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    """取消收藏(幂等):不存在也返回成功。"""
    db.query(UserPageFavorite).filter(
        UserPageFavorite.user_id == current_user["id"],
        UserPageFavorite.page_id == page_id,
    ).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


# ---------------- 模板 ----------------

def _validate_template(name: str, content: str):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名称不能为空")
    if len(name) > TEMPLATE_NAME_MAX:
        raise HTTPException(status_code=400, detail=f"模板名称不能超过 {TEMPLATE_NAME_MAX} 字符")
    content = content or ""
    if len(content) > TEMPLATE_CONTENT_MAX:
        raise HTTPException(status_code=400, detail=f"模板内容不能超过 {TEMPLATE_CONTENT_MAX} 字符")
    return name, content


def _validate_template_visibility(raw: Optional[str]) -> str:
    visibility = (raw or "private").strip().lower()
    if visibility not in TEMPLATE_VISIBILITIES:
        raise HTTPException(status_code=400, detail="visibility 仅支持 private/public")
    return visibility


def _user_display_name(user: Optional[dict]) -> str:
    if not user:
        return ""
    return user.get("name") or user.get("username") or ""


def _get_own_template_or_404(db: Session, current_user, template_id: str) -> UserTemplate:
    """仅本人模板可读写, 不命中(含他人 public)一律 404。"""
    t = (
        db.query(UserTemplate)
        .filter(UserTemplate.id == template_id, UserTemplate.user_id == current_user["id"])
        .first()
    )
    if t is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return t


def _prune_template_revisions(db: Session, template_id: str):
    """每模板仅保留最近 TEMPLATE_REVISION_LIMIT 条修订, 超出删最旧。"""
    rows = (
        db.query(UserTemplateRevision.id)
        .filter(UserTemplateRevision.template_id == template_id)
        .order_by(UserTemplateRevision.created_at.desc(), UserTemplateRevision.id.desc())
        .all()
    )
    excess = [rid for (rid,) in rows[TEMPLATE_REVISION_LIMIT:]]
    if excess:
        db.query(UserTemplateRevision).filter(UserTemplateRevision.id.in_(excess)).delete(
            synchronize_session=False
        )


def _snapshot_template_revision(db: Session, t: UserTemplate):
    """把模板当前 name/content 存成一条修订快照, 并存后剪枝。"""
    db.add(UserTemplateRevision(
        id=str(uuid.uuid4()),
        template_id=t.id,
        user_id=t.user_id,
        name=t.name or "",
        content=t.content or "",
    ))
    db.flush()  # 先落库, 剪枝排序才能看到新快照
    _prune_template_revisions(db, t.id)


def _revision_out(r: UserTemplateRevision, preview: bool = False) -> dict:
    out = {"id": r.id, "name": r.name or "", "created_at": r.created_at}
    if preview:
        content = r.content or ""
        out["content_preview"] = content[:TEMPLATE_REVISION_PREVIEW_MAX]
        out["content_truncated"] = len(content) > TEMPLATE_REVISION_PREVIEW_MAX
    return out


@router.get("/templates")
def list_templates(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """本人全部模板 + 他人 public 模板(带 owner/is_mine 标记, 共享条目只读)。"""
    uid = current_user["id"]
    rows = (
        db.query(UserTemplate)
        .filter(or_(UserTemplate.user_id == uid, UserTemplate.visibility == "public"))
        .order_by(UserTemplate.updated_at.desc())
        .all()
    )
    other_ids = {t.user_id for t in rows if t.user_id != uid}
    owner_names: dict = {}
    if other_ids:
        owner_names = {
            row.id: (row.name or row.username or "")
            for row in db.query(User.id, User.name, User.username).filter(User.id.in_(other_ids)).all()
        }
    my_name = _user_display_name(current_user)
    return {
        "items": [
            _template_out(
                t,
                owner=my_name if t.user_id == uid else owner_names.get(t.user_id, ""),
                is_mine=t.user_id == uid,
            )
            for t in rows
        ]
    }


@router.post("/templates")
def create_template(body: TemplateBody, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    name, content = _validate_template(body.name, body.content)
    visibility = _validate_template_visibility(body.visibility)
    t = UserTemplate(id=str(uuid.uuid4()), user_id=current_user["id"], name=name,
                     content=content, visibility=visibility)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _template_out(t, owner=_user_display_name(current_user), is_mine=True)


@router.put("/templates/{template_id}")
def update_template(template_id: str, body: TemplateBody, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    name, content = _validate_template(body.name, body.content)
    visibility = _validate_template_visibility(body.visibility) if body.visibility is not None else None
    t = _get_own_template_or_404(db, current_user, template_id)
    # 仅 name/content 实际变化才存修订快照(纯 visibility 切换与同名同内容保存不产生)
    if (t.name or "") != name or (t.content or "") != content:
        _snapshot_template_revision(db, t)
    t.name = name
    t.content = content
    if visibility is not None:
        t.visibility = visibility
    t.updated_at = datetime.now()
    db.commit()
    db.refresh(t)
    return _template_out(t, owner=_user_display_name(current_user), is_mine=True)


@router.get("/templates/{template_id}/revisions")
def list_template_revisions(template_id: str, db: Session = Depends(get_db),
                            current_user=Depends(get_current_user)):
    """本人模板的修订列表(时间倒序; content 以截断预览返回, 避免整表传输)。"""
    t = _get_own_template_or_404(db, current_user, template_id)
    rows = (
        db.query(UserTemplateRevision)
        .filter(UserTemplateRevision.template_id == t.id)
        .order_by(UserTemplateRevision.created_at.desc(), UserTemplateRevision.id.desc())
        .all()
    )
    return {"items": [_revision_out(r, preview=True) for r in rows]}


@router.post("/templates/{template_id}/revisions/{revision_id}/restore")
def restore_template_revision(template_id: str, revision_id: str, db: Session = Depends(get_db),
                              current_user=Depends(get_current_user)):
    """恢复某修订: 先把当前版本存快照, 再以该修订覆盖模板 name/content。"""
    t = _get_own_template_or_404(db, current_user, template_id)
    r = (
        db.query(UserTemplateRevision)
        .filter(
            UserTemplateRevision.id == revision_id,
            UserTemplateRevision.template_id == t.id,
            UserTemplateRevision.user_id == current_user["id"],
        )
        .first()
    )
    if r is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    # 仅恢复目标与当前 name/content 实际不同才存当前版本快照(重复恢复同版本不产生修订)
    if (t.name or "") != (r.name or "") or (t.content or "") != (r.content or ""):
        _snapshot_template_revision(db, t)
    t.name = r.name or ""
    t.content = r.content or ""
    t.updated_at = datetime.now()
    db.commit()
    db.refresh(t)
    return _template_out(t, owner=_user_display_name(current_user), is_mine=True)


@router.delete("/templates/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    t = _get_own_template_or_404(db, current_user, template_id)
    # SQLite 默认不启用外键级联, 手动清理修订(与 FK ondelete CASCADE 双保险)
    db.query(UserTemplateRevision).filter(UserTemplateRevision.template_id == t.id).delete(
        synchronize_session=False
    )
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------- 聊天记录 ----------------

@router.get("/chat-messages")
def list_chat_messages(limit: int = CHAT_LIMIT_DEFAULT, db: Session = Depends(get_db),
                       current_user=Depends(get_current_user)):
    limit = _clamp_limit(limit, CHAT_LIMIT_DEFAULT, CHAT_LIMIT_MAX)
    rows = (
        db.query(UserChatMessage)
        .filter(UserChatMessage.user_id == current_user["id"])
        .order_by(UserChatMessage.created_at.desc(), UserChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()  # 先取最近 N 条, 再翻转为时间升序(重载后可见最新窗口)
    return {"items": [_chat_out(m) for m in rows]}


@router.post("/chat-messages")
def create_chat_message(body: ChatMessageBody, db: Session = Depends(get_db),
                        current_user=Depends(get_current_user)):
    role = (body.role or "").strip()
    if role not in CHAT_ROLES:
        raise HTTPException(status_code=400, detail="role 仅支持 user/assistant")
    content = body.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    if len(content) > CHAT_CONTENT_MAX:
        raise HTTPException(status_code=400, detail=f"消息内容不能超过 {CHAT_CONTENT_MAX} 字符")
    sources = body.sources if body.sources is not None else []
    if not isinstance(sources, list):
        raise HTTPException(status_code=400, detail="sources 必须是数组")
    sources_json = json.dumps(sources, ensure_ascii=False)
    if len(sources_json) > CHAT_SOURCES_MAX:
        raise HTTPException(status_code=400, detail="sources 过大")
    m = UserChatMessage(
        id=str(uuid.uuid4()),
        user_id=current_user["id"],
        role=role,
        content=content,
        sources=sources_json,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return _chat_out(m)


@router.delete("/chat-messages")
def clear_chat_messages(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    db.query(UserChatMessage).filter(UserChatMessage.user_id == current_user["id"]).delete(
        synchronize_session=False
    )
    db.commit()
    return {"ok": True}
