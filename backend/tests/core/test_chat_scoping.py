"""chat 权限收敛测试：_get_kb_context 与 GraphRAG 社区按组过滤。

数据形态：研发部 / 财务部 各一条笔记本+笔记+实体+社区，另加一条公共数据。
社区可见性规则与笔记本一致：本组 OR 公共。
"""
import json

from app.api.chat import _get_kb_context
from app.api.search_common import get_visible_page_ids
from app.core.graphrag import search_communities
from app.models.database import (
    GraphCommunity,
    GraphEntity,
    Notebook,
    Page,
    get_session,
)

# 两个社区用同一向量，保证不过滤时都能被检索到，从而凸显过滤是否生效
EMB = json.dumps([1.0, 0.0])


def _seed(engine):
    """写入研发/财务/公共三套数据，返回一个可复用的 session。"""
    db = get_session(engine)
    db.add_all([
        Notebook(id="nb-a", name="研发笔记本", group_id="研发部"),
        Notebook(id="nb-b", name="财务笔记本", group_id="财务部"),
        Notebook(id="nb-pub", name="公共笔记本", group_id=None),
        Page(id="page-a", notebook_id="nb-a", title="研发-甲"),
        Page(id="page-b", notebook_id="nb-b", title="财务-乙"),
        Page(id="page-pub", notebook_id="nb-pub", title="公共-丙"),
        GraphEntity(id="ent-a", name="实体甲", page_id="page-a"),
        GraphEntity(id="ent-b", name="实体乙", page_id="page-b"),
        GraphEntity(id="ent-pub", name="实体丙", page_id="page-pub"),
        GraphCommunity(id="comm-a", title="研发社区", summary="研发摘要",
                       member_ids=json.dumps(["ent-a"]), embedding=EMB),
        GraphCommunity(id="comm-b", title="财务社区", summary="财务摘要",
                       member_ids=json.dumps(["ent-b"]), embedding=EMB),
        GraphCommunity(id="comm-pub", title="公共社区", summary="公共摘要",
                       member_ids=json.dumps(["ent-pub"]), embedding=EMB),
    ])
    db.commit()
    return db


def _community_ids(db, visible):
    return {c["id"] for c in search_communities(db, [1.0, 0.0], top_k=5, visible_page_ids=visible)}


def test_search_communities_filters_to_visible_group(api_engine, as_user):
    """研发部用户只应看到研发社区与公共社区，绝不能看到财务社区。"""
    db = _seed(api_engine)
    try:
        user = as_user(["研发部"])
        visible = get_visible_page_ids(db, user)
        assert visible == {"page-a", "page-pub"}
        ids = _community_ids(db, visible)
        assert "comm-a" in ids
        assert "comm-pub" in ids
        assert "comm-b" not in ids
    finally:
        db.close()


def test_search_communities_without_filter_returns_all(api_engine):
    """对照组：不传可见集合时三个社区都应命中，证明过滤确实由新参数驱动。"""
    db = _seed(api_engine)
    try:
        ids = {c["id"] for c in search_communities(db, [1.0, 0.0], top_k=5)}
        assert ids == {"comm-a", "comm-b", "comm-pub"}
    finally:
        db.close()


def test_search_communities_empty_visible_returns_nothing(api_engine):
    """空可见集合（无任何可读页面）不应泄露任何社区。"""
    db = _seed(api_engine)
    try:
        assert search_communities(db, [1.0, 0.0], top_k=5, visible_page_ids=set()) == []
    finally:
        db.close()


def test_get_kb_context_scoped_for_group_user(api_engine, as_user):
    """研发部用户的提示词上下文只含本组+公共笔记本/笔记。"""
    db = _seed(api_engine)
    try:
        kb = _get_kb_context(db, as_user(["研发部"]))
        assert {n["name"] for n in kb["notebooks"]} == {"研发笔记本", "公共笔记本"}
        assert {p["title"] for p in kb["pages"]} == {"研发-甲", "公共-丙"}
    finally:
        db.close()


def test_get_kb_context_admin_sees_all(api_engine, as_user):
    """本地管理员不受组过滤，看到全部笔记本/笔记。"""
    db = _seed(api_engine)
    try:
        kb = _get_kb_context(db, as_user(["__local_admin__"]))
        assert {n["name"] for n in kb["notebooks"]} == {"研发笔记本", "财务笔记本", "公共笔记本"}
        assert {p["title"] for p in kb["pages"]} == {"研发-甲", "财务-乙", "公共-丙"}
    finally:
        db.close()
