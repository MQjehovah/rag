"""V4 Phase D-1：Wiki 优先检索底座测试。

覆盖：Wiki 命中、不足、draft/archived 不可检索、跨组不可见、管理员权限、
GLM 不可用规则判断、不 import Card 模型。
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.orchestrator import RetrievalOrchestrator
from app.core.retrieval.sufficiency_judge import judge_wiki_sufficiency
from app.core.retrieval.wiki_retriever import retrieve_wiki
from app.models.database import (
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


def _seed_wiki(db, page_id, title, summary, content, acl_scope, status="published"):
    page = WikiPage(id=page_id, title=title, summary=summary, acl_scope=acl_scope, status=status)
    db.add(page); db.flush()
    rev = WikiRevision(id=f"{page_id}-rev", wiki_page_id=page_id, title=title, summary=summary, status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.flush()


def _user(groups):
    return {"id": "u1", "username": "u", "groups": groups}


# 1. Wiki 命中
def test_wiki_hit(db):
    _seed_wiki(db, "w1", "水箱安装", "水箱安装摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    assert len(result.hits) >= 1
    hit = result.hits[0]
    assert hit.wiki_page_id == "w1"
    assert hit.title == "水箱安装"
    assert "水箱" in hit.content


# 2. Wiki 不足（无命中）
def test_wiki_insufficient(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "完全无关的电池问题")
    assert result.hits == []


# 3. draft/archived 不可检索
def test_draft_archived_not_retrieved(db):
    _seed_wiki(db, "w-draft", "草稿主题", "摘要", "内容", '{"groups": ["engineering"]}', status="draft")
    _seed_wiki(db, "w-archived", "归档主题", "摘要", "内容", '{"groups": ["engineering"]}', status="archived")
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "主题")
    ids = {h.wiki_page_id for h in result.hits}
    assert "w-draft" not in ids
    assert "w-archived" not in ids


# 4. 跨组不可见
def test_cross_group_invisible(db):
    _seed_wiki(db, "w-eng", "工程主题", "摘要", "工程内容", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["sales"]), "工程主题")
    assert result.hits == []


# 5. 管理员权限
def test_admin_sees_all(db):
    _seed_wiki(db, "w-eng", "工程主题", "摘要", "工程内容与部署规范", '{"groups": ["engineering"]}')
    _seed_wiki(db, "w-sales", "销售主题", "摘要", "销售内容与报价规则", '{"groups": ["sales"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["admins"]), "工程 销售")
    ids = {h.wiki_page_id for h in result.hits}
    assert "w-eng" in ids
    assert "w-sales" in ids


# 6. GLM 不可用规则判断（judge 默认确定性，无需 LLM）
def test_judge_deterministic_without_llm(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    verdict = judge_wiki_sufficiency(result, "水箱安装")
    assert verdict.sufficient is True
    assert verdict.reason == "exact_title_match"

    empty = retrieve_wiki(db, _user(["engineering"]), "无关问题")
    verdict2 = judge_wiki_sufficiency(empty, "无关问题")
    assert verdict2.sufficient is False
    assert verdict2.reason == "no_wiki_hit"


# 7. 代码不 import Card 模型
def test_no_card_imports():
    import app.core.retrieval.wiki_retriever as wr
    import app.core.retrieval.sufficiency_judge as sj
    import app.core.retrieval.orchestrator as oc
    for mod in (wr, sj, oc):
        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in ("KnowledgeCard", "KnowledgeCardBlock", "KnowledgeCommunity", "KnowledgeObject"):
            assert forbidden not in joined


# 编排器：wiki_hit /（Wiki 与原始文档均不足时）need_community_expansion
def test_orchestrator_wiki_hit_and_need_raw(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    orch = RetrievalOrchestrator(db)

    out_hit = orch.retrieve("水箱安装", _user(["engineering"]))
    assert out_hit.mode == "wiki_hit"
    assert out_hit.wiki_results is not None
    assert out_hit.verdict.sufficient is True

    out_miss = orch.retrieve("完全无关的电池问题", _user(["engineering"]))
    assert out_miss.mode == "need_community_expansion"
    assert out_miss.trace["mode"] == "need_community_expansion"
