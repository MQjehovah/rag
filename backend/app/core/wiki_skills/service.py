"""Phase 6：Service —— 对 Pipeline 提供统一入口。

职责：
- register_builtin_skills()：加载 builtin Skill 声明（loader）→ 注册 allowlisted
  Runtime → 注册 descriptor/runtime → 显式设置 active version；default 加载失败
  → SkillDefaultError（启动中止，不允许无 default 继续）。
- decide()：构造 SkillDecision（代理 router，规则/LLM 统一入口）。
- get_runtime / resolve：Runtime 与 descriptor 解析（供 API / pipeline 复用）。
"""
from __future__ import annotations

from typing import Callable

from app.core.wiki_skills import loader, registry, router
from app.core.wiki_skills.base import SkillRuntime
from app.core.wiki_skills.builtin.default.runtime import DefaultSkillRuntime
from app.core.wiki_skills.registry import (
    SkillError,
    SkillNotFound,
    SkillRuntimeError,
)
from app.core.wiki_skills.schemas import SkillContext, SkillDecision

# 显式 active 版本（不猜测最大字符串版本）。
_DEFAULT_ACTIVE_VERSION = {"default": "1"}


class SkillServiceError(SkillError):
    """Service 受控错误。"""


def register_builtin_skills() -> list[str]:
    """加载并注册 builtin Skills。

    - 先登记 allowlist（受控代码工厂：default → DefaultSkillRuntime）；
    - loader.load_all()：default 失败 → 抛错中止；非 default 失败隔离；
    - 注册 descriptor + 实例化 allowlisted Runtime；
    - 显式设置每个 key 的 active version（_DEFAULT_ACTIVE_VERSION 优先；
      未声明且仅一个版本 → 取该版本）。
    返回成功注册的 skill key 列表。
    """
    registry.register_runtime_allowlist("default", DefaultSkillRuntime)
    descriptors = loader.load_all()  # default 缺失/损坏 → SkillDefaultError
    registered: list[str] = []
    for key in sorted(descriptors.keys()):
        versions = descriptors[key]
        for version in sorted(versions.keys()):
            desc = versions[version]
            registry.register_descriptor(desc)
            factory = registry.get_runtime_factory(desc.runtime_key)
            if factory is None:
                raise SkillRuntimeError(
                    f"runtime_key_not_allowed={desc.runtime_key}"
                )
            runtime = factory()
            if not isinstance(runtime, SkillRuntime):
                raise SkillRuntimeError(
                    f"runtime_factory_invalid={desc.runtime_key}"
                )
            registry.register_runtime(runtime)
        registered.append(key)
    # 显式 active version（active 不依赖注册顺序）。
    for key, descs in descriptors.items():
        versions = sorted(descs.keys())
        active = _DEFAULT_ACTIVE_VERSION.get(
            key, versions[0] if len(versions) == 1 else None
        )
        if active is not None and active in descs:
            registry.set_active_version(key, active)
    return registered


def register_default_skill() -> str:
    """注册 default Skill（startup 顺序：先 default，再注册 v1/v2 pipeline）。"""
    keys = register_builtin_skills()
    if "default" not in keys:
        raise SkillServiceError("default skill not registered")
    return "default"


def decide(
    context: SkillContext,
    *,
    llm_runner: Callable | None = None,
) -> SkillDecision:
    """为单个目标构造 SkillDecision（代理 Auto Skill Router）。"""
    return router.route(context, llm_runner=llm_runner)


def decide_from_snapshot(
    context: SkillContext,
    snapshot: dict,
    *,
    llm_runner: Callable | None = None,
) -> SkillDecision:
    """使用外部不可变 snapshot 路由（测试/批量复用同一注册快照）。"""
    return router.route(context, snapshot=snapshot, llm_runner=llm_runner)


def get_default_descriptor():
    """default active descriptor；缺失 → SkillNotFound（fail closed）。"""
    return registry.get_active("default")


def get_runtime(skill_key: str, version: str) -> SkillRuntime:
    """精确 Runtime（供 API/下游使用）；缺失 → SkillNotFound。"""
    return registry.get_runtime(skill_key, version)


def resolve_runtime(decision: SkillDecision) -> SkillRuntime:
    """由 decision 解析 Runtime（锁定/选中 Skill 必须能在 Registry 精确解析）。"""
    if not decision.selected_skill or not decision.selected_version:
        raise SkillNotFound(f"skill_not_selected={decision.target_key or ''}")
    descriptor = registry.get(decision.selected_skill, decision.selected_version)
    return registry.get_runtime(descriptor.key, descriptor.version)
