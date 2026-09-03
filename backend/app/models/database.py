from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey, Boolean, Integer, Float, Index, CheckConstraint, UniqueConstraint, inspect, text as sqlalchemy_text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
import os
import hashlib

import logging

logger = logging.getLogger(__name__)

Base = declarative_base()


# ---------------------------------------------------------------------------
# V4 Phase C：迁移托管字段白名单。
#
# 这些字段是 Phase C 新增的，真实库必须通过「备份 → Alembic P31」显式升级，
# 不允许 init_db()._migrate_schema 在未确认时静默 ALTER TABLE 补列。
# 测试库由 Base.metadata.create_all() 创建完整结构（字段齐全），不受影响。
# 真实库若未执行 P31 而缺这些字段，init_db 会抛 SchemaNotReadyError，
# 明确提示 schema_not_ready，而不是静默迁移或返回难懂的缺列 SQL 错误。
# ---------------------------------------------------------------------------
MANAGED_MIGRATION_COLUMNS: set[tuple[str, str]] = {
    ("wiki_pages", "source_page_ids"),
    ("wiki_pages", "dirty"),
    ("wiki_pages", "category"),
    ("wiki_revisions", "edit_type"),
    ("wiki_revisions", "updated_by"),
    ("pages", "wiki_dirty"),
    ("pages", "wiki_compiled_content_hash"),
    ("pages", "wiki_last_error"),
    # V4 Phase F：知识债务改造字段（P32 迁移托管，不得由 init_db 静默补列）。
    ("knowledge_debts", "original_query"),
    ("knowledge_debts", "normalized_query"),
    ("knowledge_debts", "cluster_key"),
    ("knowledge_debts", "affected_user_count"),
    ("knowledge_debts", "scope_id"),
    ("knowledge_debts", "retrieval_reason"),
    # V4 Phase J-4：反馈/访问申请分类字段（P37 迁移托管，不得由 init_db 静默补列）。
    ("knowledge_debts", "version_label"),
    ("knowledge_debts", "version_status"),
    ("knowledge_debts", "debt_reason"),
    # V4 Phase I：版本感知 Wiki 迁移托管字段（由 P34 显式升级，不得 init_db 静默补列）。
    ("wiki_pages", "latest_version"),
    ("wiki_sections", "version_label"),
    ("wiki_sections", "version_sort_key"),
    ("wiki_sections", "is_common"),
    ("wiki_sections", "content_origin"),
    ("wiki_sections", "merge_policy"),
    ("wiki_sections", "version_confidence"),
    ("wiki_sections", "version_status"),
    ("wiki_sections", "diff_notice"),
    # Phase 3：独立 WikiWorkspace（P41 迁移托管，不得由 init_db 静默补列）。
    ("wiki_pages", "workspace_id"),
    # Phase 6：Content Skill 持久化字段（P43 迁移托管，不得由 init_db 静默补列）。
    ("wiki_pages", "content_skill"),
    ("wiki_pages", "skill_version"),
    ("wiki_pages", "skill_selected_by"),
    ("wiki_pages", "skill_confidence"),
    ("wiki_pages", "skill_locked"),
    ("wiki_pages", "skill_decision_json"),
    ("wiki_sections", "section_key"),
    ("wiki_sections", "skill_key"),
    ("wiki_sections", "skill_version"),
    ("wiki_sections", "content_hash"),
    ("wiki_sections", "validation_status"),
    ("wiki_sections", "structure_json"),
}

# ---------------------------------------------------------------------------
# V4 Phase J-3：迁移托管表白名单。
#
# 这 5 张图谱表是 P36 新增的，真实库必须通过「备份 → Alembic P36」显式升级，
# 不允许 init_db() 在已有 P35 核心表的数据库上通过 Base.metadata.create_all()
# 静默创建（会绕过 Alembic，重演真实库误升级问题）。
# 全新空库（表尚不存在）不受影响，create_all 整体建表。
# ---------------------------------------------------------------------------
MANAGED_MIGRATION_TABLES: set[str] = {
    "v4_graph_entities",
    "v4_graph_relations",
    "v4_graph_relation_evidence",
    "v4_graph_entity_evidence",
    "v4_graph_communities",
}

# ---------------------------------------------------------------------------
# V4 Phase J-4：反馈/访问申请表（P37 新增）。真实库停在 P35/P36 时必须 fail closed，
# 不允许 init_db 的 create_all 在已有核心表的库上静默新建这些表（绕过 Alembic）。
# ---------------------------------------------------------------------------
MANAGED_J4_TABLES: set[str] = {
    "answer_snapshots",
    "answer_feedback",
    "feedback_clusters",
    "feedback_cluster_users",
    "answer_needed",
    "access_requests",
    "access_request_users",
}

# ---------------------------------------------------------------------------
# V4 Phase J-5：统一数据源路径映射表（P38 新增）。真实库停在 P37 时必须 fail
# closed，不允许 init_db 的 create_all 在已有核心表的库上静默新建（绕过 Alembic）。
# ---------------------------------------------------------------------------
MANAGED_P38_TABLES: set[str] = {
    "source_path_mappings",
}

# ---------------------------------------------------------------------------
# Phase 3：独立 WikiWorkspace 与确定性 Notebook 路由（P41 新增）。真实库停在 P40
# 时必须 fail closed，不允许 init_db 的 create_all 在已有核心表的库上静默新建
# （绕过 Alembic）。
# ---------------------------------------------------------------------------
MANAGED_WORKSPACE_TABLES: set[str] = {
    "wiki_workspaces",
    "notebook_workspace_bindings",
}

# ---------------------------------------------------------------------------
# Phase 4：KnowledgeCompileRun 持久任务（P42 新增）。真实库停在 P41 时必须
# fail closed，不允许 init_db 的 create_all 在已有核心表的库上静默新建
# （绕过 Alembic）。
# ---------------------------------------------------------------------------
MANAGED_COMPILE_TABLES: set[str] = {
    "knowledge_compile_runs",
    "knowledge_compile_stage_runs",
    "knowledge_compile_artifacts",
}

# ---------------------------------------------------------------------------
# Phase 7C（P44）：WikiSection Evidence Binding 表（api_reference 持久化）。
# 真实库停在 P43 时必须 fail closed，不允许 init_db 的 create_all 在已有核心
# 表的库上静默新建（绕过 Alembic）。
# ---------------------------------------------------------------------------
MANAGED_P44_TABLES: set[str] = {
    "wiki_section_evidence_bindings",
}


class SchemaNotReadyError(RuntimeError):
    """数据库 schema 落后于代码要求的迁移版本，需要显式执行 Alembic 升级。"""


class Notebook(Base):
    __tablename__ = 'notebooks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    group_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class NotebookGroup(Base):
    """Notebook 可访问业务组关联表（J-1 多组授权）。

    Notebook.group_id 保留为「主权限组」（兼容旧语义与快速路径）；本表保存
    Notebook 额外授权的业务组。权限判定时把 group_id + notebook_groups 合并为
    完整可访问组集合（任一组成员的用户即可见，禁止跨组复制文件）。
    """
    __tablename__ = 'notebook_groups'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey('notebooks.id', ondelete='CASCADE'), nullable=False, index=True)
    group_name = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ux_notebook_group', 'notebook_id', 'group_name', unique=True),
    )


class SourcePathMapping(Base):
    """统一数据源路径 → 目标 Notebook 映射（P38，各数据源共用）。

    - connection_id 隔离不同数据源连接；
    - path_namespace 表示连接内部的空间/项目/仓库等命名域（钉钉=space_id）；
    - folder_path 存规范化后的分段路径（不含文件名）；
    - 唯一约束 (connection_id, path_namespace, folder_path)；
    - 空 namespace/空 folder_path 使用空串，不用 NULL 参与唯一约束；
    - 权限来自目标 Notebook，本表不重复保存权限组。

    匹配规则（见 app/core/path_mapping.py）：
    - 只查询当前 connection_id 的映射；
    - path_namespace 精确匹配优先，允许空 namespace 作为连接内通配；
    - 更具体路径优先；完整路径段匹配（A/B 不误匹配 A/BC）。
    """
    __tablename__ = 'source_path_mappings'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = Column(String(36), ForeignKey('source_connections.id', ondelete='CASCADE'), nullable=False, index=True)
    # 空串表示该连接内通配命名域（不能用 NULL：SQLite 唯一约束对 NULL 不生效）。
    path_namespace = Column(String(255), nullable=False, default="", server_default="", index=True)
    folder_path = Column(String(1024), nullable=False)          # 规范化文件夹路径（不含文件名）
    notebook_id = Column(String(36), ForeignKey('notebooks.id', ondelete='CASCADE'), nullable=False, index=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ux_source_path_mapping', 'connection_id', 'path_namespace', 'folder_path', unique=True),
    )


class Page(Base):
    __tablename__ = 'pages'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey('notebooks.id', ondelete='SET NULL'), nullable=True, index=True)
    title = Column(String(255), nullable=False, default='无标题')
    content = Column(Text, default='')
    keywords = Column(Text, default='')
    source_type = Column(String(50), nullable=True)
    source_id = Column(String(255), nullable=True)
    source_path = Column(Text, nullable=True)
    source_space_id = Column(String(255), nullable=True)
    source_url = Column(Text, nullable=True)
    source_file_hash = Column(String(64), nullable=True)
    source_file_size = Column(Integer, nullable=True)
    source_mime_type = Column(String(127), nullable=True)
    source_content = Column(Text, nullable=True)
    source_content_hash = Column(String(64), nullable=True)
    source_markdown_hash = Column(String(64), nullable=True)
    source_pipeline_version = Column(String(127), nullable=True)
    content_hash = Column(String(64), nullable=True)
    indexed_content_hash = Column(String(64), nullable=True)
    index_dirty = Column(Boolean, nullable=True, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    # V4 Phase C 第二次补漏：Page 级 Wiki 待处理状态（与 index_dirty 独立）
    wiki_dirty = Column(Boolean, nullable=True, default=True, index=True)  # 待构建/刷新 Wiki
    wiki_compiled_content_hash = Column(String(64), nullable=True)        # 上次成功构建的输入内容哈希
    wiki_last_error = Column(Text, nullable=True)                         # 上次失败的可用化错误
    # ---- Phase 2：CanonicalNote 契约字段（nullable；历史 Page 缺失视为 legacy-note/v0） ----
    note_schema_version = Column(String(64), nullable=True)               # canonical-note/v1 / legacy-note/v0
    content_format = Column(String(64), nullable=True)                    # markdown
    content_kind = Column(String(64), nullable=True)                      # markdown/text/code/csv/office/pdf
    converter_key = Column(String(127), nullable=True)
    converter_version = Column(String(127), nullable=True)
    conversion_status = Column(String(32), nullable=True)                 # converted/partial/failed/blocked
    conversion_warnings_json = Column(Text, nullable=True)                # JSON 数组（note.warnings）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ux_pages_source_type_source_id', 'source_type', 'source_id', unique=True),
    )

    @property
    def current_content_hash(self) -> str:
        """返回当前正文的真实哈希，不依赖历史字段是否及时更新。"""
        return hashlib.sha256((self.content or "").encode("utf-8")).hexdigest()

    @property
    def index_status(self) -> str:
        """返回页面正文与向量索引的一致性状态。"""
        if not (self.content or "").strip():
            return "empty"
        if not self.indexed_content_hash:
            return "missing"
        if self.index_dirty or self.indexed_content_hash != self.current_content_hash:
            return "stale"
        return "current"


class PageChunk(Base):
    __tablename__ = 'page_chunks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
    content_type = Column(String(32), nullable=True, default="text")
    page_number = Column(Integer, nullable=True)
    image_id = Column(String(255), nullable=True)
    embedding = Column(Text, nullable=True)

    __table_args__ = (
        Index('ix_page_chunks_page_idx', 'page_id', 'chunk_index'),
    )


class User(Base):
    __tablename__ = 'users'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), default="")
    display_name = Column(String(255), default="")
    is_local = Column(Boolean, default=False)
    password_hash = Column(String(255), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UserGroup(Base):
    __tablename__ = 'user_groups'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    group_name = Column(String(255), nullable=False, index=True)


class VisionAnalysisJob(Base):
    """图片理解持久任务。进程重启后仍可追踪并由调度器继续处理。"""
    __tablename__ = 'vision_analysis_jobs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    status = Column(String(32), default='queued', nullable=False, index=True)
    trigger = Column(String(32), nullable=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    attempt = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    error_category = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_vision_jobs_status_created', 'status', 'created_at'),
    )


class WikiWorkspace(Base):
    """独立 Wiki 工作区（Phase 3）。

    - key：key 是独立业务身份——自动路由产生的 key 按 notebook_id 确定性派生
      （workspace_key_for_notebook(notebook.id)，该 notebook 的默认私用空间）；
      手工创建/合并场景使用调用方提供的独立业务 key 或系统生成
      （generate_manual_workspace_key）。scope_id 不再派生 key。
    - acl_scope：规范化 JSON（复用 _scope_to_acl_json 输出语义）；
    - scope_id：规范化权限域 company/admin/group:<sorted names>（供路由与 dry-run）；
    - status：active/archived（DB CHECK ck_workspace_status）。
    """
    __tablename__ = 'wiki_workspaces'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    acl_scope = Column(Text, nullable=False)
    scope_id = Column(String(255), nullable=False, index=True)
    status = Column(String(32), nullable=False, default='active', index=True)  # active/archived
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name='ck_workspace_status'),
        Index('ux_wiki_workspaces_key', 'key', unique=True),
    )


