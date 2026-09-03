"""Phase 5：wiki.default pipeline 的跨 stage 结构化 DTO。

全部 frozen dataclass，字段仅限 JSON 安全标量 / list / dict / tuple[str]，
保证任意时刻可 json.dumps（stage 契约要求 payload/metrics JSON-safe，stage 间
状态经 ctx["state"] 以 dict 传递也不失序列化能力）。

- ResolveContext：resolve_context 输出（Page/workspace/binding/scope/hash 快照；
  含 batch_rebuild 的多 Page 扩展：page_ids + page_input_hashes，保留单 page 字段兼容）。
- TopicDecision：topic_route 输出（LLM 主题判定结果，不触碰 Wiki/Revision 写）。
- SynthesisResult：synthesize_default 输出（合成正文，正文不入 artifact，落库走
  Revision；本 DTO 只作为 stage 间内存传递与产物摘要）。
- ValidationReport：validate_default 输出（发布前校验结论）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _to_dict(dto: Any) -> dict:
    """把 frozen dataclass 转 dict；tuple 展开为 list（保证 JSON 可序列化）。"""

    def _clean(value: Any) -> Any:
        if isinstance(value, tuple):
            return [_clean(v) for v in value]
        if isinstance(value, dict):
            return {k: _clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clean(v) for v in value]
        return value

    return {k: _clean(v) for k, v in asdict(dto).items()}


@dataclass(frozen=True)
class ResolveContext:
    """resolve_context 输出：本次 run 的确定性输入上下文。

    input_hash 与旧 `_page_input_hash` 口径一致（title/content_text/notebook_id/
    scope_acl_json），供 validate/publish 前重算做 STALE_INPUT 依据。
    """

    applicable: bool = False
    reason: str = ""  # 空串 = 可执行；否则 not applicable 原因（不触发发布写）。
    trigger_type: str = ""
    page_id: str | None = None
    wiki_page_id: str | None = None  # manual_rebuild 的目标 wiki（存在时）
    workspace_id: str | None = None
    notebook_id: str | None = None
    scope_acl_json: str | None = None
    title: str = ""
    content_text: str = ""
    input_hash: str = ""
    source_sync_run_id: str | None = None
    source_page_ids: tuple[str, ...] = field(default_factory=tuple)  # manual_rebuild 来源
    page_exists: bool = True  # page_deleted 且行已删 → False（不可再读 Page，但可从 deletion Artifact 恢复来源移除）
    # Phase 5.2 batch_rebuild：本批多 Page（排序去重）与每页确定性 input_hash（读输入 Artifact）。
    page_ids: tuple[str, ...] = field(default_factory=tuple)
    page_input_hashes: dict = field(default_factory=dict)
    # Phase 5.2 page_deleted：Page 行删除后从 wiki_page_deleted_input Artifact 恢复的上下文。
    deletion_source_wiki_ids: tuple[str, ...] = field(default_factory=tuple)  # 删除前该 Page 的来源 Wiki
    deletion_hash: str = ""  # 删除事件确定性指纹（完整 64）
    notebook_id_restored: str = ""  # 行删后恢复的 notebook_id（审计）

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass(frozen=True)
class TopicDecision:
    """topic_route 输出。status：

    - create_update：worthy 且有合法 op（create/update）；
    - rebuild_wiki：manual_rebuild（不跑 ingest LLM，直接重建既有 wiki）；
    - not_worthy：内容过短或 LLM 显式 not_worthy（发布时解除旧来源）；
    - page_deleted：删除触发（发布时按 remove_source 语义处理）；
    - invalid_response / service_unavailable：LLM 未产出可用主题（保持 dirty）；
    - not_applicable：resolve_context 判定无需处理。
    """

    status: str = "not_applicable"
    ops: list[dict] = field(default_factory=list)  # 合法 op（create/update，≤2）
    note: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass(frozen=True)
class SynthesisResult:
    """synthesize_default 输出。

    targets：与发布一一对应的目标（normalized title 定位，key 唯一）。
    synthesized：按 norm_title 索引的合成产物（content/summary/versioned/vc）。
    status：success / not_required（无目标） / invalid_response / service_unavailable。
    """

    status: str = "success"
    targets: list[dict] = field(default_factory=list)
    synthesized: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)


@dataclass(frozen=True)
class ValidationReport:
    """validate_default 输出。ok=False 且 stale 相关字段由发布前重算兜底。"""

    ok: bool = True
    issues: list[str] = field(default_factory=list)
    input_hash_unchanged: bool = True
    workspace_unchanged: bool = True
    has_content: bool = False
    revision_targets: int = 0

    def to_dict(self) -> dict:
        return _to_dict(self)


# 编译结果类别（Publish Manifest 与 finalize_compile_outcome 判定依据）。
OUTCOME_PUBLISHED = "published"
OUTCOME_NOT_WORTHY = "not_worthy"
OUTCOME_ARCHIVED = "archived"
OUTCOME_NOOP = "noop"
OUTCOME_NOT_APPLICABLE = "not_applicable"
OUTCOME_KEEP_DIRTY = "keep_dirty"  # 具体失败类别见 fail_code

# fail_code（知识编译失败类别，finalize 据此使 Run 反映失败而非虚假 succeeded）。
FAIL_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
FAIL_INVALID_RESPONSE = "INVALID_RESPONSE"
FAIL_STALE_INPUT = "STALE_INPUT"
FAIL_WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"
FAIL_VALIDATION_FAILED = "VALIDATION_FAILED"
FAIL_PARTIAL_SYNTHESIS = "PARTIAL_SYNTHESIS"


@dataclass(frozen=True)
class PublishManifest:
    """publish_default 持久化产物（wiki_publish_manifest Artifact）与最终判定依据。

    - 一个 Run 可能发布/影响多个 Wiki：wiki_page_ids / revision_ids / archived_wiki_ids /
      dirty_wiki_ids 精确记录本次实际影响对象（不只单值 output_revision_id）。
    - graph_targets = [{"kind": "wiki", "wiki_page_id"} | {"kind": "page", "page_id"}]，
      供 schedule_graph 真实同步重建与 graph retry 恢复全部目标。
    - outcome/fail_code/retryable：finalize_compile_outcome 据此把 Run 判为
      succeeded（正常发布/not_worthy/合法 no-op/not_applicable）或 failed
      （LLM 不可用 / 非法 / stale / workspace mismatch / validation / 部分失败）。
    只存 ID/hash/状态，不含大正文。
    """

    outcome: str = OUTCOME_NOOP
    fail_code: str | None = None
    retryable: bool = True
    page_id: str | None = None
    wiki_page_ids: tuple[str, ...] = field(default_factory=tuple)
    revision_ids: tuple[str, ...] = field(default_factory=tuple)
    archived_wiki_ids: tuple[str, ...] = field(default_factory=tuple)
    dirty_wiki_ids: tuple[str, ...] = field(default_factory=tuple)
    graph_targets: tuple[dict, ...] = field(default_factory=tuple)
    input_hash: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return _to_dict(self)
