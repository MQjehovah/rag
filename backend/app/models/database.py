from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey, Boolean, Integer, Float, Index, inspect, text as sqlalchemy_text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
import os
import hashlib

import logging

logger = logging.getLogger(__name__)

Base = declarative_base()


class Notebook(Base):
    __tablename__ = 'notebooks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    group_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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


def get_engine(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        os.makedirs("./data", exist_ok=True)
        connect_args["check_same_thread"] = False
    return create_engine(database_url, pool_pre_ping=True, pool_size=20, max_overflow=10, connect_args=connect_args)


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


def init_db(engine):
    Base.metadata.create_all(engine)
    _migrate_schema(engine)

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
