"""Phase 4.2：编译流水线注册表（封闭版本，exact-version + 深层不可变）。

- REGISTRY：pipeline_key → {version: PipelineDef}。同 key 不同 version 共存；
  「最新注册的 version」为该 key 的 active 版本（get_pipeline(key) 解析 active；
  get_pipeline(key, version) 精确匹配）。Run 创建时固化 pipeline_version。
- StageDef / PipelineDef：frozen dataclass，__post_init__ 强校验 + 不可变：
  - key/version strip 规范化（统一去空白后存规范值）；
  - StageDef.cache_key_stage_keys 转 tuple + 去重 + 禁止引用自身；
  - PipelineDef.stages 转 tuple + stage_key 去重 + 前置性校验
    （cache_key_stage_keys 引用的 stage 必须存在且位于当前 stage 之前）；
  - cachable=True 必须提供 cache descriptor（cache_type + cache_schema_version）。
- StageDef.allows_publish：仅具备 publish 能力的 stage 可返回 output_revision_id。
- StageResult（frozen）：execute 返回值的强契约载体；validate_stage_result 校验
  dict 或 StageResult，返回空串 = 合法。
- stage_error_message(code)：error_code → 服务端固定中文文案（禁止原始异常文本）。

execute(db, run, stage_run, ctx) 返回值约定（dict 或 StageResult）：
- ok: bool；False 表示阶段失败（error_code 大写 snake + retryable）。
- artifact_type/schema_version/object_type/object_id/payload/metrics/content_hash/
  output_revision_id：成功阶段产物摘要（payload 存 JSON；API 永不返回原文）。
- content_hash 由系统按 payload+artifact 元数据确定性计算，stage 不自行伪造。

failure_transition：
- "fail"（默认）：阶段失败 → run failed（其余未完成 stage 级联 skipped）。
- "cancel"：阶段失败视为取消 → run cancelled（未完成 stage 级联 cancelled）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class PipelineError(RuntimeError):
    pass


class FailureTransition(str, Enum):
    """阶段失败后的 run 级行为。"""

    FAIL = "fail"
    CANCEL = "cancel"


# 严格 stage error_code 格式（大写 snake）。
_STAGE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _deep_copy_json_safe(value: Any) -> Any:
    """深层复制 JSON-safe 值，保证 StageResult 构造后外部原始 dict/list 修改不影响
    结果（构造期冻结的不可变快照）。返回副本（仍为 dict/list，供 json.dumps 序列化）。
    """
    import copy

    return copy.deepcopy(value)


@dataclass(frozen=True)
class StageDef:
    key: str
    version: str = "1"
    retryable: bool = True
    cachable: bool = False
    cache_type: Optional[str] = None  # artifact_type；cache 产物类型
    cache_schema_version: Optional[str] = None  # 产物 schema 版本（cache 身份维度）
    cache_key_stage_keys: tuple[str, ...] | list[str] = ()  # 缓存依赖的上游 stage key；() = 依赖全部前置
    execute: Optional[Callable] = None  # execute(db, run, stage_run, ctx) -> dict | StageResult
    failure_transition: FailureTransition = FailureTransition.FAIL  # fail | cancel
    description: str = ""
    allows_publish: bool = False  # 具备 publish 能力（可返回 output_revision_id）

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise PipelineError("stage key required")
        key = self.key.strip()
        if key != self.key:
            object.__setattr__(self, "key", key)
        if not isinstance(self.version, str) or not self.version.strip():
            raise PipelineError(f"stage {key} version required")
        version = self.version.strip()
        if version != self.version:
            object.__setattr__(self, "version", version)
        # 布尔语义字段必须是真正的 bool（拒绝 0/1/"yes"/None 等伪布尔）。
        for flag_name in ("retryable", "cachable", "allows_publish"):
            flag = getattr(self, flag_name)
            if not isinstance(flag, bool):
                raise PipelineError(f"stage {key} {flag_name} must be bool")
        # cache_key_stage_keys 只接受 list/tuple（拒绝字符串等其它类型）。
        if not isinstance(self.cache_key_stage_keys, (list, tuple)):
            raise PipelineError(f"stage {key} cache_key_stage_keys must be list or tuple")
        # 输入序列不可变冻结（深拷贝入 tuple），修改外部 list 不影响已建 Pipeline。
        object.__setattr__(self, "cache_key_stage_keys", tuple(self.cache_key_stage_keys))
        # failure_transition 收敛为 Enum（允许 "fail"/"cancel" 字符串或枚举成员）。
        if isinstance(self.failure_transition, str):
            try:
                object.__setattr__(
                    self, "failure_transition", FailureTransition(self.failure_transition)
                )
            except ValueError:
                raise PipelineError(
                    f"stage {key} invalid failure_transition={self.failure_transition}"
                ) from None
        if self.failure_transition not in (FailureTransition.FAIL, FailureTransition.CANCEL):
            raise PipelineError(
                f"stage {key} invalid failure_transition={self.failure_transition}"
            )
        # cacheable 必须提供完整 cache descriptor（cache_type + schema_version）。
        if self.cachable:
            if not self.cache_type or not str(self.cache_type).strip():
                raise PipelineError(f"stage {key} cachable requires cache_type")
            if not self.cache_schema_version or not str(self.cache_schema_version).strip():
                raise PipelineError(
                    f"stage {key} cachable requires cache_schema_version"
                )
        # cache_key_stage_keys：去重 + 不能引用自身（前置性/后续/存在性校验在
        # PipelineDef.__post_init__ 依据 stage 顺序统一完成）。
        seen: set[str] = set()
        for upstream in self.cache_key_stage_keys:
            if not isinstance(upstream, str) or not upstream.strip():
                raise PipelineError(
                    f"stage {key} cache_key_stage_keys must be stage key strings"
                )
            if upstream == self.key:
                raise PipelineError(
                    f"stage {key} cache_key_stage_keys must not reference itself"
                )
            if upstream in seen:
                raise PipelineError(
                    f"stage {key} duplicate cache_key_stage_keys={upstream}"
                )
            seen.add(upstream)


@dataclass(frozen=True)
class PipelineDef:
    key: str
    version: str
    stages: list[StageDef] | tuple[StageDef, ...] = field(default_factory=list)
    # allow_null_workspace：产品编译 Run 最终必须非空 workspace（Phase 4.2）。
    # 仅 test/framework 流水线允许 True；False 时 create_run 若有效 workspace 仍为
    # None → 拒绝（400 workspace_required）。
    allow_null_workspace: bool = False
    # 注意：注册方不应复用同一 PipelineDef 实例去注册不同 version（frozen 无妨，
    # 但 version 字段是身份的一部分）。

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise PipelineError("pipeline key required")
        key = self.key.strip()
        if key != self.key:
            object.__setattr__(self, "key", key)
        if not isinstance(self.version, str) or not self.version.strip():
            raise PipelineError(f"pipeline {key} version required")
        version = self.version.strip()
        if version != self.version:
            object.__setattr__(self, "version", version)
        if not isinstance(self.allow_null_workspace, bool):
            raise PipelineError(f"pipeline {key} allow_null_workspace must be bool")
        # stages 冻结为 tuple：注册后不可追加/改序（修改外部输入 list 不影响已建 Pipeline）。
        stages = tuple(self.stages)
        object.__setattr__(self, "stages", stages)
        index_by_key: dict[str, int] = {}
        for idx, s in enumerate(stages):
            if not isinstance(s, StageDef):
                raise PipelineError(f"pipeline {key} stages must be StageDef")
            if s.key in index_by_key:
                raise PipelineError(
                    f"pipeline {key} duplicate stage_key={s.key}"
                )
            index_by_key[s.key] = idx
        # 前置性校验：cache_key_stage_keys 必须引用本 pipeline 中已位于其前的 stage。
        for idx, s in enumerate(stages):
            for upstream in s.cache_key_stage_keys:
                up_idx = index_by_key.get(upstream)
                if up_idx is None:
                    raise PipelineError(
                        f"stage {s.key} cache_key_stage_keys references "
                        f"unknown stage {upstream} in pipeline {key}"
                    )
                if up_idx >= idx:
                    raise PipelineError(
                        f"stage {s.key} cache_key_stage_keys must reference "
                        f"an upstream stage (before it), got {upstream}"
                    )

    def stage_keys(self) -> list[str]:
        return [s.key for s in self.stages]


# pipeline_key → {version: PipelineDef}。dict 保持注册顺序：最后插入 = 最新注册。
REGISTRY: dict[str, dict[str, PipelineDef]] = {}

# Phase 6：显式 active version（pipeline_key → version）。active 不依赖注册顺序；
# get_pipeline(key) 优先解析显式 active；未设置时才回退「最新注册」（向后兼容）。
_ACTIVE_VERSIONS: dict[str, str] = {}


def _version_map(key: str) -> dict[str, PipelineDef]:
    return REGISTRY.setdefault(key, {})


def register_pipeline(pipeline: PipelineDef) -> None:
    """注册流水线；同 key+version 已注册 → PipelineError（默认拒绝静默覆盖）。

    同 key 不同 version 允许共存，注册即成为该 key 的 active 版本。
    """
    if not isinstance(pipeline, PipelineDef):
        raise PipelineError("register_pipeline requires PipelineDef")
    if pipeline.key not in REGISTRY and not pipeline.stages:
        raise PipelineError(f"pipeline {pipeline.key} has no stages")
    versions = _version_map(pipeline.key)
    if pipeline.version in versions:
        raise PipelineError(
            f"pipeline_already_registered={pipeline.key}:{pipeline.version}"
        )
    versions[pipeline.version] = pipeline


def replace_for_test(pipeline: PipelineDef) -> None:
    """显式覆盖注册（测试用）：同 key+version 已注册时静默替换；新 version 则加入。

    覆盖既有 version 不改变该 key 的 active 指向（active = 最后注册 version）。
    该 key 首次注册新 version 时会成为 active。
    """
    if not isinstance(pipeline, PipelineDef):
        raise PipelineError("replace_for_test requires PipelineDef")
    if pipeline.key not in REGISTRY and not pipeline.stages:
        raise PipelineError(f"pipeline {pipeline.key} has no stages")
    versions = _version_map(pipeline.key)
    versions[pipeline.version] = pipeline


def set_active_version(key: str, version: str) -> None:
    """显式设置该 key 的 active version（Phase 6）。

    - 必须指向已注册版本，否则 PipelineError（fail closed，绝不静默指向未知）；
    - active 不依赖注册顺序：后续再注册新 version 不会自动改写 active。
    """
    versions = REGISTRY.get(key)
    if not versions or version not in versions:
        raise PipelineError(
            f"active_version_not_registered={key}:{version}"
        )
    _ACTIVE_VERSIONS[key] = version


def get_active_version(key: str) -> str | None:
    """返回显式 active version；未设置或已删除（孤悬）→ None。"""
    versions = REGISTRY.get(key)
    if not versions:
        return None
    active = _ACTIVE_VERSIONS.get(key)
    if active is not None and active in versions:
        return active
    return None


def get_pipeline(key: str, version: str | None = None) -> PipelineDef | None:
    """精确/active 查询。version=None → 显式 active；未设置 → 最新注册（兼容）。"""
    versions = REGISTRY.get(key)
    if not versions:
        return None
    if version is None:
        active = get_active_version(key)
        if active is not None:
            return versions[active]
        # dict 插入有序：最后插入的 key 即「最新注册」版本（向后兼容）。
        return next(reversed(versions.values()))
    return versions.get(version)


def require_pipeline(key: str, version: str | None = None) -> PipelineDef:
    pipeline = get_pipeline(key, version)
    if pipeline is None:
        raise PipelineError(f"pipeline_not_registered={key}:{version or ''}".rstrip(":"))
    return pipeline


def registered_versions(key: str) -> list[str]:
    """返回该 key 已注册的 version（注册顺序）。key 未注册 → []。"""
    versions = REGISTRY.get(key)
    if not versions:
        return []
    return list(versions.keys())


def unregister_pipeline(key: str) -> bool:
    """移除注册（测试隔离用）；返回是否确实移除。同时清理显式 active。"""
    removed = REGISTRY.pop(key, None) is not None
    _ACTIVE_VERSIONS.pop(key, None)
    return removed


def clear_for_tests() -> None:
    """清理全部注册与显式 active（测试隔离用）。"""
    REGISTRY.clear()
    _ACTIVE_VERSIONS.clear()


def registered_pipelines() -> list[str]:
    return sorted(REGISTRY.keys())


# ---------------------------------------------------------------------------
# StageResult 契约（frozen）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageResult:
    """阶段执行结果（强契约，frozen）。

    - 成功（ok=True）：可选产出 artifact 摘要 + metrics + payload；
      content_hash 若提供必须等于系统按 payload+元数据算出的值。
    - 失败（ok=False）：error_code 大写 snake（^[A-Z][A-Z0-9_]*$）；禁止
      artifact/output_revision_id；error_message 由服务端固定映射，禁止原始异常。
    """

    ok: bool
    error_code: Optional[str] = None
    retryable: bool = True
    artifact_type: Optional[str] = None
    schema_version: Optional[str] = None
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    metrics: Optional[dict] = None
    payload: Any = None
    content_hash: Optional[str] = None
    output_revision_id: Optional[str] = None
    error_message: Optional[str] = None  # 服务端固定 safe 文案；禁止原始异常文本

    def __post_init__(self) -> None:
        # 构造期冻结 payload/metrics 深拷贝：外部修改原始 dict/list 不影响本结果。
        object.__setattr__(self, "metrics", _deep_copy_json_safe(self.metrics))
        object.__setattr__(self, "payload", _deep_copy_json_safe(self.payload))


# 已知 error_code → 固定中文文案（服务端映射，绝不暴露内部异常 detail）。
_STAGE_SAFE_MESSAGES = {
    "STAGE_EXCEPTION": "阶段执行异常，错误详情仅记录于服务端日志",
    "STAGE_CONTRACT_VIOLATION": "阶段返回结果违反执行契约",
    "STAGE_NO_EXECUTE": "阶段未实现执行函数",
    "STAGE_COMMIT_FORBIDDEN": "阶段尝试自行提交事务，已被阻止",
    "FAKE_VALIDATION_FAILURE": "伪造校验阶段失败",
    "PIPELINE_NOT_REGISTERED": "编译流水线未注册",
    "PIPELINE_NO_STAGES": "编译流水线未定义任何阶段",
    "RETRY_EXHAUSTED": "重试次数已达上限",
    "WORKER_LOST": "执行租约丢失，任务将由系统恢复",
    "CANCELLED_BY_USER": "任务已取消",
    # Phase 7C.3：wiki.default v3 单目标发布受控失败码（固定文案，不落内部细节）。
    "TRIGGER_NOT_SUPPORTED": "知识编译失败：当前触发类型在本版本不受支持（不可回退）",
    "MULTI_TARGET_NOT_SUPPORTED": "知识编译失败：本版本仅支持单目标发布",
    "SKILL_DECISION_MISSING": "知识编译失败：缺少已持久化的 Skill 决策（禁止猜测分派）",
    "SKILL_NOT_SUPPORTED": "知识编译失败：Skill 或其版本不受本版本支持",
    "VALIDATION_FAILED": "知识编译失败：API Reference 发布前校验未通过",
    "PAGE_STALE": "知识编译失败：来源页在编译期间发生变化（不可发布）",
    "EVIDENCE_STALE": "知识编译失败：证据在编译期间发生变化或失效（不可发布）",
    "WORKSPACE_MISMATCH": "知识编译失败：工作区归属缺失或不匹配（不可自动重试）",
    "API_PROTECTED_SECTION_REQUIRES_MIGRATION": (
        "知识编译失败：当前修订含人工保护 Section，需迁移后才能由 API Reference 覆盖"
    ),
    "API_PERSIST_FAILED": "知识编译失败：API Reference 发布写入失败（不可自动重试）",
    # Phase 7C.3-B：Skill 迁移受控失败码（固定文案，不落内部细节）。
    "MIGRATION_TARGET_MISSING": "知识编译失败：迁移建议缺少显式目标 Skill（禁止猜测）",
    "MIGRATION_TARGET_INVALID": "知识编译失败：迁移目标与当前 Skill 不一致或目标无效",
    "MIGRATION_DIRECTION_NOT_SUPPORTED": "知识编译失败：当前迁移方向不受支持",
    "MIGRATION_VALIDATION_FAILED": "知识编译失败：迁移 shadow compile 未通过校验",
    # Phase 7C.3-C：混合 Skill 批处理受控失败码（固定文案，不落内部细节）。
    "BATCH_PARTIAL": "知识编译失败：批量编译存在未成功目标（可重试）",
    "BATCH_STALE": "知识编译失败：批量输入已变化，无法复用旧成功记录（请重新创建编译）",
    "BATCH_PUBLISH_FAILED": "知识编译失败：批量发布写入失败（不可自动重试）",
    "BATCH_PLAN_CHANGED": "知识编译失败：批量计划与历史不一致，无法复用（请创建新编译）",
    # Phase 7D：v2 回滚安全边界（api_reference/迁移只允许 v3）。
    "SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION": (
        "知识编译失败：当前 Pipeline 版本不支持该 Skill（请使用 v3）"
    ),
}


def stage_error_message(code: str | None) -> str:
    """error_code → 固定中文文案（未知 code 统一收敛为通用文案，不做文本清洗）。"""
    if not code:
        return "阶段执行失败"
    return _STAGE_SAFE_MESSAGES.get(code, "阶段执行失败")


def _is_json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def validate_stage_result(result: Any) -> str:
    """execute 返回值契约验证；返回空串 = 合法（dict 或 StageResult 均可）。

    拒绝矩阵：
    - 成功：artifact 类型字段必须 str/None；metrics/payload 必须 JSON-safe
      （allow_nan=False，无 bytes/datetime/custom）；content_hash/output_revision_id
      若给必须 str；metrics 非 dict → violation。
    - 失败：error_code 必须存在且匹配 ^[A-Z][A-Z0-9_]*$；不得携带 artifact_type /
      output_revision_id；retryable 必须 bool；error_message 若给必须 str。
    """
    if isinstance(result, StageResult):
        values = {
            "ok": result.ok,
            "error_code": result.error_code,
            "retryable": result.retryable,
            "artifact_type": result.artifact_type,
            "schema_version": result.schema_version,
            "object_type": result.object_type,
            "object_id": result.object_id,
            "metrics": result.metrics,
            "payload": result.payload,
            "content_hash": result.content_hash,
            "output_revision_id": result.output_revision_id,
            "error_message": result.error_message,
        }
    elif isinstance(result, dict):
        values = {
            "ok": result.get("ok"),
            "error_code": result.get("error_code"),
            "retryable": result.get("retryable", True),
            "artifact_type": result.get("artifact_type"),
            "schema_version": result.get("schema_version"),
            "object_type": result.get("object_type"),
            "object_id": result.get("object_id"),
            "metrics": result.get("metrics"),
            "payload": result.get("payload"),
            "content_hash": result.get("content_hash"),
            "output_revision_id": result.get("output_revision_id"),
            "error_message": result.get("error_message"),
        }
    else:
        return "stage_contract_violation: result must be dict or StageResult"

    ok = values["ok"]
    if not isinstance(ok, bool):
        return "stage_contract_violation: ok must be bool"

    if ok:
        for name in ("artifact_type", "schema_version", "object_type", "object_id"):
            value = values[name]
            if value is not None and not isinstance(value, str):
                return f"stage_contract_violation: {name} must be str or None"
        if values["error_code"] is not None and not isinstance(values["error_code"], str):
            return "stage_contract_violation: error_code must be str"
        content_hash = values["content_hash"]
        if content_hash is not None and not isinstance(content_hash, str):
            return "stage_contract_violation: content_hash must be str"
        output_revision_id = values["output_revision_id"]
        if output_revision_id is not None and not isinstance(output_revision_id, str):
            return "stage_contract_violation: output_revision_id must be str"
        metrics = values["metrics"]
        if metrics is not None:
            if not isinstance(metrics, dict):
                return "stage_contract_violation: metrics must be dict"
            if not _is_json_safe(metrics):
                return "stage_contract_violation: metrics not JSON-safe"
        payload = values["payload"]
        if payload is not None and not _is_json_safe(payload):
            return "stage_contract_violation: payload not JSON-safe"
        if values["error_message"] is not None and not isinstance(values["error_message"], str):
            return "stage_contract_violation: error_message must be str"
        # artifact_type=None（本 stage 不产 artifact）时，其余 artifact 产物字段
        # （除 output_revision_id：publish 能力独立，可无 artifact 产出 Revision）
        # 必须为 None。
        if values["artifact_type"] is None:
            for name in (
                "schema_version", "object_type", "object_id", "content_hash",
            ):
                if values[name] is not None:
                    return f"stage_contract_violation: no artifact but {name} set"
        return ""

    # 失败路径
    error_code = values["error_code"]
    if not isinstance(error_code, str) or not _STAGE_ERROR_CODE_RE.match(error_code):
        return "stage_contract_violation: error_code must match ^[A-Z][A-Z0-9_]*$"
    if not isinstance(values["retryable"], bool):
        return "stage_contract_violation: retryable must be bool"
    # 失败结果禁止携带任何 artifact 相关产物字段。
    for name in (
        "artifact_type", "schema_version", "object_type", "object_id",
        "payload", "metrics", "content_hash", "output_revision_id",
    ):
        if values[name] is not None:
            return f"stage_contract_violation: failed stage must not carry {name}"
    if values["error_message"] is not None and not isinstance(values["error_message"], str):
        return "stage_contract_violation: error_message must be str"
    return ""
