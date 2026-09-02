"""数据源平台（P9，V3 计划 4.3）。

Connector 契约、Registry、SyncService、Worker。导入本模块不注册任何
connector（P9-BE-03：导入模块不得启动任务）；具体 connector（钉钉/GitLab）
由各自模块在显式初始化时 register。
"""
from app.sources.base import SourceConnector
from app.sources.registry import get_connector, register, registered_keys
from app.sources.schemas import NormalizedSourceItem, SourceChange, SourceScope

__all__ = [
    "SourceConnector",
    "NormalizedSourceItem",
    "SourceChange",
    "SourceScope",
    "register",
    "get_connector",
    "registered_keys",
]
