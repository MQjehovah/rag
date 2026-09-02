import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid

from app.api.deps import get_db
from app.core import access_control
from app.models.database import Page, PageChunk, Notebook, NotebookGroup, UserGroup
from app.models.schema import NotebookCreate, NotebookResponse
from app.core.rag import VectorStore
from app.core.jwt_utils import get_current_user, is_admin_user
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notebooks", tags=["笔记本"])

# 本地管理员组：仅适合本地试点，正式上线前应替换为公司 LDAP 业务组
LOCAL_ADMIN_GROUP = "__local_admin__"


class NotebookGroupPayload(BaseModel):
    group_name: str


class NotebookGroupsPayload(BaseModel):
    group_names: List[str]


class NotebookGroupItem(BaseModel):
    id: str
    group_name: str
    notebook_id: str


def _notebook_group_names(db: Session, notebook_id: str) -> List[str]:
    """Notebook 的 notebook_groups 表中额外授权的业务组名（去重排序）。"""
    rows = db.query(NotebookGroup.group_name).filter(
        NotebookGroup.notebook_id == notebook_id
    ).all()
    return sorted({str(r[0]).strip() for r in rows if str(r[0]).strip()})


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _known_group_names(db: Session) -> List[str]:
    """系统已知业务组名：UserGroup.group_name 去重。

    排除 admin/wiki_editor 角色组（含 __local_admin__ 与配置的 LDAP 管理员组/
    编辑组），这些只决定「能做什么」，不能被当作普通业务知识组配置给 Notebook。
    """
    rows = db.query(UserGroup.group_name).distinct().all()
    role = access_control.role_groups()
    return sorted({
        str(r[0]).strip() for r in rows
        if str(r[0]).strip() and str(r[0]).strip() not in role
    })


def _normalize_group_id(db: Session, raw: str | None) -> str | None:
    """统一校验并规范化 Notebook 权限域输入（J-1 最终遗留，fail closed）。

    支持三类：
    - company：None / 空 / "__public__" / "company" → 返回 None（真正清空业务组）；
    - admin：__local_admin__ 或配置的 LDAP 管理员组 → 返回该组名；
    - 一个/多个业务组：必须属于系统已知业务组，未知 → 400（禁止静默降级为 company）。

    供 create / update / access-scope 共用同一套校验。
    """
    raw = (raw or "").strip()
    if not raw or raw in ("__public__", "company"):
        return None
    if raw in access_control._configured_admin_groups():
        return raw
    known = set(_known_group_names(db))
    if raw not in known:
        raise HTTPException(status_code=400, detail=f"未知的业务组：{raw}")
    return raw


def _validate_permissions_groups(db: Session, group_names) -> tuple[str, list[str]]:
    """校验 groups 类型的业务组列表，返回 (主组, 额外组列表)。

    规则：
    - 每个组必须是系统已知业务组（排除 admin/wiki_editor 角色组）；
    - 角色组混入业务组 → 400；
    - 未知组 → 400；
    - 空列表 → 400（不允许空 groups）。
    """
    known = set(_known_group_names(db))
    role = access_control.role_groups()
    normalized: list[str] = []
    for raw in (group_names or []):
        name = str(raw or "").strip()
        if not name:
            continue
        if name in role:
            raise HTTPException(status_code=400, detail=f"角色组不能作为业务组：{name}")
        if name not in known:
            raise HTTPException(status_code=400, detail=f"未知的业务组：{name}")
        if name not in normalized:
            normalized.append(name)
    if not normalized:
        raise HTTPException(status_code=400, detail="groups 类型至少需要一个业务组")
    return normalized[0], normalized[1:]


def _invalidate_wiki_sources_for_pages(db: Session, page_ids: list[str]) -> None:
    """在同一事务中使旧 scope Wiki 立即失效（不等待异步重建）。

    对涉及这些 Page 的 Wiki 移除来源并置为不可发布（唯一来源→archived，
    多来源→draft+dirty）。沿用 wiki_page_builder 的统一实现，commit=False
    保持事务内，保证权限变更与 Wiki 失效原子。
    """
    if not page_ids:
        return
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        remove_source_page_from_wikis,
    )
    for pid in page_ids:
        remove_source_page_from_wikis(db, pid, commit=False)


