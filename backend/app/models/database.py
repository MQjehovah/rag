from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey, Boolean, inspect
from sqlalchemy import text as sqlalchemy_text
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
    group_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class Page(Base):
    __tablename__ = 'pages'
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey('notebooks.id'), nullable=True)
    title = Column(String(255), nullable=False, default='无标题')
    content = Column(Text, default='')
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class GraphEdge(Base):
    __tablename__ = 'graph_edges'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(36), ForeignKey('pages.id'), nullable=False, index=True)
    target_id = Column(String(36), ForeignKey('pages.id'), nullable=False, index=True)
    weight = Column(String(20), default='1.0')
    edge_type = Column(String(50), default='similarity')
    created_at = Column(DateTime, default=datetime.now)

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
    group_name = Column(String(255), nullable=False)

def get_engine(database_url: str = "sqlite:///./data/notes.db"):
    if database_url.startswith("sqlite"):
        os.makedirs("./data", exist_ok=True)
        return create_engine(
            database_url.replace("sqlite:///", "sqlite:///"),
            connect_args={"check_same_thread": False}
        )
    return create_engine(database_url, pool_pre_ping=True)

def _migrate_schema(engine):
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        expected = {col.name for col in table.columns}
        if existing != expected:
            missing = expected - existing
            extra = existing - expected
            if missing and not extra:
                for col in table.columns:
                    if col.name in missing:
                        alter_sql = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
                        if not col.nullable and col.server_default is None:
                            alter_sql += " DEFAULT ''"
                        with engine.begin() as conn:
                            conn.execute(sqlalchemy_text(alter_sql))
                        logger.info(f"Added column {col.name} to table {table.name}")
            else:
                with engine.begin() as conn:
                    conn.execute(sqlalchemy_text(f'DROP TABLE IF EXISTS {table.name}'))
                Base.metadata.tables[table.name].create(engine)
                logger.warning(f"Rebuilt table {table.name}: schema mismatch (missing={missing}, extra={extra})")

def init_db(engine):
    Base.metadata.create_all(engine)
    _migrate_schema(engine)

def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()