class NotebookWorkspaceBinding(Base):
    """Notebook → WikiWorkspace 绑定（Phase 3 确定性路由）。

    - status：active/disabled（DB CHECK ck_binding_status）；
    - 首版一个 Notebook 只有一个 active binding（应用层保证 + 唯一约束）。
    """
    __tablename__ = 'notebook_workspace_bindings'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey('notebooks.id', ondelete='CASCADE'), nullable=False, index=True)
    workspace_id = Column(String(36), ForeignKey('wiki_workspaces.id', ondelete='CASCADE'), nullable=False, index=True)
    status = Column(String(32), nullable=False, default='active')  # active/disabled
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name='ck_binding_status'),
        Index('ux_nb_ws_binding', 'notebook_id', 'workspace_id', unique=True),
        # Phase 3.1：数据库层保证「一个 Notebook 同时最多一个 active binding」。
        # 部分唯一索引：UNIQUE(notebook_id) WHERE status='active'。
        # SQLite 与 PostgreSQL 均通过 sqlite_where/postgresql_where 支持谓词索引。
        Index(
            'ux_nb_ws_binding_active', 'notebook_id', unique=True,
            sqlite_where=sqlalchemy_text("status = 'active'"),
            postgresql_where=sqlalchemy_text("status = 'active'"),
        ),
    )


class KnowledgeCompileRun(Base):
    """Wiki 编译持久任务（Phase 4，P42）。

    - status：queued/running/succeeded/failed/cancelled/superseded；
      DB queued 行本身就是持久队列（重启不丢，由 worker 原子抢占）。
    - idempotency_key：唯一；重复创建返回已有 run（幂等去重）。
    - input_hash：本轮输入内容哈希（input_hash 缓存 = 同 pipeline 同输入
       不重复执行）。
    - attempt：实际执行次数（新建 queued=0；每次成功 claim 原子 +1；
      max_attempts 为最大实际执行次数；failed/retry/stale requeue 不增加）。
    - lease_token / worker_id / lease_expires_at：原子领取与租约（Phase 4.1）。
    - trigger_type：page_changed/page_deleted/manual_rebuild/skill_migration/
      manual_edit。
    """
    __tablename__ = 'knowledge_compile_runs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_key = Column(String(64), nullable=False, index=True)
    pipeline_version = Column(String(64), nullable=False)
    trigger_type = Column(String(32), nullable=False)  # page_changed/page_deleted/manual_rebuild/skill_migration/manual_edit
    trigger_object_id = Column(String(36), nullable=True)
    source_sync_run_id = Column(String(36), ForeignKey('source_sync_runs.id', ondelete='SET NULL'), nullable=True, index=True)
    workspace_id = Column(String(36), ForeignKey('wiki_workspaces.id', ondelete='SET NULL'), nullable=True, index=True)
    wiki_page_id = Column(String(36), nullable=True, index=True)  # Topic 后回填
    status = Column(String(32), nullable=False, default='queued', index=True)  # queued/running/succeeded/failed/cancelled/superseded
    idempotency_key = Column(String(128), nullable=True)  # 唯一索引见 __table_args__
    input_hash = Column(String(64), nullable=False)
    output_revision_id = Column(String(36), nullable=True)
    current_stage = Column(String(64), nullable=True)
    error_summary = Column(Text, nullable=True)
    # Phase 4.1：原子领取 lease（claim 时写入；heartbeat/recovery 按此归属校验）。
    lease_token = Column(String(36), nullable=True)
    worker_id = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    # Phase 4.1：safe 错误分层（internal detail 只写日志，API 只回 safe 字段）。
    safe_error_code = Column(String(64), nullable=True)
    safe_error_message = Column(Text, nullable=True)
    # Phase 4.1：请求幂等指纹（idempotency_key 冲突判定：同 key 不同 fingerprint → 409）。
    request_fingerprint = Column(String(64), nullable=True)
    attempt = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    cancel_requested = Column(Boolean, nullable=True, default=False)
    created_by = Column(String(36), nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'superseded')",
            name='ck_compile_run_status',
        ),
        CheckConstraint('attempt >= 0', name='ck_compile_run_attempt_nonneg'),
        CheckConstraint('max_attempts >= 1', name='ck_compile_run_max_attempts_min'),
        CheckConstraint('attempt <= max_attempts', name='ck_compile_run_attempt_le_max'),
        Index('ux_knowledge_compile_runs_idempotency_key', 'idempotency_key', unique=True),
        Index('ix_knowledge_compile_runs_request_fingerprint', 'request_fingerprint'),
    )


class KnowledgeCompileStageRun(Base):
    """Wiki 编译阶段执行记录（Phase 4，P42）。

    - status：queued/running/succeeded/failed/skipped/cancelled（DB CHECK
      ck_compile_stage_status）；
    - attempt：同 (run_id, stage_key) 的第几次尝试（1 起，重试递增）。
    - parent_stage_run_id：retry 时指向上一 attempt 的行（保留历史错误，
      新尝试不覆盖旧行）。
    - metrics_json：阶段摘要（白名单序列化后经 API 返回，不落正文/Prompt）。
    """
    __tablename__ = 'knowledge_compile_stage_runs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey('knowledge_compile_runs.id', ondelete='CASCADE'), nullable=False, index=True)
    stage_key = Column(String(64), nullable=False, index=True)
    stage_order = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default='queued', index=True)  # queued/running/succeeded/failed/skipped/cancelled
    attempt = Column(Integer, nullable=False, default=0)
    input_hash = Column(String(64), nullable=True)
    output_hash = Column(String(64), nullable=True)
    component_key = Column(String(64), nullable=True)
    component_version = Column(String(64), nullable=True)
    retryable = Column(Boolean, nullable=True, default=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    # Phase 4.1：stage 级输入哈希（系统确定性计算，缓存键核心）。
    stage_input_hash = Column(String(64), nullable=True)
    # Phase 4.1：safe 错误分层（internal detail 只写日志，API 只回 safe 字段）。
    safe_error_code = Column(String(64), nullable=True)
    safe_error_message = Column(Text, nullable=True)
    metrics_json = Column(Text, nullable=True)
    parent_stage_run_id = Column(String(36), nullable=True)  # retry 时指向上一 attempt 的行
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')",
            name='ck_compile_stage_status',
        ),
    )


class KnowledgeCompileArtifact(Base):
    """Wiki 编译产物摘要（Phase 4，P42）。

    - 只存摘要元数据 + payload_json（内部 JSON）；API 不返回 payload_json 原文。
    - 复用有效性派生自所属 run.status == 'succeeded'（artifact 无独立状态列）。
    - reused_from_artifact_id：Phase 4.2 缓存复用自引用（ondelete=SET NULL）——
      当前 run 从既有 succeeded 产物复用内容时建「当前 Artifact 行」并回指源产物；
      payload 内容 hash/schema/artifact_type 与源一致，保证当前 run 链完整。
    """
    __tablename__ = 'knowledge_compile_artifacts'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey('knowledge_compile_runs.id', ondelete='CASCADE'), nullable=False, index=True)
    stage_run_id = Column(String(36), ForeignKey('knowledge_compile_stage_runs.id', ondelete='CASCADE'), nullable=True, index=True)
    artifact_type = Column(String(32), nullable=False)  # canonical_note/topic_operation/skill_decision/knowledge_ir/blueprint/validation_report/revision
    schema_version = Column(String(32), nullable=True)
    object_type = Column(String(32), nullable=True)
    object_id = Column(String(36), nullable=True)
    content_hash = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
    # Phase 4.2：缓存复用源产物自引用（ondelete=SET NULL，nullable）。
    reused_from_artifact_id = Column(
        String(36),
        ForeignKey('knowledge_compile_artifacts.id', ondelete='SET NULL'),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 索引名显式指定 ix_knowledge_compile_artifacts_reused_from（guard 与
        # P42 迁移一致；避免自动以完整列名命名的另一套索引）。
        Index('ix_knowledge_compile_artifacts_reused_from', 'reused_from_artifact_id'),
    )


class WikiPage(Base):
    """Wiki 主题页（P6，V3 计划 11.2；Phase C：Page 驱动的正式知识层）。"""
    __tablename__ = 'wiki_pages'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    community_id = Column(String(36), nullable=True, index=True)  # 来源 Community（旧，兼容只读）
    community_key = Column(String(64), nullable=True, index=True)  # P20：按稳定 key upsert（旧，兼容只读）
    title = Column(String(255), nullable=False)
    summary = Column(Text, default='')                            # 短摘要
    acl_scope = Column(Text, nullable=True)
    status = Column(String(32), default='draft', index=True)      # draft/published/archived（published=当前有效版本）
    current_revision_id = Column(String(36), nullable=True)
    source_hash = Column(String(64), nullable=True)
    locked = Column(Boolean, nullable=True, default=False)        # 整页人工锁定（P6-BE-07）
    # Phase C：Page 驱动构建的直接来源与待刷新标记（不依赖 Card/Community）
    source_page_ids = Column(Text, nullable=True)                 # JSON 数组：来源 Page ID
    dirty = Column(Boolean, nullable=True, default=False, index=True)  # 有来源更新待刷新
    category = Column(String(128), nullable=True, index=True)     # 分类（产品资料/操作指南/...）
    latest_version = Column(String(64), nullable=True)            # V4 Phase I：确定依据的当前最新版本
    # Phase 3：所属 WikiWorkspace（ondelete=SET NULL：workspace 删除后 wiki 保留但
    # 失去归属，fail closed 由 ACL 层兜底）。自动编译产生的 Wiki 必须非空（写入端强制）。
    workspace_id = Column(String(36), ForeignKey('wiki_workspaces.id', ondelete='SET NULL'), nullable=True, index=True)
    # Phase 6（P43）：Content Skill 持久化字段。历史 Wiki 保持 nullable，不自动回填；
    # 缺字段语义按 legacy/default 兼容读取。只在 publish 成功时原子更新，失败保留原值。
    content_skill = Column(String(64), nullable=True)          # 当前内容结构 Skill key
    skill_version = Column(String(64), nullable=True)          # 当前 Skill 精确版本
    skill_selected_by = Column(String(32), nullable=True)      # auto/manual/migration/default_fallback/locked/sticky
    skill_confidence = Column(Float, nullable=True)            # NULL 或 0<=v<=1
    skill_locked = Column(Boolean, nullable=True, default=False)  # 人工锁定 Skill（自动路由不可覆盖）
    skill_decision_json = Column(Text, nullable=True)          # 安全结构化 SkillDecision 摘要
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # Phase 6：skill 字段 CHECK（与 P43 migration 一致）。
        CheckConstraint(
            "skill_confidence IS NULL OR (skill_confidence >= 0.0 AND skill_confidence <= 1.0)",
            name='ck_wiki_pages_skill_confidence',
        ),
        CheckConstraint(
            "skill_selected_by IS NULL OR skill_selected_by IN "
            "('auto','manual','migration','default_fallback','locked','sticky')",
            name='ck_wiki_pages_skill_selected_by',
        ),
    )


class WikiRevision(Base):
    """Wiki 修订（P6，V3 计划 11.2；Phase C：自动/人工编辑即当前有效版本）。"""
    __tablename__ = 'wiki_revisions'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    wiki_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=False, index=True)
    parent_revision_id = Column(String(36), nullable=True)
    title = Column(String(255), nullable=False)
    summary = Column(Text, default='')
    source_hash = Column(String(64), nullable=True)
    status = Column(String(32), default='draft', index=True)      # draft/published/superseded/rejected
    # Phase C：编辑溯源（auto=自动构建，manual=人工编辑）
    edit_type = Column(String(32), nullable=True, default='auto')
    updated_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class WikiSection(Base):
    """Wiki Section（P6，V3 计划 11.2；Phase I：版本感知版本块）。

    版本字段语义（V4 Phase I 三）：
    - version_label：产品版本标签（"2.0"/"3.0"）或 common（通用）/ unversioned（版本未标明）。
    - version_sort_key：确定性排序键（JSON 字符串），用于版本块排序与 latest 计算。
    - is_common：True 表示通用说明块，不属于任何产品版本。
    - content_origin：auto（自动构建）/ manual（人工编辑）。
    - merge_policy：auto（可刷新）/ protected（人工保护，自动刷新不得覆盖）。
    - version_confidence：float，版本识别置信度（仅记录，不参与权限/事实判定）。
    - version_status：confirmed / unversioned / ambiguous。
    - diff_notice：同一版本内部来源互相矛盾时的安全提示（不泄露具体来源）。
    """
    __tablename__ = 'wiki_sections'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    revision_id = Column(String(36), ForeignKey('wiki_revisions.id', ondelete='CASCADE'), nullable=False, index=True)
    section_type = Column(String(50), nullable=False)  # summary/entities/cards/evidence/gaps/related_topics
    heading = Column(String(255), nullable=True)
    content = Column(Text, default='')
    order_index = Column(Integer, default=0)
    locked = Column(Boolean, nullable=True, default=False)        # Section 级锁定
    # ---- V4 Phase I：版本感知字段 ----
    version_label = Column(String(64), nullable=True)  # 产品版本 / common / unversioned（复合索引见 __table_args__）
    version_sort_key = Column(String(255), nullable=True)          # JSON 序列化的确定性排序键
    is_common = Column(Boolean, nullable=True, default=False)
    content_origin = Column(String(32), nullable=True, default='auto')  # auto/manual
    merge_policy = Column(String(32), nullable=True, default='auto')    # auto/protected
    version_confidence = Column(Float, nullable=True, default=1.0)
    version_status = Column(String(32), nullable=True, default='unversioned')  # confirmed/unversioned/ambiguous
    diff_notice = Column(Text, nullable=True)                       # 同版本差异提示（不泄露来源）
    # ---- Phase 7C（P44）：API Reference Section 结构字段（全部 nullable，
    #      历史 Section 保持 NULL，不做虚假回填）----
    section_key = Column(String(255), nullable=True)   # API Reference section 稳定 key
    skill_key = Column(String(64), nullable=True)      # 生成本 Section 的 Skill key
    skill_version = Column(String(64), nullable=True)  # 生成本 Section 的 Skill 版本
    content_hash = Column(String(64), nullable=True)   # Section 内容 hash
    validation_status = Column(String(32), nullable=True)  # NULL/pass/fail
    structure_json = Column(Text, nullable=True)       # 只保存 JSON-safe 结构，不存 Prompt/ACL/Secret/正文

    __table_args__ = (
        Index('ix_wiki_sections_revision_version', 'revision_id', 'version_label'),
        # validation_status 只能为 NULL/pass/fail。
        CheckConstraint(
            "validation_status IS NULL OR validation_status IN ('pass','fail')",
            name='ck_wiki_sections_validation_status',
        ),
        # nullable section_key 若非空必须 trim 后非空。
        CheckConstraint(
            "section_key IS NULL OR length(trim(section_key)) > 0",
            name='ck_wiki_sections_section_key_nonempty',
        ),
        # (revision_id, section_key) 在 section_key 非空时唯一（部分唯一索引）。
        Index(
            'ux_wiki_sections_revision_section_key',
            'revision_id', 'section_key',
            unique=True,
            sqlite_where=sqlalchemy_text("section_key IS NOT NULL"),
            postgresql_where=sqlalchemy_text("section_key IS NOT NULL"),
        ),
    )


