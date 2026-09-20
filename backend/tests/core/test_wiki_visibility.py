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


def test_编辑他组页面_404(api_engine, api_client, as_user):
    ids = _seed_pages(api_engine)
    as_user(["研发部"])
    res = api_client.put(f"/api/wiki/{ids['fin']}", json={"content": "越权"})
    assert res.status_code == 404


def test_管理员列表看到全部(api_engine, api_client, as_user):
    _seed_pages(api_engine)
    as_user(["__local_admin__"])
    assert len(_list_titles(api_client)) == 3
