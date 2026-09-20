"""Wiki 可见性过滤的单元测试。"""
import uuid

import pytest

from app.api.search_common import visible_wiki_filter
from app.models.database import WikiPage


def _mk(group_id):
    return WikiPage(id=str(uuid.uuid4()), title=f"t-{uuid.uuid4()}", content="x", group_id=group_id)


@pytest.fixture
def db(tmp_path):
    from app.models.database import get_engine, get_session, init_db

    engine = get_engine(f"sqlite:///{tmp_path / 'wiki.db'}")
    init_db(engine)
    session = get_session(engine)
    session.add_all([_mk(None), _mk("研发部"), _mk("财务部"), _mk("仪表盘-只读")])
    session.commit()
    yield session
    session.close()


def _titles(db, user):
    return {p.title for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}


def test_普通用户看到本组与公共(db):
    rows = db.query(WikiPage).all()
    mine = {p.title for p in rows if p.group_id is None or p.group_id == "研发部"}
    assert _titles(db, {"groups": ["研发部"]}) == mine


def test_无组用户只看到公共(db):
    public = {p.title for p in db.query(WikiPage).filter(WikiPage.group_id.is_(None)).all()}
    assert _titles(db, {"groups": []}) == public


def test_管理员不过滤(db):
    assert _titles(db, {"groups": ["__local_admin__"]}) == {p.title for p in db.query(WikiPage).all()}
