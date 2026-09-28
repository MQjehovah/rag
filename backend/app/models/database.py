from sqlalchemy import create_engine, event, Column, String, Text, DateTime, ForeignKey, Boolean, Integer, Float, Index, UniqueConstraint, inspect, text as sqlalchemy_text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
import os

import logging

logger = logging.getLogger(__name__)

# 实体化的「默认空间」名称(未指定空间的 wiki 产物归此)
DEFAULT_SPACE_NAME = "默认空间"

Base = declarative_base()


class Notebook(Base):
    __tablename__ = 'notebooks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, default='')
    # 笔记本图标(emoji)
    icon = Column(String(32), default='')
    group_id = Column(String(255), nullable=True, index=True)
    # 可见性(self 仅本人 / dept 部门组 / public 公开); NULL = legacy(按 group_id 旧语义)
    visibility = Column(String(16), nullable=True)
    # 创建者(user id); visibility=self 时用于判定
    owner_id = Column(String(36), nullable=True, index=True)
    # 侧边栏排序位次与分组名(用户自定义)
    position = Column(Integer, default=0)
    section = Column(String(128), default='')
    # 该笔记本使用的嵌入模型档案;NULL 表示使用系统默认档案
    embedding_profile_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Page(Base):
    __tablename__ = 'pages'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey('notebooks.id', ondelete='SET NULL'), nullable=True, index=True)
    title = Column(String(255), nullable=False, default='无标题')
    content = Column(Text, default='')
    # 页面图标(emoji)与封面图 URL/渐变(Notion 风格)
    icon = Column(String(32), default='')
    cover = Column(Text, default='')
    # 封面纵向位置(0-100, 百分比)
    cover_offset = Column(Integer, default=50)
    # 视图类型: doc(文档) | table | board | calendar —— 非 doc 时把子页面作为数据行展示
    view_type = Column(String(16), default='doc')
    # 子页面作为数据行时的状态字段
    status = Column(String(32), default='')
    # 页面树: 父页面 + 同级排序位次
    parent_id = Column(String(36), ForeignKey('pages.id', ondelete='SET NULL'), nullable=True, index=True)
    position = Column(Integer, default=0)
    # 软删除(回收站); NULL 表示正常
    deleted_at = Column(DateTime, nullable=True, index=True)
    # 公开分享令牌; 非空表示已发布为只读分享链接
    share_token = Column(String(64), nullable=True, index=True)
    keywords = Column(Text, default='')
    term_count = Column(Integer, nullable=True, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)


class PageRevision(Base):
    """页面历史版本快照(用于查看与回滚)。"""
    __tablename__ = 'page_revisions'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    title = Column(String(255), default='')
    content = Column(Text, default='')
    editor = Column(String(255), default='')
    created_at = Column(DateTime, default=datetime.now)


class PageComment(Base):
    """页面级评论(作者/内容/是否已解决)。"""
    __tablename__ = 'page_comments'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    author_id = Column(String(36), default='')
    author_name = Column(String(255), default='')
    content = Column(Text, default='')
    resolved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class PageChunk(Base):
    __tablename__ = 'page_chunks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(Text, nullable=True)
    context = Column(Text, nullable=True)
    # 该向量由哪个嵌入档案生成(检索时按档案分组比对,避免不同模型的向量互比)
    embedding_profile = Column(String(36), nullable=True, index=True)

    __table_args__ = (
        Index('ix_page_chunks_page_idx', 'page_id', 'chunk_index'),
    )


class PageTerm(Base):
    __tablename__ = 'page_terms'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    term = Column(String(128), nullable=False, index=True)
    tf = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index('ix_page_terms_page_term', 'page_id', 'term'),
        Index('ix_page_terms_term_page', 'term', 'page_id'),
    )


class GraphEdge(Base):
    __tablename__ = 'graph_edges'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    target_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    weight = Column(Float, default=1.0)
    edge_type = Column(String(50), default='similarity')
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_graph_edges_pair', 'source_id', 'target_id'),
    )


class GraphEntity(Base):
    __tablename__ = 'graph_entities'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, index=True)
    entity_type = Column(String(64), nullable=True, default='')
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    properties = Column(Text, nullable=True, default='')
    created_at = Column(DateTime, default=datetime.now)


class GraphEntityEdge(Base):
    __tablename__ = 'graph_entity_edges'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_entity_id = Column(String(36), ForeignKey('graph_entities.id', ondelete='CASCADE'), nullable=False, index=True)
    target_entity_id = Column(String(36), ForeignKey('graph_entities.id', ondelete='CASCADE'), nullable=False, index=True)
    relation = Column(String(255), nullable=True, default='')
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    weight = Column(Float, default=1.0)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_graph_entity_edges_pair', 'source_entity_id', 'target_entity_id'),
    )


