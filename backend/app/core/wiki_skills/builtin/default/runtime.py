"""Phase 6：default Skill Runtime（Phase 5 default 行为的适配层）。

统一接口（extract/plan/render/validate）已提供；但 default 的最终合成/发布语义在
Pipeline 内复用 Phase 5 synthesis/validation/publish，**不复制整套 wiki_page_builder**。
因此本 Runtime 的方法只提供接口占位 + 最小确定性结构，生产 default Pipeline 不直接
调用它们（等价性由 Pipeline 测试保证）。不持有 Session、不 commit、不访问 Secret。
"""
from __future__ import annotations

from app.core.wiki_skills.base import SkillRuntime

_EXTRACTION_SCHEMA = "default-extraction/v1"
_BLUEPRINT_SCHEMA = "default-blueprint/v1"


class DefaultSkillRuntime(SkillRuntime):
    """default Skill：通用主题 Wiki 的默认内容结构。"""

    key = "default"
    version = "1"

    def extract(self, context, evidence):
        """Phase 6 default 不做独立提取（提取字段由 Phase 5 synthesis 完成）。

        返回最小占位 IR，保证统一接口可调用且结构 JSON-safe。
        """
        return {
            "schema_version": _EXTRACTION_SCHEMA,
            "target_key": getattr(context, "target_key", "") if context else "",
            "summary": "",
            "facts": [],
            "body": "",
        }

    def plan(self, context, ir):
        """Phase 6 default 蓝图 = Phase 5 规划（由 pipeline 完成），此处返回占位。"""
        return {
            "schema_version": _BLUEPRINT_SCHEMA,
            "target_key": (ir or {}).get("target_key", ""),
            "versioned": False,
            "sections": [],
        }

    def render(self, context, blueprint):
        """Phase 6 default 合成走 Pipeline（_synthesize_*），此处返回占位（不产正文）。"""
        return {
            "schema_version": _BLUEPRINT_SCHEMA,
            "target_key": (blueprint or {}).get("target_key", ""),
            "content": None,
        }

    def validate(self, context, output, evidence):
        """Phase 6 default 校验由 Pipeline validate_default 完成，此处返回空通过。"""
        return {"ok": True, "issues": []}
