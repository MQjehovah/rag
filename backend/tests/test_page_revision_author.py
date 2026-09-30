"""修订记录作者: 快照写入当前用户 + 列表/详情返回 + 旧行空串 + 迁移幂等。"""
from sqlalchemy import inspect, text as sql_text

import app.api.pages as pages
from app.models.database import (
    Page,
    PageRevision,
    get_engine,
    get_session,
    init_db,
    run_page_revision_author_migrations,
)


async def _noop_async(*args, **kwargs):
    return None


def _seed_page(engine, pid="p-rev"):
    db = get_session(engine)
    try:
        db.add(Page(id=pid, notebook_id=None, title="修订页", content="v1"))
        db.commit()
    finally:
        db.close()


def test_snapshot_records_current_author(api_client, api_engine, as_user, monkeypatch):
    """update_page 生成快照时写入 current_user 的 id/显示名, 列表与详情均返回。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_page(api_engine)
    as_user([], id="u-author", name="作者甲")

    res = api_client.put("/api/pages/p-rev", json={"content": "v2"})
    assert res.status_code == 200

    items = api_client.get("/api/pages/p-rev/revisions").json()["items"]
    assert len(items) == 1
    assert items[0]["author_id"] == "u-author"
    assert items[0]["author_name"] == "作者甲"
    assert items[0]["editor"] == "作者甲"  # 兼容旧字段

    detail = api_client.get(f"/api/pages/p-rev/revisions/{items[0]['id']}").json()
    assert detail["author_id"] == "u-author"
    assert detail["author_name"] == "作者甲"


def test_legacy_revision_without_author_returns_empty(api_client, api_engine, as_user):
    """迁移前旧行作者列为 NULL 时, 列表/详情归一为空串。"""
    _seed_page(api_engine)
    db = get_session(api_engine)
    try:
        db.add(PageRevision(id="r-legacy", page_id="p-rev", title="旧版本", content="old", editor=""))
        db.commit()
        db.execute(sql_text(
            "UPDATE page_revisions SET author_id = NULL, author_name = NULL WHERE id = 'r-legacy'"
        ))
        db.commit()
    finally:
        db.close()

    as_user([])
    legacy = [i for i in api_client.get("/api/pages/p-rev/revisions").json()["items"]
              if i["id"] == "r-legacy"][0]
    assert legacy["author_id"] == "" and legacy["author_name"] == ""

    detail = api_client.get("/api/pages/p-rev/revisions/r-legacy").json()
    assert detail["author_id"] == "" and detail["author_name"] == ""


def test_author_column_migration_is_idempotent(tmp_path):
    """老库(无作者列)经启动迁移补列并回填空串; 重复调用/重复启动无副作用。"""
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(sql_text(
                "CREATE TABLE page_revisions ("
                "id VARCHAR(36) PRIMARY KEY, page_id VARCHAR(36), title VARCHAR(255), "
                "content TEXT, editor VARCHAR(255), created_at DATETIME)"
            ))
            conn.execute(sql_text(
                "INSERT INTO page_revisions (id, page_id, title, content, editor, created_at) "
                "VALUES ('r1', 'p1', 't', 'c', '老用户', '2026-01-01 00:00:00')"
            ))

        init_db(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("page_revisions")}
        assert {"author_id", "author_name"} <= cols
        with engine.begin() as conn:
            row = conn.execute(sql_text(
                "SELECT author_id, author_name FROM page_revisions WHERE id = 'r1'"
            )).fetchone()
        assert row == ("", "")

        run_page_revision_author_migrations(engine)
        init_db(engine)
        with engine.begin() as conn:
            row = conn.execute(sql_text(
                "SELECT author_id, author_name FROM page_revisions WHERE id = 'r1'"
            )).fetchone()
        assert row == ("", "")
    finally:
        engine.dispose()
