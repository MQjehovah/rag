"""用户级接口(/api/me/*):最近访问/收藏/模板/聊天记录 服务端按 user_id 隔离。"""
from datetime import datetime, timedelta

from app.models.database import (
    Notebook,
    Page,
    UserChatMessage,
    UserPageFavorite,
    UserRecentPage,
    UserTemplate,
    get_session,
)


def _seed(api_engine):
    """公共本 + A 私有本 + 无归属页 + 已删页。"""
    db = get_session(api_engine)
    try:
        db.add(Notebook(id="nb-pub", name="公共本", visibility="public", owner_id="u-a"))
        db.add(Page(id="p-pub", notebook_id="nb-pub", title="公共页", icon="📄", content="x"))
        db.add(Notebook(id="nb-a", name="A的私有本", visibility="self", owner_id="u-a"))
        db.add(Page(id="p-a", notebook_id="nb-a", title="A私有页", content="x"))
        db.add(Page(id="p-free", notebook_id=None, title="无归属页", content="x"))
        db.add(Page(id="p-del", notebook_id="nb-pub", title="已删页", content="x",
                    deleted_at=datetime.now()))
        db.commit()
    finally:
        db.close()


def _login_a(as_user):
    return as_user([], id="u-a")


def _login_b(as_user):
    return as_user([], id="u-b")


