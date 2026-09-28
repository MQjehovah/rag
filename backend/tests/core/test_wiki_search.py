"""wiki 语义检索(向量)测试。"""
import json

import pytest
from sqlalchemy import text

from app.api.search_common import visible_wiki_filter
from app.core.wiki_search import _visibility_sql, search_wiki
from app.models.database import ResourceAcl, WikiPage, WikiSpace, get_engine, get_session, init_db

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


def _seed_spaces(db):
    """空间维度 fixtures:三种策略 + legacy(公共/研发),页面覆盖空间与 group 两种归属。"""
    db.add_all([
        WikiSpace(id="sp-pub", name="公开空间", visibility="public"),
        WikiSpace(id="sp-rd", name="研发空间", visibility="dept", group_id="研发部"),
        WikiSpace(id="sp-self", name="私人空间", visibility="self", owner_id="u-owner"),
        WikiSpace(id="sp-legacy-pub", name="旧公共空间", visibility=None, group_id=None),
        WikiSpace(id="sp-legacy-rd", name="旧研发空间", visibility=None, group_id="研发部"),
        WikiPage(id="wsp-pub", title="公开空间页", content="x", space_id="sp-pub",
                 group_id=None, embedding=json.dumps(_vec(0, 1))),
        WikiPage(id="wsp-rd", title="研发空间页", content="x", space_id="sp-rd",
                 group_id=None, embedding=json.dumps(_vec(0, 1, 2))),
        WikiPage(id="wsp-rd-fin", title="研发空间财务页", content="x", space_id="sp-rd",
                 group_id="财务部", embedding=json.dumps(_vec(0, 1, 2, 3))),
        WikiPage(id="wsp-self", title="私人空间页", content="x", space_id="sp-self",
                 group_id=None, embedding=json.dumps(_vec(0, 1, 2, 3, 4))),
        WikiPage(id="wsp-legacy-rd", title="旧研发空间页", content="x", space_id="sp-legacy-rd",
                 group_id=None, embedding=json.dumps(_vec(0, 1, 2, 3, 4, 5))),
        WikiPage(id="wsp-nospace", title="无空间页", content="x", space_id=None,
                 group_id=None, embedding=json.dumps(_vec(0, 1, 2, 3, 4, 5, 6))),
    ])
    db.commit()


@pytest.mark.parametrize(
    "user",
    [
        {"groups": ["__local_admin__"]},
        {"groups": [], "permissions": ["*"]},
        {"groups": ["研发部"]},
        {"groups": ["财务部"]},
        {"groups": []},
        {"groups": ["研发部"], "id": "u-rd"},
        {"groups": [], "id": "u-owner"},          # self 空间 owner
        {"groups": ["研发部"], "id": "u-owner"},  # self 空间 owner 同时同组
    ],
)
def test_空间派生可见性双实现一致(db, user):
    """页面可见性 = 页面 group 规则 AND 所属空间可见;裸 SQL 与 ORM 必须一致。"""
    _seed(db)
    _seed_spaces(db)

    orm_ids = {p.id for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}
    cond, params = _visibility_sql(user)
    rows = db.execute(text(f"SELECT id FROM wiki_pages WHERE {cond}"), params).fetchall()
    assert {r[0] for r in rows} == orm_ids


def test_空间可见性语义_研发部用户(db):
    """研发部:可见公开/研发/旧研发空间页及无空间页;私人空间与页面 group=财务部不可见。"""
    _seed(db)
    _seed_spaces(db)
    ids = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user={"groups": ["研发部"]})}
    assert {"wsp-pub", "wsp-rd", "wsp-legacy-rd", "wsp-nospace"} <= ids
    assert "wsp-self" not in ids
    assert "wsp-rd-fin" not in ids


def test_空间可见性语义_财务部用户页面规则仍生效(db):
    """财务部:公开空间可见,但研发空间页不可见;公开空间+页面组=财务部... 反之亦然。"""
    _seed(db)
    _seed_spaces(db)
    ids = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user={"groups": ["财务部"]})}
    assert {"wsp-pub", "wsp-nospace"} <= ids
    assert "wsp-rd" not in ids        # 研发空间不可见
    assert "wsp-rd-fin" not in ids    # 页面组对了, 但空间不可见
    assert "wsp-self" not in ids


def test_空间ACL双实现一致(db):
    """空间 ACL 追加授权下,裸 SQL(含 resource_acl 子查询)与 ORM 条件必须一致。"""
    _seed(db)
    _seed_spaces(db)
    db.add_all([
        ResourceAcl(resource_type="wiki_space", resource_id="sp-self",
                    subject_type="user", subject_id="u-owner2"),
        ResourceAcl(resource_type="wiki_space", resource_id="sp-rd",
                    subject_type="group", subject_id="财务部"),
    ])
    db.commit()
    for user in (
        {"groups": ["研发部"]},
        {"groups": ["财务部"]},
        {"groups": [], "id": "u-owner2"},
        {"groups": [], "id": "u-owner"},
        {"groups": [], "permissions": ["*"]},
    ):
        orm_ids = {p.id for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}
        cond, params = _visibility_sql(user)
        rows = db.execute(text(f"SELECT id FROM wiki_pages WHERE {cond}"), params).fetchall()
        assert {r[0] for r in rows} == orm_ids, user


def test_空间ACL检索命中(db):
    """空间 ACL 授权用户经语义检索也能命中该空间页面。"""
    _seed(db)
    _seed_spaces(db)
    db.add(ResourceAcl(resource_type="wiki_space", resource_id="sp-self",
                       subject_type="group", subject_id="财务部"))
    db.commit()
    hits = {r["id"] for r in search_wiki(db, _vec(0), limit=50, current_user={"groups": ["财务部"]})}
    assert "wsp-self" in hits
