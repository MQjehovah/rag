"""统一数据源路径权限映射 API（P38）。

- GET    /api/sources/path-mappings          → 映射列表（含 Notebook 名与可见范围）
- GET    /api/sources/path-mappings/paths    → 当前连接已发现的安全目录/能力状态
- POST   /api/sources/path-mappings          → 新建映射（管理员）
- PUT    /api/sources/path-mappings/{id}     → 更新映射（管理员）
- DELETE /api/sources/path-mappings/{id}     → 删除映射（管理员）
- POST   /api/sources/path-mappings/reassign → 管理员确认重新归属（通用）

规则：
- 所有接口仅管理员可用；
- connection_id 必填；
- 目标 Notebook 必须是合法 company/admin/groups scope，unknown 拒绝；
- 响应不包含密码/Token/Secret/完整认证配置；
- 一个 (connection_id, path_namespace, folder_path) 只能有一个目标；
- 更具体路径优先；空 namespace 是连接内通配。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.path_mapping import (
    NEEDS_REASSIGN_REASON,
    SAFE_REASON_UNMAPPED,
    _namespace_from_item,
    normalize_folder_path,
    resolve_target_notebook_decision,
    serialize_mapping,
)
from app.core.jwt_utils import get_current_user, is_admin_user
from app.models.database import (
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourcePathMapping,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources/path-mappings", tags=["数据源路径映射"])


class PathMappingCreatePayload(BaseModel):
    connection_id: str
    path_namespace: str | None = None
    folder_path: str
    notebook_id: str


class PathMappingUpdatePayload(BaseModel):
    connection_id: str
    path_namespace: str | None = None
    folder_path: str
    notebook_id: str


class ReassignPayload(BaseModel):
    page_id: str


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _validate_payload(db: Session, payload) -> tuple[str, str, str, str]:
    """校验并规范化映射输入，返回 (connection_id, path_namespace, folder_path, notebook_id)。"""
    conn = db.get(SourceConnection, payload.connection_id)
    if conn is None:
        raise HTTPException(status_code=400, detail="数据源连接不存在")
    folder_path = normalize_folder_path(payload.folder_path)
    if not folder_path:
        raise HTTPException(status_code=400, detail="文件夹路径不能为空")
    notebook = db.get(Notebook, payload.notebook_id)
    if notebook is None:
        raise HTTPException(status_code=400, detail="目标知识库不存在")
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        raise HTTPException(status_code=400, detail="目标知识库权限域无法确定（unknown），请先配置权限")
    path_namespace = str(payload.path_namespace or "").strip()
    return payload.connection_id, path_namespace, folder_path, payload.notebook_id


def _serialize_rows(db: Session, rows: list, *, with_scope: bool = False) -> list[dict]:
    nb_ids = {r.notebook_id for r in rows}
    notebooks = {
        n.id: n
        for n in db.query(Notebook).filter(Notebook.id.in_(nb_ids)).all()
    } if nb_ids else {}
    out = []
    for r in rows:
        d = {**serialize_mapping(r), "notebook_name": notebooks.get(r.notebook_id).name if r.notebook_id in notebooks else ""}
        if with_scope:
            nb = notebooks.get(r.notebook_id)
            d["scope_label"] = access_control.scope_from_notebook(db, nb).kind if nb else ""
        out.append(d)
    return out


@router.get("")
def list_path_mappings(
    connection_id: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """映射列表（仅管理员；含 Notebook 名）。可选按 connection_id 过滤。"""
    _require_admin(current_user)
    q = db.query(SourcePathMapping)
    if connection_id:
        q = q.filter(SourcePathMapping.connection_id == connection_id)
    rows = q.order_by(SourcePathMapping.connection_id, SourcePathMapping.path_namespace, SourcePathMapping.folder_path).all()
    return {"mappings": _serialize_rows(db, rows)}


@router.get("/paths")
def list_discovered_paths(
    connection_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回当前连接已发现的安全目录/命名域信息与路径能力状态（仅管理员）。

    - 钉钉：从本地 manifest 提取 (space_id, folder_path) 父路径；
    - 有命名域但无目录树时返回 capability=connection_level；
    - 无路径能力的连接返回 capability=none + 说明，绝不 500。
    """
    _require_admin(current_user)
    conn = db.get(SourceConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="数据源连接不存在")

    if conn.connector_key == "dingtalk":
        try:
            from app.core.dingtalk_storage import DingTalkLocalStorage
            from app.core.path_mapping import folder_path_segments, normalize_folder_path
            manifest = DingTalkLocalStorage().read_manifest()
        except Exception:  # noqa: BLE001
            return {"paths": [], "capability": "path", "source": "manifest_unavailable"}

        folders: dict[str, dict[str, None]] = {}
        for doc in manifest.get("documents", []):
            if str(doc.get("source_status") or "active") == "deleted":
                continue
            space_id = str(doc.get("space_id") or "")
            path = str(doc.get("dingtalk_path") or "").strip()
            if not path:
                continue
            folder = normalize_folder_path(path)
            segs = folder_path_segments(folder)
            if len(segs) <= 1:
                continue
            parent = "/".join(segs[:-1])
            folders.setdefault(space_id, {})[parent] = None

        paths = [
            {"path_namespace": sid, "folder_path": path}
            for sid in sorted(folders)
            for path in sorted(folders[sid])
        ]
        return {"paths": paths, "capability": "path", "source": "manifest"}

    # GitLab：真实提供 project_id + 仓库内路径，支持路径映射。从当前 connection 已有
    # SourceItem 安全提取 (project_id, 父目录)，不调用远程 API、不返回正文/凭证。
    if conn.connector_key == "gitlab":
        from app.core.path_mapping import folder_path_segments, normalize_folder_path
        folders: dict[str, dict[str, None]] = {}
        items = db.query(SourceItem).filter(
            SourceItem.connection_id == connection_id,
            SourceItem.source_path.isnot(None),
            SourceItem.source_path != "",
        ).all()
        for item in items:
            ns = _namespace_from_item(item) or ""
            path = str(item.source_path or "").strip()
            folder = normalize_folder_path(path)
            segs = folder_path_segments(folder)
            if len(segs) <= 1:
                continue  # 文件名在根目录 → 无目录前缀（根映射用空路径）
            parent = "/".join(segs[:-1])
            folders.setdefault(ns, {})[parent] = None
        paths = [
            {"path_namespace": ns, "folder_path": path}
            for ns in sorted(folders)
            for path in sorted(folders[ns])
        ]
        return {"paths": paths, "capability": "path", "source": "source_items"}

    # 无路径能力：明确返回能力状态，不伪造目录。
    return {"paths": [], "capability": "none", "source": "none"}


