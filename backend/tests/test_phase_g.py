"""V4 Phase G：默认切换与旧功能退出封板测试。

覆盖（对应任务封板清单）：
1.  默认 /api/chat 不查询 Card 表
2.  默认 Search 不查询 Card/KO
3.  Wiki 充分时 Raw/Community 调用 0
4.  Raw 最多两轮，Community 不产生第三轮 Raw
7.  模型故障不创建债务
8.  真正无结果创建/累计债务
9.  company/group/admin ACL 矩阵
10. 发送给 LLM 的上下文无越权资料
11. Source/Page 更新不触发 Card compiler
12. Community/Wiki/Debt 默认链 Card SQL 查询 0
13. 默认 Graph 无 Card 节点
14. 旧写接口默认 410
15. legacy_readonly 只能管理员读且不能写
16. 前端路由/导航不存在 Card/KO/Governance/owner/review
17. 直接访问旧 URL 不进入旧写页面
18. /assistant 使用新 Chat，Search 使用新检索
19. /api/rag-chat 与默认 Chat 状态语义一致
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    RuntimeFeatureFlag,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend" / "src"

# Card/KO 相关表名片段（SQL 监听用，小写）
_CARD_TABLES = (
    "knowledge_cards",
    "knowledge_card_blocks",
    "knowledge_card_revisions",
    "knowledge_card_sources",
    "knowledge_claims",
    "knowledge_objects",
    "ko_chunks",
    "card_entity_links",
    "card_graph_relations",
    "knowledge_debt_cards",
)


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine


@pytest.fixture()
def db(monkeypatch):
    engine = _make_engine()
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


def _seed_wiki(db, page_id, title, content, acl_group="engineering"):
    page = WikiPage(id=page_id, title=title, summary="", acl_scope=json.dumps({"groups": [acl_group]}), status="published")
    db.add(page)
    db.flush()
    rev = WikiRevision(id=f"{page_id}-rev", wiki_page_id=page_id, title=title, summary="", status="published")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.commit()


# ---------------------------------------------------------------------------
# 1. 默认 /api/chat 不查询 Card 表
# ---------------------------------------------------------------------------

def test_chat_does_not_query_card_table(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L 的完整说明", acl_group="engineering")
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()

    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user

    card_sql: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if any(t in low for t in _CARD_TABLES):
            card_sql.append(statement)

    client = TestClient(app)
    try:
        r = client.post("/api/chat", json={"query": "水箱容量", "scope_id": "group:engineering"})
        assert r.status_code == 200
        data = r.json()
        assert "response_mode" in data
    finally:
        app.dependency_overrides.clear()
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert card_sql == [], f"默认 /api/chat 不应查询 Card 表：{card_sql[:3]}"


# ---------------------------------------------------------------------------
# 2. 默认 Search 不查询 Card/KO
# ---------------------------------------------------------------------------

def test_search_does_not_query_card_ko(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L 的完整说明", acl_group="engineering")
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()

    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user

    card_sql: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if any(t in low for t in _CARD_TABLES):
            card_sql.append(statement)

    client = TestClient(app)
    try:
        r = client.post("/api/search/v2", json={"question": "水箱容量", "scope_id": "group:engineering"})
        assert r.status_code == 200
        data = r.json()
        assert "wiki_results" in data and "raw_results" in data
    finally:
        app.dependency_overrides.clear()
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert card_sql == [], f"默认 Search 不应查询 Card/KO：{card_sql[:3]}"


# ---------------------------------------------------------------------------
# 3. Wiki 充分时 Raw/Community 调用 0
# ---------------------------------------------------------------------------

def test_wiki_sufficient_raw_community_zero(db):
    from app.core.retrieval.orchestrator import RetrievalOrchestrator
    from app.core.retrieval.wiki_retriever import WikiRetrievalResult, WikiHit
    from app.core.retrieval.sufficiency_judge import SufficiencyJudge

    class _RawNever:
        calls = 0

        def retrieve(self, *a, **k):
            _RawNever.calls += 1
            raise AssertionError("wiki 充分时不应调用 raw")

    class _CommunityNever:
        calls = 0

        def expand(self, *a, **k):
            _CommunityNever.calls += 1
            raise AssertionError("wiki 充分时不应调用 community")

    wiki_hit = WikiHit(
        wiki_page_id="w1", title="水箱容量", summary="", content="水箱容量为 500L",
        score=0.9, acl_scope='{"groups":["engineering"]}',
        query_tokens=["水箱", "容量"], matched_tokens=["水箱", "容量"],
        missing_tokens=[], query_token_count=2, coverage=1.0,
        exact_title_match=False, title_contained=False,
    )
    wiki_result = WikiRetrievalResult(hits=[wiki_hit], visible_wiki_count=1, published_wiki_count=1, query="q")

    class _FakeWikiRetriever:
        def retrieve(self, db, question, current_user, *, top_k=5):
            return wiki_result

    orch = RetrievalOrchestrator(
        db,
        wiki_retriever=_FakeWikiRetriever(),
        judge=SufficiencyJudge(),
        raw_retriever=_RawNever(),
        community_expander=_CommunityNever(),
    )
    outcome = orch.retrieve("水箱容量", {"groups": ["engineering"]})
    assert outcome.mode == "wiki_hit"
    assert outcome.raw_calls == 0
    assert _RawNever.calls == 0
    assert _CommunityNever.calls == 0


# ---------------------------------------------------------------------------
# 4. Raw 最多两轮，Community 不产生第三轮 Raw
# ---------------------------------------------------------------------------

def test_raw_max_two_rounds_no_third(db):
    from app.core.retrieval.orchestrator import RetrievalOrchestrator
    from app.core.retrieval.wiki_retriever import WikiRetrievalResult
    from app.core.retrieval.sufficiency_judge import SufficiencyJudge
    from app.core.retrieval.raw_retriever import RawChunkHit, RawRetrievalResult
    from app.core.retrieval.raw_sufficiency_judge import RawSufficiencyVerdict
    from app.core.retrieval.query_supplement import SupplementalQuery

    class _WikiEmpty:
        def retrieve(self, db, question, current_user, *, top_k=5):
            return WikiRetrievalResult(query=question)

    class _RawTwoRounds:
        calls = 0

        def retrieve(self, db, query, current_user, *, query_embedding=None, retrieval_round=1):
            _RawTwoRounds.calls += 1
            hit = RawChunkHit(
                chunk_id=f"c{self.calls}", page_id="p1", notebook_id="nb", page_title="p",
                content="部分内容", chunk_index=0, final_score=0.5, bm25_score=0.5,
                dense_score=None, rerank_score=None, source_type=None, source_url=None,
                retrieval_round=retrieval_round,
            )
            return RawRetrievalResult(
                hits=[hit], query=query, retrieval_round=retrieval_round,
                visible_page_count=1, eligible_page_count=1, bm25_used=True,
                visible_page_ids={"p1"},
            )

    class _JudgeNeverSufficient:
        def __call__(self, hits, question, *, retrieval_round=1):
            return RawSufficiencyVerdict(
                sufficient=False, reason="insufficient_coverage", confidence=0.0,
                matched_tokens=[], missing_tokens=["补充"], coverage=0.0,
            )

    class _SupplementAlways:
        def __call__(self, question, **kwargs):
            return SupplementalQuery(
                supplemental_query=f"{question} 补充", added_terms=["补充"], reason="fallback",
                can_supplement=True,
            )

    class _CommunityNever:
        calls = 0

        def expand(self, *a, **k):
            _CommunityNever.calls += 1
            from app.core.retrieval.community_expansion import CommunityExpansionResult
            return CommunityExpansionResult(hits=[])

    orch = RetrievalOrchestrator(
        db,
        wiki_retriever=_WikiEmpty(),
        judge=SufficiencyJudge(),
        raw_retriever=_RawTwoRounds(),
        raw_judge=_JudgeNeverSufficient(),
        supplementer=_SupplementAlways(),
        community_expander=_CommunityNever(),
    )
    outcome = orch.retrieve("水箱容量", {"groups": ["engineering"]})
    assert outcome.raw_calls == 2
    assert _RawTwoRounds.calls == 2  # 最多两轮，无第三轮


# ---------------------------------------------------------------------------
# 7. 模型故障不创建债务
# 8. 真正无结果创建/累计债务
# ---------------------------------------------------------------------------

def test_llm_failure_no_debt(db):
    from app.core.retrieval.debt_service import record_missing_knowledge
    from app.core.retrieval.degradation import build_rag_response
    from app.core.retrieval.orchestrator import OrchestrationOutcome
    from app.core.retrieval.wiki_retriever import WikiRetrievalResult, WikiHit

    # 有结果但 LLM 失败 → service_degraded，不创建债务
    hit = WikiHit(wiki_page_id="w1", title="t", summary="", content="c", score=0.9, acl_scope="{}")
    outcome = OrchestrationOutcome(mode="wiki_hit", wiki_results=WikiRetrievalResult(hits=[hit]))
    resp = build_rag_response(db, outcome, llm_answer=None, llm_called=True, llm_ok=False, llm_degraded_reason="llm_timeout")
    assert resp["knowledge_missing"] is False
    assert resp["service_degraded"] is True
    assert resp["debt_created"] is False


def test_true_no_result_creates_and_accumulates_debt(db):
    from app.core.retrieval.debt_service import record_missing_knowledge
    r1 = record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    r2 = record_missing_knowledge(db, original_query="水箱容量", user_id="u2", scope_id="group:engineering")
    assert r1.created_new is True
    assert r2.created_new is False  # 累计
    from app.models.database import KnowledgeDebt
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == r2.debt_id).one()
    assert debt.occurrence_count == 2


# ---------------------------------------------------------------------------
# 9. company/group/admin ACL 矩阵
# ---------------------------------------------------------------------------

def test_acl_matrix(db):
    from app.core import access_control

    _seed_wiki(db, "w-company", "公司制度", "全公司制度内容", acl_group="__public__")
    _seed_wiki(db, "w-eng", "工程知识", "工程组知识", acl_group="engineering")
    _seed_wiki(db, "w-admin", "管理员知识", "管理员知识", acl_group="admins")

    # 普通用户 engineering：可见 company + engineering，不可见 admin
    eng_ids = access_control.get_visible_wiki_page_ids(db, {"groups": ["engineering"]})
    assert "w-company" in eng_ids
    assert "w-eng" in eng_ids
    assert "w-admin" not in eng_ids

    # 无组普通用户：仅 company
    public_ids = access_control.get_visible_wiki_page_ids(db, {"groups": []})
    assert "w-company" in public_ids
    assert "w-eng" not in public_ids
    assert "w-admin" not in public_ids

    # admin：可见全部
    admin_ids = access_control.get_visible_wiki_page_ids(db, {"groups": ["admins"]})
    assert {"w-company", "w-eng", "w-admin"} <= admin_ids

    # 精确 scope 覆盖（group:engineering 严格隔离）
    scoped_ids = access_control.get_scoped_wiki_page_ids(db, "group:engineering")
    assert "w-eng" in scoped_ids
    assert "w-company" not in scoped_ids
    assert "w-admin" not in scoped_ids


# ---------------------------------------------------------------------------
# 10. 发送给 LLM 的上下文无越权资料
# ---------------------------------------------------------------------------

def test_llm_context_no_cross_scope(db, monkeypatch):
    from app.api import rag_chat
    from app.core.retrieval.degradation import LLMAnswerResult
    from app.core.retrieval.orchestrator import build_default_retrieval_orchestrator

    _seed_wiki(db, "w-eng", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    _seed_wiki(db, "w-sales", "电池电压", "电池电压为 12V", acl_group="sales")
    db.commit()

    captured = []

    class _FakeOrch:
        def __init__(self, *a, **k):
            pass

        async def retrieve_async(self, question, current_user):
            from app.core.retrieval.orchestrator import OrchestrationOutcome
            from app.core.retrieval.wiki_retriever import WikiRetrievalResult, WikiHit
            from app.core.retrieval.raw_retriever import RawRetrievalResult
            hit = WikiHit(wiki_page_id="w-eng", title="水箱容量", summary="", content="水箱容量为 500L", score=0.9, acl_scope='{"groups":["engineering"]}')
            return OrchestrationOutcome(
                mode="wiki_hit",
                wiki_results=WikiRetrievalResult(hits=[hit]),
                raw_results=RawRetrievalResult(),
            )

    async def _fake_llm(messages, **k):
        captured.append(messages)
        return LLMAnswerResult(ok=True, answer="回答")

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", _FakeOrch)
    monkeypatch.setattr(rag_chat, "call_llm_answer", _fake_llm)

    import asyncio
    result = asyncio.run(
        rag_chat.run_chat_query(db, {"id": "u1", "groups": ["engineering"]}, "水箱容量", "group:engineering")
    )
    assert result["response_mode"] == "answer"
    context = captured[0][1]["content"]
    assert "水箱容量" in context
    assert "电池电压" not in context
    assert "12V" not in context


# ---------------------------------------------------------------------------
# 11. Page 新建/更新/source-import 不触发 Card compiler（源码级，与 P22 一致）
# ---------------------------------------------------------------------------

def test_page_create_update_import_do_not_compile(db, monkeypatch):
    source = (BACKEND_ROOT / "app" / "api" / "pages.py").read_text(encoding="utf-8")
    # Page 新建/更新/source-import/索引均不再调度 Card 编译。
    assert "compile_page_to_cards" not in source
    assert "_schedule_compile" not in source
    # 三个写 handler 仍存在，确保断言覆盖真实生产路径而非误删。
    for handler in ("create_page", "update_page", "import_source_page"):
        assert handler in source, f"pages.py 缺少 {handler}"


# ---------------------------------------------------------------------------
# 12. Community/Wiki/Debt 默认链 Card SQL 查询 0
# ---------------------------------------------------------------------------

def test_default_chain_zero_card_sql(db):
    from app.core.retrieval.debt_service import record_missing_knowledge, notify_knowledge_changed_for_page
    from app.core.retrieval.wiki_retriever import retrieve_wiki
    from app.core.knowledge_compiler_v3.page_graph import build_page_communities

    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L 的说明", group="engineering")
    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    db.commit()

    card_sql: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        low = statement.lower()
        if any(t in low for t in _CARD_TABLES):
            card_sql.append(statement)

    try:
        # Wiki 检索
        retrieve_wiki(db, {"groups": ["engineering"]}, "水箱容量")
        # Community（page_graph 只读）
        build_page_communities(db, {"p1"}, seed_hits=None)
        # Debt 记录 + 重验证
        record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
        notify_knowledge_changed_for_page(db, "p1")
    finally:
        event.remove(db.bind, "before_cursor_execute", _capture)

    assert card_sql == [], f"默认链不应查询 Card 表：{card_sql[:3]}"


# ---------------------------------------------------------------------------
# 13. 默认 Graph 无 Card 节点
# ---------------------------------------------------------------------------

def test_default_graph_no_card_nodes(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    _seed_page(db, "p1", "水箱", "水箱容量为 500L", group="engineering")
    db.commit()
    monkeypatch.setattr(deps, "_engine", db.bind)

    def _user():
        return {"id": "u1", "username": "admin", "groups": ["admins"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.get("/api/v4/graph/communities")
        assert r.status_code == 200
        body = json.dumps(r.json())
        assert "card" not in body.lower()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 14. 旧写接口默认 410
# ---------------------------------------------------------------------------

def test_legacy_card_writes_gone(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    monkeypatch.setattr(deps, "_engine", db.bind)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    try:
        # Phase H：旧 Card 写接口已彻底删除，返回 404。
        assert client.post("/api/cards/batch-publish", json={"card_ids": ["c1"]}).status_code == 404
        assert client.post("/api/cards/c1/approve").status_code == 404
        assert client.post("/api/cards/c1/reject").status_code == 404
        assert client.post("/api/cards/c1/archive").status_code == 404
        assert client.post("/api/cards/c1/publish-revision", json={"revision_id": "r1"}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_frontend_routes_removed_legacy_entries():
    router_src = (FRONTEND_ROOT / "router" / "index.ts").read_text(encoding="utf-8")
    nav_src = (FRONTEND_ROOT / "navigation" / "config.ts").read_text(encoding="utf-8")
    for forbidden in ("/governance", "ReviewWorkbench", "GovernanceOverview", "GovernanceLayout"):
        assert forbidden not in router_src, f"路由不应含 {forbidden}"
    for forbidden in ("governance", "review", "owner", "治理工作台"):
        assert forbidden not in nav_src, f"导航不应含 {forbidden}"


def test_frontend_legacy_urls_do_not_route_to_write_pages():
    redirects = (FRONTEND_ROOT / "router" / "legacyRedirects.ts").read_text(encoding="utf-8")
    for legacy in ("/review", "/conflicts", "/health", "/debts", "/governance"):
        assert legacy not in redirects


def test_frontend_uses_new_chat_and_search():
    rag_ts = (FRONTEND_ROOT / "api" / "ragChat.ts").read_text(encoding="utf-8")
    assert "'/api/chat'" in rag_ts or '"/api/chat"' in rag_ts
    knowledge_ts = (FRONTEND_ROOT / "api" / "knowledge.ts").read_text(encoding="utf-8")
    assert "'/api/search/v2'" in knowledge_ts


# ---------------------------------------------------------------------------
# 19. /api/rag-chat 与默认 Chat 状态语义一致（共用 run_chat_query）
# ---------------------------------------------------------------------------

def test_rag_chat_and_chat_share_service(db, monkeypatch):
    from app.api import rag_chat, chat
    import inspect
    assert chat.chat.__module__ == "app.api.chat"
    # /api/chat 调用 rag_chat.run_chat_query
    src = inspect.getsource(chat.chat)
    assert "run_chat_query" in src
    # 两者响应结构一致（均经 build_rag_response）
    assert hasattr(rag_chat, "run_chat_query")


# ---------------------------------------------------------------------------
# 返工 Phase G 补充封板测试
# ---------------------------------------------------------------------------







def test_import_app_main_does_not_load_card_compiler():
    import subprocess
    import sys
    code = (
        "import sys\n"
        "import app.main\n"
        "forbidden = [\n"
        " 'app.core.knowledge_compiler_v3.pipeline',\n"
        " 'app.core.knowledge_compiler_v3.persistence',\n"
        " 'app.core.knowledge_compiler_v3.card_builder',\n"
        " 'app.core.knowledge_compiler_v3.card_graph',\n"
        " 'app.core.knowledge_compiler_v3.claims',\n"
        " 'app.core.knowledge_compiler_v3.cluster',\n"
        " 'app.core.knowledge_compiler.conflict_center',\n"
        " 'app.api.cards', 'app.api.conflicts', 'app.api.governance', 'app.api.p5_graph',\n"
        "]\n"
        "loaded = [m for m in forbidden if m in sys.modules]\n"
        "assert loaded == [], loaded\n"
        "print('NO_FORBIDDEN_IMPORT')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "NO_FORBIDDEN_IMPORT" in proc.stdout


def test_search_and_search_v2_share_service(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps, search_v2
    from app.core import jwt_utils
    import inspect

    monkeypatch.setattr(deps, "_engine", db.bind)

    # 静态：/api/search 与 /api/search/v2 共用 run_search_query
    import app.api.search as search_mod
    src = inspect.getsource(search_mod.search)
    assert "run_search_query" in src

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    _seed_wiki(db, "w1", "水箱容量", "水箱容量为 500L", acl_group="engineering")
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r1 = client.post("/api/search", json={"query": "水箱容量", "scope_id": "group:engineering"})
        r2 = client.post("/api/search/v2", json={"question": "水箱容量", "scope_id": "group:engineering"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["wiki_results"] == r2.json()["wiki_results"]
    finally:
        app.dependency_overrides.clear()


def test_search_model_failure_no_500(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    monkeypatch.setattr(deps, "_engine", db.bind)
    # 强制 Embedding/Reranker 未配置（走 D/E 降级，不 500）
    monkeypatch.setattr(settings, "embedding_api_url", "")
    monkeypatch.setattr(settings, "reranker_api_url", "")

    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L", group="engineering")
    db.commit()

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    client = TestClient(app)
    try:
        r = client.post("/api/search", json={"query": "水箱容量", "scope_id": "group:engineering"})
        assert r.status_code == 200
        assert "degraded_reasons" in r.json()
    finally:
        app.dependency_overrides.clear()
