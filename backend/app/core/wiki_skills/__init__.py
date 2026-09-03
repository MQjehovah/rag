"""Phase 6：Content Skill 插件框架（受控本地插件）。

职责划分（封板）：
- schemas：不可变 DTO（frozen + 深层不可变 + JSON-safe）；
- base：统一 Runtime 接口；
- registry：Skill 版本与 Runtime 注册（allowlist + 显式 active）；
- loader：受控资源加载（builtin/ 目录，路径校验 + 字段白名单）；
- signals：确定性信号提取（不调 LLM）；
- router：自主 Skill 选择（规则信号 + LLM）；
- service：对 Pipeline 提供统一入口；
- builtin/default：默认 Skill 声明与 Runtime 适配层。
"""

from app.core.wiki_skills.schemas import (
    DECISION_STATUS_VALUES,
    SELECTED_BY_VALUES,
    SIGNAL_SOURCE_VALUES,
    SKILL_DECISION_SCHEMA,
    ApplicabilitySignal,
    DecisionStatus,
    SelectedBy,
    SkillCandidate,
    SkillContext,
    SkillDecision,
    SkillDescriptor,
    SignalSource,
)

__all__ = [
    "ApplicabilitySignal",
    "DECISION_STATUS_VALUES",
    "DecisionStatus",
    "SELECTED_BY_VALUES",
    "SIGNAL_SOURCE_VALUES",
    "SKILL_DECISION_SCHEMA",
    "SelectedBy",
    "SignalSource",
    "SkillCandidate",
    "SkillContext",
    "SkillDecision",
    "SkillDescriptor",
]
