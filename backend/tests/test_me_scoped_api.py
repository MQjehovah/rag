"""用户级接口(/api/me/*):最近访问/收藏/模板/聊天记录 服务端按 user_id 隔离。"""
from datetime import datetime, timedelta

from app.models.database import (
    Notebook,
    Page,
    User,
    UserChatMessage,
    UserPageFavorite,
    UserRecentPage,
    UserTemplate,
    UserTemplateRevision,
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


def test_templates_visibility_shared_readonly(api_client, api_engine, as_user):
    """跨用户共享: public 他人可见(is_mine=False + owner), private 不可见; 他人不可改删。"""
    _seed(api_engine)
    db = get_session(api_engine)
    try:
        db.add(User(id="u-a", username="10086", name="甲"))
        db.add(User(id="u-b", username="10087", name="乙"))
        db.commit()
    finally:
        db.close()
    as_user([], id="u-a", name="甲")
    t_pub = api_client.post("/api/me/templates", json={
        "name": "公开模板", "content": "正文 {{姓名}}", "visibility": "public",
    }).json()
    t_priv = api_client.post("/api/me/templates", json={"name": "私有模板", "content": "私"}).json()
    assert t_pub["visibility"] == "public" and t_pub["is_mine"] is True
    assert t_priv["visibility"] == "private" and t_priv["owner"] == "甲"

    as_user([], id="u-b", name="乙")
    items = api_client.get("/api/me/templates").json()["items"]
    assert [t["id"] for t in items] == [t_pub["id"]]  # 私有不可见
    sh = items[0]
    assert sh["is_mine"] is False and sh["owner"] == "甲" and sh["content"] == "正文 {{姓名}}"
    # 他人 public 模板不可改/删(仅本人条目可操作)
    assert api_client.put(f"/api/me/templates/{t_pub['id']}",
                          json={"name": "改名", "content": "x"}).status_code == 404
    assert api_client.delete(f"/api/me/templates/{t_pub['id']}").status_code == 404
    # B 自己的模板与共享列表合并返回(B 新建的在前, updated_at 降序)
    t_b = api_client.post("/api/me/templates", json={"name": "乙模板", "content": "y"}).json()
    assert [t["id"] for t in api_client.get("/api/me/templates").json()["items"]] == [t_b["id"], t_pub["id"]]

    # A 看 B: 只有 public; A 自己的两条都在
    as_user([], id="u-a", name="甲")
    ids = [t["id"] for t in api_client.get("/api/me/templates").json()["items"]]
    assert set(ids) == {t_pub["id"], t_priv["id"]}


def test_templates_visibility_validation_and_put_semantics(api_client, api_engine, as_user):
    """visibility 仅 private/public(大小写/空格归一), 非法值 400; PUT 不传则保持原值。"""
    _seed(api_engine)
    as_user([], id="u-a")
    for bad in ("team", "shared", "PRIVATE2", "公有"):
        assert api_client.post("/api/me/templates", json={
            "name": "t", "content": "", "visibility": bad,
        }).status_code == 400, f"visibility={bad!r}"
    t = api_client.post("/api/me/templates", json={
        "name": "t", "content": "", "visibility": "PUBLIC ",
    }).json()
    assert t["visibility"] == "public"  # 归一化
    # PUT 非法值 400, 且不落库
    assert api_client.put(f"/api/me/templates/{t['id']}", json={
        "name": "t", "content": "", "visibility": "shared",
    }).status_code == 400
    kept = api_client.put(f"/api/me/templates/{t['id']}", json={"name": "t2", "content": "x"}).json()
    assert kept["visibility"] == "public" and kept["name"] == "t2"
    down = api_client.put(f"/api/me/templates/{t['id']}", json={
        "name": "t2", "content": "x", "visibility": "PRIVATE",
    }).json()
    assert down["visibility"] == "private"


def test_template_revision_snapshot_on_update_and_prune(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    t = api_client.post("/api/me/templates", json={"name": "tpl", "content": "v0"}).json()
    tid = t["id"]
    # 无修订时列表为空; 模板不存在 → 404
    assert api_client.get(f"/api/me/templates/{tid}/revisions").json()["items"] == []
    assert api_client.get("/api/me/templates/t-nope/revisions").status_code == 404

    # 更新前把旧版本存成快照
    api_client.put(f"/api/me/templates/{tid}", json={"name": "tpl", "content": "v1"})
    revs = api_client.get(f"/api/me/templates/{tid}/revisions").json()["items"]
    assert len(revs) == 1
    assert revs[0]["name"] == "tpl" and revs[0]["content_preview"] == "v0"
    assert revs[0]["content_truncated"] is False

    # 长内容预览截断为 500 字
    api_client.put(f"/api/me/templates/{tid}", json={"name": "tpl", "content": "长" * 600})
    api_client.put(f"/api/me/templates/{tid}", json={"name": "tpl", "content": "v2"})
    revs = api_client.get(f"/api/me/templates/{tid}/revisions").json()["items"]
    assert len(revs) == 3
    assert len(revs[0]["content_preview"]) == 500 and revs[0]["content_truncated"] is True

    # 预置 25 条历史(时间递增), 再更新一次 → 共 26 条剪枝到 20, 删最旧 6 条
    db = get_session(api_engine)
    try:
        db.query(UserTemplateRevision).filter(
            UserTemplateRevision.template_id == tid
        ).delete(synchronize_session=False)
        base = datetime.now() - timedelta(days=1)
        for i in range(25):
            db.add(UserTemplateRevision(
                id=f"rev-{i:02d}", template_id=tid, user_id="u-a",
                name=f"r{i}", content=f"c{i}",
                created_at=base + timedelta(minutes=i),
            ))
        db.commit()
    finally:
        db.close()

    api_client.put(f"/api/me/templates/{tid}", json={"name": "tpl", "content": "v3"})
    db = get_session(api_engine)
    try:
        rows = db.query(UserTemplateRevision).filter(UserTemplateRevision.template_id == tid).all()
    finally:
        db.close()
    assert len(rows) == 20
    by_id = {r.id: r for r in rows}
    assert "rev-00" not in by_id and "rev-05" not in by_id  # 最旧 6 条被删
    assert all(f"rev-{i:02d}" in by_id for i in range(6, 25))
    snapshots = [r for r in rows if not r.id.startswith("rev-")]
    assert len(snapshots) == 1
    assert snapshots[0].name == "tpl" and snapshots[0].content == "v2"  # 更新前快照


def test_template_revision_restore(api_client, api_engine, as_user):
    _seed(api_engine)
    _login_a(as_user)
    t = api_client.post("/api/me/templates", json={"name": "模板", "content": "第一版"}).json()
    tid = t["id"]
    api_client.put(f"/api/me/templates/{tid}", json={"name": "模板V2", "content": "第二版"})
    api_client.put(f"/api/me/templates/{tid}", json={"name": "模板V3", "content": "第三版"})
    # 固定时间保证列表倒序确定
    db = get_session(api_engine)
    try:
        base = datetime.now() - timedelta(hours=1)
        db.query(UserTemplateRevision).filter(
            UserTemplateRevision.template_id == tid, UserTemplateRevision.content == "第一版"
        ).update({UserTemplateRevision.created_at: base}, synchronize_session=False)
        db.query(UserTemplateRevision).filter(
            UserTemplateRevision.template_id == tid, UserTemplateRevision.content == "第二版"
        ).update({UserTemplateRevision.created_at: base + timedelta(minutes=1)},
                 synchronize_session=False)
        db.commit()
    finally:
        db.close()
    revs = api_client.get(f"/api/me/templates/{tid}/revisions").json()["items"]
    assert [r["content_preview"] for r in revs] == ["第二版", "第一版"]

    # 恢复最早那版: 先存当前(第三版)快照, 再覆盖模板
    res = api_client.post(f"/api/me/templates/{tid}/revisions/{revs[1]['id']}/restore")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "模板" and body["content"] == "第一版"
    revs2 = api_client.get(f"/api/me/templates/{tid}/revisions").json()["items"]
    assert len(revs2) == 3
    assert revs2[0]["name"] == "模板V3" and revs2[0]["content_preview"] == "第三版"

    # 不存在的修订 → 404; 修订不属于该模板 → 404; 空模板无修订
    assert api_client.post(f"/api/me/templates/{tid}/revisions/r-nope/restore").status_code == 404
    t2 = api_client.post("/api/me/templates", json={"name": "另一个", "content": "x"}).json()
    assert api_client.get(f"/api/me/templates/{t2['id']}/revisions").json()["items"] == []
    assert api_client.post(
        f"/api/me/templates/{t2['id']}/revisions/{revs[0]['id']}/restore"
    ).status_code == 404


def test_template_revisions_cross_user_404(api_client, api_engine, as_user):
    """他人模板(即使 public)的修订列表/恢复一律 404。"""
    _seed(api_engine)
    _login_a(as_user)
    t = api_client.post("/api/me/templates", json={
        "name": "公开模板", "content": "v1", "visibility": "public",
    }).json()
    api_client.put(f"/api/me/templates/{t['id']}", json={"name": "公开模板", "content": "v2"})
    rid = api_client.get(f"/api/me/templates/{t['id']}/revisions").json()["items"][0]["id"]

    _login_b(as_user)
    assert api_client.get(f"/api/me/templates/{t['id']}/revisions").status_code == 404
    assert api_client.post(
        f"/api/me/templates/{t['id']}/revisions/{rid}/restore"
    ).status_code == 404

    # A 自己仍可见(修订未被 B 的操作影响)
    _login_a(as_user)
    assert len(api_client.get(f"/api/me/templates/{t['id']}/revisions").json()["items"]) == 1


def test_template_revision_cascade_on_delete(api_client, api_engine, as_user):
    """删除模板时其修订一并清理。"""
    _seed(api_engine)
    _login_a(as_user)
    t = api_client.post("/api/me/templates", json={"name": "t", "content": "v0"}).json()
    api_client.put(f"/api/me/templates/{t['id']}", json={"name": "t", "content": "v1"})
    api_client.put(f"/api/me/templates/{t['id']}", json={"name": "t", "content": "v2"})
    db = get_session(api_engine)
    try:
        assert db.query(UserTemplateRevision).filter(
            UserTemplateRevision.template_id == t["id"]
        ).count() == 2
    finally:
        db.close()
    assert api_client.delete(f"/api/me/templates/{t['id']}").json() == {"ok": True}
    db = get_session(api_engine)
    try:
        assert db.query(UserTemplateRevision).filter(
            UserTemplateRevision.template_id == t["id"]
        ).count() == 0
    finally:
        db.close()


def test_user_templates_visibility_migration_idempotent(tmp_path):
    """旧库缺 visibility 列: 幂等迁移补列并回填 private, 重跑无副作用。"""
    from sqlalchemy import create_engine, inspect, text

    from app.models.database import run_user_template_column_migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE user_templates ("
            "id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, "
            "name VARCHAR(255) NOT NULL, content TEXT, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO user_templates (id, user_id, name, content) "
            "VALUES ('t-legacy', 'u-a', '旧模板', '正文')"
        ))
    # 缺列 → 补列 + 回填; 重跑幂等
    run_user_template_column_migrations(engine)
    run_user_template_column_migrations(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("user_templates")}
    assert "visibility" in cols
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT name, content, visibility FROM user_templates WHERE id = 't-legacy'"
        )).fetchone()
    assert tuple(row) == ("旧模板", "正文", "private")
    # 已含 visibility 的表(新库路径)同样 no-op
    run_user_template_column_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO user_templates (id, user_id, name, content, visibility) "
            "VALUES ('t-new', 'u-a', '新模板', '', 'public')"
        ))
    run_user_template_column_migrations(engine)
    with engine.begin() as conn:
        assert conn.execute(text(
            "SELECT visibility FROM user_templates WHERE id = 't-new'"
        )).scalar() == "public"


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
    # limit 取最近 N 条并按时间升序返回(重载后可见最新窗口), 不含他用户
    assert [m["id"] for m in api_client.get("/api/me/chat-messages?limit=2").json()["items"]] == ["m2", "m3"]
    assert [m["id"] for m in api_client.get("/api/me/chat-messages?limit=1").json()["items"]] == ["m3"]
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


