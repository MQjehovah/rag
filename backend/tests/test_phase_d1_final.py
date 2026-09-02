"""V4 Phase D-1 最终验收补丁测试。

覆盖：Revision 归属安全 + 有意义 token + 保守充分性判断。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.sufficiency_judge import judge_wiki_sufficiency
from app.core.retrieval.wiki_retriever import (
    meaningful_tokenize,
    retrieve_wiki,
)
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


def _user(groups):
    return {"id": "u1", "username": "u", "groups": groups}


def _seed_wiki(db, page_id, title, summary, content, acl_scope, status="published", rev_status="published", rev_wiki_id=None, rev_id=None):
    page = WikiPage(id=page_id, title=title, summary=summary, acl_scope=acl_scope, status=status)
    db.add(page); db.flush()
    rev = WikiRevision(
        id=rev_id or f"{page_id}-rev",
        wiki_page_id=rev_wiki_id or page_id,
        title=title,
        summary=summary,
        status=rev_status,
    )
    db.add(rev); db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.flush()


# ---- 一、Revision 归属安全 ----

def test_wrong_revision_ownership_not_retrieved(db):
    # 先建 sales wiki + sales revision
    db.add(WikiPage(id="w-sales", title="销售主题", summary="", acl_scope='{"groups": ["sales"]}', status="published"))
    db.flush()
    sales_rev = WikiRevision(id="sales-rev", wiki_page_id="w-sales", title="销售", summary="", status="published")
    db.add(sales_rev); db.flush()
    # engineering wiki 的 current_revision_id 错误指向 sales 的 revision
    eng = WikiPage(id="w-eng", title="工程主题", summary="工程摘要", acl_scope='{"groups": ["engineering"]}', status="published", current_revision_id="sales-rev")
    db.add(eng); db.flush()
    db.commit()

    result = retrieve_wiki(db, _user(["engineering"]), "工程主题")
    ids = {h.wiki_page_id for h in result.hits}
    assert "w-eng" not in ids  # 归属非法 → 不入 hits


def test_draft_revision_not_retrieved(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定", '{"groups": ["engineering"]}', rev_status="draft")
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    assert result.hits == []


def test_missing_revision_not_retrieved(db):
    page = WikiPage(id="w1", title="水箱安装", summary="摘要", acl_scope='{"groups": ["engineering"]}', status="published", current_revision_id="nonexistent")
    db.add(page); db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    assert result.hits == []


def test_valid_revision_retrieved(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    assert len(result.hits) >= 1
    assert result.hits[0].wiki_page_id == "w1"


# ---- 二、有意义 token ----

def test_meaningful_tokenize_stopwords_and_punct():
    assert meaningful_tokenize("请问如何安装水箱？") == ["安装", "水箱"]
    assert meaningful_tokenize("？？？") == []
    assert meaningful_tokenize("如何") == []
    assert meaningful_tokenize("怎么") == []


def test_empty_or_stopword_query_no_hit(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定", '{"groups": ["engineering"]}')
    db.commit()
    assert retrieve_wiki(db, _user(["engineering"]), "？？").hits == []
    assert retrieve_wiki(db, _user(["engineering"]), "如何怎么").hits == []


# ---- 三、充分性误判 ----

def test_single_common_word_insufficient(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "安装")
    verdict = judge_wiki_sufficiency(result, "安装")
    # 单个普通词"安装"非精确标题匹配 → 一律 insufficient
    assert verdict.sufficient is False
    assert verdict.reason == "single_token_insufficient"


def test_title_exact_match_sufficient(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    verdict = judge_wiki_sufficiency(result, "水箱安装")
    assert verdict.sufficient is True
    assert verdict.reason == "exact_title_match"


def test_two_domain_terms_sufficient(db):
    _seed_wiki(db, "w1", "水箱维护指南", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱 螺栓")
    verdict = judge_wiki_sufficiency(result, "水箱 螺栓")
    assert verdict.sufficient is True


def test_single_domain_term_in_title_insufficient(db):
    # 单个领域词仅出现在标题 → 删除 single_term_in_title 自动充分规则 → insufficient
    _seed_wiki(db, "w1", "水箱维护指南", "摘要", "水箱固定到机架并拧紧螺栓", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱")
    verdict = judge_wiki_sufficiency(result, "水箱")
    assert verdict.sufficient is False
    assert verdict.reason == "single_token_insufficient"


def test_empty_wiki_content_insufficient(db):
    page = WikiPage(id="w1", title="水箱安装", summary="", acl_scope='{"groups": ["engineering"]}', status="published")
    db.add(page); db.flush()
    rev = WikiRevision(id="r1", wiki_page_id="w1", title="水箱安装", summary="", status="published")
    db.add(rev); db.flush()
    page.current_revision_id = "r1"
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    # 正文为空 → 不产生 hit（content 无有效 token）
    assert result.hits == [] or all(not h.content for h in result.hits)


def test_cross_group_still_invisible(db):
    _seed_wiki(db, "w-eng", "工程主题", "摘要", "工程内容", '{"groups": ["engineering"]}')
    db.commit()
    result = retrieve_wiki(db, _user(["sales"]), "工程主题")
    assert result.hits == []
