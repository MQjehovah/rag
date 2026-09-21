"""检索管线 wiki 召回与来源合并测试(TDD)。

用假 embedding + 内存 SQLite,走 wiki_search 的 numpy 回退路径,避开 pgvector。
"""
import json
import uuid

import pytest

from app.config import settings
from app.core.retrieval import RetrievalPipeline
from app.models.database import Page, PageChunk, WikiPage, get_engine, get_session, init_db

DIM = 1024


def _vec(*idxs):
    v = [0.0] * DIM
    for i in idxs:
        v[i] = 1.0
    return v


class FakeEmbeddingService:
    """固定查询向量的假嵌入服务,避免真实网络调用。"""

    def __init__(self, vector):
        self.vector = vector

    async def encode_batch(self, texts, batch_size=32):
        return [self.vector for _ in texts]

    async def encode(self, text):
        return self.vector

    async def close(self):
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    # 关闭查询改写,否则长查询会触发真实 LLM 调用
    monkeypatch.setattr(settings, "llm_api_url", "")
    engine = get_engine(f"sqlite:///{tmp_path / 'retrieval.db'}")
    init_db(engine)
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


def _seed_page(db, page_id):
    """一个公共可见的笔记页 + 分块向量(让管线不因无可见页面而提前返回)。"""
    db.add(Page(id=page_id, notebook_id=None, title="笔记页", content="笔记正文内容"))
    db.add(PageChunk(
        id=str(uuid.uuid4()),
        page_id=page_id,
        chunk_index=0,
        content="笔记分块内容",
        embedding=json.dumps(_vec(0)),
    ))
    db.commit()


def _seed_wiki(db, page_id, title, group_id):
    db.add(WikiPage(
        id=page_id,
        title=title,
        summary=f"{title}摘要",
        content=f"{title}正文",
        category="测试",
        group_id=group_id,
        embedding=json.dumps(_vec(0)),
    ))
    db.commit()


async def _run(db, user_groups, top_k=5):
    pipeline = RetrievalPipeline(db, embedding_svc=FakeEmbeddingService(_vec(0)))
    return await pipeline.retrieve("查询", {"groups": user_groups}, top_k=top_k)


@pytest.mark.asyncio
async def test_wiki_hit_has_prefix_and_source(db):
    _seed_page(db, "page-1")
    wid = str(uuid.uuid4())
    _seed_wiki(db, wid, "公共Wiki", None)

    outcome = await _run(db, ["研发部"])

    wiki = [r for r in outcome["results"] if r["id"] == f"wiki:{wid}"]
    assert len(wiki) == 1
    assert "wiki" in wiki[0]["sources"]


@pytest.mark.asyncio
async def test_wiki_title_and_content_from_wiki_map(db):
    """title/content 非空,证明取的是独立 wiki_map 而不是 pages 的 page_map。"""
    _seed_page(db, "page-1")
    wid = str(uuid.uuid4())
    _seed_wiki(db, wid, "公共Wiki", None)

    outcome = await _run(db, ["研发部"])

    wiki = next(r for r in outcome["results"] if r["id"] == f"wiki:{wid}")
    assert wiki["title"] == "公共Wiki"
    assert wiki["content"] == "公共Wiki摘要"
    assert wiki["chunks"][0]["content"] == "公共Wiki摘要"


@pytest.mark.asyncio
async def test_wiki_and_page_coexist(db):
    """wiki 结果与页面结果并存,互不覆盖。"""
    _seed_page(db, "page-1")
    wid = str(uuid.uuid4())
    _seed_wiki(db, wid, "公共Wiki", None)

    outcome = await _run(db, ["研发部"])

    ids = {r["id"] for r in outcome["results"]}
    assert "page-1" in ids
    assert f"wiki:{wid}" in ids
    page = next(r for r in outcome["results"] if r["id"] == "page-1")
    assert page["title"] == "笔记页"
    assert "vector" in page["sources"]


@pytest.mark.asyncio
async def test_other_group_wiki_not_returned(db):
    _seed_page(db, "page-1")
    wid = str(uuid.uuid4())
    _seed_wiki(db, wid, "财务Wiki", "财务部")

    outcome = await _run(db, ["研发部"])

    assert all(not r["id"].startswith("wiki:") for r in outcome["results"])


@pytest.mark.asyncio
async def test_admin_sees_other_group_wiki(db):
    _seed_page(db, "page-1")
    wid = str(uuid.uuid4())
    _seed_wiki(db, wid, "财务Wiki", "财务部")

    outcome = await _run(db, ["__local_admin__"])

    assert f"wiki:{wid}" in {r["id"] for r in outcome["results"]}
