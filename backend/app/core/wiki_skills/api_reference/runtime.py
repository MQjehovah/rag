"""Phase 7B：ApiReferenceRuntime（SkillRuntime 内存适配，不注册生产）。

职责：
- key=`api_reference`、version=`1`；
- extract/plan/render/validate 分别转发到对应纯函数；
- 本 Runtime 不进入 builtin、不注册生产 Registry（7C 之前不接入 Pipeline）；
- 不持有 Session、不调用真实模型、不写库。

context 为预留参数（Phase 6 统一接口要求），本阶段方法直接透传纯函数参数，
保证「Runtime 方法结果 == 直接纯函数结果」。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from app.core.wiki_skills.base import SkillRuntime
from app.core.wiki_skills.api_reference import compiler
from app.core.wiki_skills.api_reference.blueprint import plan_document
from app.core.wiki_skills.api_reference.compiler import (
    ApiCompileResult,
    ApiSourceDocument,
    extract_source,
)
from app.core.wiki_skills.api_reference.renderer import render_document
from app.core.wiki_skills.api_reference.validator import validate_compile


def compile_api_reference(
    sources: Sequence[ApiSourceDocument],
    llm_runner: Callable[[str], str] | None = None,
) -> ApiCompileResult:
    """纯内存编译入口（转发 compiler，供 Runtime 与测试共用）。"""
    return compiler.compile_api_reference(sources, llm_runner=llm_runner)


class ApiReferenceRuntime(SkillRuntime):
    """API Reference Skill 的内存 Runtime（未注册生产 Registry）。"""

    key = "api_reference"
    version = "1"

    def extract(self, context: Any = None, source: ApiSourceDocument | None = None,
                evidence=None) -> Any:
        """单 Source → ApiDocumentIR（与 compiler.extract_source 纯函数一致）。"""
        if source is None:
            raise TypeError("source is required")
        ir, _diags = extract_source(source)
        return ir

    def plan(self, context: Any = None, ir=None, source_labels: Iterable[str] = ()) -> Any:
        if ir is None:
            raise TypeError("ir is required")
        return plan_document(ir, source_labels)

    def render(self, context: Any = None, ir=None, blueprint=None) -> Any:
        if ir is None or blueprint is None:
            raise TypeError("ir and blueprint are required")
        return render_document(ir, blueprint)

    def validate(self, context: Any = None, ir=None, blueprint=None,
                 sections=(), available_ids: Iterable[str] = ()) -> Any:
        if ir is None or blueprint is None:
            raise TypeError("ir and blueprint are required")
        return validate_compile(ir, blueprint, sections, available_ids)
