"""Wiki 可见性过滤的单元测试。"""
import json
import uuid

import pytest

from app.api.search_common import visible_wiki_filter
from app.api.wiki import _wiki_visible
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


@pytest.mark.parametrize(
    "groups",
    [
        ["__local_admin__"],  # 管理员
        ["研发部"],            # 本组页面归属者
        ["市场部"],            # 非成员(仅公共)
        [],                    # 无组用户
    ],
)
def test_SQL与Python可见性实现一致(db, groups):
    """把 SQL(visible_wiki_filter) 与 Python(_wiki_visible) 两条实现钉在一起。

    页面 fixtures:NULL / 研发部 / 财务部 / 仪表盘-只读,覆盖公共、本组、他组三种归属。
    """
    user = {"groups": groups}
    all_pages = db.query(WikiPage).all()
    sql_result = {p.title for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}
    py_result = {p.title for p in all_pages if _wiki_visible(p, user)}
    assert sql_result == py_result


# --- 端点行为(走 TestClient,见 conftest 的 api_engine/api_client/as_user) ---

def _seed_pages(engine):
    """向 api_client 共用的库里写入三页:公共 / 研发部 / 财务部。"""
    from app.models.database import get_session

    ids = {"public": str(uuid.uuid4()), "rd": str(uuid.uuid4()), "fin": str(uuid.uuid4())}
    session = get_session(engine)
    session.add_all([
        WikiPage(id=ids["public"], title=f"公共-{uuid.uuid4()}", content="x", group_id=None),
        WikiPage(id=ids["rd"], title=f"研发-{uuid.uuid4()}", content="x", group_id="研发部"),
        WikiPage(id=ids["fin"], title=f"财务-{uuid.uuid4()}", content="x", group_id="财务部"),
    ])
    session.commit()
    session.close()
    return ids


def _list_titles(client):
    data = client.get("/api/wiki").json()
    return {p["title"] for cat in data["categories"] for p in cat["pages"]}


def test_列表不含他组页面(api_engine, api_client, as_user):
    _seed_pages(api_engine)
    as_user(["研发部"])
    titles = _list_titles(api_client)
    assert len(titles) == 2
    assert any("研发-" in t for t in titles)
    assert any("公共-" in t for t in titles)
    assert all("财务-" not in t for t in titles)


def test_详情他组页面_404(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["研发部"])
    assert api_client.get(f"/api/wiki/{ids['fin']}").status_code == 404


def test_详情本组与公共_200(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["研发部"])
    res_own = api_client.get(f"/api/wiki/{ids['rd']}")
    res_public = api_client.get(f"/api/wiki/{ids['public']}")
    assert res_own.status_code == 200
    assert res_own.json()["group_id"] == "研发部"
    assert res_public.status_code == 200
    assert res_public.json()["group_id"] is None


def _seed_public_wiki_with_sources(engine):
    """公共 wiki 页,来源含一条公共笔记本笔记与一条财务部笔记本笔记。"""
    from app.models.database import Notebook, Page, get_session

    session = get_session(engine)
    session.add_all([
        Notebook(id="nb-public", name="公共笔记本", group_id=None),
        Notebook(id="nb-fin", name="财务笔记本", group_id="财务部"),
        Page(id="note-public", notebook_id="nb-public", title="公共来源笔记"),
        Page(id="note-fin", notebook_id="nb-fin", title="财务-绝密-工资表"),
        WikiPage(
            id="wiki-public",
            title="公共Wiki页面",
            content="x",
            group_id=None,
            source_note_ids=json.dumps(["note-public", "note-fin"]),
        ),
    ])
    session.commit()
    session.close()


def test_公共页来源笔记按可见性过滤(api_engine, api_client, as_user):
    """他组用户能看到公共 wiki 页(200),但看不到他组来源笔记的标题。"""
    _seed_public_wiki_with_sources(api_engine)
    as_user(["研发部"])
    res = api_client.get("/api/wiki/wiki-public")
    assert res.status_code == 200
    titles = {s["title"] for s in res.json()["sources"]}
    assert "公共来源笔记" in titles
    assert "财务-绝密-工资表" not in titles
    assert titles == {"公共来源笔记"}


def test_编辑他组页面_404(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["研发部"])
    res = api_client.put(f"/api/wiki/{ids['fin']}", json={"content": "越权"})
    assert res.status_code == 404


def test_管理员列表看到全部(api_engine, api_client, as_user):
    _seed_pages(api_engine)
    as_user(["__local_admin__"])
    assert len(_list_titles(api_client)) == 3


# --- 管理员指定归属(PUT /{page_id}/group) ---

def test_管理员可设与清空归属(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["__local_admin__"])

    # 命中 set_wiki_group 才会返回 group_id 字段;若被 PUT /{page_id} 吞掉则没有该字段
    res = api_client.put(f"/api/wiki/{ids['public']}/group", json={"group_id": "财务部"})
    assert res.status_code == 200
    assert res.json()["group_id"] == "财务部"
    assert api_client.get(f"/api/wiki/{ids['public']}").json()["group_id"] == "财务部"

    for blank in (None, "", "   "):
        res = api_client.put(f"/api/wiki/{ids['rd']}/group", json={"group_id": blank})
        assert res.status_code == 200
        assert res.json()["group_id"] is None


def test_普通用户不可指定归属(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["研发部"])
    res = api_client.put(f"/api/wiki/{ids['rd']}/group", json={"group_id": "财务部"})
    assert res.status_code == 403
