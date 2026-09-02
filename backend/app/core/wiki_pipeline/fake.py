"""Phase 4.2：FakePipeline/Stage（可测试，只记录执行不产生真实副作用）。

- 内容哈希由系统确定性计算（executor.compute_artifact_content_hash，基于 payload
  + artifact 元数据）；stage 不返回 content_hash（不信任 stage 自报）。
- 失败标记由 trigger_object_id 驱动：
    "fail"    → validate 阶段返回失败（error_code=FAKE_VALIDATION_FAILURE，大写）；
    "explode" → validate 阶段抛异常（STAGE_EXCEPTION 路径）。
- canonicalize 阶段 cachable=True（cache_type=canonical_note，
  cache_schema_version=fake/v1），用于证明 stage_input_hash 缓存
  （同输入同链不重复执行，标记 cached）。
- publish 阶段 allows_publish=True（产出 run.output_revision_id）。
- 只向知识库写入 knowledge_compile_* 表，不触碰 wiki_pages/wiki_revisions，
  不劫持真实 Wiki 编译路径。
"""
from __future__ import annotations

from app.core.wiki_pipeline.registry import (
    FailureTransition,
    PipelineDef,
    StageDef,
    replace_for_test,
    unregister_pipeline,
)

FAKE_PIPELINE_KEYS = (
    "fake.wiki.compile.v1",
    "fake.wiki.compile.cancel-fail",
    "fake.wiki.compile.strict",
)

FAKE_CACHE_SCHEMA_VERSION = "fake/v1"


def _make_stages(failure_transition: FailureTransition) -> list[StageDef]:
    """构建 Fake 三阶段：canonicalize（cachable）→ validate → publish。"""

    def _canonicalize(db, run, stage_run, ctx):
        return {
            "ok": True,
            "artifact_type": "canonical_note",
            "schema_version": "fake/v1",
            "object_type": "wiki_page",
            "object_id": run.wiki_page_id or run.trigger_object_id or "none",
            "payload": {"kind": "canonical_note", "note": "fake-canonical"},
            "metrics": {"stage": "canonicalize"},
        }

    def _validate(db, run, stage_run, ctx):
        if run.trigger_object_id == "fail":
            return {
                "ok": False,
                "error_code": "FAKE_VALIDATION_FAILURE",
                "retryable": True,
            }
        if run.trigger_object_id == "explode":
            raise RuntimeError("fake explode")
        return {
            "ok": True,
            "artifact_type": "validation_report",
            "schema_version": "fake/v1",
            "object_type": "wiki_page",
            "object_id": run.wiki_page_id or run.trigger_object_id or "none",
            "payload": {"report": "ok", "validated": True},
            "metrics": {"stage": "validate"},
        }

    def _publish(db, run, stage_run, ctx):
        return {"ok": True, "output_revision_id": f"rev-fake-{run.id}"}

    return [
        StageDef(
            key="canonicalize",
            version="1",
            retryable=True,
            cachable=True,
            cache_type="canonical_note",
            cache_schema_version=FAKE_CACHE_SCHEMA_VERSION,
            execute=_canonicalize,
            failure_transition=failure_transition,
            description="fake canonical_note",
        ),
        StageDef(
            key="validate",
            version="1",
            retryable=True,
            cachable=False,
            execute=_validate,
            failure_transition=failure_transition,
            description="fake validation_report",
        ),
        StageDef(
            key="publish",
            version="1",
            retryable=False,
            cachable=False,
            execute=_publish,
            failure_transition=failure_transition,
            description="fake publish",
            allows_publish=True,
        ),
    ]


def register_fake_pipelines() -> None:
    """显式注册 Fake 流水线（Phase 4 共三组；不在 import 时自动注册）。

    测试可能多次注册（幂等场景）：replace_for_test 对同 key+version 静默覆盖。
    - fake.wiki.compile.v1 / .cancel-fail：test/framework 语义，allow_null_workspace
      =True（允许 NULL workspace 的既有测试不破坏）。
    - fake.wiki.compile.strict：模拟「真实产品流水线」——不允许 NULL workspace
      （allow_null_workspace=False），用于 workspace_required(400) 反例。
    """
    replace_for_test(PipelineDef(
        key="fake.wiki.compile.v1",
        version="1",
        stages=_make_stages(FailureTransition.FAIL),
        allow_null_workspace=True,
    ))
    replace_for_test(PipelineDef(
        key="fake.wiki.compile.cancel-fail",
        version="1",
        stages=_make_stages(FailureTransition.CANCEL),
        allow_null_workspace=True,
    ))
    replace_for_test(PipelineDef(
        key="fake.wiki.compile.strict",
        version="1",
        stages=_make_stages(FailureTransition.FAIL),
        allow_null_workspace=False,
    ))


def unregister_fake_pipelines() -> None:
    for key in FAKE_PIPELINE_KEYS:
        unregister_pipeline(key)