def test_chat_sources_non_list_rejected(api_client, api_engine, as_user):
    """sources 非 list/None(如字符串) → 400, 不落库。"""
    _seed(api_engine)
    _login_a(as_user)
    for bad in ("not-a-list", 123, {"page_id": "p-pub"}):
        res = api_client.post("/api/me/chat-messages", json={
            "role": "user", "content": "你好", "sources": bad,
        })
        assert res.status_code == 400, f"sources={bad!r} -> {res.status_code}"
    assert api_client.get("/api/me/chat-messages").json()["items"] == []


def test_chat_sources_too_large_rejected(api_client, api_engine, as_user):
    """sources 序列化后超过 CHAT_SOURCES_MAX(200_000) → 400, 不落库。"""
    _seed(api_engine)
    _login_a(as_user)
    huge = [{"page_id": "p-pub", "content": "x" * 200_000}]
    res = api_client.post("/api/me/chat-messages", json={
        "role": "assistant", "content": "回复", "sources": huge,
    })
    assert res.status_code == 400
    assert res.json()["detail"] == "sources 过大"
    assert api_client.get("/api/me/chat-messages").json()["items"] == []


def test_chat_messages_same_created_at_id_tiebreaker(api_client, api_engine, as_user):
    """同 created_at 时按 id desc 取窗口, 再翻转为升序返回(确定性)。"""
    _seed(api_engine)
    _login_a(as_user)
    ts = datetime.now() - timedelta(hours=1)
    db = get_session(api_engine)
    try:
        db.add(UserChatMessage(id="m-a", user_id="u-a", role="user", content="先", created_at=ts))
        db.add(UserChatMessage(id="m-b", user_id="u-a", role="assistant", content="后", created_at=ts))
        db.add(UserChatMessage(id="m-c", user_id="u-a", role="user", content="更早",
                               created_at=ts - timedelta(minutes=1)))
        db.commit()
    finally:
        db.close()
    # 同刻取窗口: id desc → 取到 id 最大者
    assert [m["id"] for m in api_client.get("/api/me/chat-messages?limit=1").json()["items"]] == ["m-b"]
    assert [m["id"] for m in api_client.get("/api/me/chat-messages?limit=2").json()["items"]] == ["m-a", "m-b"]
    # 全量: 时间升序, 同刻按 id 升序(窗口翻转后)
    assert [m["id"] for m in api_client.get("/api/me/chat-messages").json()["items"]] == ["m-c", "m-a", "m-b"]


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
        ("get", "/api/me/templates/t-1/revisions", None),
        ("post", "/api/me/templates/t-1/revisions/r-1/restore", None),
        ("get", "/api/me/chat-messages", None),
        ("post", "/api/me/chat-messages", {"role": "user", "content": "x"}),
        ("delete", "/api/me/chat-messages", None),
    ]
    for method, url, body in calls:
        res = api_client.request(method.upper(), url, json=body)
        assert res.status_code == 401, f"{method} {url} -> {res.status_code}"
