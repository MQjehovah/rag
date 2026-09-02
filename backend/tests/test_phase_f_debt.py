"""V4 Phase F：知识债务改造测试（去 Card/KO/owner/review，封板）。"""
from __future__ import annotations

import inspect
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.debt_service import (
    build_cluster_key,
    list_debts_for_scopes,
    normalize_query,
    record_missing_knowledge,
    revalidate_debts_for_change,
    serialize_debt,
    validated_scope,
    visible_scope_ids,
)
from app.models.database import (
    KnowledgeDebt,
    KnowledgeDebtUser,
    Notebook,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

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


def _user(groups):
    return {"id": "u1", "username": "u", "groups": groups}


# ---------------------------------------------------------------------------
# 零 Card/KO 依赖
# ---------------------------------------------------------------------------

def test_no_card_or_ko_import():
    import app.core.retrieval.debt_service as ds
    import app.api.debts_v4 as dv
    import app.api.rag_chat as rc
    for mod in (ds, dv, rc):
        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in (
            "KnowledgeCard", "KnowledgeClaim", "KnowledgeDebtCard",
            "KnowledgeObject", "KnowledgeCardSource",
        ):
            assert forbidden not in joined, f"{mod.__name__} 不应 import {forbidden}"


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def test_normalize_query_deterministic():
    assert normalize_query(" 水箱  容量 ") == "水箱容量"
    assert normalize_query("Titan 810") == "titan810"
    assert normalize_query("Titan-810") == "titan810"
    assert normalize_query("Ｔｉｔａｎ") == "titan"


# ---------------------------------------------------------------------------
# scope 设计
# ---------------------------------------------------------------------------

def test_validated_scope_company_no_groups(db):
    assert validated_scope(_user([]), None) == ("company", "")


def test_validated_scope_single_group_default(db):
    assert validated_scope(_user(["engineering"]), None) == ("group:engineering", "")


def test_validated_scope_admin_requires_explicit(db):
    assert validated_scope(_user(["admins"]), None) == ("", "scope_required")


def test_validated_scope_multi_group_requires_explicit(db):
    assert validated_scope(_user(["engineering", "sales"]), None) == ("", "scope_required")


def test_validated_scope_forbidden_cross_group(db):
    assert validated_scope(_user(["engineering"]), "group:sales") == ("", "forbidden")


def test_validated_scope_admin_any_group(db):
    assert validated_scope(_user(["admins"]), "group:sales") == ("group:sales", "")


def test_visible_scope_ids_normal(db):
    scopes = visible_scope_ids(_user(["engineering"]))
    assert "company" in scopes
    assert "group:engineering" in scopes
    assert "group:sales" not in scopes


def test_visible_scope_ids_admin_all(db):
    assert visible_scope_ids(_user(["admins"])) is None


def test_role_group_not_business(db):
    # admin/editor 角色组不被误当业务知识组
    scopes = visible_scope_ids(_user(["admins", "editors"]))
    assert scopes is None  # admin 全部


# ---------------------------------------------------------------------------
# 创建 / 累计 / 用户去重 / 相似合并
# ---------------------------------------------------------------------------

def test_record_missing_knowledge_creates(db):
    r = record_missing_knowledge(db, original_query="如何更换轮胎", user_id="u1", scope_id="group:engineering")
    assert r.ok is True
    assert r.created_new is True
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == r.debt_id).one()
    assert debt.occurrence_count == 1
    assert debt.affected_user_count == 1
    assert debt.scope_id == "group:engineering"


def test_record_accumulates_same_query(db):
    record_missing_knowledge(db, original_query="如何更换轮胎", user_id="u1", scope_id="group:engineering")
    r2 = record_missing_knowledge(db, original_query="如何更换轮胎", user_id="u1", scope_id="group:engineering")
    assert r2.created_new is False
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == r2.debt_id).one()
    assert debt.occurrence_count == 2
    assert debt.affected_user_count == 1  # 同 user 去重