def test_me_data_isolated_between_users(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    assert api_client.post("/api/me/favorites", json={"page_id": "p-pub"}).json() == {"ok": True}
    assert api_client.post("/api/me/recent-pages", json={"page_id": "p-pub"}).status_code == 200
    tpl = api_client.post("/api/me/templates", json={"name": "我的模板", "content": "正文"}).json()
    msg = api_client.post("/api/me/chat-messages", json={
        "role": "user", "content": "你好", "sources": [{"page_id": "p-pub"}],
    }).json()

    _login_b(as_user)
    assert api_client.get("/api/me/favorites").json()["items"] == []
    assert api_client.get("/api/me/recent-pages").json()["items"] == []
    assert api_client.get("/api/me/templates").json()["items"] == []
    assert api_client.get("/api/me/chat-messages").json()["items"] == []
    # B 改/删 A 的模板 → 404(按 id 的更新必须带 user_id 条件)
    assert api_client.put(f"/api/me/templates/{tpl['id']}",
                          json={"name": "改名", "content": "x"}).status_code == 404
    assert api_client.delete(f"/api/me/templates/{tpl['id']}").status_code == 404
    # B 清空自己的数据,不影响 A
    assert api_client.delete("/api/me/favorites").json() == {"ok": True}
    assert api_client.delete("/api/me/recent-pages").json() == {"ok": True}
    assert api_client.delete("/api/me/chat-messages").json() == {"ok": True}

    _login_a(as_user)
    assert [t["id"] for t in api_client.get("/api/me/templates").json()["items"]] == [tpl["id"]]
    chat_items = api_client.get("/api/me/chat-messages").json()["items"]
    assert [m["id"] for m in chat_items] == [msg["id"]]
    assert chat_items[0]["sources"] == [{"page_id": "p-pub"}]
    assert [i["id"] for i in api_client.get("/api/me/favorites").json()["items"]] == ["p-pub"]
    assert [i["id"] for i in api_client.get("/api/me/recent-pages").json()["items"]] == ["p-pub"]


def test_recent_pages_upsert_order_and_limit(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    for pid in ("p-pub", "p-free", "p-a"):
        assert api_client.post("/api/me/recent-pages", json={"page_id": pid}).status_code == 200
    assert len(api_client.get("/api/me/recent-pages").json()["items"]) == 3  # upsert 去重

    # 本机 datetime.now 精度粗(约 15ms),直接落库为可区分的访问时间,验证倒序
    base = datetime.now() - timedelta(hours=1)
    db = get_session(api_engine)
    try:
        stamps = {"p-pub": base, "p-free": base + timedelta(minutes=1),
                  "p-a": base + timedelta(minutes=2)}
        for pid, ts in stamps.items():
            db.query(UserRecentPage).filter(UserRecentPage.page_id == pid).update(
                {UserRecentPage.visited_at: ts}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    items = api_client.get("/api/me/recent-pages").json()["items"]
    assert [i["id"] for i in items] == ["p-a", "p-free", "p-pub"]
    assert items[0]["title"] == "A私有页" and items[2]["icon"] == "📄"

    # 重访最早一条 → 提到最前且不重复
    assert api_client.post("/api/me/recent-pages", json={"page_id": "p-pub"}).status_code == 200
    items = api_client.get("/api/me/recent-pages").json()["items"]
    assert [i["id"] for i in items] == ["p-pub", "p-a", "p-free"]

    # limit 生效 + clamp 1..50
    assert [i["id"] for i in api_client.get("/api/me/recent-pages?limit=2").json()["items"]] == ["p-pub", "p-a"]
    assert len(api_client.get("/api/me/recent-pages?limit=0").json()["items"]) == 1
    assert len(api_client.get("/api/me/recent-pages?limit=99999999").json()["items"]) == 3

    # 清空本人
    assert api_client.delete("/api/me/recent-pages").json() == {"ok": True}
    assert api_client.get("/api/me/recent-pages").json()["items"] == []


def test_favorites_idempotent_order_and_remove(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    assert api_client.post("/api/me/favorites", json={"page_id": "p-pub"}).status_code == 200
    assert api_client.post("/api/me/favorites", json={"page_id": "p-pub"}).status_code == 200  # 幂等
    assert api_client.post("/api/me/favorites", json={"page_id": "p-free"}).status_code == 200
    assert len(api_client.get("/api/me/favorites").json()["items"]) == 2

    base = datetime.now() - timedelta(hours=1)
    db = get_session(api_engine)
    try:
        db.query(UserPageFavorite).filter(UserPageFavorite.page_id == "p-pub").update(
            {UserPageFavorite.created_at: base}, synchronize_session=False)
        db.query(UserPageFavorite).filter(UserPageFavorite.page_id == "p-free").update(
            {UserPageFavorite.created_at: base + timedelta(minutes=1)}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert [i["id"] for i in api_client.get("/api/me/favorites").json()["items"]] == ["p-free", "p-pub"]

    # 取消收藏幂等:重复删除/不存在均 200,且只删本人该条
    assert api_client.delete("/api/me/favorites/p-pub").json() == {"ok": True}
    assert api_client.delete("/api/me/favorites/p-pub").json() == {"ok": True}
    assert api_client.delete("/api/me/favorites/p-nope").json() == {"ok": True}
    assert [i["id"] for i in api_client.get("/api/me/favorites").json()["items"]] == ["p-free"]


def test_page_visibility_and_trash_filtering(api_client, api_engine, as_user):
    _seed(api_engine)
    # B 看不到 A 私有页;已删页/不存在页一律 404
    _login_b(as_user)
    for pid in ("p-a", "p-del", "nope"):
        assert api_client.post("/api/me/favorites", json={"page_id": pid}).status_code == 404
        assert api_client.post("/api/me/recent-pages", json={"page_id": pid}).status_code == 404
    assert api_client.post("/api/me/favorites", json={"page_id": "p-pub"}).status_code == 200

    # admin(跳过可见性过滤)可收藏 A 的私有页,但回收站仍不可见
    as_user(["__local_admin__"], id="u-admin")
    assert api_client.post("/api/me/favorites", json={"page_id": "p-a"}).status_code == 200
    assert api_client.post("/api/me/favorites", json={"page_id": "p-del"}).status_code == 404
    assert [i["id"] for i in api_client.get("/api/me/favorites").json()["items"]] == ["p-a"]

    # A 收藏自己的私有页后软删 → 从 GET 列表被过滤
    _login_a(as_user)
    assert api_client.post("/api/me/favorites", json={"page_id": "p-a"}).status_code == 200
    assert api_client.post("/api/me/recent-pages", json={"page_id": "p-a"}).status_code == 200
    db = get_session(api_engine)
    try:
        db.query(Page).filter(Page.id == "p-a").update(
            {Page.deleted_at: datetime.now()}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert api_client.get("/api/me/favorites").json()["items"] == []
    assert api_client.get("/api/me/recent-pages").json()["items"] == []


def test_templates_crud_validation_and_order(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    # 校验:name 去空格非空 ≤128;content ≤200_000
    assert api_client.post("/api/me/templates", json={"name": "   ", "content": "x"}).status_code == 400
    assert api_client.post("/api/me/templates", json={"name": "长" * 129, "content": ""}).status_code == 400
    assert api_client.post("/api/me/templates", json={"name": "ok", "content": "x" * 200_001}).status_code == 400

    t1 = api_client.post("/api/me/templates", json={"name": " 旧模板 ", "content": "一"}).json()
    assert t1["name"] == "旧模板"
    t2 = api_client.post("/api/me/templates", json={"name": "新模板", "content": "二"}).json()
    # updated_at 降序
    db = get_session(api_engine)
    try:
        base = datetime.now() - timedelta(hours=1)
        for tid, ts in ((t1["id"], base), (t2["id"], base + timedelta(minutes=1))):
            db.query(UserTemplate).filter(UserTemplate.id == tid).update(
                {UserTemplate.updated_at: ts}, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert [t["id"] for t in api_client.get("/api/me/templates").json()["items"]] == [t2["id"], t1["id"]]

    # PUT 校验同 create;仅本人(不存在 → 404)
    assert api_client.put(f"/api/me/templates/{t2['id']}",
                          json={"name": " ", "content": "x"}).status_code == 400
    assert api_client.put(f"/api/me/templates/{t2['id']}",
                          json={"name": "x", "content": "y" * 200_001}).status_code == 400
    assert api_client.put("/api/me/templates/t-nope", json={"name": "x", "content": ""}).status_code == 404
    updated = api_client.put(f"/api/me/templates/{t2['id']}",
                             json={"name": " 改名 ", "content": "新正文"}).json()
    assert updated["name"] == "改名" and updated["content"] == "新正文"

    # 删除本人模板;再删 → 404
    assert api_client.delete(f"/api/me/templates/{t2['id']}").json() == {"ok": True}
    assert api_client.delete(f"/api/me/templates/{t2['id']}").status_code == 404
    assert [t["id"] for t in api_client.get("/api/me/templates").json()["items"]] == [t1["id"]]


def test_chat_messages_order_limit_sources_and_validation(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    base = datetime.now() - timedelta(hours=1)
    db = get_session(api_engine)
    try:
        db.add(UserChatMessage(id="m1", user_id="u-a", role="user", content="一", created_at=base))
        db.add(UserChatMessage(id="m2", user_id="u-a", role="assistant", content="二",
                               sources='[{"page_id": "p-pub"}]', created_at=base + timedelta(minutes=1)))
        db.add(UserChatMessage(id="m3", user_id="u-a", role="user", content="三",
                               sources="not-json", created_at=base + timedelta(minutes=2)))
        db.add(UserChatMessage(id="m-other", user_id="u-b", role="user", content="别人的",
                               created_at=base + timedelta(minutes=3)))
        db.commit()
    finally:
        db.close()

    items = api_client.get("/api/me/chat-messages").json()["items"]
    assert [m["id"] for m in items] == ["m1", "m2", "m3"]  # created_at 升序,不含他用户
    assert items[1]["sources"] == [{"page_id": "p-pub"}]
    assert items[2]["sources"] == []  # 解析失败回 []
    assert [m["id"] for m in api_client.get("/api/me/chat-messages?limit=2").json()["items"]] == ["m1", "m2"]
    assert len(api_client.get("/api/me/chat-messages?limit=0").json()["items"]) == 1

    # 校验:role 仅 user|assistant;content 非空 ≤100_000
    assert api_client.post("/api/me/chat-messages", json={"role": "system", "content": "x"}).status_code == 400
    assert api_client.post("/api/me/chat-messages", json={"role": "", "content": "x"}).status_code == 400
    assert api_client.post("/api/me/chat-messages", json={"role": "user", "content": "   "}).status_code == 400
    assert api_client.post("/api/me/chat-messages", json={"role": "assistant", "content": ""}).status_code == 400
    assert api_client.post("/api/me/chat-messages", json={
        "role": "assistant", "content": "x" * 100_001,
    }).status_code == 400

    created = api_client.post("/api/me/chat-messages", json={"role": "assistant", "content": "回复"}).json()
    assert created["role"] == "assistant" and created["sources"] == []

    # 清空本人(不碰他用户)
    assert api_client.delete("/api/me/chat-messages").json() == {"ok": True}
    assert api_client.get("/api/me/chat-messages").json()["items"] == []
    db = get_session(api_engine)
    try:
        assert db.query(UserChatMessage).filter(UserChatMessage.user_id == "u-b").count() == 1
    finally:
        db.close()


def test_me_endpoints_require_auth(api_client):
    """未登录(无 Authorization 头)全部 401。"""
    calls = [
        ("get", "/api/me/recent-pages", None),
        ("post", "/api/me/recent-pages", {"page_id": "p-pub"}),
        ("delete", "/api/me/recent-pages", None),
        ("get", "/api/me/favorites", None),
        ("post", "/api/me/favorites", {"page_id": "p-pub"}),
        ("delete", "/api/me/favorites", None),
        ("delete", "/api/me/favorites/p-pub", None),
        ("get", "/api/me/templates", None),
        ("post", "/api/me/templates", {"name": "t", "content": ""}),
        ("put", "/api/me/templates/t-1", {"name": "t", "content": ""}),
        ("delete", "/api/me/templates/t-1", None),
        ("get", "/api/me/chat-messages", None),
        ("post", "/api/me/chat-messages", {"role": "user", "content": "x"}),
        ("delete", "/api/me/chat-messages", None),
    ]
    for method, url, body in calls:
        res = api_client.request(method.upper(), url, json=body)
        assert res.status_code == 401, f"{method} {url} -> {res.status_code}"
