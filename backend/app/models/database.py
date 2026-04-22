from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey, Boolean, Integer, Float, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
import os

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
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)


class PageChunk(Base):
    __tablename__ = 'page_chunks'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    page_id = Column(String(36), ForeignKey('pages.id', ondelete='CASCADE'), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
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


def init_db(engine):
    Base.metadata.create_all(engine)

    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect == "postgresql":
                from sqlalchemy import text
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

                table_name = "page_chunks"
                has_embedding_col = False
                try:
                    result = conn.execute(text(
                        f"SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name='{table_name}' AND column_name='embedding_vec'"
                    ))
                    has_embedding_col = result.fetchone() is not None
                except Exception:
                    pass

                if not has_embedding_col:
                    try:
                        conn.execute(text(
                            "ALTER TABLE page_chunks ADD COLUMN embedding_vec vector(1024)"
                        ))
                    except Exception:
                        pass

                try:
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_page_chunks_embedding_hnsw "
                        "ON page_chunks USING hnsw (embedding_vec vector_cosine_ops)"
                    ))
                except Exception:
                    pass

                try:
                    conn.execute(text(
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