class WikiSectionEvidenceBinding(Base):
    """Wiki Section ↔ Evidence 绑定（Phase 7C，P44）。

    只表达「已发布 Section 的哪个 field 由哪条 Evidence 支撑」，为 7C.2 的
    Evidence Binding 表（持久化读取/展示层）。不重复 WikiRevision→CompileRun
    关系（由 KnowledgeCompileRun.output_revision_id 与 Artifact 链表达）。

    - field_path 非空（IR field_path，如 responses.200）；
    - usage_type：support/conflict（CHECK 约束）；
    - evidence_content_hash：Evidence 快照 hash（发布时确定性记录）；
    - (section_id, field_path, evidence_id, usage_type) 唯一。
    """
    __tablename__ = 'wiki_section_evidence_bindings'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    section_id = Column(String(36), ForeignKey('wiki_sections.id', ondelete='CASCADE'), nullable=False, index=True)
    evidence_id = Column(String(36), ForeignKey('evidence_items.id', ondelete='CASCADE'), nullable=False, index=True)
    field_path = Column(String(255), nullable=False)
    usage_type = Column(String(32), nullable=False, default='support')
    evidence_content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # (section_id, field_path, evidence_id, usage_type) 唯一（命名 unique Index，
        # 与 P36/P41 模式一致，SQLite 反射仍保留索引名）。
        Index(
            'ux_wiki_section_evidence_section_field_evidence_usage',
            'section_id', 'field_path', 'evidence_id', 'usage_type',
            unique=True,
        ),
        CheckConstraint(
            "usage_type IN ('support','conflict')",
            name='ck_wiki_section_evidence_bindings_usage_type',
        ),
        # field_path trim 后非空；evidence_content_hash 必须 64 字符（写入层再验
        # 小写 SHA-256）。
        CheckConstraint(
            "length(trim(field_path)) > 0",
            name='ck_wiki_section_evidence_bindings_field_path_nonempty',
        ),
        CheckConstraint(
            "length(evidence_content_hash) = 64",
            name='ck_wiki_section_evidence_bindings_evidence_hash_len',
        ),
    )


class WikiVersionSource(Base):
    """V4 Phase I：Wiki 版本 → Page 的来源映射（后台保留，前端不展示）。

    用途（V4 Phase I 四）：ACL、文件更新/删除后的增量刷新、判断某版本是否仍有
    有效来源、内部追溯与故障排查、防止跨 scope 合并、自动重建。

    实际映射粒度（如实声明，勿夸大）：version → Page。当前写入只填充 page_id；
    chunk_id / source_item_id 列为预留结构，本次不填充（不做 Chunk 级溯源）。

    - wiki_page_id + version_label + page_id 唯一，幂等（chunk_id 为 NULL）。
    - 不对外返回 page_id/chunk_id/source 结构。
    """
    __tablename__ = 'wiki_version_sources'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    wiki_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=False, index=True)
    version_label = Column(String(64), nullable=False, index=True)
    page_id = Column(String(36), nullable=True, index=True)   # 来源 Page（实际填充；NULL=manual/无来源）
    chunk_id = Column(String(36), nullable=True)               # 预留：来源 Chunk（本次不填充）
    source_item_id = Column(String(36), nullable=True)         # 预留：来源 SourceItem（本次不填充）
    acl_scope = Column(Text, nullable=True)                    # 来源所属 scope（防跨 scope 合并）
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ux_wiki_version_sources', 'wiki_page_id', 'version_label', 'page_id', 'chunk_id', unique=True),
    )


class WikiLink(Base):
    """Wiki 主题关联（P6，V3 计划 11.2）。Related Topics 关联。"""
    __tablename__ = 'wiki_links'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=False, index=True)
    target_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=False, index=True)
    link_type = Column(String(32), default='related')             # related/parent/child


class V4GraphEntity(Base):
    """V4 实体关系图谱：实体节点（J-3）。

    id 是确定性稳定 key（scope 隔离）：`{scope_key}|{entity_type}:{normalized_name}`。
    同一 scope 内同名同类型实体合并为一条；不同 scope 严格隔离（不跨域合并）。
    """
    __tablename__ = 'v4_graph_entities'

    id = Column(String(512), primary_key=True)
    entity_type = Column(String(64), nullable=False, index=True)
    normalized_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    community_key = Column(String(512), nullable=True, index=True)  # 所属 Community 稳定 key
    acl_scope = Column(Text, nullable=True)                        # company / admin / group:<名>
    version_label = Column(String(64), nullable=True)              # 合并后版本（多版本冲突=ambiguous）
    version_status = Column(String(32), nullable=True, default='unversioned')
    fingerprint = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)


class V4GraphRelation(Base):
    """V4 实体关系图谱：带关系名的边（J-3）。

    id 是确定性稳定 key：`{source_id}|{relation_type}|{target_id}|{version_label}`。
    同一 (source, type, target, version) 去重（evidence_count 由 provenance 重算）；
    不同版本、不同关系类型、不同目标分别保留（冲突版本不合并）。
    version_family 是产品/主题版本族稳定 key（用于 per-family latest，不能跨产品
    取全局最大版本）。无法确定唯一版本族时为空，latest 计算 fail closed。
    """
    __tablename__ = 'v4_graph_relations'

    id = Column(String(1024), primary_key=True)
    source_id = Column(String(512), ForeignKey('v4_graph_entities.id', ondelete='CASCADE'), nullable=False, index=True)
    target_id = Column(String(512), ForeignKey('v4_graph_entities.id', ondelete='CASCADE'), nullable=False, index=True)
    relation_type = Column(String(64), nullable=False)             # contains/applies_to/参数名/...
    version_label = Column(String(64), nullable=True)              # 产品版本 / common / unversioned
    version_status = Column(String(32), nullable=True, default='unversioned')
    version_family = Column(String(255), nullable=True, index=True)  # 产品/主题版本族稳定 key
    evidence_count = Column(Integer, nullable=False, default=1)
    acl_scope = Column(Text, nullable=True)
    fingerprint = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)


class V4GraphRelationEvidence(Base):
    """V4 图谱关系 → 来源追溯映射（J-3，后台内部，普通 API 不返回）。

    relation_id → (page_id, chunk_id, evidence_id, wiki_page_id, revision_id, section_id)
    唯一，幂等。普通图谱 API 绝不返回这些内部 ID。
    三种 provenance 互斥来源：Page/Chunk、Evidence、Wiki。
    """
    __tablename__ = 'v4_graph_relation_evidence'

    id = Column(String(64), primary_key=True)
    relation_id = Column(String(1024), ForeignKey('v4_graph_relations.id', ondelete='CASCADE'), nullable=False, index=True)
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    chunk_id = Column(String(36), ForeignKey('page_chunks.id', ondelete='CASCADE'), nullable=True)
    evidence_id = Column(String(36), ForeignKey('evidence_items.id', ondelete='CASCADE'), nullable=True)
    wiki_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=True)
    # J-3 封板：revision_id 加真实 FK（graph provenance → wiki_revisions.id 不构成循环，
    # 因为 wiki_revisions 不反向引用 graph provenance 表）。
    revision_id = Column(String(36), ForeignKey('wiki_revisions.id', ondelete='CASCADE'), nullable=True)
    # Section 删除后 provenance 一并失效（CASCADE），不残留可见旧关系。
    section_id = Column(String(36), ForeignKey('wiki_sections.id', ondelete='CASCADE'), nullable=True)

    __table_args__ = (
        Index('ux_v4_graph_rel_evidence', 'relation_id', 'page_id', 'chunk_id', 'evidence_id', 'wiki_page_id', 'revision_id', 'section_id', unique=True),
    )


class V4GraphEntityEvidence(Base):
    """V4 图谱实体 → 来源追溯映射（J-3，后台内部，普通 API 不返回）。

    entity_id → (page_id, chunk_id, evidence_id, wiki_page_id, revision_id, section_id)
    唯一，幂等。用于判定「孤立实体」：无任何 entity_evidence 的实体视为孤立、可清理。
    """
    __tablename__ = 'v4_graph_entity_evidence'

    id = Column(String(64), primary_key=True)
    entity_id = Column(String(512), ForeignKey('v4_graph_entities.id', ondelete='CASCADE'), nullable=False, index=True)
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    chunk_id = Column(String(36), ForeignKey('page_chunks.id', ondelete='CASCADE'), nullable=True)
    evidence_id = Column(String(36), ForeignKey('evidence_items.id', ondelete='CASCADE'), nullable=True)
    wiki_page_id = Column(String(36), ForeignKey('wiki_pages.id', ondelete='CASCADE'), nullable=True)
    revision_id = Column(String(36), ForeignKey('wiki_revisions.id', ondelete='CASCADE'), nullable=True)
    section_id = Column(String(36), ForeignKey('wiki_sections.id', ondelete='CASCADE'), nullable=True)

    __table_args__ = (
        Index('ux_v4_graph_entity_evidence', 'entity_id', 'page_id', 'chunk_id', 'evidence_id', 'wiki_page_id', 'revision_id', 'section_id', unique=True),
    )


class V4GraphCommunity(Base):
    """V4 图谱 Community 分组（J-3，只做分组/颜色/布局，不作为用户内容节点）。"""
    __tablename__ = 'v4_graph_communities'

    key = Column(String(512), primary_key=True)                   # `{scope_key}|{代表实体规范化名}`
    display_name = Column(String(255), nullable=False)            # 确定性代表实体名称
    acl_scope = Column(Text, nullable=True, index=True)
    fingerprint = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class KnowledgeDebt(Base):
    """知识债务（W12 + P3 扩展，V3 计划 8.3）。从查询日志与治理状态识别知识缺口。

    debt_type 为 W12 旧分类（no_answer/low_score/expired/no_evidence），
    root_cause 为 P3 细化分类（missing_knowledge/missing_evidence/conflict/outdated/
    missing_visual/retrieval_failure/index_failure/system_failure/ambiguous_query）。
    """
    __tablename__ = 'knowledge_debts'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    debt_type = Column(String(50), nullable=False, index=True)  # W12 旧分类（兼容）
    description = Column(Text, nullable=False)
    related_question = Column(Text, nullable=True)
    score = Column(Float, default=0.0)             # 缺口严重度
    status = Column(String(32), default='open', index=True)  # open / resolved
    created_at = Column(DateTime, default=datetime.now)
    resolved_at = Column(DateTime, nullable=True)

    # P3 扩展字段
    title = Column(String(255), nullable=True)
    root_cause = Column(String(50), nullable=True, index=True)  # P3 九类根因
    scope_json = Column(Text, nullable=True)                     # 产品/版本/区域
    priority = Column(String(16), nullable=True)                 # P0/P1/P2/P3
    occurrence_count = Column(Integer, nullable=True, default=1)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    acl_scope = Column(Text, nullable=True)                      # JSON，可见范围

    # ---- V4 Phase F：知识债务改造新增字段（nullable，向后兼容） ----
    # 语义：债务只代表「用户需要但知识库确实缺失的内容」，不代表模型故障/Card/审核。
    original_query = Column(Text, nullable=True)                 # 原始查询（代表性）
    normalized_query = Column(String(255), nullable=True, index=True)  # NFKC 规范化查询
    cluster_key = Column(String(255), nullable=True)                   # scope + normalized_query 去重键（唯一索引见 __table_args__）
    affected_user_count = Column(Integer, nullable=True, default=1)    # 不同用户去重计数
    scope_id = Column(String(255), nullable=True, index=True)          # 规范化权限域 company/group:<名>/admin
    retrieval_reason = Column(String(50), nullable=True)               # 记录时的检索缺失原因
    # ---- V4 Phase J-4：反馈/访问申请分类字段（nullable，向后兼容） ----
    version_label = Column(String(64), nullable=True)                  # 产品版本标签（2.0/3.0/unversioned）
    version_status = Column(String(32), nullable=True)                 # specified/unspecified/ambiguous
    debt_reason = Column(String(50), nullable=True)                    # query_missing/feedback_incorrect/feedback_incomplete/feedback_unspecified

    __table_args__ = (
        # 数据库级唯一约束：精确规范化债务去重（NULL 不参与唯一，兼容旧记录）。
        # reopen 语义：同 cluster_key 全表唯一，resolved 后再缺失 → 复用原记录。
        # 注意：不额外给 cluster_key 生成普通索引（index=True），避免与唯一索引重复。
        Index('ux_knowledge_debts_cluster_key', 'cluster_key', unique=True),
    )


