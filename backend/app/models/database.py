from sqlalchemy import create_engine, Column, String, Text, DateTime, ForeignKey, inspect
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid
import os

Base = declarative_base()

class Notebook(Base):
    __tablename__ = 'notebooks'
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
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

def get_engine(database_url: str = "sqlite:///./data/notes.db"):
    if database_url.startswith("sqlite"):
        os.makedirs("./data", exist_ok=True)
        return create_engine(
            database_url.replace("sqlite:///", "sqlite:///"),
            connect_args={"check_same_thread": False}
        )
    return create_engine(database_url, pool_pre_ping=True)

def init_db(engine):
    Base.metadata.create_all(engine)

def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()