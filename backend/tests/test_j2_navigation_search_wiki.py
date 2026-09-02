"""Phase J-2 反例测试：用户入口精简与新版 Wiki 收口。

覆盖（对应 J-2 验收清单）：
一、普通用户顶级导航没有「搜索」。
二、普通用户 /search 不再是正式入口。
三、Editor 等旧入口不会跳到失效 /search。
四、Wiki 主题搜索仍可用。
五、原始资料搜索仍可用。
六、AI 问答 retrieval_only 降级仍可返回资料。
七、管理员检索评测仍可达。
八、Wiki API 响应不包含 community_id/community_key。
九、Wiki API 不包含 Card/KO Citation 或内部 Page/Chunk 来源映射。
十、Page 驱动 Wiki 不生成「社区 1」等内部聚类编号标题。
十一、旧自动生成 Community Wiki 能被 dry-run 识别为可清理。
十二、含人工编辑、locked/protected 内容的旧 Wiki 不会被标记为自动删除。
十三、清理方案重复运行结果一致，不重复删除或改变判断。
十四、ACL 仍先于 Wiki/原始资料搜索结果和计数生效。

全程临时 SQLite，不触碰真实库，不调用真实模型。
"""
from __future__ import annotations

import json
import pathlib

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend" / "src"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed_page(db, page_id, title, content, group="engineering"):
    nb = Notebook(id=f"nb-{page_id}", name=f"nb-{page_id}", group_id=group)
    db.add(nb)
    db.flush()
    page = Page(id=page_id, notebook_id=nb.id, title=title, content=content)
    db.add(page)
    db.flush()
    db.add(PageChunk(id=f"{page_id}-c0", page_id=page_id, chunk_index=0, content=content, content_type="text"))
    db.flush()
    return page


def _seed_wiki(db, page_id, title, content, acl_group="engineering", status="published"):
    page = WikiPage(id=page_id, title=title, summary="", acl_scope=json.dumps({"groups": [acl_group]}), status=status)
    db.add(page)
    db.flush()
    rev = WikiRevision(id=f"{page_id}-rev", wiki_page_id=page_id, title=title, summary="", status=status)
    db.add(rev)
    db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.commit()


# ---------------------------------------------------------------------------
# 一/二/三：导航、路由、Editor 旧入口（源码级断言）
# ---------------------------------------------------------------------------

def test_primary_nav_has_no_search_entry():
    nav_src = (FRONTEND_ROOT / "navigation" / "config.ts").read_text(encoding="utf-8")
    # 顶级导航不得再有 label 为「搜索」、route 为 /search 的项。
    assert "'/search'" not in nav_src
    assert "label: '搜索'" not in nav_src


def test_user_search_route_removed():
    router_src = (FRONTEND_ROOT / "router" / "index.ts").read_text(encoding="utf-8")
    # 普通用户 /search 正式路由已移除；管理员检索评测走 /admin/retrieval。
    assert "path: '/search'" not in router_src
    assert "'retrieval'" in router_src
    assert "Search.vue" in router_src  # 复用 Search.vue 但仅管理员可达


def test_editor_does_not_redirect_to_legacy_search():
    editor_src = (FRONTEND_ROOT / "views" / "Editor.vue").read_text(encoding="utf-8")
    # Editor 不得再跳转到旧 /search 顶级入口。
    assert "path: '/search'" not in editor_src
    assert "router.push({ path: '/search'" not in editor_src


# ---------------------------------------------------------------------------
# 四/五：Wiki 主题搜索与原始资料搜索仍可用（共用 Search 服务）
# ---------------------------------------------------------------------------

