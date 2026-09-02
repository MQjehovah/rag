"""统一数据源路径 → Notebook 映射规则（P38，各数据源共用）。

统一输入三元组：
- connection_id：数据源连接；
- path_namespace：连接内部的空间/项目/仓库等命名域（钉钉=space_id）；
- folder_path：规范化分段路径（不含文件名）。

匹配规则（确定性）：
1. 只查询当前 connection_id 的映射；
2. path_namespace 精确匹配优先，允许空 namespace 作为该连接内通配；
3. 更具体路径（段数更多）优先于父路径；
4. 同深度时精确 namespace 优先于通配；
5. 按完整路径段匹配：A/B 可匹配 A/B/C，但 A/B 不得匹配 A/BC；
6. 结果排序稳定、确定性。

硬约束：
- 不根据文件正文/标题/作者/LLM 猜测路径；
- 路径规范化不破坏中文、空格和产品名称。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.database import (
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourcePathMapping,
)

# 未映射的安全原因（同步结果明确返回）
SAFE_REASON_UNMAPPED = "路径未配置权限映射"
# 未映射时应写入 SourceItem 的通用错误码
SKIP_CODE_UNMAPPED = "SOURCE_PATH_NOT_MAPPED"
# 映射变化、待重新归属的安全原因与错误码。
NEEDS_REASSIGN_REASON = "路径映射变化，需要重新归属"
NEEDS_REASSIGN_CODE = "NEEDS_REASSIGN"


def normalize_folder_path(path: str | None) -> str:
    """规范化文件夹路径：/ 分隔、去首尾空白/斜杠；根目录返回空串。"""
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip().strip("/")
    return p


def folder_path_segments(path: str | None) -> tuple[str, ...]:
    """按完整文件夹边界切段（丢弃空段）。"""
    p = normalize_folder_path(path)
    if not p:
        return ()
    return tuple(seg for seg in p.split("/") if seg)


def path_is_subpath(child: str | None, parent: str | None) -> bool:
    """child 是否位于 parent 文件夹内（按完整段边界，不误匹配相似前缀）。"""
    child_segs = folder_path_segments(child)
    parent_segs = folder_path_segments(parent)
    if not parent_segs:
        return True  # 根目录是所有路径的父级
    if len(child_segs) < len(parent_segs):
        return False
    return child_segs[: len(parent_segs)] == parent_segs


def _namespace_from_item(item) -> str | None:
    """从 SourceItem.acl_json 解析命名域（钉钉=space_id，GitLab=project_id）。"""
    raw = getattr(item, "acl_json", None) or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        # 钉钉用 space_id；GitLab 用 project_id。优先 space_id，回退 project_id。
        for key in ("space_id", "project_id"):
            val = data.get(key)
            if val:
                return str(val)
    return None


def load_mappings(db: Session, connection_id: str) -> list[dict]:
    """加载指定连接的映射（仅映射到存在 Notebook 的行）。"""
    rows = (
        db.query(SourcePathMapping)
        .filter(SourcePathMapping.connection_id == connection_id)
        .order_by(SourcePathMapping.path_namespace, SourcePathMapping.folder_path)
        .all()
    )
    result: list[dict] = []
    notebook_ids = {r.notebook_id for r in rows}
    notebooks = {
        n.id: n
        for n in db.query(Notebook).filter(Notebook.id.in_(notebook_ids)).all()
    } if notebook_ids else {}
    for r in rows:
        nb = notebooks.get(r.notebook_id)
        if nb is None:
            continue  # 指向已删除 Notebook 的映射不参与匹配（fail closed）
        result.append({
            "id": r.id,
            "path_namespace": r.path_namespace,
            "folder_path": r.folder_path,
            "notebook_id": r.notebook_id,
            "notebook_name": nb.name,
        })
    return result


def resolve_target_notebook_id(
    db: Session,
    connection_id: str,
    path_namespace: str | None,
    file_path: str | None,
) -> str | None:
    """按 connection_id + namespace + 文件路径匹配目标 Notebook id。

    返回 None 表示未映射（同步方应跳过，不创建 Page/Chunk/Evidence）。
    """
    # 文件路径 = 文件夹路径 + 文件名；取 dirname 即文件所在文件夹路径。
    file_path = str(file_path or "").strip()
    if file_path:
        folder = str(PurePosixPath(file_path.replace("\\", "/")).parent)
    else:
        folder = ""
    mappings = load_mappings(db, connection_id)
    ns = str(path_namespace or "").strip()

    matched: list[tuple[int, int, str, dict]] = []  # (段数, ns精确度, 稳定键, 映射)
    for m in mappings:
        m_ns = str(m.get("path_namespace") or "").strip()
        # namespace 精确匹配优先；空 namespace 是连接内通配。
        if m_ns and ns and m_ns != ns:
            continue  # namespace 限定不匹配
        if m_ns and not ns:
            continue  # 文件无 namespace 但映射限定 namespace → 不匹配
        if path_is_subpath(folder, m["folder_path"]):
            segs = len(folder_path_segments(m["folder_path"]))
            stable = (m_ns, m["folder_path"])
            # ns_priority：精确 namespace（0）优先于空 namespace 通配（1）。
            ns_priority = 1
            if ns and m_ns and m_ns == ns:
                ns_priority = 0
            matched.append((segs, ns_priority, stable, m))

    if not matched:
        return None
    # 优先级：1. 更具体路径（段数更多）；2. 同深度精确 namespace 优先；3. 稳定排序。
    matched.sort(key=lambda t: (-t[0], t[1], t[2]))
    return matched[0][3]["notebook_id"]


def resolve_target_notebook_id_for_entry(
    db: Session,
    connection_id: str,
    entry: dict,
) -> str | None:
    """从标准化条目解析目标 Notebook id（connection_id + namespace + 路径）。"""
    ns = entry.get("path_namespace") or entry.get("space_id") or entry.get("project_id")
    path = entry.get("folder_path") or entry.get("dingtalk_path") or entry.get("source_path")
    return resolve_target_notebook_id(db, connection_id, ns, path)


# ---------------------------------------------------------------------------
# 统一目标 Notebook 决策服务
# ---------------------------------------------------------------------------

@dataclass
class TargetNotebookDecision:
    """统一目标 Notebook 决策结果。

    - notebook_id：最终目标 Notebook id（unmapped 时为 None）；
    - source：mapping / connection_fallback / unmapped；
    - mapping_id：命中映射时的映射 id（可选）；
    - path_namespace：规范化后的命名域；
    - normalized_path：规范化后的完整路径。
    """
    notebook_id: str | None
    source: str  # mapping / connection_fallback / unmapped
    mapping_id: str | None = None
    path_namespace: str = ""
    normalized_path: str = ""


def resolve_target_notebook_decision(
    db: Session,
    connection: SourceConnection,
    path_namespace: str | None,
    source_path: str | None,
) -> TargetNotebookDecision:
    """统一目标 Notebook 决策（executor / reevaluate / reassign 三处共用）。

    规则：
    1. 先查 connection_id + path_namespace + source_path 的 SourcePathMapping；
    2. 命中 → 返回映射 Notebook（source=mapping）；映射指向不存在/unknown Notebook 时 fail closed；
    3. 未命中但 connection.target_notebook_id 存在且合法 → source=connection_fallback；
    4. 两者都没有 → source=unmapped，notebook_id=None。

    注意：不根据正文/标题/LLM 猜测 namespace；namespace 空串表示无命名域。
    """
    ns = str(path_namespace or "").strip()
    normalized = str(source_path or "").strip()

    # 1. 先查路径映射
    target_nb_id = resolve_target_notebook_id(db, connection.id, ns, normalized)
    if target_nb_id is not None:
        from app.core import access_control
        nb = db.get(Notebook, target_nb_id)
        if nb is None:
            # 映射指向不存在的 Notebook → fail closed，不静默回退连接级。
            raise ValueError(f"路径映射指向不存在的 Notebook：{target_nb_id}")
        scope = access_control.scope_from_notebook(db, nb)
        if scope.kind == access_control.SCOPE_UNKNOWN:
            raise ValueError(f"路径映射指向 unknown 权限域 Notebook：{target_nb_id}")
        # 找到命中映射的 id（用于追溯）
        mapping_id = _mapping_id_for(db, connection.id, ns, normalized)
        return TargetNotebookDecision(
            notebook_id=target_nb_id, source="mapping", mapping_id=mapping_id,
            path_namespace=ns, normalized_path=normalized,
        )

    # 2. 未命中映射 → 连接级兜底（钉钉除外：钉钉文件归属完全由路径映射决定，
    #    连接级 target_notebook_id 是历史遗留，不作为兜底；未映射即 unmapped）。
    if connection.connector_key != "dingtalk" and connection.target_notebook_id:
        from app.core import access_control
        nb = db.get(Notebook, connection.target_notebook_id)
        if nb is not None:
            scope = access_control.scope_from_notebook(db, nb)
            if scope.kind != access_control.SCOPE_UNKNOWN:
                return TargetNotebookDecision(
                    notebook_id=nb.id, source="connection_fallback",
                    path_namespace=ns, normalized_path=normalized,
                )

    # 3. 两者都没有 → unmapped
    return TargetNotebookDecision(
        notebook_id=None, source="unmapped",
        path_namespace=ns, normalized_path=normalized,
    )


def _mapping_id_for(db: Session, connection_id: str, path_namespace: str, source_path: str) -> str | None:
    """返回命中映射的 id（用于追溯），复用 resolve 的匹配逻辑。"""
    folder = ""
    sp = str(source_path or "").strip()
    if sp:
        folder = str(PurePosixPath(sp.replace("\\", "/")).parent)
    mappings = load_mappings(db, connection_id)
    ns = str(path_namespace or "").strip()
    matched: list[tuple[int, int, str, dict]] = []
    for m in mappings:
        m_ns = str(m.get("path_namespace") or "").strip()
        if m_ns and ns and m_ns != ns:
            continue
        if m_ns and not ns:
            continue
        if path_is_subpath(folder, m["folder_path"]):
            segs = len(folder_path_segments(m["folder_path"]))
            ns_priority = 1
            if ns and m_ns and m_ns == ns:
                ns_priority = 0
            matched.append((segs, ns_priority, (m_ns, m["folder_path"]), m))
    if not matched:
        return None
    matched.sort(key=lambda t: (-t[0], t[1], t[2]))
    return matched[0][3]["id"]


def _mark_needs_reassign(db: Session, item, page) -> None:
    """把既有 SourceItem/Page 标记为待重新归属（fail closed，不迁移不删除）。

    在同一事务内同步失效旧 Wiki 来源 + 旧图谱 provenance（均 commit=False），
    任何异常向上传播，由调用方 rollback。
    """
    item.state = "skipped"
    item.last_error = NEEDS_REASSIGN_REASON
    item.metadata_hash = None
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        remove_source_page_from_wikis,
    )
    remove_source_page_from_wikis(db, page.id, commit=False)
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    remove_page_graph(db, page.id, commit=False)
    db.flush()


def _source_items_batch(
    db: Session,
    connection_id: str,
    *,
    last_id: str | None = None,
    batch: int = 500,
):
    """keyset 分页：只取指定 connection 且已有 page_id 的 SourceItem。"""
    q = (
        db.query(SourceItem)
        .filter(
            SourceItem.connection_id == connection_id,
            SourceItem.source_path.isnot(None),
            SourceItem.source_path != "",
            SourceItem.page_id.isnot(None),
        )
    )
    if last_id:
        q = q.filter(SourceItem.id > last_id)
    return q.order_by(SourceItem.id).limit(batch).all()


def reevaluate_mapping_items(
    db: Session,
    connection_id: str,
    path_namespace: str | None,
    folder_path: str,
    *,
    batch: int = 500,
) -> dict:
    """映射新增/修改/删除后，重评估当前连接下受影响的 SourceItem（keyset 完整处理）。

    - 只处理当前 connection_id（不跨 connection）；
    - namespace 精确匹配（不跨 namespace）；
    - 路径按段边界前缀匹配；
    - 目标 Notebook 与 Page 当前归属不同 / 无目标 → _mark_needs_reassign。
    """
    segs = folder_path_segments(folder_path)
    ns = str(path_namespace or "").strip()
    connection = db.get(SourceConnection, connection_id)
    last_id: str | None = None
    reassigned = 0
    scanned = 0
    while True:
        page_items = _source_items_batch(db, connection_id, last_id=last_id, batch=batch)
        if not page_items:
            break
        last_id = page_items[-1].id
        for item in page_items:
            item_path = str(item.source_path or "")
            item_segs = folder_path_segments(item_path)
            if item_segs[: len(segs)] != segs:
                continue
            item_ns = _namespace_from_item(item)
            if ns and item_ns != ns:
                continue
            scanned += 1
            page = db.get(Page, item.page_id)
            if page is None:
                continue
            # 统一决策：映射命中 → 连接级兜底 → 无目标（unmapped）。
            if connection is None:
                decision = TargetNotebookDecision(notebook_id=None, source="unmapped")
            else:
                decision = resolve_target_notebook_decision(db, connection, item_ns, item_path)
            if decision.notebook_id is None or page.notebook_id != decision.notebook_id:
                _mark_needs_reassign(db, item, page)
                reassigned += 1
        if len(page_items) < batch:
            break
    return {"reassigned": reassigned, "scanned": scanned}


def serialize_mapping(r: SourcePathMapping, notebook_name: str = "") -> dict:
    return {
        "id": r.id,
        "connection_id": r.connection_id,
        "path_namespace": r.path_namespace,
        "folder_path": r.folder_path,
        "notebook_id": r.notebook_id,
        "notebook_name": notebook_name,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
