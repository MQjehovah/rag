"""钉钉文件夹 → Notebook 权限映射 API（J-1 薄兼容层）。

统一路径映射（source_path_mappings）上线后，本模块保留旧 URL 作为兼容层，
内部委托统一服务（app.api.source_path_mappings 的等价逻辑），把 space_id 映射为
path_namespace，并自动定位唯一钉钉 SourceConnection。

保留原因：前端 Sources.vue 与旧定向测试仍通过 /api/sources/dingtalk/folder-mappings
访问；统一迁移完成后可下线。本层不维护第二套业务逻辑。
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
    normalize_folder_path,
    resolve_target_notebook_id,
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

router = APIRouter(prefix="/api/sources/dingtalk/folder-mappings", tags=["钉钉文件夹映射（兼容）"])


class FolderMappingCreatePayload(BaseModel):
    space_id: str | None = None
    folder_path: str
    notebook_id: str


class FolderMappingUpdatePayload(BaseModel):
    space_id: str | None = None
    folder_path: str
    notebook_id: str


class ReassignPayload(BaseModel):
    page_id: str


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _dingtalk_connection_id(db: Session) -> str | None:
    rows = db.query(SourceConnection.id).filter(SourceConnection.connector_key == "dingtalk").all()
    if len(rows) != 1:
        return None
    return rows[0][0]


def _validate_payload(db: Session, payload) -> tuple[str, str, str]:
    folder_path = normalize_folder_path(payload.folder_path)
    if not folder_path:
        raise HTTPException(status_code=400, detail="文件夹路径不能为空")
    notebook = db.get(Notebook, payload.notebook_id)
    if notebook is None:
        raise HTTPException(status_code=400, detail="目标知识库不存在")
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        raise HTTPException(status_code=400, detail="目标知识库权限域无法确定（unknown），请先配置权限")
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        raise HTTPException(status_code=400, detail="无法唯一确定钉钉连接")
    space_id = str(payload.space_id or "").strip()
    return conn_id, space_id, folder_path


def _space_id_from_acl(item) -> str | None:
    import json as _json
    raw = getattr(item, "acl_json", None) or "{}"
    try:
        data = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        sid = data.get("space_id")
        return str(sid) if sid else None
    return None


def _serialize_rows(db: Session, rows: list) -> list[dict]:
    nb_ids = {r.notebook_id for r in rows}
    notebooks = {
        n.id: n.name for n in db.query(Notebook).filter(Notebook.id.in_(nb_ids)).all()
    } if nb_ids else {}
    out = []
    for r in rows:
        d = {**serialize_mapping(r), "notebook_name": notebooks.get(r.notebook_id, "")}
        d["space_id"] = d.get("path_namespace")
        out.append(d)
    return out


@router.get("/folders")
def list_discovered_folders(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """兼容：已发现的钉钉文件夹列表。"""
    _require_admin(current_user)
    try:
        from app.core.dingtalk_storage import DingTalkLocalStorage
        from app.core.path_mapping import folder_path_segments, normalize_folder_path
        manifest = DingTalkLocalStorage().read_manifest()
    except Exception:  # noqa: BLE001
        return {"folders": [], "source": "manifest_unavailable"}
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
    result = [{"space_id": sid, "folder_path": path} for sid in sorted(folders) for path in sorted(folders[sid])]
    return {"folders": result, "source": "manifest"}


@router.get("")
def list_folder_mappings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        return {"mappings": []}
    rows = db.query(SourcePathMapping).filter(SourcePathMapping.connection_id == conn_id).order_by(
        SourcePathMapping.path_namespace, SourcePathMapping.folder_path
    ).all()
    return {"mappings": _serialize_rows(db, rows)}


@router.post("")
def create_folder_mapping(
    payload: FolderMappingCreatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    conn_id, space_id, folder_path = _validate_payload(db, payload)
    dup = db.query(SourcePathMapping).filter(
        SourcePathMapping.connection_id == conn_id,
        SourcePathMapping.path_namespace == space_id,
        SourcePathMapping.folder_path == folder_path,
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该文件夹已配置映射，请先更新或删除现有映射")
    row = SourcePathMapping(
        id=str(uuid.uuid4()),
        connection_id=conn_id,
        path_namespace=space_id,
        folder_path=folder_path,
        notebook_id=payload.notebook_id,
        created_by=current_user.get("id"),
    )
    db.add(row)
    from app.core.path_mapping import reevaluate_mapping_items
    try:
        reevaluate_mapping_items(db, conn_id, space_id, folder_path)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return _serialize_rows(db, [row])[0]


@router.put("/{mapping_id}")
def update_folder_mapping(
    mapping_id: str,
    payload: FolderMappingUpdatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    row = db.query(SourcePathMapping).filter(SourcePathMapping.id == mapping_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="映射不存在")
    conn_id, space_id, folder_path = _validate_payload(db, payload)
    dup = db.query(SourcePathMapping).filter(
        SourcePathMapping.connection_id == conn_id,
        SourcePathMapping.path_namespace == space_id,
        SourcePathMapping.folder_path == folder_path,
        SourcePathMapping.id != mapping_id,
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该文件夹已配置映射，请先更新或删除现有映射")
    old_path = row.folder_path
    old_ns = row.path_namespace
    row.path_namespace = space_id
    row.folder_path = folder_path
    row.notebook_id = payload.notebook_id
    from app.core.path_mapping import reevaluate_mapping_items
    affected = {(conn_id, old_ns, old_path), (conn_id, space_id, folder_path)}
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
def delete_folder_mapping(
    mapping_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
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
    """兼容：管理员确认重新归属（委托统一逻辑）。"""
    _require_admin(current_user)
    page = db.get(Page, payload.page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page 不存在")
    item = (
        db.query(SourceItem)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(SourceItem.page_id == page.id, SourceConnection.connector_key == "dingtalk")
        .order_by(SourceItem.updated_at.desc())
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="未找到关联的 DingTalk SourceItem")
    if item.state != "skipped" or item.last_error not in (NEEDS_REASSIGN_REASON, SAFE_REASON_UNMAPPED):
        raise HTTPException(status_code=409, detail="该记录不处于待重新归属状态（NEEDS_REASSIGN / FOLDER_NOT_MAPPED）")
    space_id = _space_id_from_acl(item)
    target_nb_id = resolve_target_notebook_id(db, item.connection_id, space_id, item.source_path)
    if target_nb_id is None:
        raise HTTPException(status_code=400, detail="该文件当前无可用映射，无法重新归属")
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