def _invalidate_graph_for_pages(db: Session, page_ids: list[str]) -> None:
    """在同一事务中使旧 scope 图谱 provenance 立即失效（不等待异步重建）。

    remove_page_graph(commit=False) 删除该 Page 的旧 provenance，并清理孤立
    关系/实体、重建受影响 Community；旧组立即看不到旧节点/关系。
    """
    if not page_ids:
        return
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    for pid in page_ids:
        remove_page_graph(db, pid, commit=False)


def _apply_notebook_permissions(
    db: Session,
    notebook: Notebook,
    payload,
) -> dict:
    """单事务原子更新 Notebook 权限（J-1 最终返工）。

    - 校验全部输入（未知组/角色组/空 groups/非法 scope_type → 400）；
    - 更新 Notebook.group_id；
    - 替换 notebook_groups（company/admin 清空额外组；groups 主组+额外一致不重复）；
    - 立即失效该 Notebook 下所有 Page 的旧 scope Wiki 来源；
    - 任一步抛异常 → 由调用方 rollback，权限完全不变。

    返回 {"group_id", "groups", "page_ids"}；page_ids 供调用方 commit 后
    调度新 scope Wiki 重建。
    """
    scope_type = (payload.scope_type or "").strip()
    if scope_type not in ("company", "admin", "groups"):
        raise HTTPException(status_code=400, detail="scope_type 必须为 company / admin / groups")

    if scope_type == "company":
        group_id: str | None = None
        extra: list[str] = []
    elif scope_type == "admin":
        group_id = LOCAL_ADMIN_GROUP
        extra = []
    else:
        group_id, extra = _validate_permissions_groups(db, payload.group_names)

    notebook.group_id = group_id
    db.query(NotebookGroup).filter(NotebookGroup.notebook_id == notebook.id).delete()
    for name in sorted(set(extra)):
        db.add(NotebookGroup(id=str(uuid.uuid4()), notebook_id=notebook.id, group_name=name))
    db.flush()

    # 识别该 Notebook 下所有 Page 并立即失效其旧 scope Wiki 来源。
    page_ids = [
        r[0] for r in db.query(Page.id).filter(Page.notebook_id == notebook.id).all()
    ]
    if page_ids:
        _invalidate_wiki_sources_for_pages(db, page_ids)
        _invalidate_graph_for_pages(db, page_ids)
    db.flush()

    return {
        "group_id": group_id,
        "groups": sorted(set(extra)),
        "page_ids": page_ids,
    }