class KnowledgeDebtUser(Base):
    """V4 Phase F：债务受影响用户去重关联表（内部，不对外返回 user_id）。"""
    __tablename__ = 'knowledge_debt_users'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    debt_id = Column(String(36), ForeignKey('knowledge_debts.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)

    __table_args__ = (
        Index('ux_kdu_debt_user', 'debt_id', 'user_id', unique=True),
    )


# ---------------------------------------------------------------------------
# V4 Phase J-4：回答反馈、知识缺口与访问申请
# ---------------------------------------------------------------------------

class AnswerSnapshot(Base):
    """回答快照（J-4）。每条 Chat 响应生成一个不可预测、稳定、持久化的 answer_id。

    只保存分类所需字段，不复制完整 LLM context，不保存隐藏正文。
    answer_id 只能由原回答用户使用；其他用户提交返回 404/403。

    business_groups / is_admin 是产生该回答时的权限快照（内部，普通 API 不返回），
    用于后台分类按「回答产生时」的权限上下文判断，而非提交反馈时的当前组。
    """
    __tablename__ = 'answer_snapshots'

    id = Column(String(36), primary_key=True)                          # answer_id（服务端 UUID）
    user_id = Column(String(36), nullable=False, index=True)
    original_query = Column(Text, nullable=False)
    normalized_query = Column(String(255), nullable=False, index=True)
    version_label = Column(String(64), nullable=True)                  # specified 时的版本标签
    version_status = Column(String(32), nullable=True)                 # specified/unspecified/ambiguous
    response_mode = Column(String(32), nullable=True)                  # answer/retrieval_only/insufficient
    retrieval_completed = Column(Boolean, nullable=True, default=False)
    answer_eligible = Column(Boolean, nullable=True, default=False)
    service_degraded = Column(Boolean, nullable=True, default=False)
    has_visible_sufficient_evidence = Column(Boolean, nullable=True, default=False)  # = answer_eligible 快照
    business_groups = Column(Text, nullable=True)                      # JSON 数组：规范化稳定排序的业务组集合
    is_admin = Column(Boolean, nullable=True, default=False)           # 回答产生时是否管理员
    created_at = Column(DateTime, default=datetime.now)


class AnswerFeedback(Base):
    """回答反馈（J-4）。同一用户对同一 answer 只有一条当前反馈（upsert）。

    feedback_cluster_key 记录该负面反馈实际加入的 cluster（精确关联，撤销时
    不再靠相似度重猜）。SET NULL：cluster 被删时反馈保留、仅断开关联。
    """
    __tablename__ = 'answer_feedback'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    answer_id = Column(String(36), ForeignKey('answer_snapshots.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    helpful = Column(Boolean, nullable=False)                          # True=👍 / False=👎
    reason = Column(String(32), nullable=True)                         # incorrect/incomplete（null=未选原因）
    note = Column(Text, nullable=True)
    feedback_cluster_key = Column(String(255), ForeignKey('feedback_clusters.cluster_key', ondelete='SET NULL'), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ux_answer_feedback_answer_user', 'answer_id', 'user_id', unique=True),
    )


class FeedbackCluster(Base):
    """负面反馈聚类（J-4）。按产品/主题 + normalized_query + version + reason 合并。

    classified_at 非空表示已达阈值并完成一次后台分类（只触发一次）。
    """
    __tablename__ = 'feedback_clusters'

    cluster_key = Column(String(255), primary_key=True)                # sha256(j4fb:version:normalized_query:reason)
    normalized_query = Column(String(255), nullable=False, index=True)
    version_label = Column(String(64), nullable=True)
    version_status = Column(String(32), nullable=True)
    reason = Column(String(32), nullable=False)                        # incorrect/incomplete/unspecified
    classification = Column(String(32), nullable=True)                 # visible_sufficient/hidden_sufficient/globally_incomplete/globally_missing/degraded
    classified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    last_seen_at = Column(DateTime, default=datetime.now)


class FeedbackClusterUser(Base):
    """负面反馈聚类去重用户（J-4）。同一 cluster 同一用户只计一次。"""
    __tablename__ = 'feedback_cluster_users'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_key = Column(String(255), ForeignKey('feedback_clusters.cluster_key', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ux_fcu_cluster_user', 'cluster_key', 'user_id', unique=True),
    )


class AnswerNeeded(Base):
    """「我仍需要这个答案」去重（J-4）。同一 answer 幂等，并发不创建重复记录。"""
    __tablename__ = 'answer_needed'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    answer_id = Column(String(36), ForeignKey('answer_snapshots.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ux_answer_needed_answer', 'answer_id', unique=True),
    )


class AccessRequest(Base):
    """访问申请（J-4）。用户当前范围不足、其他范围有充分答案时创建/累计。

    requesting_groups 存规范化、稳定排序的业务组集合（多组用户不猜单组）。
    target_notebook_id / target_wiki_page_id 仅管理员可见（内部目标）。
    """
    __tablename__ = 'access_requests'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cluster_key = Column(String(255), nullable=False)                  # 确定性 cluster_key（唯一索引见 __table_args__）
    normalized_query = Column(String(255), nullable=False, index=True)
    original_query = Column(Text, nullable=True)
    version_label = Column(String(64), nullable=True)
    version_status = Column(String(32), nullable=True)
    requesting_groups = Column(Text, nullable=True)                    # JSON 数组，规范化稳定排序
    target_notebook_id = Column(String(36), nullable=True)             # 内部目标（仅管理员可见）
    target_wiki_page_id = Column(String(36), nullable=True)            # 内部目标（仅管理员可见）
    occurrence_count = Column(Integer, nullable=True, default=1)
    affected_user_count = Column(Integer, nullable=True, default=1)
    status = Column(String(32), default='open', index=True)            # open/resolved
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ux_access_requests_cluster_key', 'cluster_key', unique=True),
    )


class AccessRequestUser(Base):
    """访问申请受影响用户去重（J-4，内部，不对外返回 user_id）。"""
    __tablename__ = 'access_request_users'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id = Column(String(36), ForeignKey('access_requests.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ux_aru_request_user', 'request_id', 'user_id', unique=True),
    )


class EvidenceItem(Base):
    """证据原子（P1，V3 计划 6.2）。可追溯的原始文本/图片观察/表格/代码证据。

    幂等键：content_hash（P1-BE-02 转换时查询去重，不加唯一索引——
    同内容出现在不同 chunk 位置是合法的）。
    """
    __tablename__ = 'evidence_items'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    source_chunk_id = Column(String(36), ForeignKey('page_chunks.id', ondelete='SET NULL'), nullable=True, index=True)
    source_asset_id = Column(String(255), nullable=True, index=True)  # Asset 表未建，先存 chunk.image_id
    evidence_type = Column(String(32), nullable=False, default='text')  # text/image_observation/table/code/manual
    content = Column(Text, nullable=False, default='')
    locator_json = Column(Text, default='{}')                # page_number/heading/image_id/bbox
    content_hash = Column(String(64), nullable=True, index=True)
    source_doc_hash = Column(String(64), nullable=True, index=True)  # 生成时 source_page.content_hash 快照（P1-BE-08）
    extraction_method = Column(String(32), nullable=True)    # parser/ocr/vlm/manual
    model_name = Column(String(127), nullable=True)
    confidence = Column(Float, default=1.0)
    needs_review = Column(Boolean, nullable=True, default=False)
    status = Column(String(32), default='active', index=True)  # active/stale/rejected
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_evidence_page_status', 'source_page_id', 'status'),
    )


class AssetObservation(Base):
    """图片观察（P1，V3 计划 6.2）。图片的结构化解读结果，供 GLM-5.1 读取。

    asset_id 指向 chunk.image_id（Asset 表未建，先存字符串）。
    """
    __tablename__ = 'asset_observations'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id = Column(String(255), nullable=False, index=True)
    observation_type = Column(String(32), nullable=False, default='ocr')  # ocr/ui_function/operation_flow/table/layout
    content = Column(Text, nullable=False, default='')
    extraction_method = Column(String(32), nullable=True)    # parser/ocr/vlm/manual
    model_name = Column(String(127), nullable=True)
    confidence = Column(Float, default=0.5)
    needs_review = Column(Boolean, nullable=True, default=False)
    content_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    analysis_status = Column(String(32), default='done', index=True)  # pending/done/failed/skipped
    provider = Column(String(32), nullable=True)  # disabled/local/remote/ocr
    model_version = Column(String(127), nullable=True)
    error_category = Column(String(64), nullable=True)
    context_hash = Column(String(64), nullable=True, index=True)
    inferred = Column(Boolean, nullable=True, default=False)
    source_page_id = Column(String(36), nullable=True, index=True)


class RuntimeFeatureFlag(Base):
    """Persistent runtime override for a configured V3 feature flag."""
    __tablename__ = 'runtime_feature_flags'

    name = Column(String(64), primary_key=True)
    enabled = Column(Boolean, nullable=False)
    updated_by = Column(String(36), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SourceConnection(Base):
    """数据源连接（P9，V3 计划 4.5）。connector_key + name 唯一。

    config_json 只保存非敏感配置；Secret 走 secret_ref / 环境变量。
    """
    __tablename__ = 'source_connections'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connector_key = Column(String(64), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=True, default=True)
    config_json = Column(Text, nullable=True)            # 非敏感配置
    secret_ref = Column(String(255), nullable=True)      # 敏感凭证引用，不进明文
    target_notebook_id = Column(String(36), nullable=True)
    default_acl_json = Column(Text, nullable=True)
    cursor_json = Column(Text, nullable=True)            # 增量游标
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ux_source_connection_key_name', 'connector_key', 'name', unique=True),
    )


class SourceItem(Base):
    """数据源条目（P9，V3 计划 4.5）。connection_id + external_id 唯一。

    state：active/skipped/deleted/error。
    """
    __tablename__ = 'source_items'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = Column(String(36), ForeignKey('source_connections.id', ondelete='CASCADE'), nullable=False, index=True)
    external_id = Column(String(512), nullable=False)
    external_version = Column(String(255), nullable=True)
    content_hash = Column(String(64), nullable=True)
    metadata_hash = Column(String(64), nullable=True)
    source_url = Column(Text, nullable=True)
    source_path = Column(Text, nullable=True)
    page_id = Column(String(36), nullable=True)          # 关联 Page
    state = Column(String(32), default='active', index=True)  # active/skipped/deleted/error
    acl_json = Column(Text, nullable=True)
    source_updated_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=True, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ux_source_item_conn_external', 'connection_id', 'external_id', unique=True),
    )


class SourceSyncRun(Base):
    """同步任务（P9，V3 计划 4.5）。同一连接只允许一个 queued/running。"""
    __tablename__ = 'source_sync_runs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id = Column(String(36), ForeignKey('source_connections.id', ondelete='CASCADE'), nullable=False, index=True)
    mode = Column(String(32), nullable=False, default='incremental')  # incremental/backfill/full_reconcile
    status = Column(String(32), default='queued', index=True)  # queued/running/succeeded/failed/cancelled
    stage = Column(String(32), nullable=True)                   # 当前阶段：remote/refresh/discover/persist/index/compile
    progress = Column(Text, nullable=True)                      # 人类可读进度描述
    cursor_before_json = Column(Text, nullable=True)
    cursor_after_json = Column(Text, nullable=True)
    discovered_count = Column(Integer, nullable=True, default=0)
    created_count = Column(Integer, nullable=True, default=0)
    updated_count = Column(Integer, nullable=True, default=0)
    unchanged_count = Column(Integer, nullable=True, default=0)
    deleted_count = Column(Integer, nullable=True, default=0)
    failed_count = Column(Integer, nullable=True, default=0)
    cancel_requested = Column(Boolean, nullable=True, default=False)
    llm_degraded = Column(Boolean, nullable=True, default=False)  # GLM/LLM 不可用时降级标记
    degraded_reason = Column(Text, nullable=True)                 # 降级原因
    heartbeat_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_summary = Column(Text, nullable=True)
    created_by = Column(String(36), nullable=True)


class SourceSyncError(Base):
    """同步错误（P9，V3 计划 4.5）。stage：discover/fetch/parse/persist/index/compile。"""
    __tablename__ = 'source_sync_errors'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey('source_sync_runs.id', ondelete='CASCADE'), nullable=False, index=True)
    external_id = Column(String(512), nullable=True)
    stage = Column(String(32), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    retryable = Column(Boolean, nullable=True, default=True)
    retry_count = Column(Integer, nullable=True, default=0)
    created_at = Column(DateTime, default=datetime.now)
    resolved_at = Column(DateTime, nullable=True)


def get_engine(database_url: str):
    from app.config import settings

    connect_args = {}
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        os.makedirs("./data", exist_ok=True)
        connect_args["check_same_thread"] = False

    if is_sqlite and settings.sqlite_use_nullpool:
        # P0-BE-03 评估项：NullPool 每次借出新建连接，避免池内长驻连接阻碍 WAL checkpoint
        from sqlalchemy.pool import NullPool
        engine = create_engine(database_url, pool_pre_ping=True, poolclass=NullPool, connect_args=connect_args)
    else:
        # P0-BE-03：SQLite 单写者模型下缩小常驻连接池；PostgreSQL 等保持 20/10
        pool_size, max_overflow = (5, 10) if is_sqlite else (20, 10)
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args=connect_args,
        )

    if is_sqlite and settings.sqlite_pragma_enabled:
        _attach_sqlite_pragma_listener(engine, database_url)
    return engine


def _attach_sqlite_pragma_listener(engine, database_url: str):
    """P0-BE-03：为文件型 SQLite 的 engine 实例注册 connect 事件，逐连接注入 PRAGMA。

    - WAL：读写并发，消除 journal_mode=delete 下读写完全互斥导致的卡顿
    - synchronous=NORMAL：WAL 下安全（断电最多丢最后一个事务，不损坏数据库）
    - busy_timeout：写冲突时等待重试而非立刻抛 database is locked
    - P0-BE-08：foreign_keys=ON 使模型 DDL 声明的 CASCADE/SET NULL 级联真正生效
      （SQLite 默认关闭外键约束，此前删除父行会留下孤儿行）
    - 仅挂到本 engine 实例（类级监听会误伤 PostgreSQL 与测试自建的内存库）
    - :memory: / mode=memory 跳过（WAL 对内存库无意义）
    - 回滚：settings.sqlite_pragma_enabled=False（环境变量 SQLITE_PRAGMA_ENABLED=false）。
      注意 journal_mode 持久化在 DB 文件中，完全还原需手动执行 PRAGMA journal_mode=DELETE；
      foreign_keys 可单独用 SQLITE_FOREIGN_KEYS_ENABLED=false 关闭
    """
    from sqlalchemy import event
    from app.config import settings

    if ":memory:" in database_url or "mode=memory" in database_url:
        return

    busy_timeout_ms = settings.sqlite_busy_timeout_ms
    pragmas = [
        f"PRAGMA busy_timeout = {busy_timeout_ms}",
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
    ]
    if settings.sqlite_foreign_keys_enabled:
        pragmas.append("PRAGMA foreign_keys = ON")

    def _set_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        try:
            for pragma in pragmas:
                cursor.execute(pragma)
        finally:
            cursor.close()

    event.listen(engine, "connect", _set_pragmas)


def _migrate_schema(engine):
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        expected = {col.name for col in table.columns}
        missing = expected - existing
        if missing:
            for col in table.columns:
                if col.name in missing:
                    # 迁移托管字段不自动补列，改由 check_managed_migrations 显式报错。
                    if (table.name, col.name) in MANAGED_MIGRATION_COLUMNS:
                        logger.warning(
                            "managed_migration_column_skipped",
                            extra={"table": table.name, "column": col.name},
                        )
                        continue
                    col_type = col.type.compile(engine.dialect)
                    alter_sql = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}'
                    if not col.nullable and col.server_default is None:
                        alter_sql += " DEFAULT ''"
                    with engine.begin() as conn:
                        conn.execute(sqlalchemy_text(alter_sql))
                    logger.info(f"Added column {col.name} to table {table.name}")

    if inspector.has_table("pages"):
        try:
            with engine.begin() as conn:
                conn.execute(sqlalchemy_text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_pages_source_type_source_id "
                    "ON pages (source_type, source_id)"
                ))
        except Exception as exc:
            logger.warning(f"Could not create DingTalk source index: {exc}")

        try:
            with engine.begin() as conn:
                conn.execute(sqlalchemy_text(
                    "UPDATE pages SET indexed_content_hash = content_hash "
                    "WHERE indexed_content_hash IS NULL AND content_hash IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM page_chunks WHERE page_chunks.page_id = pages.id)"
                ))
                conn.execute(sqlalchemy_text(
                    "UPDATE pages SET source_content_hash = content_hash "
                    "WHERE source_type = 'dingtalk' AND source_content_hash IS NULL "
                    "AND content_hash IS NOT NULL"
                ))
                conn.execute(sqlalchemy_text(
                    "UPDATE pages SET index_dirty = 0 "
                    "WHERE index_dirty IS NULL AND indexed_content_hash IS NOT NULL"
                ))
                conn.execute(sqlalchemy_text(
                    "UPDATE pages SET index_dirty = 1 WHERE index_dirty IS NULL"
                ))
        except Exception as exc:
            logger.warning(f"Could not backfill page hash fields: {exc}")


