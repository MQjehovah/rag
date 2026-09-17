"""WikiSkill 演化存储模型（阶段 2 P45 / 阶段 3 经验 Wiki）：独立 metadata。

设计要点：
- 内容版本不可变：只允许新增行（seq 单调递增），无原地修改路径；
- 本模块模型**不注册到业务 Base.metadata**：任何业务库 init_db/create_all 都不会
  自动建/改演化表（新功能关闭时旧 P44 库可继续运行）；启用演化（experiment/
  business 使用这些表）时调用方显式 `create_evolution_schema` / `require_evolution_schema`，
  业务库须先经 Alembic P45+ 显式迁移；
- 绑定区分 experiment / business 两种 kind；业务绑定默认不启用；
- 显式空技能集合 = binding 行（skill/version 为 NULL，set_kind='empty'）；
- 阶段 3 经验 Wiki（pattern/revision/run/log/index）同 metadata：权威存储在实验控制
  库（root/skill_store.db），Markdown 仅作导出快照。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

EVOLUTION_SCHEMA_VERSION = "skill-evolution/v1"

# 绑定 kind（实验 / 业务）；业务绑定阶段 2 默认不启用。
BINDING_KIND_EXPERIMENT = "experiment"
BINDING_KIND_BUSINESS = "business"
VALID_BINDING_KINDS = (BINDING_KIND_EXPERIMENT, BINDING_KIND_BUSINESS)

# 绑定内容形态：skill=精确技能版本；empty=显式空技能集合（无技能基线）。
SET_KIND_SKILL = "skill"
SET_KIND_EMPTY = "empty"
VALID_SET_KINDS = (SET_KIND_SKILL, SET_KIND_EMPTY)

# 技能版本来源类型。
SOURCE_TYPE_BUILTIN_SEED = "builtin_seed"
SOURCE_TYPE_MANUAL_SEED = "manual_seed"
VALID_SOURCE_TYPES = (SOURCE_TYPE_BUILTIN_SEED, SOURCE_TYPE_MANUAL_SEED)

# 经验模式状态：不把一次观察自动标记为稳定规律。
PATTERN_STATUS_OBSERVED = "observed"
PATTERN_STATUS_SUPPORTED = "supported"
PATTERN_STATUS_CONTRADICTED = "contradicted"
VALID_PATTERN_STATUSES = (
    PATTERN_STATUS_OBSERVED,
    PATTERN_STATUS_SUPPORTED,
    PATTERN_STATUS_CONTRADICTED,
)

# 维护运行状态。
RUN_STATUS_CREATED = "created"
RUN_STATUS_APPLIED = "applied"
RUN_STATUS_FAILED = "failed"
VALID_RUN_STATUSES = (RUN_STATUS_CREATED, RUN_STATUS_APPLIED, RUN_STATUS_FAILED)

# ---------------------------------------------------------------------------
# 阶段 5 状态与决策。
# 门控决策：accepted=提升接受（strict > best）；rejected=拒绝（tie/degrade）；
# invalid=评估无效（基础设施/存储/配置），不晋升也不伪装成效果拒绝。
# ---------------------------------------------------------------------------
GATE_ACCEPTED = "accepted"
GATE_REJECTED = "rejected"
GATE_INVALID = "invalid"
VALID_GATE_DECISIONS = (GATE_ACCEPTED, GATE_REJECTED, GATE_INVALID)
EXPERIMENT_STATUS_ACTIVE = "active"
EXPERIMENT_STATUS_CLOSED = "closed"

# 演化运行状态（阶段 6）。
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_BUDGET_EXHAUSTED = "budget_exhausted"
VALID_RUN_STATUSES = (RUN_QUEUED, RUN_RUNNING, RUN_PAUSED, RUN_COMPLETED,
                      RUN_FAILED, RUN_CANCELLED, RUN_BUDGET_EXHAUSTED)
# 正常结束原因：达到最大轮数 / 验证满分提前停止。
STOP_MAX_ITERATIONS = "max_iterations"
STOP_PERFECT_SCORE = "perfect_score"
# 轮次步骤（检查点顺序）。
ITER_STEP_TRAIN = "train"
ITER_STEP_MAINTAIN = "maintain"
ITER_STEP_PROPOSE = "propose"
ITER_STEP_EVAL = "eval"
ITER_STEP_GATE = "gate"
ITER_STEP_DONE = "done"
VALID_ITER_STEPS = (ITER_STEP_TRAIN, ITER_STEP_MAINTAIN, ITER_STEP_PROPOSE,
                    ITER_STEP_EVAL, ITER_STEP_GATE, ITER_STEP_DONE)
ITER_RUNNING = "running"
ITER_PAUSED = "paused"
ITER_DONE = "done"
ITER_FAILED = "failed"
VALID_ITER_STATUSES = (ITER_RUNNING, ITER_PAUSED, ITER_DONE, ITER_FAILED)

# ---------------------------------------------------------------------------
# 阶段 4 提议状态（ProposalRun/Proposal 共用白名单）。
# candidate_saved=候选已保存（≠ accepted/rejected，门控结论属于阶段 5）；
# output_invalid=格式非法；prerequisites_insufficient=轨迹不足等前置不足；
# budget_exhausted=预算耗尽；no_action=无修改；failed=其它失败。
# ---------------------------------------------------------------------------
PROPOSER_RUN_GENERATING = "generating"
PROPOSAL_CANDIDATE_SAVED = "candidate_saved"
PROPOSAL_NO_ACTION = "no_action"
PROPOSAL_PREREQ_INSUFFICIENT = "prerequisites_insufficient"
PROPOSAL_OUTPUT_INVALID = "output_invalid"
PROPOSAL_FAILED = "failed"
PROPOSAL_BUDGET_EXHAUSTED = "budget_exhausted"
VALID_PROPOSAL_STATUSES = (
    PROPOSAL_CANDIDATE_SAVED,
    PROPOSAL_NO_ACTION,
    PROPOSAL_PREREQ_INSUFFICIENT,
    PROPOSAL_OUTPUT_INVALID,
    PROPOSAL_FAILED,
    PROPOSAL_BUDGET_EXHAUSTED,
)
PROPOSER_ACTION_CREATE = "create"
PROPOSER_ACTION_PATCH = "patch"
PROPOSER_ACTION_NO_ACTION = "no_action"
VALID_PROPOSER_ACTIONS = (PROPOSER_ACTION_CREATE, PROPOSER_ACTION_PATCH,
                          PROPOSER_ACTION_NO_ACTION)

metadata = MetaData()
EvolutionBase = declarative_base(metadata=metadata)

class EvolutionExperiment(EvolutionBase):
    """最小实验状态（阶段 5；阶段 6 才做多轮调度，本表不承载调度器）。"""

    __tablename__ = "evolution_experiments"

    experiment_id = Column(String(64), primary_key=True)
    workspace_id = Column(String(64), nullable=False, index=True)
    domain = Column(String(64), nullable=False)
    dataset_version = Column(String(64), nullable=False)
    grader_version = Column(String(64), nullable=False)
    runner_config_json = Column(Text, nullable=False)     # 模型/runner/执行配置
    pipeline_key = Column(String(64), nullable=False)
    pipeline_version = Column(String(64), nullable=False)
    runtime_ref = Column(String(64), nullable=False)
    initial_skill_set_json = Column(Text, nullable=False)
    current_skill_set_json = Column(Text, nullable=False)
    best_skill_set_json = Column(Text, nullable=True)
    best_score_passed = Column(Integer, nullable=True)
    best_score_total = Column(Integer, nullable=True)
    best_evaluation_id = Column(String(64), nullable=True)
    baseline_evaluation_id = Column(String(64), nullable=True)
    status_rev = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default=EXPERIMENT_STATUS_ACTIVE)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'closed')",
                        name="ck_evolution_experiment_status"),
        CheckConstraint("status_rev >= 1", name="ck_evolution_experiment_rev"),
        CheckConstraint("domain IN ('wiki_compile.default')",
                        name="ck_evolution_experiment_domain"),
    )


class EvolutionEvaluation(EvolutionBase):
    """一次评估（baseline / candidate / best 复测），单事务写入（不跨长事务执行）。"""

    __tablename__ = "evolution_evaluations"

    evaluation_id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(64), nullable=False)
    experiment_id = Column(String(64), nullable=False, index=True)
    kind = Column(String(16), nullable=False)          # baseline / candidate
    proposal_id = Column(String(64), nullable=True)
    base_set_json = Column(Text, nullable=False)
    candidate_set_json = Column(Text, nullable=False)
    task_ids_json = Column(Text, nullable=False)
    per_task_results_json = Column(Text, nullable=False)
    config_json = Column(Text, nullable=False)
    main_passed = Column(Integer, nullable=False)
    main_total = Column(Integer, nullable=False)
    valid = Column(Boolean, nullable=False, default=False)
    invalid_reason = Column(Text, nullable=True)
    usage_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_evolution_eval_idem"),
        CheckConstraint("kind IN ('baseline', 'candidate')",
                        name="ck_evolution_evaluation_kind"),
        CheckConstraint("main_total >= 0 AND main_passed >= 0 "
                        "AND main_passed <= main_total",
                        name="ck_evolution_evaluation_score"),
    )


class EvolutionGateEvent(EvolutionBase):
    """门控事件（接受/拒绝/invalid），与实验指针更新同事务原子保存。"""

    __tablename__ = "evolution_gate_events"

    event_id = Column(String(64), primary_key=True)
    experiment_id = Column(String(64), nullable=False, index=True)
    candidate_evaluation_id = Column(String(64), nullable=False, index=True)
    baseline_evaluation_id = Column(String(64), nullable=True)
    best_evaluation_id = Column(String(64), nullable=True)
    decision = Column(String(16), nullable=False)       # accepted/rejected/invalid
    reason = Column(Text, nullable=False)
    candidate_version_ids_json = Column(Text, nullable=False)
    candidate_score = Column(Text, nullable=True)       # {"passed":n,"total":m}
    best_score = Column(Text, nullable=True)
    previous_set_json = Column(Text, nullable=False)
    next_set_json = Column(Text, nullable=True)
    status_rev_before = Column(Integer, nullable=False)
    status_rev_after = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("experiment_id", "candidate_evaluation_id",
                         name="ux_evolution_gate_once"),
        CheckConstraint("decision IN ('accepted', 'rejected', 'invalid')",
                        name="ck_evolution_gate_decision"),
    )




class EvolutionSkillVersion(EvolutionBase):
    """不可变技能指令版本（SKILL.md + PURPOSE.md + 内容哈希）。"""

    __tablename__ = "evolution_skill_versions"

    version_id = Column(String(64), primary_key=True)
    skill_id = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    schema_version = Column(String(32), nullable=False, default=EVOLUTION_SCHEMA_VERSION)
    domain = Column(String(64), nullable=False)
    runtime_ref = Column(String(64), nullable=False)
    parent_version_id = Column(String(64), nullable=True)
    skill_md = Column(Text, nullable=False)
    purpose_md = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    source_type = Column(String(24), nullable=False)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("skill_id", "seq", name="ux_evolution_skill_seq"),
        CheckConstraint("seq >= 1", name="ck_evolution_skill_seq_positive"),
        CheckConstraint(
            "source_type IN ('builtin_seed', 'manual_seed')",
            name="ck_evolution_skill_source_type",
        ),
        CheckConstraint(
            "schema_version IN ('skill-evolution/v1')",
            name="ck_evolution_skill_schema_version",
        ),
        CheckConstraint(
            "length(trim(skill_md)) > 0 AND length(trim(purpose_md)) > 0",
            name="ck_evolution_skill_content_nonempty",
        ),
    )


class EvolutionSkillBinding(EvolutionBase):
    """Workspace + domain 的生效指令绑定（实验/业务分开；可切换、可回退）。"""

    __tablename__ = "evolution_skill_bindings"

    id = Column(String(36), primary_key=True)
    kind = Column(String(16), nullable=False)
    workspace_id = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    set_kind = Column(String(16), nullable=False, default=SET_KIND_SKILL)
    skill_id = Column(String(64), nullable=True)
    version_id = Column(String(64), nullable=True)
    # B2（P52）：完整集合语义列。members_json=NULL 表示旧单技能绑定（兼容读：
    # 以 skill_id/version_id 为准）；否则为规范化成员列表 JSON（固定顺序）。
    # rev 为绑定修订号（CAS 用 rev+set_hash，天然防 ABA）；set_hash 为集合哈希。
    rev = Column(Integer, nullable=False, default=1, server_default="1")
    set_hash = Column(String(64), nullable=True)
    members_json = Column(Text, nullable=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("kind", "workspace_id", "domain",
                         name="ux_evolution_binding_scope"),
        CheckConstraint(
            "kind IN ('experiment', 'business')",
            name="ck_evolution_binding_kind",
        ),
        CheckConstraint(
            "set_kind IN ('skill', 'empty')",
            name="ck_evolution_binding_set_kind",
        ),
        CheckConstraint(
            "(set_kind = 'skill' AND skill_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(set_kind = 'empty' AND skill_id IS NULL AND version_id IS NULL)",
            name="ck_evolution_binding_pair",
        ),
    )


# 业务晋升/回退审计事件（阶段 8D）：记录业务绑定“实际生效版本”的每一次切换。
BUSINESS_ACTION_PROMOTE = "promote"
BUSINESS_ACTION_ROLLBACK = "rollback"
VALID_BUSINESS_ACTIONS = (BUSINESS_ACTION_PROMOTE, BUSINESS_ACTION_ROLLBACK)


class EvolutionBusinessEvent(EvolutionBase):
    """业务绑定审计（append-only，幂等键去重；不参与运行时查询热路径）。"""

    __tablename__ = "evolution_business_events"

    event_id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)     # promote / rollback
    workspace_id = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    from_version_id = Column(String(64), nullable=True)
    to_version_id = Column(String(64), nullable=True)
    # P53（B2 写路径）：完整 from/to 集合 + 各自业务集合哈希与修订（可回退审计
    # 材料）。单成员集合镜像 from_version_id/to_version_id 兼容旧链；多成员集合
    # pair 列留 NULL、完整成员在 *_members_json —— 绝不只留首版本。
    from_members_json = Column(Text, nullable=True)
    to_members_json = Column(Text, nullable=True)
    from_set_hash = Column(String(64), nullable=True)
    to_set_hash = Column(String(64), nullable=True)
    from_rev = Column(Integer, nullable=True)
    to_rev = Column(Integer, nullable=True)
    experiment_id = Column(String(64), nullable=True)
    run_id = Column(String(64), nullable=True)
    evidence_json = Column(Text, nullable=True)
    reason = Column(Text, nullable=False)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_evolution_business_event_idem"),
        CheckConstraint("action IN ('promote', 'rollback')",
                        name="ck_evolution_business_action"),
    )


class EvolutionPattern(EvolutionBase):
    """经验 Pattern 头（作用域 + 当前修订指针；内容不可变地放在修订行）。"""

    __tablename__ = "evolution_patterns"

    pattern_id = Column(String(64), primary_key=True)
    workspace_id = Column(String(64), nullable=False, index=True)
    domain = Column(String(64), nullable=False)
    title = Column(String(255), nullable=False)
    status = Column(String(24), nullable=False, default=PATTERN_STATUS_OBSERVED)
    current_revision_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("workspace_id", "domain", "title",
                         name="ux_evolution_pattern_title"),
        CheckConstraint(
            "status IN ('observed', 'supported', 'contradicted')",
            name="ck_evolution_pattern_status",
        ),
    )


class EvolutionPatternRevision(EvolutionBase):
    """经验修订（不可变）：内容快照 + 证据 + 来源维护运行。"""

    __tablename__ = "evolution_pattern_revisions"

    revision_id = Column(String(64), primary_key=True)
    pattern_id = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    parent_revision_id = Column(String(64), nullable=True)
    run_id = Column(String(64), nullable=True)          # 来源维护运行（可为空：导入）
    payload_json = Column(Text, nullable=False)          # 现象/原因假设/建议/适用/证据
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("pattern_id", "seq", name="ux_evolution_pattern_seq"),
        CheckConstraint("seq >= 1", name="ck_evolution_pattern_seq_positive"),
        CheckConstraint(
            "length(trim(payload_json)) > 0", name="ck_evolution_pattern_payload_nonempty"),
    )


class EvolutionMaintenanceRun(EvolutionBase):
    """一次维护运行（采样输入/配置/模型调用摘要/应用状态/错误诊断）。"""

    __tablename__ = "evolution_maintenance_runs"

    run_id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default=RUN_STATUS_CREATED)
    config_json = Column(Text, nullable=False)
    input_execution_ids_json = Column(Text, nullable=False)
    model_calls_json = Column(Text, nullable=True)       # 消息/角色/用量（usage=null）
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    applied_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_evolution_run_idem"),
        CheckConstraint(
            "status IN ('created', 'applied', 'failed')",
            name="ck_evolution_run_status",
        ),
    )


class EvolutionLog(EvolutionBase):
    """本轮变化日志（追加写；与修订同事务应用）。"""

    __tablename__ = "evolution_logs"

    log_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=False, index=True)
    domain = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False)
    entry = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="ux_evolution_log_seq"),
    )


class EvolutionIndex(EvolutionBase):
    """经验索引快照（当前生效；与已应用模式修订一致，同一事务更新）。"""

    __tablename__ = "evolution_indexes"

    scope_key = Column(String(160), primary_key=True)    # {workspace_id}|{domain}
    index_json = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    run_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EvolutionProposalRun(EvolutionBase):
    """一次提议运行（多轮工具/模型调用记录、固定基础、状态）。"""

    __tablename__ = "evolution_proposal_runs"

    run_id = Column(String(64), primary_key=True)
    kind = Column(String(16), nullable=False, default="proposer")
    idempotency_key = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    dataset_version = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default=PROPOSER_RUN_GENERATING)
    scope_snapshot_json = Column(Text, nullable=True)   # 固定基础经验/技能快照
    authorized_execution_ids_json = Column(Text, nullable=False)
    proposer_config_json = Column(Text, nullable=False)
    model_calls_json = Column(Text, nullable=True)      # 模型/工具调用记录（usage=null）
    read_execution_ids_json = Column(Text, nullable=False, default="[]")
    tool_events_json = Column(Text, nullable=True)      # 工具读取审计（长度/截断）
    proposal_id = Column(String(64), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="ux_evolution_proposal_run_idem"),
        CheckConstraint(
            "status IN ('generating', 'candidate_saved', 'no_action', "
            "'prerequisites_insufficient', 'output_invalid', 'failed', "
            "'budget_exhausted')",
            name="ck_evolution_proposal_run_status",
        ),
    )


class EvolutionProposal(EvolutionBase):
    """最终提案（create/patch/no_action）；候选版本不可变并关联。"""

    __tablename__ = "evolution_proposals"

    proposal_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    dataset_version = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)
    skill_id = Column(String(64), nullable=False)
    parent_version_id = Column(String(64), nullable=True)
    parent_content_hash = Column(String(64), nullable=True)
    patch_json = Column(Text, nullable=True)
    reason = Column(Text, nullable=False)
    pattern_ids_json = Column(Text, nullable=False, default="[]")
    pattern_revision_ids_json = Column(Text, nullable=False, default="[]")
    evidence_execution_ids_json = Column(Text, nullable=False, default="[]")
    candidate_version_id = Column(String(64), nullable=True)
    candidate_content_hash = Column(String(64), nullable=True)
    duplicate_of_version_id = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'patch', 'no_action')",
            name="ck_evolution_proposal_action",
        ),
        CheckConstraint(
            "status IN ('candidate_saved', 'no_action', "
            "'prerequisites_insufficient', 'output_invalid', 'failed', "
            "'budget_exhausted')",
            name="ck_evolution_proposal_status",
        ),
        CheckConstraint(
            "reason IS NULL OR length(trim(reason)) > 0",
            name="ck_evolution_proposal_reason_nonempty",
        ),
    )


# ---------------------------------------------------------------------------
# Schema 管理（独立 metadata；业务库须 Alembic 显式迁移）
# ---------------------------------------------------------------------------

# 各表必要列（校验用；与 ORM 定义一致）。

class EvolutionRun(EvolutionBase):
    """多轮进化运行（阶段 6 调度实体；逐轮状态/预算/租约/检查点）。"""

    __tablename__ = "evolution_runs"

    run_id = Column(String(64), primary_key=True)
    experiment_id = Column(String(64), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=False, index=True)
    domain = Column(String(64), nullable=False)
    dataset_version = Column(String(64), nullable=False)
    init_mode = Column(String(16), nullable=False)   # paper / business
    config_json = Column(Text, nullable=False)       # 配置与预算快照（含 train/val 清单）
    initial_skill_set_json = Column(Text, nullable=False)
    initial_experience_snapshot_json = Column(Text, nullable=True)
    max_iterations = Column(Integer, nullable=False)
    current_iteration = Column(Integer, nullable=False, default=0)
    status = Column(String(24), nullable=False, default=RUN_QUEUED)
    stop_reason = Column(String(32), nullable=True)
    used_model_calls = Column(Integer, nullable=False, default=0)
    used_tool_calls = Column(Integer, nullable=False, default=0)
    used_estimated_chars = Column(Integer, nullable=False, default=0)
    # 在途/已预留调用标记（发送前落库；崩溃后保守视为占用，不丢预算）。
    reserved_in_flight_json = Column(Text, nullable=False,
                                     default=lambda: "[]")
    # P54：整次 run 累计 active 执行秒数 + 当前 active 段起点（暂停不计入）。
    used_active_seconds = Column(Float, nullable=False, default=0.0,
                                 server_default="0")
    active_segment_started_at = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    pause_requested = Column(Boolean, nullable=False, default=False)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','paused','completed','failed',"
            "'cancelled','budget_exhausted')",
            name="ck_evolution_run_status"),
        CheckConstraint("init_mode IN ('paper','business')",
                        name="ck_evolution_run_init_mode"),
        CheckConstraint("max_iterations >= 1", name="ck_evolution_run_max_iter"),
    )


class EvolutionIteration(EvolutionBase):
    """单轮迭代（冻结集合/步骤指针/子模块关联/检查点）。"""

    __tablename__ = "evolution_iterations"

    iteration_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default=ITER_RUNNING)
    step = Column(String(16), nullable=False, default=ITER_STEP_TRAIN)
    freeze_set_json = Column(Text, nullable=False)
    experiment_status_rev = Column(Integer, nullable=False)
    train_execution_ids_json = Column(Text, nullable=False, default="[]")
    maintenance_run_id = Column(String(64), nullable=True)
    proposal_run_id = Column(String(64), nullable=True)
    proposal_id = Column(String(64), nullable=True)
    evaluation_id = Column(String(64), nullable=True)
    gate_event_id = Column(String(64), nullable=True)
    no_action = Column(Boolean, nullable=False, default=False)
    attempts = Column(Integer, nullable=False, default=1)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("run_id", "number", name="ux_evolution_iter_number"),
        CheckConstraint(
            "status IN ('running','paused','done','failed')",
            name="ck_evolution_iter_status"),
        CheckConstraint(
            "step IN ('train','maintain','propose','eval','gate','done')",
            name="ck_evolution_iter_step"),
        CheckConstraint("number >= 1", name="ck_evolution_iter_number_pos"),
    )


_REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "evolution_runs": {
        "run_id": "VARCHAR", "experiment_id": "VARCHAR",
        "workspace_id": "VARCHAR", "domain": "VARCHAR",
        "dataset_version": "VARCHAR", "init_mode": "VARCHAR",
        "config_json": "TEXT", "initial_skill_set_json": "TEXT",
        "initial_experience_snapshot_json": "TEXT",
        "max_iterations": "INTEGER", "current_iteration": "INTEGER",
        "status": "VARCHAR", "stop_reason": "VARCHAR",
        "used_model_calls": "INTEGER", "used_tool_calls": "INTEGER",
        "used_estimated_chars": "INTEGER",
        "reserved_in_flight_json": "TEXT",
        "used_active_seconds": "FLOAT",
        "active_segment_started_at": "DATETIME",
        "lease_owner": "VARCHAR", "lease_token": "VARCHAR",
        "lease_expires_at": "DATETIME",
        "pause_requested": "BOOLEAN", "cancel_requested": "BOOLEAN",
        "error_code": "VARCHAR", "error_message": "TEXT",
        "created_at": "DATETIME", "started_at": "DATETIME",
        "finished_at": "DATETIME", "updated_at": "DATETIME",
    },
    "evolution_iterations": {
        "iteration_id": "VARCHAR", "run_id": "VARCHAR", "number": "INTEGER",
        "status": "VARCHAR", "step": "VARCHAR",
        "freeze_set_json": "TEXT", "experiment_status_rev": "INTEGER",
        "train_execution_ids_json": "TEXT", "maintenance_run_id": "VARCHAR",
        "proposal_run_id": "VARCHAR", "proposal_id": "VARCHAR",
        "evaluation_id": "VARCHAR", "gate_event_id": "VARCHAR",
        "no_action": "BOOLEAN", "attempts": "INTEGER",
        "error_code": "VARCHAR", "error_message": "TEXT",
        "created_at": "DATETIME", "updated_at": "DATETIME",
    },
    "evolution_experiments": {
        "experiment_id": "VARCHAR", "workspace_id": "VARCHAR", "domain": "VARCHAR",
        "dataset_version": "VARCHAR", "grader_version": "VARCHAR",
        "runner_config_json": "TEXT", "pipeline_key": "VARCHAR",
        "pipeline_version": "VARCHAR", "runtime_ref": "VARCHAR",
        "initial_skill_set_json": "TEXT", "current_skill_set_json": "TEXT",
        "best_skill_set_json": "TEXT", "best_score_passed": "INTEGER",
        "best_score_total": "INTEGER", "best_evaluation_id": "VARCHAR",
        "baseline_evaluation_id": "VARCHAR", "status_rev": "INTEGER",
        "status": "VARCHAR", "created_at": "DATETIME", "updated_at": "DATETIME",
    },
    "evolution_evaluations": {
        "evaluation_id": "VARCHAR", "idempotency_key": "VARCHAR",
        "experiment_id": "VARCHAR", "kind": "VARCHAR", "proposal_id": "VARCHAR",
        "base_set_json": "TEXT", "candidate_set_json": "TEXT",
        "task_ids_json": "TEXT", "per_task_results_json": "TEXT",
        "config_json": "TEXT", "main_passed": "INTEGER", "main_total": "INTEGER",
        "valid": "BOOLEAN", "invalid_reason": "TEXT", "usage_json": "TEXT",
        "created_at": "DATETIME",
    },
    "evolution_gate_events": {
        "event_id": "VARCHAR", "experiment_id": "VARCHAR",
        "candidate_evaluation_id": "VARCHAR", "baseline_evaluation_id": "VARCHAR",
        "best_evaluation_id": "VARCHAR", "decision": "VARCHAR",
        "reason": "TEXT", "candidate_version_ids_json": "TEXT",
        "candidate_score": "TEXT", "best_score": "TEXT",
        "previous_set_json": "TEXT", "next_set_json": "TEXT",
        "status_rev_before": "INTEGER", "status_rev_after": "INTEGER",
        "created_at": "DATETIME",
    },
    "evolution_proposal_runs": {
        "run_id": "VARCHAR", "kind": "VARCHAR", "idempotency_key": "VARCHAR",
        "workspace_id": "VARCHAR", "domain": "VARCHAR", "dataset_version": "VARCHAR",
        "status": "VARCHAR", "scope_snapshot_json": "TEXT",
        "authorized_execution_ids_json": "TEXT", "proposer_config_json": "TEXT",
        "model_calls_json": "TEXT", "read_execution_ids_json": "TEXT",
        "tool_events_json": "TEXT", "proposal_id": "VARCHAR",
        "error_code": "VARCHAR", "error_message": "TEXT",
        "created_at": "DATETIME", "finished_at": "DATETIME",
    },
    "evolution_proposals": {
        "proposal_id": "VARCHAR", "run_id": "VARCHAR", "workspace_id": "VARCHAR",
        "domain": "VARCHAR", "dataset_version": "VARCHAR", "action": "VARCHAR",
        "skill_id": "VARCHAR", "parent_version_id": "VARCHAR",
        "parent_content_hash": "VARCHAR", "patch_json": "TEXT", "reason": "TEXT",
        "pattern_ids_json": "TEXT", "pattern_revision_ids_json": "TEXT",
        "evidence_execution_ids_json": "TEXT", "candidate_version_id": "VARCHAR",
        "candidate_content_hash": "VARCHAR", "duplicate_of_version_id": "VARCHAR",
        "status": "VARCHAR", "created_at": "DATETIME",
    },
    "evolution_skill_versions": {
        "version_id": "VARCHAR", "skill_id": "VARCHAR", "seq": "INTEGER",
        "schema_version": "VARCHAR", "domain": "VARCHAR", "runtime_ref": "VARCHAR",
        "parent_version_id": "VARCHAR", "skill_md": "TEXT", "purpose_md": "TEXT",
        "content_hash": "VARCHAR", "source_type": "VARCHAR",
        "created_by": "VARCHAR", "created_at": "DATETIME",
    },
    "evolution_skill_bindings": {
        "id": "VARCHAR", "kind": "VARCHAR", "workspace_id": "VARCHAR",
        "domain": "VARCHAR", "set_kind": "VARCHAR", "skill_id": "VARCHAR",
        "version_id": "VARCHAR", "rev": "INTEGER", "set_hash": "VARCHAR",
        "members_json": "TEXT", "created_by": "VARCHAR",
        "created_at": "DATETIME", "updated_at": "DATETIME",
    },
    "evolution_business_events": {
        "event_id": "VARCHAR", "idempotency_key": "VARCHAR",
        "action": "VARCHAR", "workspace_id": "VARCHAR", "domain": "VARCHAR",
        "from_version_id": "VARCHAR", "to_version_id": "VARCHAR",
        "from_members_json": "TEXT", "to_members_json": "TEXT",
        "from_set_hash": "VARCHAR", "to_set_hash": "VARCHAR",
        "from_rev": "INTEGER", "to_rev": "INTEGER",
        "experiment_id": "VARCHAR", "run_id": "VARCHAR",
        "evidence_json": "TEXT", "reason": "TEXT", "created_by": "VARCHAR",
        "created_at": "DATETIME",
    },
    "evolution_patterns": {
        "pattern_id": "VARCHAR", "workspace_id": "VARCHAR", "domain": "VARCHAR",
        "title": "VARCHAR", "status": "VARCHAR", "current_revision_id": "VARCHAR",
        "created_at": "DATETIME", "updated_at": "DATETIME",
    },
    "evolution_pattern_revisions": {
        "revision_id": "VARCHAR", "pattern_id": "VARCHAR", "seq": "INTEGER",
        "parent_revision_id": "VARCHAR", "run_id": "VARCHAR",
        "payload_json": "TEXT", "payload_hash": "VARCHAR", "created_at": "DATETIME",
    },
    "evolution_maintenance_runs": {
        "run_id": "VARCHAR", "idempotency_key": "VARCHAR", "workspace_id": "VARCHAR",
        "domain": "VARCHAR", "status": "VARCHAR", "config_json": "TEXT",
        "input_execution_ids_json": "TEXT", "model_calls_json": "TEXT",
        "error_code": "VARCHAR", "error_message": "TEXT",
        "created_at": "DATETIME", "applied_at": "DATETIME",
    },
    "evolution_logs": {
        "log_id": "VARCHAR", "run_id": "VARCHAR", "workspace_id": "VARCHAR",
        "domain": "VARCHAR", "seq": "INTEGER", "entry": "TEXT",
        "created_at": "DATETIME",
    },
    "evolution_indexes": {
        "scope_key": "VARCHAR", "index_json": "TEXT", "content_hash": "VARCHAR",
        "run_id": "VARCHAR", "created_at": "DATETIME", "updated_at": "DATETIME",
    },
}

EVOLUTION_TABLES = tuple(sorted(_REQUIRED_COLUMNS))


def create_evolution_schema(engine) -> None:
    """在给定库上创建演化 schema（幂等；仅演化表，不碰业务表）。"""
    metadata.create_all(engine)


def missing_evolution_tables(engine) -> list[str]:
    """返回缺失的演化表（不修改库）。"""
    from sqlalchemy import inspect
    inspector = inspect(engine)
    missing = []
    for table in EVOLUTION_TABLES:
        if not inspector.has_table(table):
            missing.append(table)
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        expected = set(_REQUIRED_COLUMNS[table])
        for col in sorted(expected - cols):
            missing.append(f"{table}.{col}")
    return missing


def require_evolution_schema(engine, *, context: str = "") -> None:
    """WikiSkill 启用时的硬校验：缺演化表/列 → RuntimeError（不静默退化）。"""
    missing = missing_evolution_tables(engine)
    if missing:
        raise RuntimeError(
            f"evolution_schema_not_ready [{context}]: 缺少 "
            + ", ".join(missing)
            + "；启用 WikiSkill 需先对数据库执行 Alembic 迁移（P45 起）或 "
            "使用实验库 create_evolution_schema。")