def test_wiki_topic_search_still_available(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.post("/api/search/v2", json={"question": "水箱容量"})
        assert r.status_code == 200
        data = r.json()
        assert data["wiki_results"], "Wiki 主题搜索应返回结果"
        assert data["wiki_results"][0]["title"] == "水箱容量"
    finally:
        app.dependency_overrides.clear()


def test_raw_search_still_available(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L 的原始资料", group="engineering")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.post("/api/search/v2", json={"question": "水箱容量"})
        assert r.status_code == 200
        data = r.json()
        assert data["raw_results"], "原始资料搜索应返回结果"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 六：AI 问答 retrieval_only 降级仍可返回资料
# ---------------------------------------------------------------------------

def test_chat_retrieval_only_still_returns_materials(db, monkeypatch):
    from app.api import rag_chat
    from app.core.retrieval.degradation import LLMAnswerResult
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiRetrievalResult, WikiHit
    from app.core.retrieval.raw_retriever import RawRetrievalResult

    class _FakeOrch:
        def __init__(self, *a, **k):
            pass

        async def retrieve_async(self, question, current_user):
            hit = WikiHit(wiki_page_id="w1", title="水箱容量", summary="", content="水箱容量为 500L", score=0.9, acl_scope='{"groups":["engineering"]}')
            return OrchestrationOutcome(
                mode="wiki_hit",
                wiki_results=WikiRetrievalResult(hits=[hit]),
                raw_results=RawRetrievalResult(),
            )

    async def _fake_llm(messages, **k):
        return LLMAnswerResult(ok=False, degraded_reason="llm_timeout")

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", _FakeOrch)
    monkeypatch.setattr(rag_chat, "call_llm_answer", _fake_llm)

    import asyncio
    result = asyncio.run(
        rag_chat.run_chat_query(db, {"id": "u1", "groups": ["engineering"]}, "水箱容量", None)
    )
    assert result["response_mode"] == "retrieval_only"
    assert result["wiki_results"], "retrieval_only 降级仍应返回资料"


# ---------------------------------------------------------------------------
# 七：管理员检索评测仍可达
# ---------------------------------------------------------------------------

def test_admin_retrieval_eval_reachable(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["admins"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    try:
        # 检索评测复用默认 Search 服务，管理员可直接调用（不依赖普通用户 /search 路由）。
        r = client.post("/api/search/v2", json={"question": "水箱容量"})
        assert r.status_code == 200
        assert "wiki_results" in r.json()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 八/九：Wiki API 不泄露 community 字段、Card/KO Citation、内部来源映射
# ---------------------------------------------------------------------------

def test_wiki_api_no_community_fields(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    # 旧 Community 遗留字段非空的 Wiki，也必须不出现在响应中。
    page = WikiPage(
        id="w-legacy", title="社区 1", summary="",
        community_id="comm-1", community_key="legacy-key",
        acl_scope=json.dumps({"groups": ["engineering"]}), status="published",
    )
    db.add(page)
    db.flush()
    rev = WikiRevision(id="w-legacy-rev", wiki_page_id="w-legacy", title="社区 1", summary="", status="published")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id="w-legacy-sec", revision_id=rev.id, section_type="facts", content="内容", order_index=0))
    page.current_revision_id = rev.id
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["admins"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    try:
        r = client.get("/api/wiki")
        assert r.status_code == 200
        body = json.dumps(r.json())
        assert "community_id" not in body
        assert "community_key" not in body
        # 详情接口同样不泄露
        r2 = client.get("/api/wiki/w-legacy")
        body2 = json.dumps(r2.json())
        assert "community_id" not in body2
        assert "community_key" not in body2
    finally:
        app.dependency_overrides.clear()


def test_wiki_api_no_card_or_internal_source_mapping(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["admins"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    try:
        r = client.get("/api/wiki/w1")
        assert r.status_code == 200
        body = json.dumps(r.json())
        # 不泄露 Card/KO Citation 结构，也不泄露 Page/Chunk 内部来源映射。
        assert '"card"' not in body.lower()
        assert "source_page_ids" not in body
        assert "chunk_id" not in body
        assert "page_id" not in body
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 十：Page 驱动 Wiki 标题不生成「社区 1」等内部聚类编号
# ---------------------------------------------------------------------------

def test_page_driven_wiki_does_not_use_community_title():
    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import _COMMUNITY_TITLE_RE

    # Page 驱动构建器从真实主题识别产出标题，不产生「社区 N」聚类编号标题。
    # 清理方案的旧标题识别正则不应误匹配正常主题标题。
    assert _COMMUNITY_TITLE_RE.match("社区 1") is not None  # 旧遗留标题
    assert _COMMUNITY_TITLE_RE.match("水箱容量") is None
    assert _COMMUNITY_TITLE_RE.match("社区交流群讨论") is None

    # 构建器源码硬约束：不读取 Community，也不写 community_id/community_key。
    # 标题来自 LLM 主题识别（_identify_topics 的 op title），与 Community 名称无关。
    builder_src = (BACKEND_ROOT / "app" / "core" / "knowledge_compiler_v3" / "wiki_page_builder.py").read_text(encoding="utf-8")
    assert "community_id" not in builder_src
    assert "community_key" not in builder_src
    lifecycle_src = (BACKEND_ROOT / "app" / "core" / "knowledge_compiler_v3" / "wiki_lifecycle.py").read_text(encoding="utf-8")
    assert "community_id" not in lifecycle_src
    assert "community_key" not in lifecycle_src


# ---------------------------------------------------------------------------
# 十一/十二/十三：旧 Community Wiki 清理方案（dry-run / 保护 / 幂等）
# ---------------------------------------------------------------------------

def _seed_legacy_auto(db):
    """纯自动生成的旧 Community draft Wiki（可清理）。"""
    page = WikiPage(
        id="w-auto", title="社区 1", summary="", community_id="comm-1", community_key="k1",
        acl_scope=json.dumps({"groups": ["engineering"]}), status="draft",
    )
    db.add(page)
    db.flush()
    rev = WikiRevision(id="w-auto-rev", wiki_page_id="w-auto", title="社区 1", summary="", status="draft", edit_type="auto")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id="w-auto-sec", revision_id=rev.id, section_type="facts", content="自动内容", content_origin="auto", merge_policy="auto"))
    db.commit()


def _seed_legacy_human(db):
    """含人工编辑痕迹的旧 Community Wiki（必须人工处理）。"""
    page = WikiPage(
        id="w-human", title="社区 2", summary="", community_id="comm-2", community_key="k2",
        acl_scope=json.dumps({"groups": ["engineering"]}), status="draft",
    )
    db.add(page)
    db.flush()
    rev = WikiRevision(id="w-human-rev", wiki_page_id="w-human", title="社区 2", summary="", status="draft", edit_type="manual", updated_by="alice")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id="w-human-sec", revision_id=rev.id, section_type="facts", content="人工内容", content_origin="manual", merge_policy="protected", locked=True))
    db.commit()


def test_legacy_auto_wiki_identified_cleanable(db):
    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import scan_legacy_wikis

    _seed_legacy_auto(db)
    report = scan_legacy_wikis(db)
    assert report.total_legacy == 1
    assert len(report.cleanable) == 1
    assert report.cleanable[0].wiki_page_id == "w-auto"


def test_legacy_human_wiki_not_marked_auto_delete(db):
    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import scan_legacy_wikis

    _seed_legacy_human(db)
    report = scan_legacy_wikis(db)
    assert report.total_legacy == 1
    assert len(report.cleanable) == 0
    assert len(report.human_protected) == 1
    assert report.human_protected[0].wiki_page_id == "w-human"


def test_cleanup_idempotent_and_stable(db):
    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import cleanup_legacy_wikis

    _seed_legacy_auto(db)
    _seed_legacy_human(db)

    # 第一次 dry-run：不写库。
    r1 = cleanup_legacy_wikis(db, apply=False)
    assert db.get(WikiPage, "w-auto") is not None, "dry-run 不得删除"
    assert db.get(WikiPage, "w-human") is not None, "dry-run 不得删除"

    # 第二次 dry-run：判定完全一致。
    r2 = cleanup_legacy_wikis(db, apply=False)
    assert {v.wiki_page_id for v in r1.cleanable} == {v.wiki_page_id for v in r2.cleanable}
    assert {v.wiki_page_id for v in r1.human_protected} == {v.wiki_page_id for v in r2.human_protected}

    # apply：只删可清理，保留人工。
    r3 = cleanup_legacy_wikis(db, apply=True)
    assert "w-auto" in r3.applied
    assert db.get(WikiPage, "w-auto") is None
    assert db.get(WikiPage, "w-human") is not None, "人工内容 Wiki 必须保留"

    # 再次 apply：幂等，无重复删除，人工记录仍保留且判定不变。
    r4 = cleanup_legacy_wikis(db, apply=True)
    assert r4.applied == [], "重复运行不得再删除"
    assert db.get(WikiPage, "w-human") is not None


# ---------------------------------------------------------------------------
# 十四：ACL 先于 Wiki/原始资料搜索结果与计数生效
# ---------------------------------------------------------------------------

def test_acl_precedes_search_results_and_count(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w-eng", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    _seed_wiki(db, "w-sales", "电池电压", "电池电压为 12V", acl_group="sales")
    _seed_page(db, "p-eng", "水箱容量", "水箱容量为 500L", group="engineering")
    _seed_page(db, "p-sales", "电池电压", "电池电压为 12V", group="sales")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _eng():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _eng
    client = TestClient(app)
    try:
        r = client.post("/api/search/v2", json={"question": "容量 电压"})
        assert r.status_code == 200
        data = r.json()
        titles = [w["title"] for w in data["wiki_results"]]
        raw_titles = [x["title"] for x in data["raw_results"]]
        # ACL 先于结果与计数：engineering 用户看不到 sales 的 Wiki/原始资料。
        assert "电池电压" not in titles
        assert "电池电压" not in raw_titles
        assert "水箱容量" in titles
        assert data["total"] == len(data["wiki_results"]) + len(data["raw_results"])
    finally:
        app.dependency_overrides.clear()