def check_managed_migrations(engine) -> list[str]:
    """检测迁移托管字段/表是否缺失，返回缺失项列表。

    - MANAGED_MIGRATION_COLUMNS：已有表缺托管字段 → 'table.column'。
    - P36 图谱表：核心表已存在（已有库）时精确校验表结构，缺表/错列/错类型/错
      主键/错索引/错唯一约束/错 FK/错 ON DELETE 均 fail closed。

    不修改数据库；仅报告 schema_not_ready 所需的信息。
    """
    inspector = inspect(engine)
    missing: list[str] = []
    for table_name, col_name in sorted(MANAGED_MIGRATION_COLUMNS):
        if not inspector.has_table(table_name):
            continue  # 表尚不存在，create_all 会整体创建
        existing = {c["name"] for c in inspector.get_columns(table_name)}
        if col_name not in existing:
            missing.append(f"{table_name}.{col_name}")

    # P36 图谱表守卫：核心表已存在（说明是已有库）时精确校验结构。
    core_exists = inspector.has_table("pages") or inspector.has_table("wiki_pages")
    if core_exists:
        missing.extend(_check_graph_schema(inspector))
        missing.extend(_check_j4_schema(inspector))
        missing.extend(_check_workspace_schema(inspector))
        missing.extend(_check_compile_schema(inspector))
        # P38 统一路径映射表：已有库必须存在（由 Alembic P38 显式创建）。
        for table in sorted(MANAGED_P38_TABLES):
            if not inspector.has_table(table):
                missing.append(table)
        # Phase 3 独立 WikiWorkspace 表：已有库必须存在（由 Alembic P41 显式创建）。
        for table in sorted(MANAGED_WORKSPACE_TABLES):
            if not inspector.has_table(table):
                missing.append(table)
        # Phase 4 编译持久任务表：已有库必须存在（由 Alembic P42 显式创建）。
        for table in sorted(MANAGED_COMPILE_TABLES):
            if not inspector.has_table(table):
                missing.append(table)
        # Phase 7C（P44）：WikiSection Evidence Binding 表与结构。
        for table in sorted(MANAGED_P44_TABLES):
            if not inspector.has_table(table):
                missing.append(table)
        missing.extend(_check_p44_schema(inspector))
    return missing


# ---------------------------------------------------------------------------
# P36 图谱 schema 精确校验（表/列/类型/主键/索引/唯一约束/FK/ON DELETE）
# ---------------------------------------------------------------------------

# 五张 P36 图谱表：必要列 → (SQLAlchemy 类型字符串, nullable)。
_GRAPH_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "v4_graph_entities": {
        "id": ("VARCHAR(512)", False),
        "entity_type": ("VARCHAR(64)", False),
        "normalized_name": ("VARCHAR(255)", False),
        "display_name": ("VARCHAR(255)", False),
        "community_key": ("VARCHAR(512)", True),
        "acl_scope": ("TEXT", True),
        "version_label": ("VARCHAR(64)", True),
        "version_status": ("VARCHAR(32)", True),
        "fingerprint": ("VARCHAR(64)", True),
        "updated_at": ("DATETIME", True),
    },
    "v4_graph_relations": {
        "id": ("VARCHAR(1024)", False),
        "source_id": ("VARCHAR(512)", False),
        "target_id": ("VARCHAR(512)", False),
        "relation_type": ("VARCHAR(64)", False),
        "version_label": ("VARCHAR(64)", True),
        "version_status": ("VARCHAR(32)", True),
        "version_family": ("VARCHAR(255)", True),
        "evidence_count": ("INTEGER", False),
        "acl_scope": ("TEXT", True),
        "fingerprint": ("VARCHAR(64)", True),
        "updated_at": ("DATETIME", True),
    },
    "v4_graph_relation_evidence": {
        "id": ("VARCHAR(64)", False),
        "relation_id": ("VARCHAR(1024)", False),
        "page_id": ("VARCHAR(36)", True),
        "chunk_id": ("VARCHAR(36)", True),
        "evidence_id": ("VARCHAR(36)", True),
        "wiki_page_id": ("VARCHAR(36)", True),
        "revision_id": ("VARCHAR(36)", True),
        "section_id": ("VARCHAR(36)", True),
    },
    "v4_graph_entity_evidence": {
        "id": ("VARCHAR(64)", False),
        "entity_id": ("VARCHAR(512)", False),
        "page_id": ("VARCHAR(36)", True),
        "chunk_id": ("VARCHAR(36)", True),
        "evidence_id": ("VARCHAR(36)", True),
        "wiki_page_id": ("VARCHAR(36)", True),
        "revision_id": ("VARCHAR(36)", True),
        "section_id": ("VARCHAR(36)", True),
    },
    "v4_graph_communities": {
        "key": ("VARCHAR(512)", False),
        "display_name": ("VARCHAR(255)", False),
        "acl_scope": ("TEXT", True),
        "fingerprint": ("VARCHAR(64)", True),
        "updated_at": ("DATETIME", True),
    },
}

# 主键 → 列
_GRAPH_PKS: dict[str, str] = {
    "v4_graph_entities": "id",
    "v4_graph_relations": "id",
    "v4_graph_relation_evidence": "id",
    "v4_graph_entity_evidence": "id",
    "v4_graph_communities": "key",
}

# 必须存在的普通索引 → 列（有序）。
_GRAPH_INDEXES: dict[str, list[str]] = {
    "v4_graph_entities": ["entity_type", "community_key", "updated_at"],
    "v4_graph_relations": ["source_id", "target_id", "version_family", "updated_at"],
    "v4_graph_relation_evidence": ["relation_id", "page_id"],
    "v4_graph_entity_evidence": ["entity_id", "page_id"],
    "v4_graph_communities": ["acl_scope"],
}

# 唯一索引 → (索引名称, 精确列顺序)。必须同时校验名称、列顺序、unique=True；
# 不能接受名称相异但列相同的唯一索引冒充。
_GRAPH_UNIQUE: dict[str, tuple[str, list[str]]] = {
    "v4_graph_relation_evidence": (
        "ux_v4_graph_rel_evidence",
        ["relation_id", "page_id", "chunk_id", "evidence_id", "wiki_page_id", "revision_id", "section_id"],
    ),
    "v4_graph_entity_evidence": (
        "ux_v4_graph_entity_evidence",
        ["entity_id", "page_id", "chunk_id", "evidence_id", "wiki_page_id", "revision_id", "section_id"],
    ),
}

# FK：(表, 列) → (目标表, ondelete)
_GRAPH_FKS: dict[tuple[str, str], tuple[str, str]] = {
    ("v4_graph_relations", "source_id"): ("v4_graph_entities", "CASCADE"),
    ("v4_graph_relations", "target_id"): ("v4_graph_entities", "CASCADE"),
    ("v4_graph_relation_evidence", "relation_id"): ("v4_graph_relations", "CASCADE"),
    ("v4_graph_relation_evidence", "page_id"): ("pages", "CASCADE"),
    ("v4_graph_relation_evidence", "chunk_id"): ("page_chunks", "CASCADE"),
    ("v4_graph_relation_evidence", "evidence_id"): ("evidence_items", "CASCADE"),
    ("v4_graph_relation_evidence", "wiki_page_id"): ("wiki_pages", "CASCADE"),
    ("v4_graph_relation_evidence", "revision_id"): ("wiki_revisions", "CASCADE"),
    ("v4_graph_relation_evidence", "section_id"): ("wiki_sections", "CASCADE"),
    ("v4_graph_entity_evidence", "entity_id"): ("v4_graph_entities", "CASCADE"),
    ("v4_graph_entity_evidence", "page_id"): ("pages", "CASCADE"),
    ("v4_graph_entity_evidence", "chunk_id"): ("page_chunks", "CASCADE"),
    ("v4_graph_entity_evidence", "evidence_id"): ("evidence_items", "CASCADE"),
    ("v4_graph_entity_evidence", "wiki_page_id"): ("wiki_pages", "CASCADE"),
    ("v4_graph_entity_evidence", "revision_id"): ("wiki_revisions", "CASCADE"),
    ("v4_graph_entity_evidence", "section_id"): ("wiki_sections", "CASCADE"),
}


def _check_graph_schema(inspector) -> list[str]:
    """精确校验 P36 图谱表结构。返回缺失/错误的项列表（空 = 结构正确）。

    校验：列集合完全一致（额外列 fail closed）、列类型/长度/nullable、主键、普通索引
    （名称/列顺序/unique 属性）、唯一索引、FK（constrained columns/referred table/
    referred columns/ON DELETE）。
    """
    problems: list[str] = []
    for table in sorted(MANAGED_MIGRATION_TABLES):
        if not inspector.has_table(table):
            problems.append(table)
            continue

        # 列 + 类型 + nullable + 额外列
        existing_cols = {c["name"]: c for c in inspector.get_columns(table)}
        expected_col_names = set(_GRAPH_COLUMNS[table].keys())
        extra_cols = set(existing_cols.keys()) - expected_col_names
        if extra_cols:
            problems.append(f"{table}:extra_cols={sorted(extra_cols)}")
        for col, (type_str, nullable) in _GRAPH_COLUMNS[table].items():
            if col not in existing_cols:
                problems.append(f"{table}.{col}")
                continue
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"{table}.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"{table}.{col}:nullable={colinfo['nullable']}")

        # 主键（列名 + 顺序）
        pk = inspector.get_pk_constraint(table)
        pk_cols = list(pk.get("constrained_columns") or [])
        expected_pk = _GRAPH_PKS[table]
        if pk_cols != [expected_pk]:
            problems.append(f"{table}:pk={pk_cols}")

        # 普通索引：名称 + 列顺序 + unique 属性必须精确匹配
        # 索引名规则：ix_<table>_<column>（与 ORM index=True 自动命名一致）。
        existing_indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        for col in _GRAPH_INDEXES.get(table, []):
            idx_name = f"ix_{table}_{col}"
            ix = existing_indexes.get(idx_name)
            if ix is None:
                problems.append(f"{table}:index={col}")
                continue
            if list(ix.get("column_names") or []) != [col]:
                problems.append(f"{table}:index_cols={col}:{ix.get('column_names')}")
            if bool(ix.get("unique")):
                problems.append(f"{table}:index_unique={col}")

        # 唯一索引：名称 + 列顺序 + unique 属性必须精确匹配。
        if table in _GRAPH_UNIQUE:
            expected_uniq_name, expected_uniq_cols = _GRAPH_UNIQUE[table]
            uniq_indexes = [ix for ix in inspector.get_indexes(table) if ix.get("unique")]
            matched_uniq = next(
                (ix for ix in uniq_indexes if ix.get("name") == expected_uniq_name),
                None,
            )
            if matched_uniq is None:
                problems.append(f"{table}:unique_name={expected_uniq_name}")
            else:
                if list(matched_uniq.get("column_names") or []) != expected_uniq_cols:
                    problems.append(f"{table}:unique_cols={expected_uniq_name}:{matched_uniq.get('column_names')}")

        # FK：constrained columns（精确 == [col]）+ referred table + referred columns + ON DELETE
        fks = inspector.get_foreign_keys(table)
        for (tbl, col), (target, ondelete) in _GRAPH_FKS.items():
            if tbl != table:
                continue
            matched = None
            for fk in fks:
                if list(fk.get("constrained_columns") or []) == [col]:
                    matched = fk
                    break
            if matched is None:
                # 区分：完全缺失 vs constrained columns 错误（复合/错列）
                partial = any(
                    col in (fk.get("constrained_columns") or [])
                    for fk in fks
                )
                if partial:
                    problems.append(f"{table}.{col}:fk_constrained_columns")
                else:
                    problems.append(f"{table}.{col}:fk_missing")
                continue
            actual_target = matched.get("referred_table")
            if actual_target != target:
                problems.append(f"{table}.{col}:fk_target={actual_target}")
            # referred columns 必须是目标表主键（如 id / key）
            referred_cols = list(matched.get("referred_columns") or [])
            expected_referred = [_GRAPH_PKS.get(target, "id")]
            if referred_cols != expected_referred:
                problems.append(f"{table}.{col}:fk_referred={referred_cols}")
            actual_ondelete = (matched.get("options") or {}).get("ondelete")
            if actual_ondelete != ondelete:
                problems.append(f"{table}.{col}:ondelete={actual_ondelete}")

    return problems


