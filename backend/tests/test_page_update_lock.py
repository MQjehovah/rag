"""非协作模式乐观锁: PUT /api/pages/{id} 的 base_updated_at 校验。

- base 过期(或不可解析) → 409, 且不落库、不产生修订;
- base 匹配(秒级) → 200, 行为与旧客户端一致;
- 未传 base(旧客户端/协作模式) → 200, 行为不变。
"""
from datetime import datetime, timedelta

import app.api.pages as pages
from app.models.database import Page, PageRevision, get_session


async def _noop_async(*args, **kwargs):
    """替代真实后台索引任务, 避免测试触发 embedding 调用。"""
    return None


def _seed_page(engine, content="v1"):
    db = get_session(engine)
    try:
        db.add(Page(id="p-lock", notebook_id=None, title="锁测试", content=content))
        db.commit()
    finally:
        db.close()


def _page_content(engine):
    db = get_session(engine)
    try:
        return db.query(Page).filter(Page.id == "p-lock").first().content
    finally:
        db.close()


def _revision_count(engine):
    db = get_session(engine)
    try:
        return db.query(PageRevision).filter(PageRevision.page_id == "p-lock").count()
    finally:
        db.close()


def test_stale_base_conflicts_without_write_or_revision(api_client, api_engine, as_user, monkeypatch):
    """base 过期 → 409: 内容未变、未产生修订。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_page(api_engine)
    as_user([])
    base = api_client.get("/api/pages/p-lock").json()["updated_at"]

    # 模拟他人先保存: 服务端 updated_at 前移
    db = get_session(api_engine)
    try:
        db.query(Page).filter(Page.id == "p-lock").update(
            {Page.updated_at: datetime.now() + timedelta(seconds=30)}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    res = api_client.put("/api/pages/p-lock", json={"content": "v2", "base_updated_at": base})
    assert res.status_code == 409
    assert res.json()["detail"] == "页面已被他人修改，请刷新后再保存"
    assert _page_content(api_engine) == "v1"
    assert _revision_count(api_engine) == 0


def test_malformed_base_fails_closed(api_client, api_engine, as_user, monkeypatch):
    """base 无法解析 → 按 409 fail-closed, 不落库不记修订。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_page(api_engine)
    as_user([])
    res = api_client.put("/api/pages/p-lock", json={"content": "v2", "base_updated_at": "不是时间"})
    assert res.status_code == 409
    assert _page_content(api_engine) == "v1"
    assert _revision_count(api_engine) == 0


def test_matching_base_second_precision_updates(api_client, api_engine, as_user, monkeypatch):
    """base 与服务端秒级一致(容忍毫秒/格式差异) → 200。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_page(api_engine)
    as_user([])
    db = get_session(api_engine)
    try:
        db.query(Page).filter(Page.id == "p-lock").update(
            {Page.updated_at: datetime(2026, 1, 2, 3, 4, 5, 678901)}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    res = api_client.put("/api/pages/p-lock", json={"content": "v2", "base_updated_at": "2026-01-02T03:04:05"})
    assert res.status_code == 200
    assert _page_content(api_engine) == "v2"

    # 回传服务端原样时间(含毫秒)同样通过
    res2 = api_client.put(
        "/api/pages/p-lock",
        json={"content": "v3", "base_updated_at": res.json()["updated_at"]},
    )
    assert res2.status_code == 200
    assert _page_content(api_engine) == "v3"


def test_update_without_base_keeps_legacy_behavior(api_client, api_engine, as_user, monkeypatch):
    """未传 base(旧客户端/协作模式) → 200, 行为不变。"""
    monkeypatch.setattr(pages, "background_index_page", _noop_async)
    _seed_page(api_engine)
    as_user([])
    res = api_client.put("/api/pages/p-lock", json={"content": "v2"})
    assert res.status_code == 200
    assert _page_content(api_engine) == "v2"