class WikiPage(Base):
    """LLM-generated wiki pages distilled from notes (read-only for users)."""
    __tablename__ = 'wiki_pages'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 标题在"空间内"唯一(复合唯一索引见 __table_args__); 跨空间可同名
    title = Column(String(255), nullable=False, index=True)
    category = Column(String(128), nullable=True, default='', index=True)
    group_id = Column(String(255), nullable=True, index=True)
    content = Column(Text, default='')
    summary = Column(Text, default='')
    embedding = Column(Text, nullable=True)
    source_note_ids = Column(Text, default='[]')
    # 所属空间(NULL=默认空间)
    space_id = Column(String(36), nullable=True, index=True)
    # 树状层级: 父页面 + 同级排序位次
    parent_id = Column(String(36), nullable=True, index=True)
    position = Column(Integer, default=0)
    # 由哪个编译管道产出(NULL=经典 wiki 蒸馏管道)
    pipeline_id = Column(String(36), nullable=True, index=True)
    # 编译单元键(pipeline_id + 来源单元),用于增量 upsert
    source_key = Column(String(255), nullable=True, index=True)
    embedding_profile = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # 标题在空间内唯一(跨空间可同名); 旧库由迁移脚本重建该索引
        Index('uq_wiki_pages_space_title', 'space_id', 'title', unique=True),
    )


class WikiSpace(Base):
    """知识库空间(类似 Docmost/Outline 的 Space): 顶层分组。"""
    __tablename__ = 'wiki_spaces'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    icon = Column(String(32), default='')
    description = Column(Text, default='')
    position = Column(Integer, default=0)
    group_id = Column(String(255), nullable=True, index=True)
    # 可见性(self 仅本人 / dept 部门组 / public 公开); NULL = legacy(按 group_id 旧语义)
    visibility = Column(String(16), nullable=True)
    # 创建者(user id); visibility=self 时用于判定
    owner_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)


class ResourceAcl(Base):
    """资源级追加授权: 多选用户/部门(组)。"""
    __tablename__ = 'resource_acl'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    resource_type = Column(String(20), nullable=False, index=True)   # 'notebook' | 'wiki_space'
    resource_id = Column(String(36), nullable=False, index=True)
    subject_type = Column(String(10), nullable=False)                # 'user' | 'group'
    subject_id = Column(String(255), nullable=False)                 # user.id 或 group 名

    __table_args__ = (
        UniqueConstraint('resource_type', 'resource_id', 'subject_type', 'subject_id', name='uq_resource_acl'),
    )


class GraphCommunity(Base):
    """GraphRAG community summaries (global Q&A layer)."""
    __tablename__ = 'graph_communities'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    level = Column(Integer, default=1)
    title = Column(String(255), default='')
    summary = Column(Text, default='')
    member_ids = Column(Text, default='[]')
    embedding = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ImageAsset(Base):
    """Image assets extracted from notes (multimodal pre-support, off by
    default).  When multimodal is enabled, OCR/caption/embedding fill in."""
    __tablename__ = 'image_assets'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=True, index=True)
    url = Column(Text, nullable=False)
    alt = Column(Text, default='')
    chunk_index = Column(Integer, default=0)
    ocr_text = Column(Text, default='')
    caption = Column(Text, default='')
    embedding = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class JiraIssue(Base):
    """Sync state for Jira issues imported into notes (dedupe + incremental)."""
    __tablename__ = 'jira_issues'

    issue_key = Column(String(64), primary_key=True)
    page_id = Column(String(36), nullable=True)
    jira_updated = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class SourceItem(Base):
    """Generic dedupe/sync state for data-source plugin imports."""
    __tablename__ = 'source_items'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_key = Column(String(64), nullable=False, index=True)
    item_key = Column(String(128), nullable=False)
    page_id = Column(String(36), nullable=True)
    source_updated = Column(DateTime, nullable=True)
    skipped = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_source_items_source_item', 'source_key', 'item_key', unique=True),
    )


class User(Base):
    __tablename__ = 'users'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), default="")
    name = Column(String(255), default="")
    # 工号(SSO sub);历史上 username 即工号,故迁移时按 username 回填
    work_id = Column(String(64), default="", index=True)
    phone = Column(String(32), default="")
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