# ---------------------------------------------------------------------------
# P37 J-4 schema 精确校验（表/列/类型/主键/索引/唯一约束/FK/ON DELETE）
# ---------------------------------------------------------------------------

# 七张 P37 表：必要列 → (SQLAlchemy 类型字符串, nullable)。
_J4_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "answer_snapshots": {
        "id": ("VARCHAR(36)", False),
        "user_id": ("VARCHAR(36)", False),
        "original_query": ("TEXT", False),
        "normalized_query": ("VARCHAR(255)", False),
        "version_label": ("VARCHAR(64)", True),
        "version_status": ("VARCHAR(32)", True),
        "response_mode": ("VARCHAR(32)", True),
        "retrieval_completed": ("BOOLEAN", True),
        "answer_eligible": ("BOOLEAN", True),
        "service_degraded": ("BOOLEAN", True),
        "has_visible_sufficient_evidence": ("BOOLEAN", True),
        "business_groups": ("TEXT", True),
        "is_admin": ("BOOLEAN", True),
        "created_at": ("DATETIME", True),
    },
    "answer_feedback": {
        "id": ("VARCHAR(36)", False),
        "answer_id": ("VARCHAR(36)", False),
        "user_id": ("VARCHAR(36)", False),
        "helpful": ("BOOLEAN", False),
        "reason": ("VARCHAR(32)", True),
        "note": ("TEXT", True),
        "feedback_cluster_key": ("VARCHAR(255)", True),
        "created_at": ("DATETIME", True),
        "updated_at": ("DATETIME", True),
    },
    "feedback_clusters": {
        "cluster_key": ("VARCHAR(255)", False),
        "normalized_query": ("VARCHAR(255)", False),
        "version_label": ("VARCHAR(64)", True),
        "version_status": ("VARCHAR(32)", True),
        "reason": ("VARCHAR(32)", False),
        "classification": ("VARCHAR(32)", True),
        "classified_at": ("DATETIME", True),
        "created_at": ("DATETIME", True),
        "last_seen_at": ("DATETIME", True),
    },
    "feedback_cluster_users": {
        "id": ("VARCHAR(36)", False),
        "cluster_key": ("VARCHAR(255)", False),
        "user_id": ("VARCHAR(36)", False),
        "created_at": ("DATETIME", True),
    },
    "answer_needed": {
        "id": ("VARCHAR(36)", False),
        "answer_id": ("VARCHAR(36)", False),
        "user_id": ("VARCHAR(36)", False),
        "created_at": ("DATETIME", True),
    },
    "access_requests": {
        "id": ("VARCHAR(36)", False),
        "cluster_key": ("VARCHAR(255)", False),
        "normalized_query": ("VARCHAR(255)", False),
        "original_query": ("TEXT", True),
        "version_label": ("VARCHAR(64)", True),
        "version_status": ("VARCHAR(32)", True),
        "requesting_groups": ("TEXT", True),
        "target_notebook_id": ("VARCHAR(36)", True),
        "target_wiki_page_id": ("VARCHAR(36)", True),
        "occurrence_count": ("INTEGER", True),
        "affected_user_count": ("INTEGER", True),
        "status": ("VARCHAR(32)", True),
        "first_seen_at": ("DATETIME", True),
        "last_seen_at": ("DATETIME", True),
        "resolved_at": ("DATETIME", True),
    },
    "access_request_users": {
        "id": ("VARCHAR(36)", False),
        "request_id": ("VARCHAR(36)", False),
        "user_id": ("VARCHAR(36)", False),
        "created_at": ("DATETIME", True),
    },
}

# 主键 → 列
_J4_PKS: dict[str, str] = {
    "answer_snapshots": "id",
    "answer_feedback": "id",
    "feedback_clusters": "cluster_key",
    "feedback_cluster_users": "id",
    "answer_needed": "id",
    "access_requests": "id",
    "access_request_users": "id",
}

# 必须存在的普通索引 → 列（有序）。
_J4_INDEXES: dict[str, list[str]] = {
    "answer_snapshots": ["user_id", "normalized_query"],
    "answer_feedback": ["answer_id", "user_id", "feedback_cluster_key"],
    "feedback_clusters": ["normalized_query"],
    "feedback_cluster_users": ["cluster_key", "user_id"],
    "answer_needed": ["answer_id", "user_id"],
    "access_requests": ["normalized_query", "status"],
    "access_request_users": ["request_id", "user_id"],
}

# 唯一索引 → (索引名称, 精确列顺序)。
_J4_UNIQUE: dict[str, tuple[str, list[str]]] = {
    "answer_feedback": ("ux_answer_feedback_answer_user", ["answer_id", "user_id"]),
    "feedback_cluster_users": ("ux_fcu_cluster_user", ["cluster_key", "user_id"]),
    "answer_needed": ("ux_answer_needed_answer", ["answer_id"]),
    "access_requests": ("ux_access_requests_cluster_key", ["cluster_key"]),
    "access_request_users": ("ux_aru_request_user", ["request_id", "user_id"]),
}

# FK：(表, 列) → (目标表, ondelete)
_J4_FKS: dict[tuple[str, str], tuple[str, str]] = {
    ("answer_feedback", "answer_id"): ("answer_snapshots", "CASCADE"),
    ("answer_feedback", "feedback_cluster_key"): ("feedback_clusters", "SET NULL"),
    ("feedback_cluster_users", "cluster_key"): ("feedback_clusters", "CASCADE"),
    ("answer_needed", "answer_id"): ("answer_snapshots", "CASCADE"),
    ("access_request_users", "request_id"): ("access_requests", "CASCADE"),
}

# KnowledgeDebt 新增字段 → (类型, nullable)。
_J4_KNOWLEDGE_DEBT_COLUMNS: dict[str, tuple[str, bool]] = {
    "version_label": ("VARCHAR(64)", True),
    "version_status": ("VARCHAR(32)", True),
    "debt_reason": ("VARCHAR(50)", True),
}


def _check_j4_schema(inspector) -> list[str]:
    """P37 J-4 表精确校验（表/列/类型/nullable/主键/索引/唯一约束/FK/ON DELETE）。

    核心表已存在（已有库）时校验；同名但错误定义必须 fail closed。仅报告问题，
    不修改数据库（init_db 在报 SchemaNotReadyError 前不得改动 schema）。
    """
    problems: list[str] = []
    for table in sorted(MANAGED_J4_TABLES):
        if not inspector.has_table(table):
            problems.append(table)
            continue

        existing_cols = {c["name"]: c for c in inspector.get_columns(table)}
        expected_col_names = set(_J4_COLUMNS[table].keys())
        extra_cols = set(existing_cols.keys()) - expected_col_names
        if extra_cols:
            problems.append(f"{table}:extra_cols={sorted(extra_cols)}")
        for col, (type_str, nullable) in _J4_COLUMNS[table].items():
            if col not in existing_cols:
                problems.append(f"{table}.{col}")
                continue
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"{table}.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"{table}.{col}:nullable={colinfo['nullable']}")

        pk = inspector.get_pk_constraint(table)
        pk_cols = list(pk.get("constrained_columns") or [])
        if pk_cols != [_J4_PKS[table]]:
            problems.append(f"{table}:pk={pk_cols}")

        existing_indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        for col in _J4_INDEXES.get(table, []):
            idx_name = f"ix_{table}_{col}"
            ix = existing_indexes.get(idx_name)
            if ix is None:
                problems.append(f"{table}:index={col}")
                continue
            if list(ix.get("column_names") or []) != [col]:
                problems.append(f"{table}:index_cols={col}:{ix.get('column_names')}")
            if bool(ix.get("unique")):
                problems.append(f"{table}:index_unique={col}")

        if table in _J4_UNIQUE:
            expected_uniq_name, expected_uniq_cols = _J4_UNIQUE[table]
            uniq_indexes = [ix for ix in inspector.get_indexes(table) if ix.get("unique")]
            matched_uniq = next(
                (ix for ix in uniq_indexes if ix.get("name") == expected_uniq_name),
                None,
            )
            if matched_uniq is None:
                problems.append(f"{table}:unique_name={expected_uniq_name}")
            elif list(matched_uniq.get("column_names") or []) != expected_uniq_cols:
                problems.append(f"{table}:unique_cols={expected_uniq_name}:{matched_uniq.get('column_names')}")

        fks = inspector.get_foreign_keys(table)
        for (tbl, col), (target, ondelete) in _J4_FKS.items():
            if tbl != table:
                continue
            matched = None
            for fk in fks:
                if list(fk.get("constrained_columns") or []) == [col]:
                    matched = fk
                    break
            if matched is None:
                partial = any(
                    col in (fk.get("constrained_columns") or [])
                    for fk in fks
                )
                if partial:
                    problems.append(f"{table}.{col}:fk_constrained_columns")
                else:
                    problems.append(f"{table}.{col}:fk_missing")
                continue
            actual_target = matched.get("referred_table")
            if actual_target != target:
                problems.append(f"{table}.{col}:fk_target={actual_target}")
            referred_cols = list(matched.get("referred_columns") or [])
            if referred_cols != [_J4_PKS.get(target, "id")]:
                problems.append(f"{table}.{col}:fk_referred={referred_cols}")
            actual_ondelete = (matched.get("options") or {}).get("ondelete")
            if actual_ondelete != ondelete:
                problems.append(f"{table}.{col}:ondelete={actual_ondelete}")

    # KnowledgeDebt 三个新增字段类型/nullable 精确校验。
    if inspector.has_table("knowledge_debts"):
        kd_cols = {c["name"]: c for c in inspector.get_columns("knowledge_debts")}
        for col, (type_str, nullable) in _J4_KNOWLEDGE_DEBT_COLUMNS.items():
            if col not in kd_cols:
                problems.append(f"knowledge_debts.{col}")
                continue
            colinfo = kd_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"knowledge_debts.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"knowledge_debts.{col}:nullable={colinfo['nullable']}")

    return problems


# ---------------------------------------------------------------------------
# Phase 3：WikiWorkspace schema 精确校验（表/列/类型/主键/索引/唯一约束/FK/ON DELETE）
# ---------------------------------------------------------------------------

# 两张 Phase 3 表：必要列 → (SQLAlchemy 类型字符串, nullable)。
_WORKSPACE_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "wiki_workspaces": {
        "id": ("VARCHAR(36)", False),
        "key": ("VARCHAR(255)", False),
        "name": ("VARCHAR(255)", False),
        "description": ("TEXT", True),
        "acl_scope": ("TEXT", False),
        "scope_id": ("VARCHAR(255)", False),
        "status": ("VARCHAR(32)", False),
        "created_by": ("VARCHAR(36)", True),
        "created_at": ("DATETIME", True),
        "updated_at": ("DATETIME", True),
    },
    "notebook_workspace_bindings": {
        "id": ("VARCHAR(36)", False),
        "notebook_id": ("VARCHAR(36)", False),
        "workspace_id": ("VARCHAR(36)", False),
        "status": ("VARCHAR(32)", False),
        "created_by": ("VARCHAR(36)", True),
        "created_at": ("DATETIME", True),
        "updated_at": ("DATETIME", True),
    },
}

# 主键 → 列
_WORKSPACE_PKS: dict[str, str] = {
    "wiki_workspaces": "id",
    "notebook_workspace_bindings": "id",
}

# 必须存在的普通索引 → 列（有序）。
_WORKSPACE_INDEXES: dict[str, list[str]] = {
    "wiki_workspaces": ["scope_id", "status"],
    "notebook_workspace_bindings": ["notebook_id", "workspace_id"],
}

# 唯一索引 → (索引名称, 精确列顺序)。
_WORKSPACE_UNIQUE: dict[str, tuple[str, list[str]]] = {
    "wiki_workspaces": ("ux_wiki_workspaces_key", ["key"]),
    "notebook_workspace_bindings": ("ux_nb_ws_binding", ["notebook_id", "workspace_id"]),
}

# 部分唯一索引 → (索引名称, 列, 谓词子串)（Phase 3.1：一个 notebook 单 active binding）。
# SQLite/PostgreSQL 谓词索引；guard 校验存在性 + unique + 列 + 谓词片段。
_WORKSPACE_PARTIAL_UNIQUE: dict[str, tuple[str, list[str], str]] = {
    "notebook_workspace_bindings": ("ux_nb_ws_binding_active", ["notebook_id"], "active"),
}

