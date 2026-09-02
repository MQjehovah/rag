"""Connector Registry（P9-BE-03，V3 计划 4.3）。

插件注册表：导入模块时不得启动任务。注册只记录工厂，实例化由调用方
显式触发（避免 import side effect 拉起同步/网络）。
"""
from __future__ import annotations

from typing import Callable, Type

from app.sources.base import SourceConnector

# connector_key -> connector 类工厂
ConnectorFactory = Callable[[dict], SourceConnector]

_registry: dict[str, ConnectorFactory] = {}


def register(key: str, factory: ConnectorFactory) -> None:
    """注册 connector 工厂。重复注册覆盖旧值（幂等）。"""
    _registry[key] = factory


def get_connector(key: str, config: dict) -> SourceConnector:
    """按 key 实例化 connector。未知 key 抛 KeyError。"""
    if key not in _registry:
        raise KeyError(f"unknown connector: {key}")
    return _registry[key](config)


def registered_keys() -> list[str]:
    return sorted(_registry.keys())


def is_registered(key: str) -> bool:
    return key in _registry
