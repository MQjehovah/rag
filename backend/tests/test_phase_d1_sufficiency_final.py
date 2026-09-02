"""V4 Phase D-1 充分性判断收口测试。

覆盖任务四列出的误判场景：单普通词、单领域词、精确标题匹配、
标题包含在更长问题中、去重 coverage、Revision 归属安全、跨组不可见，
以及 WikiHit 输出结构的可解释性与稳定性。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
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


ENG = '{"groups": ["engineering"]}'


# ---- 场景 1：查询“安装”，标题“水箱安装”，正文包含“安装” → insufficient ----

def test_single_common_word_insufficient_even_if_in_content(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱安装步骤：先固定机架再拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "安装")
    assert len(result.hits) >= 1
    verdict = judge_wiki_sufficiency(result, "安装")
    assert verdict.sufficient is False
    assert verdict.reason == "single_token_insufficient"


# ---- 场景 2：查询“水箱”，标题“水箱维护指南” → insufficient ----

def test_single_domain_word_in_longer_title_insufficient(db):
    _seed_wiki(db, "w1", "水箱维护指南", "摘要", "水箱固定到机架并拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱")
    verdict = judge_wiki_sufficiency(result, "水箱")
    assert verdict.sufficient is False
    assert verdict.reason == "single_token_insufficient"


# ---- 场景 3：查询“MobaXterm”，标题“MobaXterm” → exact_title_match → sufficient ----

def test_exact_title_match_sufficient_single_token(db):
    _seed_wiki(db, "w1", "MobaXterm", "摘要", "MobaXterm 是一个终端工具", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "MobaXterm")
    assert len(result.hits) >= 1
    assert result.hits[0].exact_title_match is True
    verdict = judge_wiki_sufficiency(result, "MobaXterm")
    assert verdict.sufficient is True
    assert verdict.reason == "exact_title_match"


# ---- 场景 4：查询“水箱安装出现故障”，标题“水箱安装”，正文没有“故障” → insufficient ----

def test_title_contained_longer_query_insufficient(db):
    # 正文只有安装方法，无“故障”/“出现”
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱安装方法：固定机架并拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装出现故障")
    assert len(result.hits) >= 1
    hit = result.hits[0]
    assert hit.exact_title_match is False
    assert hit.title_contained is True
    verdict = judge_wiki_sufficiency(result, "水箱安装出现故障")
    assert verdict.sufficient is False
    assert verdict.reason == "insufficient_coverage"
    assert "故障" in verdict.missing_aspects


# ---- 场景 5：查询“水箱安装出现故障”，正文确实包含故障处理 → sufficient ----

def test_title_contained_but_covered_sufficient(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱安装出现故障时请检查螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装出现故障")
    verdict = judge_wiki_sufficiency(result, "水箱安装出现故障")
    assert verdict.sufficient is True
    assert verdict.reason == "multi_token_coverage"


# ---- 场景 6：查询“水箱 螺栓”，两个有效 token 均命中 → sufficient ----

def test_two_tokens_both_matched_sufficient(db):
    _seed_wiki(db, "w1", "水箱维护指南", "摘要", "水箱固定到机架并拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱 螺栓")
    verdict = judge_wiki_sufficiency(result, "水箱 螺栓")
    assert verdict.sufficient is True


# ---- 场景 7：重复查询 token，coverage 按去重 token 计算 ----

def test_duplicate_query_tokens_do_not_inflate_coverage(db):
    _seed_wiki(db, "w1", "水箱维护指南", "摘要", "水箱固定到机架并拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱 水箱 水箱 故障")
    hit = result.hits[0]
    # 去重后有效 query token = 水箱、故障（不是 4 个）
    assert hit.query_tokens == ["水箱", "故障"]
    assert hit.query_token_count == 2
    assert hit.matched_tokens == ["水箱"]
    assert hit.missing_tokens == ["故障"]
    assert hit.coverage == 0.5
    verdict = judge_wiki_sufficiency(result, "水箱 水箱 水箱 故障")
    assert verdict.sufficient is False


# ---- 场景 8：查询只有标点或停用词 → 无 WikiHit / insufficient ----

def test_punctuation_or_stopword_only_no_hit(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定到机架并拧紧螺栓", ENG)
    db.commit()
    assert retrieve_wiki(db, _user(["engineering"]), "？？？").hits == []
    assert retrieve_wiki(db, _user(["engineering"]), "如何怎么").hits == []


# ---- 场景 9：current Revision 归属错误 → 不进入 hits ----

def test_wrong_revision_ownership_not_in_hits(db):
    db.add(WikiPage(id="w-sales", title="销售主题", summary="", acl_scope='{"groups": ["sales"]}', status="published"))
    db.flush()
    sales_rev = WikiRevision(id="sales-rev", wiki_page_id="w-sales", title="销售", summary="", status="published")
    db.add(sales_rev); db.flush()
    eng = WikiPage(
        id="w-eng", title="工程主题", summary="工程摘要",
        acl_scope='{"groups": ["engineering"]}', status="published", current_revision_id="sales-rev",
    )
    db.add(eng); db.commit()

    result = retrieve_wiki(db, _user(["engineering"]), "工程主题")
    assert "w-eng" not in {h.wiki_page_id for h in result.hits}


# ---- 场景 10：current Revision 为 draft → 不进入 hits ----

def test_draft_current_revision_not_in_hits(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱固定", ENG, rev_status="draft")
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装")
    assert result.hits == []


# ---- 场景 11：跨组 Wiki → 不可见 ----

def test_cross_group_invisible(db):
    _seed_wiki(db, "w-eng", "工程主题", "摘要", "工程内容", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["sales"]), "工程主题")
    assert result.hits == []


# ---- 场景 12：输出结构可解释且稳定 ----

def test_match_fields_stable_and_explainable(db):
    _seed_wiki(db, "w1", "水箱安装", "摘要", "水箱安装方法：固定机架并拧紧螺栓", ENG)
    db.commit()
    result = retrieve_wiki(db, _user(["engineering"]), "水箱安装出现故障")
    hit = result.hits[0]
    assert hit.exact_title_match is False
    assert hit.title_contained is True
    assert hit.query_tokens == ["水箱", "安装", "出现", "故障"]
    assert hit.matched_tokens == ["水箱", "安装"]
    assert hit.missing_tokens == ["出现", "故障"]
    assert hit.query_token_count == 4
    assert hit.coverage == 0.5
    verdict = judge_wiki_sufficiency(result, "水箱安装出现故障")
    assert verdict.sufficient is False
    assert verdict.reason == "insufficient_coverage"
    assert verdict.missing_aspects == ["出现", "故障"]