# FK：(表, 列) → (目标表, ondelete)
_WORKSPACE_FKS: dict[tuple[str, str], tuple[str, str]] = {
    ("notebook_workspace_bindings", "notebook_id"): ("notebooks", "CASCADE"),
    ("notebook_workspace_bindings", "workspace_id"): ("wiki_workspaces", "CASCADE"),
    ("wiki_pages", "workspace_id"): ("wiki_workspaces", "SET NULL"),
}


def _check_workspace_schema(inspector) -> list[str]:
    """Phase 3 WikiWorkspace 表精确校验（表/列/类型/nullable/主键/索引/唯一约束/FK/ON DELETE）。

    核心表已存在（已有库）时校验；同名但错误定义必须 fail closed。仅报告问题，
    不修改数据库。
    """
    problems: list[str] = []
    for table in sorted(MANAGED_WORKSPACE_TABLES):
        if not inspector.has_table(table):
            problems.append(table)
            continue

        existing_cols = {c["name"]: c for c in inspector.get_columns(table)}
        expected_col_names = set(_WORKSPACE_COLUMNS[table].keys())
        extra_cols = set(existing_cols.keys()) - expected_col_names
        if extra_cols:
            problems.append(f"{table}:extra_cols={sorted(extra_cols)}")
        for col, (type_str, nullable) in _WORKSPACE_COLUMNS[table].items():
            if col not in existing_cols:
                problems.append(f"{table}.{col}")
                continue
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"{table}.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"{table}.{col}:nullable={colinfo['nullable']}")

        pk = inspector.get_pk_constraint(table)
        pk_cols = list(pk.get("constrained_columns") or [])
        if pk_cols != [_WORKSPACE_PKS[table]]:
            problems.append(f"{table}:pk={pk_cols}")

        existing_indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        for col in _WORKSPACE_INDEXES.get(table, []):
            idx_name = f"ix_{table}_{col}"
            ix = existing_indexes.get(idx_name)
            if ix is None:
                problems.append(f"{table}:index={col}")
                continue
            if list(ix.get("column_names") or []) != [col]:
                problems.append(f"{table}:index_cols={col}:{ix.get('column_names')}")
            if bool(ix.get("unique")):
                problems.append(f"{table}:index_unique={col}")

        if table in _WORKSPACE_UNIQUE:
            expected_uniq_name, expected_uniq_cols = _WORKSPACE_UNIQUE[table]
            uniq_indexes = [ix for ix in inspector.get_indexes(table) if ix.get("unique")]
            matched_uniq = next(
                (ix for ix in uniq_indexes if ix.get("name") == expected_uniq_name),
                None,
            )
            if matched_uniq is None:
                problems.append(f"{table}:unique_name={expected_uniq_name}")
            elif list(matched_uniq.get("column_names") or []) != expected_uniq_cols:
                problems.append(f"{table}:unique_cols={expected_uniq_name}:{matched_uniq.get('column_names')}")

        # Phase 3.1：部分唯一索引（ux_nb_ws_binding_active）存在性校验。
        if table in _WORKSPACE_PARTIAL_UNIQUE:
            p_name, p_cols, p_predicate = _WORKSPACE_PARTIAL_UNIQUE[table]
            partial_uniq_indexes = [
                ix for ix in inspector.get_indexes(table)
                if ix.get("unique") and ix.get("name") == p_name
            ]
            if not partial_uniq_indexes:
                problems.append(f"{table}:partial_unique_name={p_name}")
            else:
                p_ix = partial_uniq_indexes[0]
                if list(p_ix.get("column_names") or []) != p_cols:
                    problems.append(f"{table}:partial_unique_cols={p_name}:{p_ix.get('column_names')}")
                # 谓词片段校验：SQLAlchemy inspector 不直接暴露 sqlite_where 文本，
                # 通过 dialect_options 判断是否有 where 谓词；文本匹配不可靠时降级为
                # 仅校验列/唯一（部分索引必然携带 dialect 谓词，见 ORM 定义）。
                dialect_opts = p_ix.get("dialect_options") or {}
                has_predicate = any(
                    "where" in str(k).lower() or "where" in str(v).lower()
                    for k, v in dialect_opts.items()
                )
                if not has_predicate:
                    problems.append(f"{table}:partial_unique_predicate={p_name}")

        fks = inspector.get_foreign_keys(table)
        for (tbl, col), (target, ondelete) in _WORKSPACE_FKS.items():
            if tbl != table:
                continue
            matched = None
            for fk in fks:
                if list(fk.get("constrained_columns") or []) == [col]:
                    matched = fk
                    break
            if matched is None:
                partial = any(
                    col in (fk.get("constrained_columns") or [])
                    for fk in fks
                )
                if partial:
                    problems.append(f"{table}.{col}:fk_constrained_columns")
                else:
                    problems.append(f"{table}.{col}:fk_missing")
                continue
            actual_target = matched.get("referred_table")
            if actual_target != target:
                problems.append(f"{table}.{col}:fk_target={actual_target}")
            referred_cols = list(matched.get("referred_columns") or [])
            if referred_cols != [_WORKSPACE_PKS.get(target, "id")]:
                problems.append(f"{table}.{col}:fk_referred={referred_cols}")
            actual_ondelete = (matched.get("options") or {}).get("ondelete")
            if actual_ondelete != ondelete:
                problems.append(f"{table}.{col}:ondelete={actual_ondelete}")

    # wiki_pages.workspace_id：列类型/nullable + 普通索引 + FK（ondelete=SET NULL）。
    if inspector.has_table("wiki_pages"):
        wp_cols = {c["name"]: c for c in inspector.get_columns("wiki_pages")}
        if "workspace_id" in wp_cols:
            colinfo = wp_cols["workspace_id"]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith("VARCHAR(36)"):
                problems.append(f"wiki_pages.workspace_id:type={actual_type}")
            if bool(colinfo["nullable"]) is not True:
                problems.append(f"wiki_pages.workspace_id:nullable={colinfo['nullable']}")
            wp_indexes = {ix["name"]: ix for ix in inspector.get_indexes("wiki_pages")}
            ix = wp_indexes.get("ix_wiki_pages_workspace_id")
            if ix is None:
                problems.append("wiki_pages:index=workspace_id")
            elif bool(ix.get("unique")):
                problems.append("wiki_pages:index_unique=workspace_id")
            fks = inspector.get_foreign_keys("wiki_pages")
            matched = None
            for fk in fks:
                if list(fk.get("constrained_columns") or []) == ["workspace_id"]:
                    matched = fk
                    break
            if matched is None:
                problems.append("wiki_pages.workspace_id:fk_missing")
            else:
                if matched.get("referred_table") != "wiki_workspaces":
                    problems.append(f"wiki_pages.workspace_id:fk_target={matched.get('referred_table')}")
                referred_cols = list(matched.get("referred_columns") or [])
                if referred_cols != ["id"]:
                    problems.append(f"wiki_pages.workspace_id:fk_referred={referred_cols}")
                actual_ondelete = (matched.get("options") or {}).get("ondelete")
                if actual_ondelete != "SET NULL":
                    problems.append(f"wiki_pages.workspace_id:ondelete={actual_ondelete}")
        # 列缺失本身由 MANAGED_MIGRATION_COLUMNS 检查报告，这里不重复。

    return problems


# ---------------------------------------------------------------------------
# P44 WikiSection Evidence Binding schema 精确校验（表/列/类型/nullable/主键/
# 索引/唯一约束/部分唯一索引/FK/ON DELETE）
# ---------------------------------------------------------------------------

_P44_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "wiki_section_evidence_bindings": {
        "id": ("VARCHAR(36)", False),
        "section_id": ("VARCHAR(36)", False),
        "evidence_id": ("VARCHAR(36)", False),
        "field_path": ("VARCHAR(255)", False),
        "usage_type": ("VARCHAR(32)", False),
        "evidence_content_hash": ("VARCHAR(64)", False),
        "created_at": ("DATETIME", True),
    },
}

_P44_PKS: dict[str, str] = {
    "wiki_section_evidence_bindings": "id",
}

_P44_INDEXES: dict[str, list[str]] = {
    "wiki_section_evidence_bindings": ["section_id", "evidence_id"],
}

_P44_UNIQUE: dict[str, tuple[str, list[str]]] = {
    "wiki_section_evidence_bindings": (
        "ux_wiki_section_evidence_section_field_evidence_usage",
        ["section_id", "field_path", "evidence_id", "usage_type"],
    ),
}

_P44_FKS: dict[tuple[str, str], tuple[str, str]] = {
    ("wiki_section_evidence_bindings", "section_id"): ("wiki_sections", "CASCADE"),
    ("wiki_section_evidence_bindings", "evidence_id"): ("evidence_items", "CASCADE"),
}

# wiki_sections 的部分唯一索引（P44）。
_P44_SECTION_PARTIAL_UNIQUE = (
    "ux_wiki_sections_revision_section_key",
    ["revision_id", "section_key"],
)

# P44 wiki_sections 新字段类型/nullable 精确校验（列存在性由
# MANAGED_MIGRATION_COLUMNS 负责）。
_P44_SECTION_COLUMNS: dict[str, tuple[str, bool]] = {
    "section_key": ("VARCHAR(255)", True),
    "skill_key": ("VARCHAR(64)", True),
    "skill_version": ("VARCHAR(64)", True),
    "content_hash": ("VARCHAR(64)", True),
    "validation_status": ("VARCHAR(32)", True),
    "structure_json": ("TEXT", True),
}

# 必须存在的 CHECK 约束名（结构与 ORM/migration 一致）。
_P44_SECTION_CHECKS = (
    "ck_wiki_sections_validation_status",
    "ck_wiki_sections_section_key_nonempty",
)
_P44_BINDING_CHECKS = (
    "ck_wiki_section_evidence_bindings_usage_type",
    "ck_wiki_section_evidence_bindings_field_path_nonempty",
    "ck_wiki_section_evidence_bindings_evidence_hash_len",
)


def _check_p44_schema(inspector) -> list[str]:
    """精确校验 P44 WikiSection Evidence Binding 结构 + wiki_sections 部分唯一索引。

    列/类型/nullable/额外列、主键、普通索引、唯一约束、FK（目标/ON DELETE）、
    部分唯一索引（名称/列顺序/谓词存在性）、以及 P44 各 CHECK 约束存在性全部
    fail closed。仅报告，不改库。
    """
    problems: list[str] = []

    # wiki_sections：六个 P44 新字段的类型/nullable 精确校验（防“列存在但类型错”）。
    if inspector.has_table("wiki_sections"):
        existing_cols = {c["name"]: c for c in inspector.get_columns("wiki_sections")}
        for col, (type_str, nullable) in _P44_SECTION_COLUMNS.items():
            if col not in existing_cols:
                continue  # 缺失由 MANAGED_MIGRATION_COLUMNS 报告
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"wiki_sections.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(
                    f"wiki_sections.{col}:nullable={colinfo['nullable']}")
        # CHECK 存在性（结构看似存在但 CHECK 缺失 → fail closed）。
        checks = {c.get("name") for c in inspector.get_check_constraints(
            "wiki_sections")}
        for name in _P44_SECTION_CHECKS:
            if name not in checks:
                problems.append(f"wiki_sections:check_missing={name}")

    for table in sorted(MANAGED_P44_TABLES):
        if not inspector.has_table(table):
            continue  # 缺失由 check_managed_migrations 的 membership 报告

        existing_cols = {c["name"]: c for c in inspector.get_columns(table)}
        expected_col_names = set(_P44_COLUMNS[table].keys())
        extra_cols = set(existing_cols.keys()) - expected_col_names
        if extra_cols:
            problems.append(f"{table}:extra_cols={sorted(extra_cols)}")
        for col, (type_str, nullable) in _P44_COLUMNS[table].items():
            if col not in existing_cols:
                problems.append(f"{table}.{col}")
                continue
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"{table}.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"{table}.{col}:nullable={colinfo['nullable']}")

        pk = inspector.get_pk_constraint(table)
        pk_cols = list(pk.get("constrained_columns") or [])
        if pk_cols != [_P44_PKS[table]]:
            problems.append(f"{table}:pk={pk_cols}")

        existing_indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        for col in _P44_INDEXES.get(table, []):
            idx_name = f"ix_{table}_{col}"
            ix = existing_indexes.get(idx_name)
            if ix is None:
                problems.append(f"{table}:index={col}")
                continue
            if list(ix.get("column_names") or []) != [col]:
                problems.append(f"{table}:index_cols={col}:{ix.get('column_names')}")
            if bool(ix.get("unique")):
                problems.append(f"{table}:index_unique={col}")

        if table in _P44_UNIQUE:
            expected_uniq_name, expected_uniq_cols = _P44_UNIQUE[table]
            uniq_indexes = [ix for ix in inspector.get_indexes(table)
                            if ix.get("unique")]
            matched_uniq = next(
                (ix for ix in uniq_indexes if ix.get("name") == expected_uniq_name),
                None,
            )
            if matched_uniq is None:
                problems.append(f"{table}:unique_name={expected_uniq_name}")
            elif list(matched_uniq.get("column_names") or []) != expected_uniq_cols:
                problems.append(
                    f"{table}:unique_cols={expected_uniq_name}:"
                    f"{matched_uniq.get('column_names')}")

        fks = inspector.get_foreign_keys(table)
        for (tbl, col), (target, ondelete) in _P44_FKS.items():
            if tbl != table:
                continue
            matched = next(
                (fk for fk in fks
                 if list(fk.get("constrained_columns") or []) == [col]),
                None,
            )
            if matched is None:
                problems.append(f"{table}.{col}:fk_missing")
                continue
            if matched.get("referred_table") != target:
                problems.append(f"{table}.{col}:fk_target={matched.get('referred_table')}")
            referred_cols = list(matched.get("referred_columns") or [])
            if referred_cols != ["id"]:
                problems.append(f"{table}.{col}:fk_referred={referred_cols}")
            actual_ondelete = (matched.get("options") or {}).get("ondelete")
            if actual_ondelete != ondelete:
                problems.append(f"{table}.{col}:ondelete={actual_ondelete}")

        # Binding CHECK 存在性（结构看似存在但 CHECK 缺失 → fail closed）。
        checks = {c.get("name") for c in inspector.get_check_constraints(table)}
        for name in _P44_BINDING_CHECKS:
            if name not in checks:
                problems.append(f"{table}:check_missing={name}")

    # wiki_sections 部分唯一索引（列存在性由 MANAGED_MIGRATION_COLUMNS 负责）。
    if inspector.has_table("wiki_sections"):
        p_name, p_cols = _P44_SECTION_PARTIAL_UNIQUE
        partial = [
            ix for ix in inspector.get_indexes("wiki_sections")
            if ix.get("unique") and ix.get("name") == p_name
        ]
        if not partial:
            problems.append(f"wiki_sections:partial_unique_name={p_name}")
        else:
            p_ix = partial[0]
            if list(p_ix.get("column_names") or []) != p_cols:
                problems.append(
                    f"wiki_sections:partial_unique_cols={p_name}:"
                    f"{p_ix.get('column_names')}")
            dialect_opts = p_ix.get("dialect_options") or {}
            has_predicate = any(
                "where" in str(k).lower() or "where" in str(v).lower()
                for k, v in dialect_opts.items()
            )
            if not has_predicate:
                problems.append(f"wiki_sections:partial_unique_predicate={p_name}")

    return problems