def _schedule_wiki_rebuild_for_pages(page_ids: list[str]) -> None:
    """commit 后为新 scope 调度 Wiki + 图谱重建（异步、幂等）。"""
    if not page_ids:
        return
    try:
        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import (
            schedule_page_refresh,
        )
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import (
            schedule_page_graph_rebuild,
        )
        for pid in page_ids:
            schedule_page_refresh(pid, changed=True)
            schedule_page_graph_rebuild(pid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule wiki/graph rebuild after permission change failed: %s", exc)


def _revalidate_access_requests_after_permission_change(db: Session, notebook_id: str) -> None:
    """Notebook 权限变化后：有界 keyset 分批重验证 open 访问申请（失败保持 open）。

    J-4：申请业务组现在已能访问充分知识时自动 resolved；系统不自动扩大权限。
    company/admin/多组权限变化均触发（不按新组名过滤，避免 company 变化漏检）。
    """
    try:
        from app.core.retrieval.feedback_service import revalidate_access_requests_for_notebook_change
        revalidate_access_requests_for_notebook_change(db, notebook_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("access request revalidation after permission change failed: %s", exc)


class AccessScopeItem(BaseModel):
    id: str
    name: str
    type: str  # local / ldap


class AccessScopeListResponse(BaseModel):
    scopes: List[AccessScopeItem]


class AccessScopeUpdatePayload(BaseModel):
    group_id: str


class NotebookAccessScopeResponse(BaseModel):
    id: str
    name: str
    group_id: Optional[str] = None


class NotebookPermissionsPayload(BaseModel):
    """J-1 最终返工：Notebook 权限原子更新载荷。

    - scope_type: company | admin | groups
    - group_names: groups 类型时必填（一个或多个业务组，主组 + 额外组一致不重复）
    """
    scope_type: str
    group_names: List[str] = []


@router.get("/access-scopes", response_model=AccessScopeListResponse)
def list_access_scopes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回系统已知用户组（仅管理员）。

    数据来自 UserGroup.group_name 去重；__local_admin__ 仅适合本地试点。
    不回显用户密码或 LDAP 凭证，不新增负责人概念。
    """
    _require_admin(current_user)
    scopes: List[AccessScopeItem] = [
        AccessScopeItem(id=LOCAL_ADMIN_GROUP, name="本地管理员", type="local"),
    ]
    for name in _known_group_names(db):
        if name == LOCAL_ADMIN_GROUP:
            continue
        scopes.append(AccessScopeItem(id=name, name=name, type="ldap"))
    return AccessScopeListResponse(scopes=scopes)


@router.patch("/{notebook_id}/access-scope", response_model=NotebookAccessScopeResponse)
def update_access_scope(
    notebook_id: str,
    payload: AccessScopeUpdatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """设置 Notebook 的访问权限范围（group_id）。

    - 仅管理员可调用
    - Notebook 必须存在
    - group_id 必须来自系统已知 UserGroup（或 __local_admin__），禁止任意提交
    - 这是访问范围，不是负责人
    - 不删除 Notebook 现有数据
    """
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")

    # J-1 最终返工：复用同一原子服务。单组 access-scope 映射为对应 scope_type。
    raw = (payload.group_id or "").strip()
    if not raw or raw in ("__public__", "company"):
        mapped = NotebookPermissionsPayload(scope_type="company", group_names=[])
    elif raw in access_control._configured_admin_groups():
        mapped = NotebookPermissionsPayload(scope_type="admin", group_names=[])
    else:
        # 未知组由原子服务校验（400），已知组走 groups（单业务组）。
        mapped = NotebookPermissionsPayload(scope_type="groups", group_names=[raw])

    try:
        result = _apply_notebook_permissions(db, notebook, mapped)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(notebook)
    _schedule_wiki_rebuild_for_pages(result.get("page_ids") or [])
    _revalidate_access_requests_after_permission_change(db, notebook.id)
    logger.info(
        "notebook_access_scope_changed",
        extra={
            "notebook_id": notebook.id,
            "notebook_name": notebook.name,
            "new_group_id": notebook.group_id,
            "operator_id": current_user.get("id"),
            "operator_username": current_user.get("username"),
        },
    )
    return {
        "id": notebook.id,
        "name": notebook.name,
        "group_id": notebook.group_id,
    }


@router.put("/{notebook_id}/permissions")
def update_notebook_permissions(
    notebook_id: str,
    payload: NotebookPermissionsPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """J-1 最终返工：Notebook 权限原子更新（仅管理员）。

    payload：scope_type（company/admin/groups）+ group_names。
    单事务完成校验、group_id 更新、notebook_groups 替换、旧 scope Wiki 立即失效；
    任一步失败全部 rollback。commit 后为新 scope 调度 Wiki 重建。
    """
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    try:
        result = _apply_notebook_permissions(db, notebook, payload)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(notebook)
    # commit 后：新 scope Wiki 重建（旧 scope Wiki 已在事务内失效）。
    _schedule_wiki_rebuild_for_pages(result.get("page_ids") or [])
    _revalidate_access_requests_after_permission_change(db, notebook.id)
    logger.info(
        "notebook_permissions_atomic",
        extra={
            "notebook_id": notebook.id,
            "scope_type": payload.scope_type,
            "group_id": notebook.group_id,
            "groups": result.get("groups", []),
            "operator_id": current_user.get("id"),
        },
    )
    return {
        "id": notebook.id,
        "name": notebook.name,
        "scope_type": payload.scope_type,
        "group_id": notebook.group_id,
        "groups": result.get("groups", []),
    }


@router.get("/{notebook_id}/groups", response_model=List[NotebookGroupItem])
def list_notebook_groups(
    notebook_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回 Notebook 额外授权的业务组（J-1：完整列表仅管理员可读）。"""
    # J-1 遗留修复：完整多组授权列表只对管理员暴露；普通用户不能枚举。
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    rows = db.query(NotebookGroup).filter(NotebookGroup.notebook_id == notebook_id).all()
    return [
        NotebookGroupItem(id=r.id, group_name=r.group_name, notebook_id=r.notebook_id)
        for r in rows
    ]


@router.put("/{notebook_id}/groups")
def replace_notebook_groups(
    notebook_id: str,
    payload: NotebookGroupsPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """整体替换 Notebook 额外授权的业务组集合（仅管理员）。

    - 必须从系统已知 UserGroup 选择，禁止任意组名；
    - 只修改 notebook_groups 关联表，不触碰 Notebook.group_id；
    - 同一文件多组共享：把多个组都加入该 Notebook 的可访问组，禁止复制文件。
    """
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")

    # J-1 最终返工：复用同一原子服务（groups 类型：主组 + 额外组一致不重复）。
    mapped = NotebookPermissionsPayload(scope_type="groups", group_names=payload.group_names)
    try:
        result = _apply_notebook_permissions(db, notebook, mapped)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(notebook)
    _schedule_wiki_rebuild_for_pages(result.get("page_ids") or [])
    _revalidate_access_requests_after_permission_change(db, notebook.id)
    logger.info(
        "notebook_groups_replaced",
        extra={"notebook_id": notebook_id, "groups": result.get("groups", [])},
    )
    return {
        "id": notebook.id,
        "name": notebook.name,
        "group_id": notebook.group_id,
        "groups": result.get("groups", []),
    }


@router.post("", response_model=NotebookResponse)
def create_notebook(data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # J-1 遗留修复：Notebook 创建仅管理员。
    _require_admin(current_user)
    # 统一校验：未知业务组 → 400（禁止静默降级为 company）。
    group_id = _normalize_group_id(db, data.group_id)
    notebook = Notebook(id=str(uuid.uuid4()), name=data.name, group_id=group_id)
    db.add(notebook)
    db.commit()
    db.refresh(notebook)
    return notebook

@router.get("", response_model=List[NotebookResponse])
def list_notebooks(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    query = db.query(Notebook)
    if not access_control.is_admin(current_user):
        # J-1 遗留修复：只用业务组匹配，排除 admin/wiki_editor 角色组被当成业务组。
        groups = list(access_control.business_groups(current_user))
        query = query.filter(
            (Notebook.group_id.is_(None))
            | (Notebook.group_id == "__public__")
            | (Notebook.group_id.in_(groups))
            | (
                Notebook.id.in_(
                    db.query(NotebookGroup.notebook_id)
                    .filter(NotebookGroup.group_name.in_(groups))
                )
            )
        )
    notebooks = query.order_by(Notebook.updated_at.desc()).all()
    return notebooks

@router.get("/{notebook_id}", response_model=NotebookResponse)
def get_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    # J-1 遗留修复：统一复用 access_control.can_view_notebook（支持额外授权组 +
    # 配置的 LDAP 管理员组，不再硬编码 __local_admin__）。
    if not access_control.can_view_notebook(db, current_user, notebook):
        raise HTTPException(status_code=403, detail="无权访问该笔记本")
    return notebook

@router.put("/{notebook_id}", response_model=NotebookResponse)
def update_notebook(notebook_id: str, data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # J-1 最终封板：Notebook 更新仅管理员；名称更新继续。
    # 若显式提交 group_id，必须转换为统一权限 payload 走同一原子服务
    # （_apply_notebook_permissions），不得直接写 notebook.group_id 绕过
    # 额外组一致性与旧 Wiki 失效逻辑。
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    notebook.name = data.name
    if "group_id" in data.model_fields_set:
        raw = (data.group_id or "").strip()
        if not raw or raw in ("__public__", "company"):
            mapped = NotebookPermissionsPayload(scope_type="company", group_names=[])
        elif raw in access_control._configured_admin_groups():
            mapped = NotebookPermissionsPayload(scope_type="admin", group_names=[])
        else:
            mapped = NotebookPermissionsPayload(scope_type="groups", group_names=[raw])
        try:
            result = _apply_notebook_permissions(db, notebook, mapped)
        except HTTPException:
            db.rollback()
            raise
        db.commit()
        _schedule_wiki_rebuild_for_pages(result.get("page_ids") or [])
        _revalidate_access_requests_after_permission_change(db, notebook.id)
    else:
        db.commit()
    db.refresh(notebook)
    return notebook

@router.delete("/{notebook_id}")
def delete_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # J-1 遗留修复：Notebook 删除仅管理员。
    _require_admin(current_user)
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")

    pages = db.query(Page).filter(Page.notebook_id == notebook_id).all()
    for page in pages:
        db.query(PageChunk).filter(PageChunk.page_id == page.id).delete()
        db.delete(page)

    db.delete(notebook)
    db.commit()
    return {"message": "删除成功"}
