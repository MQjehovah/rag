"""数据源初始化与连接配置状态（P10-BE-05）。

- ensure_builtin_connections：注册内置 connector + 幂等创建钉钉连接
- ensure_dingtalk_connection：幂等创建钉钉 SourceConnection，绝不静默选目标笔记本
- get_connector_setup_status：返回每个 connector 的配置状态与 setup_blockers

原则：
- 凭证只来自环境变量（settings），绝不写入 config_json。
- 目标笔记本没有 group_id 时视为权限范围缺失，禁止同步（fail-closed）。
- group_id 表示访问范围，不是负责人；不引入 owner 字段。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.core.feature_flags import feature_enabled
from app.models.database import Notebook, SourceConnection

logger = logging.getLogger(__name__)


def _register_builtin_connectors() -> None:
    """显式注册内置 connector（幂等）。"""
    from app.sources.dingtalk import register_dingtalk_connector
    from app.sources.gitlab import register_gitlab_connector

    register_dingtalk_connector()
    register_gitlab_connector()


def ensure_dingtalk_connection(db: Session) -> SourceConnection | None:
    """幂等创建钉钉 SourceConnection。

    - 已存在则直接返回，不重复创建。
    - 凭证未配置时不创建（页面显示「钉钉凭证未配置」）。
    - 无法明确确定带 group_id 的目标笔记本时不创建（不静默选择）。
    - 新建连接默认 enabled=False，配合 dingtalk_connector_enabled 开关一起控制。
    """
    existing = db.query(SourceConnection).filter(
        SourceConnection.connector_key == "dingtalk"
    ).first()
    if existing is not None:
        return existing

    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        return None

    notebook: Notebook | None = None
    if settings.remote_rag_notebook_id:
        notebook = db.get(Notebook, settings.remote_rag_notebook_id)
    if notebook is None:
        name = settings.remote_rag_notebook_name or "钉钉知识库"
        notebook = (
            db.query(Notebook)
            .filter(Notebook.name == name, Notebook.group_id.isnot(None))
            .first()
        )
    if notebook is None or not notebook.group_id:
        return None

    conn = SourceConnection(
        id=str(uuid.uuid4()),
        connector_key="dingtalk",
        name="钉钉知识库",
        enabled=False,  # 默认停用，配置完成后再启用
        config_json="{}",
        target_notebook_id=notebook.id,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    logger.info("bootstrap: 创建钉钉连接 conn=%s notebook=%s", conn.id, notebook.id)
    return conn


def ensure_builtin_connections(db: Session) -> None:
    """注册内置 connector 并幂等初始化连接（import 不触发网络/任务）。"""
    _register_builtin_connectors()
    ensure_dingtalk_connection(db)


def get_connector_setup_status(db: Session) -> dict:
    """返回每个 connector 的配置状态，供前端连接卡片与 /connectors 使用。

    结构：
    {
      "<key>": {
        "connector_key", "name", "enabled", "configured", "has_connection",
        "connection_id", "target_notebook_id", "target_notebook_group_id",
        "setup_blockers": [{"code", "message"}], "supported_modes": [...]
      }
    }
    """
    status: dict = {}

    # 钉钉
    dt_enabled = feature_enabled(db, "dingtalk_connector_enabled")
    dt_configured = bool(settings.dingtalk_app_key and settings.dingtalk_app_secret)
    dt_conn = db.query(SourceConnection).filter(
        SourceConnection.connector_key == "dingtalk"
    ).first()
    dt_blockers: list[dict] = []
    if not dt_configured:
        dt_blockers.append({"code": "credentials_missing", "message": "钉钉凭证未配置"})
    if dt_conn is None:
        dt_blockers.append({"code": "connection_missing", "message": "尚未创建钉钉连接"})
    else:
        # J-1 遗留修复：钉钉文件归属完全由文件夹权限映射决定，不再依赖连接级
        # target_notebook_id。仅当连接仍绑定旧目标知识库时校验其有效性。
        if dt_conn.target_notebook_id:
            nb = db.get(Notebook, dt_conn.target_notebook_id)
            if nb is None:
                dt_blockers.append({"code": "target_notebook_missing", "message": "目标知识库不存在"})
            elif not nb.group_id:
                dt_blockers.append({
                    "code": "target_notebook_no_group",
                    "message": "目标知识库尚未配置权限范围",
                })
        # 缺少文件夹映射提示（管理员需在「数据源路径权限映射」配置归属，否则不导入）。
        from app.models.database import SourcePathMapping
        has_mapping = db.query(SourcePathMapping.id).first() is not None
        if not has_mapping:
            dt_blockers.append({
                "code": "folder_mapping_missing",
                "message": "尚未配置钉钉路径权限映射，未映射文件不会导入",
            })
    if not dt_enabled:
        dt_blockers.append({"code": "connector_disabled", "message": "钉钉 Connector 尚未启用"})

    status["dingtalk"] = {
        "connector_key": "dingtalk",
        "name": "钉钉知识库",
        "enabled": dt_enabled,
        "configured": dt_configured,
        "has_connection": dt_conn is not None,
        "connection_id": dt_conn.id if dt_conn else None,
        "target_notebook_id": dt_conn.target_notebook_id if dt_conn else None,
        "setup_blockers": dt_blockers,
        "supported_modes": ["incremental", "backfill", "full_reconcile"],
    }

    # GitLab
    gl_enabled = feature_enabled(db, "gitlab_connector_enabled")
    gl_conn = db.query(SourceConnection).filter(
        SourceConnection.connector_key == "gitlab"
    ).first()
    gl_blockers: list[dict] = []
    if not gl_enabled:
        gl_blockers.append({"code": "connector_disabled", "message": "GitLab Connector 尚未启用"})
    if gl_conn is None:
        gl_blockers.append({"code": "connection_missing", "message": "尚未创建 GitLab 连接"})
    status["gitlab"] = {
        "connector_key": "gitlab",
        "name": "GitLab",
        "enabled": gl_enabled,
        "configured": True,
        "has_connection": gl_conn is not None,
        "connection_id": gl_conn.id if gl_conn else None,
        "target_notebook_id": gl_conn.target_notebook_id if gl_conn else None,
        "setup_blockers": gl_blockers,
        "supported_modes": ["incremental", "backfill", "full_reconcile"],
    }

    return status
