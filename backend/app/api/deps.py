from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import get_engine, get_session, init_db

_engine = None


def get_db():
    """Yield a fresh SQLAlchemy session per request.

    FastAPI runs sync endpoints in a threadpool, so sharing a single global
    Session across threads would be unsafe.  Each request gets its own
    connection-backed session, closed when the request finishes.
    """
    global _engine
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    db: Session = get_session(_engine)
    try:
        yield db
    finally:
        db.close()