class Role(Base):
    """RBAC 角色:permissions 为权限键 JSON 列表; 内置 admin = ["*"]。"""
    __tablename__ = 'roles'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(64), unique=True, nullable=False)
    display_name = Column(String(128), default='')
    permissions = Column(Text, default='[]')  # JSON list[str]
    is_system = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class UserRole(Base):
    __tablename__ = 'user_roles'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    role_id = Column(String(36), ForeignKey('roles.id', ondelete='CASCADE'), nullable=False, index=True)

    __table_args__ = (
        Index('uq_user_roles_user_role', 'user_id', 'role_id', unique=True),
    )


class Group(Base):
    """组注册表:登记可用的组名(资源可见域仍存自由文本 group_id; 本表用于管理/下拉)。"""
    __tablename__ = 'groups'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), unique=True, nullable=False)
    source = Column(String(16), default='local')  # local | ldap | sso
    created_at = Column(DateTime, default=datetime.now)


class EmbeddingProfile(Base):
    """可配置的嵌入模型档案：api_url + model + dimensions + 鉴权。

    ``kind``: openai(OpenAI 兼容 /v1/embeddings) | ollama(/api/embed)。
    ``is_default``: 全局默认档案(也作为新建笔记本的缺省)。
    """
    __tablename__ = 'embedding_profiles'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(128), nullable=False)
    kind = Column(String(16), default='openai')  # openai | ollama
    api_url = Column(String(512), nullable=False)
    api_key = Column(String(512), default='')
    model = Column(String(255), nullable=False)
    dimensions = Column(Integer, default=1024)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class CompileTemplate(Base):
    """编译模板库：可单独维护的「编译规则」(提示词/规则/输出模板), 供管道选用。"""
    __tablename__ = 'compile_templates'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, default='')
    compiler_kind = Column(String(16), default='wiki')
    prompt = Column(Text, default='')      # 提示词(角色+目标)
    rules = Column(Text, default='')       # 规则(约束)
    template = Column(Text, default='')    # 输出模板(正文结构)
    group_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Pipeline(Base):
    """编译管道：把某范围笔记本里的笔记编译成 wiki 页（或其它格式并入 wiki 浏览）。"""
    __tablename__ = 'pipelines'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, default='')
    # 来源范围: notebooks(指定笔记本) | group(本组全部) | all(全部可见)
    scope_type = Column(String(16), default='notebooks')
    notebook_ids = Column(Text, default='[]')  # JSON list[str]
    # 编译方式: wiki(蒸馏) | api_doc(接口文档) | markdown(合集) | changelog(变更记录) | custom
    compiler_kind = Column(String(16), default='wiki')
    # 选用的编译模板库条目(可空); 选中后其 kind/prompt/rules/template 作为缺省, 本管道字段可覆盖
    template_id = Column(String(36), nullable=True, index=True)
    # 编译规则(三段): 提示词(角色+目标) / 规则(约束) / 输出模板(正文结构); 留空用内置
    prompt_template = Column(Text, default='')
    compile_rules = Column(Text, default='')
    compile_template = Column(Text, default='')
    model = Column(String(255), default='')     # 留空用全局 LLM
    target_category = Column(String(128), default='')  # 编译产物在 wiki 的分类
    # 目标空间(NULL=默认空间): 产物写入该空间, 页面层级由 LLM/prompt 决定
    target_space_id = Column(String(36), nullable=True, index=True)
    # 笔记变更时自动编译(逐条笔记 ingest, 按笔记+管道限流)
    auto_trigger = Column(Boolean, default=False)
    incremental = Column(Boolean, default=True)  # 仅编译自上次运行后有更新的笔记
    group_id = Column(String(255), nullable=True, index=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class PipelineRun(Base):
    """编译管道的一次运行记录(状态/进度/变更数)。"""
    __tablename__ = 'pipeline_runs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id = Column(String(36), nullable=False, index=True)
    status = Column(String(16), default='running')  # running | success | failed
    processed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    changed = Column(Integer, default=0)
    message = Column(Text, default='')
    error = Column(Text, default='')
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)


def get_engine(database_url: str):
    connect_args = {}
    pool_kwargs = {"pool_pre_ping": True, "pool_size": 20, "max_overflow": 10}
    if database_url.startswith("sqlite"):
        os.makedirs("./data", exist_ok=True)
        # SQLite: one fresh connection per session (no cross-thread sharing)
        # and wait up to 60s for a busy database instead of the default 5s.
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 60
        pool_kwargs = {"poolclass": NullPool}
    engine = create_engine(database_url, connect_args=connect_args, **pool_kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            # WAL allows readers while a writer is active; without it one
            # writer blocks every read (and vice versa) -> "database is locked".
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.close()

    return engine


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
                    col_type = col.type.compile(engine.dialect)
                    alter_sql = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}'
                    if not col.nullable and col.server_default is None:
                        alter_sql += " DEFAULT ''"
                    with engine.begin() as conn:
                        conn.execute(sqlalchemy_text(alter_sql))
                    logger.info(f"Added column {col.name} to table {table.name}")