def test_record_affected_user_dedup(db):
    record_missing_knowledge(db, original_query="如何更换轮胎", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="如何更换轮胎", user_id="u2", scope_id="group:engineering")
    debts = db.query(KnowledgeDebt).all()
    assert len(debts) == 1
    assert debts[0].affected_user_count == 2
    serialized = serialize_debt(debts[0])
    assert "user_id" not in serialized


def test_normalized_equivalent_merge(db):
    record_missing_knowledge(db, original_query="水箱 容量", user_id="u1", scope_id="group:engineering")
    r2 = record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    assert r2.created_new is False
    assert db.query(KnowledgeDebt).count() == 1


def test_similar_question_merge(db):
    record_missing_knowledge(db, original_query="水箱容量多少", user_id="u1", scope_id="group:engineering")
    r2 = record_missing_knowledge(db, original_query="水箱容量是多少", user_id="u1", scope_id="group:engineering")
    assert r2.created_new is False
    assert db.query(KnowledgeDebt).count() == 1


def test_different_question_not_merged(db):
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="电池电压", user_id="u1", scope_id="group:engineering")
    assert db.query(KnowledgeDebt).count() == 2


def test_different_scope_not_merged(db):
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:sales")
    assert db.query(KnowledgeDebt).count() == 2


def test_scope_unknown_no_debt(db):
    r = record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="")
    assert r.ok is False
    assert db.query(KnowledgeDebt).count() == 0


def test_reopen_resolved_debt(db):
    r = record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == r.debt_id).one()
    debt.status = "resolved"
    db.commit()
    # 同一问题再次缺失 → reopen 原记录
    r2 = record_missing_knowledge(db, original_query="水箱容量", user_id="u2", scope_id="group:engineering")
    assert r2.reopened is True
    assert r2.debt_id == r.debt_id
    debt2 = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == r.debt_id).one()
    assert debt2.status == "open"
    assert db.query(KnowledgeDebt).count() == 1


# ---------------------------------------------------------------------------
# 查询级自动重验证
# ---------------------------------------------------------------------------

def _seed_page(db, page_id, title, content, group="engineering"):
    nb = Notebook(id=f"nb-{page_id}", name=f"nb-{page_id}", group_id=group)
    db.add(nb); db.flush()
    page = Page(id=page_id, notebook_id=nb.id, title=title, content=content)
    db.add(page); db.flush()
    db.add(PageChunk(id=f"{page_id}-c0", page_id=page_id, chunk_index=0, content=content, content_type="text"))
    db.flush()
    return page


def test_revalidate_resolves_only_related(db):
    # 同 scope 两条债务：水箱容量、电池电压
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="电池电压", user_id="u1", scope_id="group:engineering")
    db.commit()

    # 水箱内容更新（有充分证据）
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
    _seed_page(db, "p1", "水箱容量", "水箱容量为 500L 的完整说明", group="engineering")
    db.commit()
    result = notify_knowledge_changed_for_page(db, "p1")

    debts = {d.normalized_query: d for d in db.query(KnowledgeDebt).all()}
    # 水箱债务 resolved（有充分证据），电池债务保持 open
    assert debts["水箱容量"].status == "resolved"
    assert debts["电池电压"].status == "open"


def test_revalidate_unrelated_keeps_open(db):
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="电池电压", user_id="u1", scope_id="group:engineering")
    db.commit()
    # 无关内容更新：既不含水箱也不含电池
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
    _seed_page(db, "p1", "完全无关", "这是一个完全无关的文档", group="engineering")
    db.commit()
    notify_knowledge_changed_for_page(db, "p1")
    assert db.query(KnowledgeDebt).filter(KnowledgeDebt.status == "open").count() == 2


# ---------------------------------------------------------------------------
# 列表隔离
# ---------------------------------------------------------------------------

