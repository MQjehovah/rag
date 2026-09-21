"""wiki 语义检索(向量)测试。"""
import json

import pytest
from sqlalchemy import text

from app.api.search_common import visible_wiki_filter
from app.core.wiki_search import _visibility_sql, search_wiki
from app.models.database import WikiPage, get_engine, get_session, init_db

DIM = 1024


def _vec(*idxs):
    v = [0.0] * DIM
    for i in idxs:
        v[i] = 1.0
    return v


@pytest.fixture
def db(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'search.db'}")
    init_db(engine)
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


def _seed(db):
    """公共/研发部/财务部各一页,向量构造使查询 e0 的相关度依次递减。"""
    db.add_all([
        WikiPage(id="public", title="公共页", summary="公共摘要", content="公共正文",
                 category="公共", group_id=None, embedding=json.dumps(_vec(0))),
        WikiPage(id="rd", title="研发页", summary="研发摘要", content="研发正文",
                 category="研发", group_id="研发部", embedding=json.dumps(_vec(0, 1))),
        WikiPage(id="fin", title="财务页", summary="财务摘要", content="财务正文",
                 category="财务", group_id="财务部", embedding=json.dumps(_vec(0, 1, 2))),
    ])
    db.commit()


def _ids(results):
    return [r["id"] for r in results]


def test_研发部用户只看到公共与本组(db):
    _seed(db)
    results = search_wiki(db, _vec(0), limit=10, current_user={"groups": ["研发部"]})
    assert set(_ids(results)) == {"public", "rd"}


def test_管理员看到全部(db):
    _seed(db)
    results = search_wiki(db, _vec(0), limit=10, current_user={"groups": ["__local_admin__"]})
    assert set(_ids(results)) == {"public", "rd", "fin"}


def test_按得分降序返回且字段完整(db):
    _seed(db)
    results = search_wiki(db, _vec(0), limit=10, current_user={"groups": ["__local_admin__"]})
    assert _ids(results) == ["public", "rd", "fin"]
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    first = results[0]
    assert first["title"] == "公共页"
    assert first["summary"] == "公共摘要"
    assert first["content"] == "公共正文"
    assert first["category"] == "公共"
    assert first["distance"] == pytest.approx(0.0, abs=1e-9)


def test_limit_top_k(db):
    _seed(db)
    results = search_wiki(db, _vec(0), limit=2, current_user={"groups": ["__local_admin__"]})
    assert _ids(results) == ["public", "rd"]


def test_无向量页面被排除(db):
    _seed(db)
    db.add(WikiPage(id="nov", title="无向量", content="x", group_id=None, embedding=None))
    db.commit()
    results = search_wiki(db, _vec(0), limit=10, current_user={"groups": ["__local_admin__"]})
    assert "nov" not in _ids(results)


def test_坏向量被跳过(db):
    _seed(db)
    db.add(WikiPage(id="bad", title="坏向量", content="x", group_id=None, embedding="not-json"))
    db.add(WikiPage(id="zero", title="零向量", content="x", group_id=None, embedding=json.dumps([0.0] * DIM)))
    db.commit()
    results = search_wiki(db, _vec(0), limit=10, current_user={"groups": ["__local_admin__"]})
    assert set(_ids(results)) == {"public", "rd", "fin"}


@pytest.mark.parametrize("query", [[], [0.0] * DIM])
def test_空或零查询向量返回空(db, query):
    _seed(db)
    assert search_wiki(db, query, limit=10, current_user={"groups": ["__local_admin__"]}) == []


# --- 可见性双实现等价性(裸 SQL 与 ORM 条件钉在一起) ---

@pytest.mark.parametrize(
    "groups",
    [["__local_admin__"], ["研发部"], ["市场部"], [], ["研发部", "财务部"]],
)
def test_裸SQL与ORM可见性实现一致(db, groups):
    """把 _visibility_sql(裸 SQL) 与 visible_wiki_filter(ORM) 两条实现钉在一起。

    页面 fixtures:NULL / 研发部 / 财务部 / 仪表盘-只读,覆盖公共、本组、他组归属。
    """
    _seed(db)
    db.add(WikiPage(id="dash", title="仪表盘", content="x", group_id="仪表盘-只读"))
    db.commit()

    user = {"groups": groups}
    orm_ids = {p.id for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}

    cond, params = _visibility_sql(user)
    rows = db.execute(text(f"SELECT id FROM wiki_pages WHERE {cond}"), params).fetchall()
    assert {r[0] for r in rows} == orm_ids
