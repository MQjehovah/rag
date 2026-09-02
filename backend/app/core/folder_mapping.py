"""钉钉文件夹 → Notebook 映射（J-1 兼容层）。

统一路径映射（app.core.path_mapping）上线后，本模块作为钉钉专用薄兼容层，
保留原有函数签名与语义，内部委托给统一服务：
- resolve_target_notebook_id(db, space_id, file_path) → 统一服务（钉钉连接 + namespace=space_id）
- reevaluate_mapping_items(db, space_id, folder_path) → 统一服务

不再直接读写 DingtalkFolderMapping（该表已在 P38 迁移中删除，统一为 source_path_mappings）。
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from app.core import path_mapping
from app.models.database import SourceConnection

# 向后兼容导出的常量（语义与统一服务一致）。
SAFE_REASON_UNMAPPED = path_mapping.SAFE_REASON_UNMAPPED
SKIP_CODE_UNMAPPED = path_mapping.SKIP_CODE_UNMAPPED
NEEDS_REASSIGN_REASON = path_mapping.NEEDS_REASSIGN_REASON
NEEDS_REASSIGN_CODE = path_mapping.NEEDS_REASSIGN_CODE

# 复用规范化/切段/子路径纯函数（保持对外兼容）。
normalize_folder_path = path_mapping.normalize_folder_path
folder_path_segments = path_mapping.folder_path_segments
path_is_subpath = path_mapping.path_is_subpath


def _space_id_from_item(item) -> str | None:
    """向后兼容：从 SourceItem.acl_json 解析 space_id（钉钉命名域）。"""
    return path_mapping._namespace_from_item(item)


def load_mappings(db: Session) -> list[dict]:
    """向后兼容：加载钉钉连接的映射（space_id 映射为 path_namespace）。"""
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        return []
    result = path_mapping.load_mappings(db, conn_id)
    # 兼容旧字段名 space_id
    for m in result:
        m["space_id"] = m.get("path_namespace")
    return result


def _dingtalk_connection_id(db: Session) -> str | None:
    """返回唯一钉钉 SourceConnection.id；0 个或 >1 个返回 None（fail closed）。"""
    rows = db.query(SourceConnection.id).filter(
        SourceConnection.connector_key == "dingtalk"
    ).all()
    if len(rows) != 1:
        return None
    return rows[0][0]


def resolve_target_notebook_id(
    db: Session,
    space_id: str | None,
    file_path: str | None,
) -> str | None:
    """钉钉兼容：space_id 作为 path_namespace，委托统一服务。"""
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        return None
    return path_mapping.resolve_target_notebook_id(db, conn_id, space_id, file_path)


def resolve_target_notebook_id_for_entry(
    db: Session,
    entry: dict,
) -> str | None:
    """钉钉兼容：从 manifest 条目解析目标 Notebook。"""
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        return None
    space_id = str(entry.get("space_id") or "") or None
    path = str(entry.get("dingtalk_path") or "") or None
    return path_mapping.resolve_target_notebook_id(db, conn_id, space_id, path)


def reevaluate_mapping_items(
    db: Session,
    space_id: str | None,
    folder_path: str,
    *,
    batch: int = 500,
) -> dict:
    """钉钉兼容：重评估钉钉连接下受影响的 SourceItem。"""
    conn_id = _dingtalk_connection_id(db)
    if conn_id is None:
        return {"reassigned": 0, "scanned": 0}
    return path_mapping.reevaluate_mapping_items(db, conn_id, space_id, folder_path, batch=batch)


# 兼容导出（旧签名，内部走统一服务）。
_serialize_mapping = path_mapping.serialize_mapping


def serialize_mapping(r, notebook_name: str = "") -> dict:
    return path_mapping.serialize_mapping(r, notebook_name)


def mappings_as_json(mappings: Iterable[dict]) -> list[str]:
    import json
    return [json.dumps(m, ensure_ascii=False) for m in mappings]
