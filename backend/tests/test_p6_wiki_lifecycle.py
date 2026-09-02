"""P6-BE-04/05/06/07：Wiki 生命周期测试。Windows 用 StaticPool。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.knowledge_compiler_v3.wiki_lifecycle import (
    archive_wiki_page,
    diff_wiki_revisions,
    publish_wiki_revision,
    rollback_wiki,
    update_locked_section,
)
from app.models.database import (
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


@pytest.fixture()
def lc_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    yield engine
    engine.dispose()


def _session(engine):
    return sessionmaker(bind=engine)()


def _make_page_with_revisions(db, n=2):
    page = WikiPage(id=str(uuid.uuid4()), title="t", summary="s")
    db.add(page)
    db.flush()
    revs = []
    for i in range(n):
        rev = WikiRevision(id=str(uuid.uuid4()), wiki_page_id=page.id, title=f"t{i}", summary="")
        db.add(rev)
        db.flush()
        sec = WikiSection(id=str(uuid.uuid4()), revision_id=rev.id, section_type="summary",
                          heading="摘要", content=f"内容 {i}", order_index=0)
        db.add(sec)
        revs.append(rev)
    page.current_revision_id = revs[0].id
    db.commit()
    return page, revs


def test_publish_revision_supersedes_old(lc_db):
    db = _session(lc_db)
    page, revs = _make_page_with_revisions(db)
    publish_wiki_revision(db, page.id, revs[0].id)
    superseded = publish_wiki_revision(db, page.id, revs[1].id)
    assert revs[0].id in superseded
    assert revs[1].status == "published"
    assert page.current_revision_id == revs[1].id
    db.close()


def test_rollback(lc_db):
    db = _session(lc_db)
    page, revs = _make_page_with_revisions(db)
    publish_wiki_revision(db, page.id, revs[1].id)
    rollback_wiki(db, page.id, revs[0].id)
    assert page.current_revision_id == revs[0].id
    assert revs[0].status == "published"
    db.close()


def test_diff_wiki_revisions(lc_db):
    db = _session(lc_db)
    page, revs = _make_page_with_revisions(db)
    diff = diff_wiki_revisions(db, revs[0].id, revs[1].id)
    assert not diff.is_empty
    assert len(diff.changed_sections) == 1
    db.close()


def test_locked_section_blocks_update(lc_db):
    db = _session(lc_db)
    page, revs = _make_page_with_revisions(db, n=1)
    sec = db.query(WikiSection).filter(WikiSection.revision_id == revs[0].id).first()
    sec.locked = True
    db.commit()

    result = update_locked_section(db, sec.id, "新内容")
    assert result is False
    db.refresh(sec)
    assert sec.content == "内容 0"
    db.close()


def test_unlocked_section_updates(lc_db):
    db = _session(lc_db)
    page, revs = _make_page_with_revisions(db, n=1)
    sec = db.query(WikiSection).filter(WikiSection.revision_id == revs[0].id).first()
    result = update_locked_section(db, sec.id, "新内容")
    assert result is True
    db.refresh(sec)
    assert sec.content == "新内容"
    db.close()


def test_archive_wiki_page(lc_db):
    db = _session(lc_db)
    page, _revs = _make_page_with_revisions(db, n=1)
    archive_wiki_page(db, page.id)
    db.refresh(page)
    assert page.status == "archived"
    db.close()
