"""Phase 6：SkillRuntime 统一接口。

Skill Runtime 是受信本地组件（非安全沙箱）。Phase 6 只要求统一接口 + 保证 default
Pipeline 等价；default runtime 是 Phase 5 default 行为的适配层，不复制整套 builder。
Runtime 不持有 SQLAlchemy Session、不自行 commit、不访问 Secret。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SkillRuntime(ABC):
    """统一 Runtime 接口。key/version 必须与 descriptor 对齐。"""

    key: str = ""
    version: str = ""

    @abstractmethod
    def extract(self, context, evidence) -> Any:
        """从证据提取字段 → Knowledge IR。Phase 6 default 返回最小占位 IR。"""

    @abstractmethod
    def plan(self, context, ir) -> Any:
        """依据 IR 规划 Section。Phase 6 default 返回空蓝图占位。"""

    @abstractmethod
    def render(self, context, blueprint) -> Any:
        """蓝图 → 可发布内容。Phase 6 default 复用 Phase 5 synthesis。"""

    @abstractmethod
    def validate(self, context, output, evidence) -> Any:
        """内容校验。Phase 6 default 复用 Phase 5 validate。"""