@router.post("")
def create_path_mapping(
    payload: PathMappingCreatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """新建统一路径映射（仅管理员）。"""
    _require_admin(current_user)
    connection_id, path_namespace, folder_path, notebook_id = _validate_payload(db, payload)
    dup = db.query(SourcePathMapping).filter(
        SourcePathMapping.connection_id == connection_id,
        SourcePathMapping.path_namespace == path_namespace,
        SourcePathMapping.folder_path == folder_path,
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该路径已配置映射，请先更新或删除现有映射")
    row = SourcePathMapping(
        id=str(uuid.uuid4()),
        connection_id=connection_id,
        path_namespace=path_namespace,
        folder_path=folder_path,
        notebook_id=notebook_id,
        created_by=current_user.get("id"),
    )
    db.add(row)
    from app.core.path_mapping import reevaluate_mapping_items
    try:
        reevaluate_mapping_items(db, connection_id, path_namespace, folder_path)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return _serialize_rows(db, [row])[0]


@router.put("/{mapping_id}")
def update_path_mapping(
    mapping_id: str,
    payload: PathMappingUpdatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """更新统一路径映射（仅管理员）。"""
    _require_admin(current_user)
    row = db.query(SourcePathMapping).filter(SourcePathMapping.id == mapping_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="映射不存在")
    connection_id, path_namespace, folder_path, notebook_id = _validate_payload(db, payload)
    dup = db.query(SourcePathMapping).filter(
        SourcePathMapping.connection_id == connection_id,
        SourcePathMapping.path_namespace == path_namespace,
        SourcePathMapping.folder_path == folder_path,
        SourcePathMapping.id != mapping_id,
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该路径已配置映射，请先更新或删除现有映射")
    old_conn = row.connection_id
    old_ns = row.path_namespace
    old_path = row.folder_path
    row.connection_id = connection_id
    row.path_namespace = path_namespace
    row.folder_path = folder_path
    row.notebook_id = notebook_id
    from app.core.path_mapping import reevaluate_mapping_items
    affected = {(old_conn, old_ns, old_path), (connection_id, path_namespace, folder_path)}
    try:
        for cid, ns, fp in affected:
            reevaluate_mapping_items(db, cid, ns, fp)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return _serialize_rows(db, [row])[0]


@router.delete("/{mapping_id}")
def delete_path_mapping(
    mapping_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """删除统一路径映射（仅管理员）。"""
    _require_admin(current_user)
    row = db.query(SourcePathMapping).filter(SourcePathMapping.id == mapping_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="映射不存在")
    old_conn = row.connection_id
    old_ns = row.path_namespace
    old_path = row.folder_path
    db.delete(row)
    from app.core.path_mapping import reevaluate_mapping_items
    try:
        reevaluate_mapping_items(db, old_conn, old_ns, old_path)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"message": "映射已删除"}


@router.post("/reassign")
def confirm_reassign(
    payload: ReassignPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """管理员确认重新归属（通用 source path reassign，仅管理员）。

    重新解析当前路径映射 → 更新 Page.notebook_id → SourceItem 恢复 active →
    清除 NEEDS_REASSIGN/SOURCE_PATH_NOT_MAPPED → 重索引、同步 Evidence、重建新 scope Wiki/图谱。
    """
    _require_admin(current_user)
    from app.core.path_mapping import _namespace_from_item

    page = db.get(Page, payload.page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page 不存在")

    item = (
        db.query(SourceItem)
        .filter(SourceItem.page_id == page.id)
        .order_by(SourceItem.updated_at.desc())
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="未找到关联的 SourceItem")

    if item.state != "skipped" or item.last_error not in (
        NEEDS_REASSIGN_REASON,
        SAFE_REASON_UNMAPPED,
    ):
        raise HTTPException(
            status_code=409,
            detail="该记录不处于待重新归属状态（NEEDS_REASSIGN / SOURCE_PATH_NOT_MAPPED）",
        )

    namespace = _namespace_from_item(item)
    connection = db.get(SourceConnection, item.connection_id)
    if connection is None:
        raise HTTPException(status_code=400, detail="数据源连接不存在")
    decision = resolve_target_notebook_decision(db, connection, namespace, item.source_path)
    if decision.source == "unmapped" or decision.notebook_id is None:
        raise HTTPException(status_code=400, detail="该文件当前无可用目标（映射或连接级兜底均缺失），无法重新归属")
    target_nb_id = decision.notebook_id
    target = db.get(Notebook, target_nb_id)
    if target is None:
        raise HTTPException(status_code=400, detail="目标知识库不存在")

    page.notebook_id = target_nb_id
    item.state = "active"
    item.last_error = None
    item.metadata_hash = None
    db.commit()
    db.refresh(page)

    try:
        from app.core.evidence_ingest import sync_page_evidence
        sync_page_evidence(db, page.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reassign sync evidence failed page=%s: %s", page.id, exc)
    try:
        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_page_refresh
        schedule_page_refresh(page.id, changed=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reassign schedule wiki rebuild failed page=%s: %s", page.id, exc)
    try:
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
        schedule_page_graph_rebuild(page.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reassign schedule graph rebuild failed page=%s: %s", page.id, exc)

    return {"page_id": page.id, "notebook_id": target_nb_id, "state": item.state}