def _ensure_wiki_group_index(engine):
    """为已存在的 wiki_pages 补 group_id 索引(建表路径由 create_all 覆盖)。"""
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy_text(
                "CREATE INDEX IF NOT EXISTS ix_wiki_pages_group_id ON wiki_pages (group_id)"
            ))
    except Exception:
        # 索引不是正确性前提,失败不阻断启动
        logger.warning("创建 wiki_pages.group_id 索引失败", exc_info=True)


def _ensure_wiki_embedding_column(engine):
    """Postgres 上为 wiki_pages 补 embedding_vec 向量列与 HNSW 索引(裸 DDL,失败不阻断)。

    SQLite 等方言整段 no-op。JSON 文本 embedding → embedding_vec 的一次性回填
    与 page_chunks 同法,便于存量页面在新列建立前也能被检索。
    """
    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(sqlalchemy_text(
                    "ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS embedding_vec vector(1024)"
                ))
                conn.execute(sqlalchemy_text(
                    "CREATE INDEX IF NOT EXISTS ix_wiki_pages_embedding_hnsw "
                    "ON wiki_pages USING hnsw (embedding_vec vector_cosine_ops)"
                ))
                conn.execute(sqlalchemy_text(
                    "UPDATE wiki_pages SET embedding_vec = embedding::vector "
                    "WHERE embedding IS NOT NULL AND embedding_vec IS NULL"
                ))
    except Exception:
        logger.warning("初始化 wiki_pages 向量列失败", exc_info=True)



def _backfill_text_defaults(engine):
    """补齐迁移新增的文本列在历史行上的 NULL(否则响应模型校验失败)。"""
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy_text(
                "UPDATE notebooks SET description = '' WHERE description IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET icon = '' WHERE icon IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET cover = '' WHERE cover IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET position = 0 WHERE position IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET cover_offset = 50 WHERE cover_offset IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET status = '' WHERE status IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE pages SET view_type = 'doc' WHERE view_type IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE notebooks SET position = 0 WHERE position IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE notebooks SET section = '' WHERE section IS NULL"
            ))
            conn.execute(sqlalchemy_text(
                "UPDATE notebooks SET icon = '' WHERE icon IS NULL"
            ))
    except Exception:
        logger.warning("回填文本列默认值失败", exc_info=True)


def run_user_column_migrations(engine):
    """users 表幂等迁移(PG): display_name→name, 补 work_id(按 username 回填工号)与 phone(无回填)。

    SQLite(测试/本地)直接跳过。必须在 _migrate_schema 之前调用, 否则它会
    先把缺失的 name 列以空值补上, 导致 RENAME 因目标列已存在而被跳过、旧数据丢失。
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        # 多 worker 并发启动时串行化迁移, 避免同时 ALTER 互相打断
        conn.execute(sqlalchemy_text("SELECT pg_advisory_xact_lock(839201002)"))
        cols = {
            row[0]
            for row in conn.execute(sqlalchemy_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='users' AND table_schema = current_schema()"
            )).fetchall()
        }
        if "name" not in cols and "display_name" in cols:
            conn.execute(sqlalchemy_text("ALTER TABLE users RENAME COLUMN display_name TO name"))
        if "work_id" not in cols:
            conn.execute(sqlalchemy_text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS work_id VARCHAR(64) DEFAULT ''"
            ))
            # 旧数据 username 即工号: 仅加列当次回填, 不覆盖后续人工清空
            conn.execute(sqlalchemy_text(
                "UPDATE users SET work_id = username WHERE work_id = '' OR work_id IS NULL"
            ))
        if "phone" not in cols:
            conn.execute(sqlalchemy_text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32) DEFAULT ''"
            ))
        conn.execute(sqlalchemy_text(
            "CREATE INDEX IF NOT EXISTS ix_users_work_id ON users(work_id)"
        ))


def init_db(engine):
    Base.metadata.create_all(engine)
    run_user_column_migrations(engine)
    _migrate_schema(engine)
    _backfill_text_defaults(engine)
    _ensure_wiki_group_index(engine)

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

    # 必须在 pgvector 扩展建立之后调用(vector 类型/ops 此时才可用)
    _ensure_wiki_embedding_column(engine)


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
