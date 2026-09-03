"""Phase 7D：wiki.default v3 统一生产启动 bootstrap。

职责：
1. 注册 builtin Skills（default + api_reference）；
2. 注册 wiki.default v1、v2、v3（历史 queued Run 按固化 pipeline_version 精确恢复，
   即使 active=v3 也必须注册 v1/v2）；
3. 从配置显式设置 active version（wiki_pipeline_active_version，只允许 "2"/"3"）；
4. 验证 v3 所需 default/api_reference 精确版本已注册；缺失 → fail closed；
5. 幂等：重复执行不产生重复注册、不产生 active version 漂移；
6. 注册/校验失败 → raise 中止启动（不吞异常）。

v1/v2 注册函数本身不抛（定义一致时幂等）；冲突会抛 PipelineError → 启动中止。
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

PIPELINE_KEY = "wiki.default"
_ALLOWED_ACTIVE = ("2", "3")
_REQUIRED_SKILLS = (("default", "1"), ("api_reference", "1"))


def _resolve_active_version() -> str:
    value = str(settings.wiki_pipeline_active_version or "3").strip()
    if value not in _ALLOWED_ACTIVE:
        raise RuntimeError(
            f"wiki_pipeline_active_version 只允许 {'/'.join(_ALLOWED_ACTIVE)}，"
            f"收到 {value!r}")
    return value


def _register_builtin_skills() -> None:
    from app.core.wiki_skills import service as skill_service

    skill_service.register_builtin_skills()


def _register_pipelines() -> None:
    from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        register_default_pipeline_v2,
    )
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        register_default_pipeline_v3,
    )

    register_default_pipeline()     # v1（幂等）
    register_default_pipeline_v2()  # v2（幂等；内部会设 active=v2，随后统一覆写）
    register_default_pipeline_v3()  # v3（幂等；不改 active）


def _validate_registered() -> None:
    from app.core.wiki_skills import registry as skill_registry
    from app.core.wiki_pipeline import registry as pipeline_registry

    for key, version in _REQUIRED_SKILLS:
        if not skill_registry.has(key, version):
            raise RuntimeError(
                f"required skill missing: {key}:{version}")
    for version in ("1", "2", "3"):
        if pipeline_registry.get_pipeline(PIPELINE_KEY, version) is None:
            raise RuntimeError(
                f"required pipeline missing: {PIPELINE_KEY}:{version}")


def bootstrap_wiki_pipeline() -> str:
    """生产 wiki pipeline bootstrap：返回实际 active version。

    顺序：builtin Skills → v1/v2/v3 注册 → 必需版本校验 → 显式 active。
    幂等；任何失败 raise（fail closed，阻止启动）。
    """
    _register_builtin_skills()
    _register_pipelines()
    _validate_registered()
    active = _resolve_active_version()
    from app.core.wiki_pipeline import registry as pipeline_registry

    pipeline_registry.set_active_version(PIPELINE_KEY, active)
    logger.info("wiki.default pipeline bootstrap active=%s (v1/v2/v3 registered)", active)
    return active