def _check_compile_schema(inspector) -> list[str]:
    """Phase 4 KnowledgeCompile 表精确校验（表/列/类型/nullable/主键/索引/唯一约束/FK/ON DELETE）。
    核心表已存在（已有库）时校验；同名但错误定义必须 fail closed。仅报告问题，
    不修改数据库。校验集与 ORM 定义严格一致（含 wiki_pages 等目标表引用）。
    """
    problems: list[str] = []
    for table in sorted(MANAGED_COMPILE_TABLES):
        if not inspector.has_table(table):
            problems.append(table)
            continue

        existing_cols = {c["name"]: c for c in inspector.get_columns(table)}
        expected_col_names = set(_COMPILE_COLUMNS[table].keys())
        extra_cols = set(existing_cols.keys()) - expected_col_names
        if extra_cols:
            problems.append(f"{table}:extra_cols={sorted(extra_cols)}")
        for col, (type_str, nullable) in _COMPILE_COLUMNS[table].items():
            if col not in existing_cols:
                problems.append(f"{table}.{col}")
                continue
            colinfo = existing_cols[col]
            actual_type = str(colinfo["type"]).upper()
            if not actual_type.startswith(type_str):
                problems.append(f"{table}.{col}:type={actual_type}")
            if bool(colinfo["nullable"]) != nullable:
                problems.append(f"{table}.{col}:nullable={colinfo['nullable']}")

        pk = inspector.get_pk_constraint(table)
        pk_cols = list(pk.get("constrained_columns") or [])
        if pk_cols != [_COMPILE_PKS[table]]:
            problems.append(f"{table}:pk={pk_cols}")

        existing_indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        for col in _COMPILE_INDEXES.get(table, []):
            idx_name = _COMPILE_INDEX_NAME_OVERRIDE.get((table, col), f"ix_{table}_{col}")
            ix = existing_indexes.get(idx_name)
            if ix is None:
                problems.append(f"{table}:index={col}")
                continue
            if list(ix.get("column_names") or []) != [col]:
                problems.append(f"{table}:index_cols={col}:{ix.get('column_names')}")
            if bool(ix.get("unique")):
                problems.append(f"{table}:index_unique={col}")

        if table in _COMPILE_UNIQUE:
            expected_uniq_name, expected_uniq_cols = _COMPILE_UNIQUE[table]
            uniq_indexes = [ix for ix in inspector.get_indexes(table) if ix.get("unique")]
            matched_uniq = next(
                (ix for ix in uniq_indexes if ix.get("name") == expected_uniq_name),
                None,
            )
            if matched_uniq is None:
                problems.append(f"{table}:unique_name={expected_uniq_name}")
            elif list(matched_uniq.get("column_names") or []) != expected_uniq_cols:
                problems.append(f"{table}:unique_cols={expected_uniq_name}:{matched_uniq.get('column_names')}")

        fks = inspector.get_foreign_keys(table)
        for (tbl, col), (target, ondelete) in _COMPILE_FKS.items():
            if tbl != table:
                continue
            matched = None
            for fk in fks:
                if list(fk.get("constrained_columns") or []) == [col]:
                    matched = fk
                    break
            if matched is None:
                partial = any(
                    col in (fk.get("constrained_columns") or [])
                    for fk in fks
                )
                if partial:
                    problems.append(f"{table}.{col}:fk_constrained_columns")
                else:
                    problems.append(f"{table}.{col}:fk_missing")
                continue
            actual_target = matched.get("referred_table")
            if actual_target != target:
                problems.append(f"{table}.{col}:fk_target={actual_target}")
            referred_cols = list(matched.get("referred_columns") or [])
            if referred_cols != [_COMPILE_PKS.get(target, "id")]:
                problems.append(f"{table}.{col}:fk_referred={referred_cols}")
            actual_ondelete = (matched.get("options") or {}).get("ondelete")
            if actual_ondelete != ondelete:
                problems.append(f"{table}.{col}:ondelete={actual_ondelete}")

    return problems


# ---------------------------------------------------------------------------
# Phase 4：KnowledgeCompile schema 精确校验定义（与 ORM 模型逐列一致）
# ---------------------------------------------------------------------------

# 三张 Phase 4 表：必要列 → (SQLAlchemy 类型字符串, nullable)。
_COMPILE_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "knowledge_compile_runs": {
        "id": ("VARCHAR(36)", False),
        "pipeline_key": ("VARCHAR(64)", False),
        "pipeline_version": ("VARCHAR(64)", False),
        "trigger_type": ("VARCHAR(32)", False),
        "trigger_object_id": ("VARCHAR(36)", True),
        "source_sync_run_id": ("VARCHAR(36)", True),
        "workspace_id": ("VARCHAR(36)", True),
        "wiki_page_id": ("VARCHAR(36)", True),
        "status": ("VARCHAR(32)", False),
        "idempotency_key": ("VARCHAR(128)", True),
        "input_hash": ("VARCHAR(64)", False),
        "output_revision_id": ("VARCHAR(36)", True),
        "current_stage": ("VARCHAR(64)", True),
        "error_summary": ("TEXT", True),
        "lease_token": ("VARCHAR(36)", True),
        "worker_id": ("VARCHAR(64)", True),
        "lease_expires_at": ("DATETIME", True),
        "safe_error_code": ("VARCHAR(64)", True),
        "safe_error_message": ("TEXT", True),
        "request_fingerprint": ("VARCHAR(64)", True),
        "attempt": ("INTEGER", False),
        "max_attempts": ("INTEGER", False),
        "cancel_requested": ("BOOLEAN", True),
        "created_by": ("VARCHAR(36)", True),
        "started_at": ("DATETIME", True),
        "finished_at": ("DATETIME", True),
        "heartbeat_at": ("DATETIME", True),
        "created_at": ("DATETIME", True),
    },
    "knowledge_compile_stage_runs": {
        "id": ("VARCHAR(36)", False),
        "run_id": ("VARCHAR(36)", False),
        "stage_key": ("VARCHAR(64)", False),
        "stage_order": ("INTEGER", False),
        "status": ("VARCHAR(32)", False),
        "attempt": ("INTEGER", False),
        "input_hash": ("VARCHAR(64)", True),
        "output_hash": ("VARCHAR(64)", True),
        "component_key": ("VARCHAR(64)", True),
        "component_version": ("VARCHAR(64)", True),
        "retryable": ("BOOLEAN", True),
        "error_code": ("VARCHAR(64)", True),
        "error_message": ("TEXT", True),
        "stage_input_hash": ("VARCHAR(64)", True),
        "safe_error_code": ("VARCHAR(64)", True),
        "safe_error_message": ("TEXT", True),
        "metrics_json": ("TEXT", True),
        "parent_stage_run_id": ("VARCHAR(36)", True),
        "started_at": ("DATETIME", True),
        "finished_at": ("DATETIME", True),
        "created_at": ("DATETIME", True),
    },
    "knowledge_compile_artifacts": {
        "id": ("VARCHAR(36)", False),
        "run_id": ("VARCHAR(36)", False),
        "stage_run_id": ("VARCHAR(36)", True),
        "artifact_type": ("VARCHAR(32)", False),
        "schema_version": ("VARCHAR(32)", True),
        "object_type": ("VARCHAR(32)", True),
        "object_id": ("VARCHAR(36)", True),
        "content_hash": ("VARCHAR(64)", True),
        "payload_json": ("TEXT", True),
        "reused_from_artifact_id": ("VARCHAR(36)", True),
        "created_at": ("DATETIME", True),
    },
}

# 主键 → 列
_COMPILE_PKS: dict[str, str] = {
    "knowledge_compile_runs": "id",
    "knowledge_compile_stage_runs": "id",
    "knowledge_compile_artifacts": "id",
}

# 必须存在的普通索引 → 列（有序）。
_COMPILE_INDEXES: dict[str, list[str]] = {
    "knowledge_compile_runs": ["pipeline_key", "source_sync_run_id", "workspace_id", "wiki_page_id", "status", "request_fingerprint"],
    "knowledge_compile_stage_runs": ["run_id", "stage_key", "status"],
    "knowledge_compile_artifacts": ["run_id", "stage_run_id", "reused_from_artifact_id"],
}

# 索引名与「ix_{table}_{column}」默认规则不一致的显式命名（列名过长缩写场景）。
_COMPILE_INDEX_NAME_OVERRIDE: dict[tuple[str, str], str] = {
    ("knowledge_compile_artifacts", "reused_from_artifact_id"): "ix_knowledge_compile_artifacts_reused_from",
}

# 唯一索引 → (索引名称, 精确列顺序)。
_COMPILE_UNIQUE: dict[str, tuple[str, list[str]]] = {
    "knowledge_compile_runs": (
        "ux_knowledge_compile_runs_idempotency_key",
        ["idempotency_key"],
    ),
}

# FK：(表, 列) → (目标表, ondelete)
# reused_from_artifact_id：自引用 FK（目标同表，SET NULL）。guard 对自引用同样按
# 列/目标表/引用列/ondelete 校验；CHECK 约束 SQLite inspector 无法可靠读取，不校验。
_COMPILE_FKS: dict[tuple[str, str], tuple[str, str]] = {
    ("knowledge_compile_runs", "source_sync_run_id"): ("source_sync_runs", "SET NULL"),
    ("knowledge_compile_runs", "workspace_id"): ("wiki_workspaces", "SET NULL"),
    ("knowledge_compile_stage_runs", "run_id"): ("knowledge_compile_runs", "CASCADE"),
    ("knowledge_compile_artifacts", "run_id"): ("knowledge_compile_runs", "CASCADE"),
    ("knowledge_compile_artifacts", "stage_run_id"): ("knowledge_compile_stage_runs", "CASCADE"),
    ("knowledge_compile_artifacts", "reused_from_artifact_id"): ("knowledge_compile_artifacts", "SET NULL"),
}


def init_db(engine):
    # V4 Phase I/J-3：create_all 之前先检测「已有库缺迁移托管字段/表」。若 schema
    # 落后（如真实库停在 P35、缺 P36 图谱表），必须立即报 schema_not_ready，不得让
    # create_all 静默为落后库新建 ORM 新表（那会污染真实库、绕过 Alembic）。
    # 全新空库（表尚不存在）跳过检查，create_all 整体建表。
    missing_managed = check_managed_migrations(engine)
    if missing_managed:
        raise SchemaNotReadyError(
            "schema_not_ready: 缺少迁移托管字段或表 "
            + ", ".join(missing_managed)
            + "；请先对真实库执行「备份 → Alembic」显式升级。"
        )

    Base.metadata.create_all(engine)
    try:
        _migrate_schema(engine)
    except Exception as exc:
        logger.warning(f"schema migrate skipped: {exc}")

    # Phase C/J-3：create_all 后复查托管字段/表（全新库 create_all 后齐全）。
    missing_managed = check_managed_migrations(engine)
    if missing_managed:
        raise SchemaNotReadyError(
            "schema_not_ready: 缺少迁移托管字段或表 "
            + ", ".join(missing_managed)
            + "；请先对真实库执行「备份 → Alembic」显式升级。"
        )

    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect == "postgresql":
                conn.execute(sqlalchemy_text("CREATE EXTENSION IF NOT EXISTS vector"))

                has_embedding_vec = False
                try:
                    result = conn.execute(sqlalchemy_text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='page_chunks' AND column_name='embedding_vec'"
                    ))
                    has_embedding_vec = result.fetchone() is not None
                except Exception:
                    pass

                if not has_embedding_vec:
                    try:
                        conn.execute(sqlalchemy_text(
                            "ALTER TABLE page_chunks ADD COLUMN embedding_vec vector(1024)"
                        ))
                    except Exception:
                        pass

                try:
                    conn.execute(sqlalchemy_text(
                        "CREATE INDEX IF NOT EXISTS ix_page_chunks_embedding_hnsw "
                        "ON page_chunks USING hnsw (embedding_vec vector_cosine_ops)"
                    ))
                except Exception:
                    pass

                try:
                    conn.execute(sqlalchemy_text(
                        "UPDATE page_chunks SET embedding_vec = embedding::vector WHERE embedding IS NOT NULL AND embedding_vec IS NULL"
                    ))
                except Exception:
                    pass

                logger.info("PostgreSQL pgvector extension initialized")
    except Exception as e:
        logger.warning(f"Vector extension setup skipped: {e}")


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
