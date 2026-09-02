"""共享数据库依赖（P0-BE-07）。

统一原先散落在各 API 模块中的 get_db 重复实现：
- 进程级共享 engine（懒加载，首次使用时建表）
- 每请求独立 Session（yield 依赖，请求结束关闭）
"""
from __future__ import annotations

from app.config import settings
from app.models.database import get_engine, get_session, init_db

_engine = None


def get_shared_engine():
    """返回初始化后的进程级共享 engine（懒加载）。

    P0-BE-07：抽出各模块 get_db 中重复的懒加载逻辑。
    """
    global _engine
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    return _engine


def get_db():
    """FastAPI 依赖：每请求独立 Session。"""
    db = get_session(get_shared_engine())
    try:
        yield db
    finally:
        db.close()