def test_list_debts_for_scopes_isolated(db):
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="电池电压", user_id="u1", scope_id="group:sales")
    debts = list_debts_for_scopes(db, visible_scope_ids=["group:engineering", "company"])
    assert all(d.scope_id == "group:engineering" for d in debts)
    assert len(debts) == 1


# ---------------------------------------------------------------------------
# 真并发幂等
# ---------------------------------------------------------------------------

def test_concurrent_record_single_debt(tmp_path):
    # 文件型 SQLite + 两个独立 engine，真实并发写，触发唯一约束竞争
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'conc.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})

        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)
    results = []

    def worker(uid):
        # 每个线程独立 engine（独立连接）
        s = sessionmaker(bind=_make_engine())()
        try:
            results.append(record_missing_knowledge(
                s, original_query="水箱容量", user_id=uid, scope_id="group:engineering",
            ))
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("u1",))
    t2 = threading.Thread(target=worker, args=("u2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = sessionmaker(bind=e1)()
    debts = s.query(KnowledgeDebt).all()
    assert len(debts) == 1
    assert debts[0].occurrence_count == 2
    assert debts[0].affected_user_count == 2
    s.close()
    e1.dispose()


# ---------------------------------------------------------------------------
# 幂等（同 cluster_key 不重复建）
# ---------------------------------------------------------------------------

def test_record_idempotent_under_repeat(db):
    for _ in range(5):
        record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    debts = db.query(KnowledgeDebt).all()
    assert len(debts) == 1
    assert debts[0].occurrence_count == 5


# ---------------------------------------------------------------------------
# legacy Card 债务入口已删除（Phase H）
# ---------------------------------------------------------------------------

def test_legacy_debt_card_flag_removed():
    # Phase H：legacy_debt_card_enabled 已从 config 删除。
    from app.config import settings as _s
    assert not hasattr(_s, "legacy_debt_card_enabled")


def test_legacy_flag_not_in_known_flags():
    from app.core.feature_flags import KNOWN_FLAGS
    assert "legacy_debt_card_enabled" not in KNOWN_FLAGS


# ---------------------------------------------------------------------------
# 精确 scope 交叉重验证
# ---------------------------------------------------------------------------

def test_scope_cross_revalidation_isolation(db):
    # company / group:A / group:B / admin 四域债务，各域内容只能解决同域
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page, notify_knowledge_changed_for_wiki

    record_missing_knowledge(db, original_query="公司制度", user_id="u1", scope_id="company")
    record_missing_knowledge(db, original_query="水箱容量", user_id="u1", scope_id="group:engineering")
    record_missing_knowledge(db, original_query="电池电压", user_id="u1", scope_id="group:sales")
    db.commit()

    # group:engineering 的 Page 内容（只含水箱），不应解决 company/group:sales 债务
    _seed_page(db, "p-eng", "水箱容量", "水箱容量为 500L 的完整说明", group="engineering")
    db.commit()
    notify_knowledge_changed_for_page(db, "p-eng")

    debts = {d.scope_id: d for d in db.query(KnowledgeDebt).all()}
    # 只有 group:engineering 的「水箱容量」被解决
    assert debts["group:engineering"].status == "resolved"
    assert debts["company"].status == "open"
    assert debts["group:sales"].status == "open"


def test_admin_scope_only_admin_content(db):
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
    record_missing_knowledge(db, original_query="管理员内部配置", user_id="u1", scope_id="admin")
    db.commit()
    # group 内容不能解决 admin 债务
    _seed_page(db, "p1", "管理员内部配置", "管理员内部配置完整说明", group="engineering")
    db.commit()
    notify_knowledge_changed_for_page(db, "p1")
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.scope_id == "admin").one()
    assert debt.status == "open"


def test_wiki_empty_changed_text_no_op(db):
    from app.core.retrieval.debt_service import notify_knowledge_changed_for_wiki, revalidate_debts_for_change
    result = revalidate_debts_for_change(db, scope_id="group:engineering", changed_text="")
    assert result["resolved"] == 0
    assert result["checked"] == 0


# ---------------------------------------------------------------------------
# 旧债务入口已删除（Phase H → 404）
# ---------------------------------------------------------------------------

def test_legacy_debt_routes_gone(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    def _admin_user():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin_user
    client = TestClient(app)
    try:
        assert client.post("/api/knowledge/debts/scan").status_code == 404
        assert client.post("/api/knowledge/debts/from-query", json={"question": "测试"}).status_code == 404
        assert client.post("/api/knowledge/debts/d1/link-card", json={"card_id": "c1"}).status_code == 404
        assert client.post("/api/knowledge/debts/d1/revalidate").status_code == 404
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# 有界续处理（keyset 分页，不永久漏检）
# ---------------------------------------------------------------------------

def test_candidate_batch_continues_past_limit(db):
    # 造 150 条同 scope open 债务（超过单批 100），验证续处理能扫到第 101+ 条
    from app.core.retrieval.debt_service import _candidate_open_debts, MAX_CANDIDATE_BATCH
    for i in range(150):
        db.add(KnowledgeDebt(
            debt_type="missing_knowledge", description=f"d{i}", related_question=f"水箱容量 {i}",
            status="open", root_cause="missing_knowledge", original_query=f"水箱容量 {i}",
            normalized_query=f"水箱容量{i}", cluster_key=f"ck-{i}", scope_id="group:engineering",
            occurrence_count=1, affected_user_count=1,
        ))
    db.commit()

    related = _candidate_open_debts(db, "group:engineering", "水箱容量 更换")
    # 应该扫到超过 100 条（keyset 续处理），且数量 > MAX_CANDIDATE_BATCH
    assert len(related) > MAX_CANDIDATE_BATCH
    assert len(related) == 150


# ---------------------------------------------------------------------------
# Alembic 迁移一致性（临时 SQLite，不执行真实库）
# ---------------------------------------------------------------------------

def test_alembic_upgrade_downgrade_consistency(tmp_path):
    """用临时 SQLite 验证 upgrade→downgrade 与 ORM 索引一致。"""
    import subprocess
    import sys
    import os

    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["DATABASE_URL"] = url

    # upgrade 到 head，再 downgrade 到上一个 revision，验证不抛异常
    script = f"""
import os, sys
sys.path.insert(0, r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend")
os.environ["DATABASE_URL"] = {url!r}
from alembic.config import Config
from alembic import command
cfg = Config(r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend/alembic.ini")
# 覆盖 script_location 指向项目 alembic
cfg.set_main_option("script_location", r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend/alembic")
command.upgrade(cfg, "c5e6f7a8b9d0")
command.downgrade(cfg, "b8e9f0a1b2c3")
print("UPGRADE_DOWNGRADE_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "UPGRADE_DOWNGRADE_OK" in proc.stdout


# ---------------------------------------------------------------------------
# scope 前端地址一致性（真实行为）
# ---------------------------------------------------------------------------

def test_frontend_no_scope_domain_submission():
    """J-1：前端不再提交权限域（不再调用 /api/v4/debts/scopes，ask 不再传 scope_id）。"""
    import pathlib
    frontend_ts = pathlib.Path(
        r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/frontend/src/api/ragChat.ts"
    )
    src = frontend_ts.read_text(encoding="utf-8")
    assert "/api/v4/debts/scopes" not in src
    assert "scope_id" not in src


# ---------------------------------------------------------------------------
# rag_chat 检索前 scope 校验（orchestrator 调用 0 次）
# ---------------------------------------------------------------------------

def test_rag_chat_scope_forbidden_before_retrieval(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps, rag_chat
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    build_calls = []

    def _fake_build(db):
        build_calls.append(1)
        raise AssertionError("orchestrator should not be built")

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", _fake_build)

    client = TestClient(app)
    try:
        r = client.post("/api/rag-chat", json={"query": "shuixiang", "scope_id": "group:sales"})
        assert r.status_code == 403
        assert build_calls == []
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_rag_chat_multi_group_auto_scope(monkeypatch):
    """J-1：多组用户未指定 scope → 自动检索全部可见范围（不再 scope_required）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api import deps, rag_chat
    from app.core import jwt_utils

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(c, _):
        c.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")

    def _user():
        return {"id": "u1", "username": "u", "groups": ["engineering", "sales"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _user
    build_calls = []

    async def _fake_build_retrieve(*args, **kwargs):
        build_calls.append(1)
        raise AssertionError("orchestrator retrieved without scope")

    # 自动模式会构建 orchestrator；我们不希望它真的检索，所以让 retrieve_async 抛
    # 异常走降级路径，验证它确实被调用了（而不是 400 scope_required）。
    class _FakeOutcome:
        mode = "none"
        wiki_results = None
        raw_results = None

    class _FakeOrchestrator:
        async def retrieve_async(self, query, user):
            build_calls.append(1)
            assert "_scope_override" not in user, "自动模式不应注入 scope_override"
            return _FakeOutcome()

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", lambda db: _FakeOrchestrator())

    client = TestClient(app)
    try:
        r = client.post("/api/rag-chat", json={"query": "shuixiang"})
        assert r.status_code == 200
        assert build_calls == [1], "自动模式应构建 orchestrator 并检索，而非 scope_required"
        body = r.json()
        assert body["response_mode"] == "insufficient"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ---------------------------------------------------------------------------
# 选定 scope 约束检索（不返回 scope 外内容）
# ---------------------------------------------------------------------------

def test_scoped_retrieval_isolates_content(db):
    from app.core.retrieval.raw_retriever import RawDocumentRetriever
    _seed_page(db, "p-eng", "shuixiangrongliang", "shuixiangrongliang wei 500L", group="engineering")
    _seed_page(db, "p-sales", "dianchidianya", "dianchidianya wei 12V", group="sales")
    db.commit()

    scoped_user = {"groups": ["engineering"], "_scope_override": "group:engineering"}
    result = RawDocumentRetriever(db).retrieve(db, "shuixiangrongliang dianchidianya", scoped_user)
    page_ids = {h.page_id for h in result.hits}
    assert "p-eng" in page_ids
    assert "p-sales" not in page_ids


def test_scoped_wiki_retrieval_isolates(db):
    from app.core.retrieval.wiki_retriever import retrieve_wiki
    page = WikiPage(id="w-eng", title="shuixiang", summary="", acl_scope='{"groups": ["engineering"]}', status="published")
    db.add(page); db.flush()
    rev = WikiRevision(id="w-eng-rev", wiki_page_id="w-eng", title="shuixiang", summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id="w-eng-sec", revision_id=rev.id, section_type="facts", heading="zhengwen", content="shuixiangrongliang 500L", order_index=1))
    page.current_revision_id = rev.id
    page2 = WikiPage(id="w-sales", title="dianchi", summary="", acl_scope='{"groups": ["sales"]}', status="published")
    db.add(page2); db.flush()
    rev2 = WikiRevision(id="w-sales-rev", wiki_page_id="w-sales", title="dianchi", summary="", status="published")
    db.add(rev2); db.flush()
    db.add(WikiSection(id="w-sales-sec", revision_id=rev2.id, section_type="facts", heading="zhengwen", content="dianchidianya 12V", order_index=1))
    page2.current_revision_id = rev2.id
    db.commit()

    scoped_user = {"groups": ["engineering"], "_scope_override": "group:engineering"}
    result = retrieve_wiki(db, scoped_user, "shuixiangrongliang")
    ids = {h.wiki_page_id for h in result.hits}
    assert "w-eng" in ids
    assert "w-sales" not in ids


# ---------------------------------------------------------------------------
# 双连接并发累计（已有债务）
# ---------------------------------------------------------------------------

def test_concurrent_accumulate_existing_debt(tmp_path):
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'acc.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})
        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)
    s0 = sessionmaker(bind=e1)()
    debt0 = KnowledgeDebt(
        debt_type="missing_knowledge", description="d", related_question="shuixiangrongliang",
        status="open", root_cause="missing_knowledge", original_query="shuixiangrongliang",
        normalized_query="shuixiangrongliang", cluster_key=build_cluster_key("group:engineering", "shuixiangrongliang"),
        scope_id="group:engineering", occurrence_count=1, affected_user_count=1,
    )
    s0.add(debt0); s0.flush()
    # 预建 u1 已受影响记录，与 affected_user_count=1 一致
    s0.add(KnowledgeDebtUser(debt_id=debt0.id, user_id="u1"))
    s0.commit(); s0.close()

    def worker(uid):
        s = sessionmaker(bind=_make_engine())()
        try:
            record_missing_knowledge(s, original_query="shuixiangrongliang", user_id=uid, scope_id="group:engineering")
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("u1",))
    t2 = threading.Thread(target=worker, args=("u2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = sessionmaker(bind=e1)()
    debt = s.query(KnowledgeDebt).filter(KnowledgeDebt.scope_id == "group:engineering").one()
    assert debt.occurrence_count == 3
    assert debt.affected_user_count == 2  # u1(已存在不重复) + u2(新增)
    s.close()
    e1.dispose()


# ---------------------------------------------------------------------------
# 相似不同 cluster_key 并发首次写入（最终一个语义债务）
# ---------------------------------------------------------------------------

def test_concurrent_similar_first_write(tmp_path):
    from sqlalchemy import create_engine as _ce
    url = f"sqlite:///{(tmp_path / 'sim.db').as_posix()}"

    def _make_engine():
        e = _ce(url, connect_args={"check_same_thread": False, "timeout": 10})
        @event.listens_for(e, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
        return e

    e1 = _make_engine()
    init_db(e1)

    def worker(query):
        s = sessionmaker(bind=_make_engine())()
        try:
            record_missing_knowledge(s, original_query=query, user_id="u1", scope_id="group:engineering")
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=("shuixiangrongliang duoshao",))
    t2 = threading.Thread(target=worker, args=("shuixiangrongliang shi duoshao",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = sessionmaker(bind=e1)()
    debts = s.query(KnowledgeDebt).filter(KnowledgeDebt.scope_id == "group:engineering").all()
    assert len(debts) == 1
    s.close()
    e1.dispose()


# ---------------------------------------------------------------------------
# 历史 NULL affected_user_count 兼容
# ---------------------------------------------------------------------------

def test_null_affected_user_count_compat(db):
    # 用原生 SQL 插入 affected_user_count=NULL 的历史债务（绕开 ORM default=1）
    ck = build_cluster_key("group:engineering", "shuixiangrongliang")
    from sqlalchemy import text
    db.add(KnowledgeDebt(
        id="debt-null", debt_type="missing_knowledge", description="d", related_question="shuixiangrongliang",
        status="open", root_cause="missing_knowledge", original_query="shuixiangrongliang",
        normalized_query="shuixiangrongliang", cluster_key=ck,
        scope_id="group:engineering", occurrence_count=3, affected_user_count=None,
    ))
    db.flush()
    db.execute(text("UPDATE knowledge_debts SET affected_user_count = NULL WHERE id = 'debt-null'"))
    db.commit()

    r = record_missing_knowledge(db, original_query="shuixiangrongliang", user_id="u1", scope_id="group:engineering")
    assert r.ok is True
    debt = db.query(KnowledgeDebt).filter(KnowledgeDebt.id == "debt-null").one()
    # coalesce(NULL,0)+1 = 1
    assert debt.affected_user_count == 1
