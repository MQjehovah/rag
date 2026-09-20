# 知识库权限 实施计划（rag）

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 给 rag 的 Wiki 增加按组可见/可编辑（管理员可人工指定归属），并堵住 GraphRAG 全局回退、`_get_kb_context`、图片接口、全局图谱重建、`update_page` 五处同类漏洞。

**Architecture:** 复用既有的「笔记本按组」语义（`notebooks.group_id`，NULL = 公共）；给 `wiki_pages` 增加同语义的 `group_id` 列，集中一个 `visible_wiki_filter()` 供所有读/写路径调用；图片因 `<img>` 无法带 Bearer 头，改用「登录后换取 HMAC 签名 URL」的方式鉴权。

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy（PostgreSQL 生产、SQLite 开发回退）/ pytest；前端 Vue 3 + TypeScript。

**设计依据：** `rag/docs/plans/2026-09-20-knowledge-authz-design.md`

---

## 前置事实（已核实，实现时直接用）

- **无 Alembic**。`app/models/database.py:229` 的 `_migrate_schema(engine)` 会自动为已存在的表补齐模型里新增的列（`ALTER TABLE ... ADD COLUMN`），所以**给模型加列即自动迁移**；但它**不建索引**，索引需另加幂等 DDL。
- `init_db(engine)`（`database.py:249`）= `create_all` + `_migrate_schema`，在 `api/deps.py:19` 等处以模块级 `_engine` 调用。
- 现有唯一隔离 helper：`app/api/search_common.py:10` `get_visible_page_ids(db, current_user)`。
- 管理员判定统一是 `"__local_admin__" in current_user["groups"]`。
- 用户 payload 由 `app/core/user_utils.py:23` `build_user_payload` 产出，含 `groups: list[str]`。

样式：Python 4 空格缩进、注释与 UI 文案中文、标识符英文；前端 2 空格、无分号、单引号。

---

## Task 1: 数据模型 + 可见性 helper

**Files:**
- Modify: `rag/backend/app/models/database.py`（`WikiPage` 加列；`init_db` 加幂等索引）
- Modify: `rag/backend/app/api/search_common.py`（新增 `visible_wiki_filter`）
- Test: `rag/backend/tests/core/test_wiki_visibility.py`

**Step 1: 写失败测试**

Create `tests/core/test_wiki_visibility.py`：

```python
"""Wiki 可见性过滤的单元测试。"""
import uuid

import pytest

from app.api.search_common import visible_wiki_filter
from app.models.database import WikiPage


def _mk(group_id):
    return WikiPage(id=str(uuid.uuid4()), title=f"t-{uuid.uuid4()}", content="x", group_id=group_id)


@pytest.fixture
def db(tmp_path):
    from app.models.database import get_engine, get_session, init_db

    engine = get_engine(f"sqlite:///{tmp_path / 'wiki.db'}")
    init_db(engine)
    session = get_session(engine)
    session.add_all([_mk(None), _mk("研发部"), _mk("财务部"), _mk("仪表盘-只读")])
    session.commit()
    yield session
    session.close()


def _titles(db, user):
    return {p.title for p in db.query(WikiPage).filter(visible_wiki_filter(user)).all()}


def test_普通用户看到本组与公共(db):
    rows = db.query(WikiPage).all()
    mine = {p.title for p in rows if p.group_id is None or p.group_id == "研发部"}
    assert _titles(db, {"groups": ["研发部"]}) == mine


def test_无组用户只看到公共(db):
    public = {p.title for p in db.query(WikiPage).filter(WikiPage.group_id.is_(None)).all()}
    assert _titles(db, {"groups": []}) == public


def test_管理员不过滤(db):
    assert _titles(db, {"groups": ["__local_admin__"]}) == {p.title for p in db.query(WikiPage).all()}
```

**Step 2: 运行确认失败**

Run（在 `rag/backend` 下）: `python -m pytest tests/core/test_wiki_visibility.py -q`
Expected: FAIL —— `WikiPage` 无 `group_id` / `visible_wiki_filter` 不存在。

**Step 3: 实现**

`app/models/database.py` 的 `WikiPage` 中加入（放在 `category` 之后）：

```python
    group_id = Column(String(255), nullable=True, index=True)
```

`init_db` 中，在 `_migrate_schema(engine)` 之后补一段幂等索引（因为 `_migrate_schema` 只加列不建索引）：

```python
def _ensure_wiki_group_index(engine):
    """为已存在的 wiki_pages 补 group_id 索引(建表路径由 create_all 覆盖)。"""
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy_text(
                "CREATE INDEX IF NOT EXISTS ix_wiki_pages_group_id ON wiki_pages (group_id)"
            ))
    except Exception:
        # 索引不是正确性前提,失败不阻断启动
        logger.warning("创建 wiki_pages.group_id 索引失败", exc_info=True)


def init_db(engine):
    Base.metadata.create_all(engine)
    _migrate_schema(engine)
    _ensure_wiki_group_index(engine)
```

`app/api/search_common.py` 中加入（`WikiPage` 需加入 import）：

```python
def visible_wiki_filter(current_user):
    """WikiPage 的可见性条件;本地管理员返回恒真条件(而非 None)。

    语义与 notebook 一致:group_id 为 NULL 视为公共,所有登录用户可见。
    管理员分支返回 true() 以便调用方直接 filter(),避免 filter(None) 退化成
    WHERE NULL 而静默返回 0 行。
    """
    if "__local_admin__" in current_user["groups"]:
        return true()
    return or_(WikiPage.group_id.is_(None), WikiPage.group_id.in_(current_user["groups"]))
```

这样调用方可以无条件 `q = q.filter(visible_wiki_filter(current_user))`，无需判空。

**Step 4: 运行确认通过**

Run: `python -m pytest tests/core/test_wiki_visibility.py -q`
Expected: 3 passed。

**Step 5: 提交**

```bash
git add backend/app/models/database.py backend/app/api/search_common.py backend/tests/core/test_wiki_visibility.py
git commit -m "feat(rag): wiki_pages 增加 group_id 与可见性过滤 helper"
```

---

## Task 2: Wiki 读/写按组收敛

**Files:**
- Modify: `rag/backend/app/api/wiki.py`

**Step 1: 收敛列表**

`list_wiki`（`wiki.py:34-53`）：查询改为

```python
    q = db.query(WikiPage)
    q = q.filter(visible_wiki_filter(current_user))
    pages = q.order_by(WikiPage.category, WikiPage.title).all()
```

并在返回的每个 page dict 中加入 `"group_id": p.group_id`。

**Step 2: 收敛详情**

`get_wiki_page`（`:61-86`）：把 `db.query(WikiPage).filter(WikiPage.id == page_id).first()` 改为「先取，再判可见」：

```python
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
```

新增模块级 helper（放在文件顶部即可）：

```python
def _wiki_visible(page: WikiPage, current_user) -> bool:
    if "__local_admin__" in current_user["groups"]:
        return True
    return page.group_id is None or page.group_id in current_user["groups"]
```

返回体加 `"group_id": page.group_id`。来源笔记查询（`:76`）保持不变。

**Step 3: 收敛编辑**

`update_wiki_page`（`:89-108`）：同样把 404 判断改为 `if not page or not _wiki_visible(page, current_user)`。

**Step 4: 加测试**

在 `tests/core/test_wiki_visibility.py` 追加针对端点行为的测试（若不便起 TestClient，则至少覆盖 `_wiki_visible` 的三条分支：公共 / 本组 / 他组，以及管理员恒 True）。**优先起 TestClient**，参见 Task 9 的 test client fixture。

**Step 5: 运行与提交**

Run: `python -m pytest tests/core/test_wiki_visibility.py -q`
```bash
git add backend/app/api/wiki.py backend/tests/core/test_wiki_visibility.py
git commit -m "feat(rag): wiki 列表/详情/编辑按组可见"
```

---

## Task 3: 管理员指定归属

**Files:**
- Modify: `rag/backend/app/api/wiki.py`
- Modify: `rag/frontend/src/views/Wiki.vue`

**Step 1: 新增端点**

```python
class WikiGroupUpdate(BaseModel):
    group_id: str | None = None


@router.put("/{page_id}/group")
def set_wiki_group(
    page_id: str,
    data: WikiGroupUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """人工指定 wiki 页面的归属组;仅管理员。None 表示公共。"""
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    page.group_id = (data.group_id or "").strip() or None
    db.commit()
    return {"message": "已保存", "id": page.id, "group_id": page.group_id}
```

注意路由注册顺序：`/{page_id}/group` 是更具体的路径，必须放在 `/{page_id}` **之后**没有冲突（FastAPI 按定义顺序匹配，`/{page_id}` 不会吞掉带第二段的路径），实现后用一个请求验证。

**Step 2: 前端选择器**

`frontend/src/views/Wiki.vue`：在详情面板中，仅当 `isAdmin`（该文件已有此判断，`:135-137`）时渲染一个输入框 + 保存按钮，调用
`request('rag', \`/api/wiki/${id}/group\`, { method: 'PUT', body: { group_id } })`，
并在页面数据里显示当前 `group_id`。保存成功后刷新详情。

（该文件是 `/wiki` 页面；实现前先读一遍确认它已有的 API 调用封装与状态管理方式，沿用同一套。）

**Step 3: 验证**

Run: `cd frontend; npm run build`（含 `vue-tsc` 类型检查）
Run: `python -m pytest tests/core/test_wiki_visibility.py -q`

**Step 4: 提交**

```bash
git add backend/app/api/wiki.py frontend/src/views/Wiki.vue
git commit -m "feat(rag): 管理员可为 wiki 页面指定归属组"
```

---

## Task 4: `_get_kb_context` 与 GraphRAG 社区按组过滤

**Files:**
- Modify: `rag/backend/app/api/chat.py`
- Modify: `rag/backend/app/core/graphrag.py`

**Step 1: `_get_kb_context` 按组**

`chat.py:389-394` 改为（`Notebook` 可见性规则与 notebook 一致）：

```python
def _get_kb_context(db: Session, current_user) -> dict:
    if "__local_admin__" in current_user["groups"]:
        notebooks = db.query(Notebook).all()
        pages_q = db.query(Page.id, Page.title, Page.notebook_id)
    else:
        visible_nb_ids = db.query(Notebook.id).filter(
            or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
        ).subquery()
        notebooks = db.query(Notebook).filter(
            or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
        ).all()
        pages_q = db.query(Page.id, Page.title, Page.notebook_id).filter(
            or_(Page.notebook_id.is_(None), Page.notebook_id.in_(visible_nb_ids))
        )
    nb_list = [{"id": nb.id, "name": nb.name} for nb in notebooks]
    pages = pages_q.order_by(Page.updated_at.desc()).limit(100).all()
    ...
```

三个调用点（`:616`、`:662`、`:698`）都要把 `current_user` 传进去。

**Step 2: GraphRAG 社区按组**

`core/graphrag.py` 的 `search_communities` 增加可选参数 `visible_page_ids`：

```python
def search_communities(db, query_embedding, top_k=5, visible_page_ids=None):
```

在加载社区之后、排序之前插入过滤：取可见页面关联的实体 id 集合，只保留 `member_ids` 与之相交的社区：

```python
    if visible_page_ids is not None:
        from app.models.database import GraphEntity
        entity_ids = {
            e[0] for e in db.query(GraphEntity.id).filter(
                GraphEntity.page_id.in_(visible_page_ids)
            ).all()
        } if visible_page_ids else set()
        def _members(c):
            try:
                return set(json.loads(c.member_ids or "[]"))
            except Exception:
                return set()
        communities = [c for c in communities if _members(c) & entity_ids]
```

`api/chat.py:196` 改为：

```python
        visible_ids = await asyncio.to_thread(get_visible_page_ids, db, current_user)
        communities = search_communities(db, emb, top_k=5, visible_page_ids=visible_ids)
```

（`get_visible_page_ids` 若在该函数中已算过则复用变量，避免重复查询。`current_user` 是 `chat` 端点的参数，需确认在 `:196` 的作用域内可用；不可用则向上传参。）

**Step 3: 测试**

在 `tests/core/` 新增 `test_chat_scoping.py`：
- 构造两个组各一个 notebook + page + 一条 GraphCommunity（member_ids 指向各组实体），断言普通用户只拿到本组社区。
- 断言 `_get_kb_context` 普通用户不含他组 notebook。

**Step 4: 提交**

```bash
git add backend/app/api/chat.py backend/app/core/graphrag.py backend/tests/core/test_chat_scoping.py
git commit -m "fix(rag): GraphRAG 社区与 kb 上下文按组过滤"
```

---

## Task 5: 图片签名工具与签名端点

**Files:**
- Create: `rag/backend/app/core/image_sign.py`
- Modify: `rag/backend/app/config.py`
- Modify: `rag/backend/app/api/upload.py`
- Test: `rag/backend/tests/core/test_image_sign.py`

**Step 1: 写失败测试**

```python
"""图片签名 URL 的单元测试。"""
import time

from app.core.image_sign import sign_image_url, verify_image_signature


def test_签名可验证():
    url = sign_image_url("/api/upload/images/20260920/a.png")
    base, sig, exp = url.split("?", 1)[1].split("&")[0], None, None
    assert "sig=" in url and "exp=" in url
    path, query = url.split("?", 1)
    params = dict(kv.split("=", 1) for kv in query.split("&"))
    assert verify_image_signature(path, params["sig"], int(params["exp"]))


def test_篡改被拒():
    url = sign_image_url("/api/upload/images/20260920/a.png")
    path, query = url.split("?", 1)
    params = dict(kv.split("=", 1) for kv in query.split("&"))
    assert not verify_image_signature("/api/upload/images/20260920/b.png", params["sig"], int(params["exp"]))


def test_过期被拒():
    url = sign_image_url("/api/upload/images/20260920/a.png", ttl_seconds=-1)
    path, query = url.split("?", 1)
    params = dict(kv.split("=", 1) for kv in query.split("&"))
    assert not verify_image_signature(path, params["sig"], int(params["exp"]))
```

**Step 2: 实现 `app/core/image_sign.py`**

```python
"""图片 URL 的 HMAC 签名:解决 <img> 无法携带 Authorization 头的问题。

签名绑定「路径 + 过期时间」,由已登录用户调用 /api/upload/images/sign 换取。
"""
import hashlib
import hmac
import time
from urllib.parse import quote

from app.config import settings

DEFAULT_TTL_SECONDS = 3600


def _secret() -> bytes:
    key = getattr(settings, "image_sign_secret", "") or settings.jwt_secret_key
    return key.encode("utf-8")


def _digest(path: str, exp: int) -> str:
    msg = f"{path}:{exp}".encode("utf-8")
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def sign_image_url(path: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """返回带 sig/exp 的完整路径(只签 path,保留已有 query)。"""
    base, _, query = path.partition("?")
    exp = int(time.time()) + ttl_seconds
    sig = _digest(base, exp)
    sep = "&" if query else ""
    return f"{base}?{query}{sep}sig={sig}&exp={exp}"


def verify_image_signature(path: str, sig: str | None, exp: int | None) -> bool:
    if not sig or not exp:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_digest(path, exp), sig)
```

> `path` 必须是**不含 query** 的路径部分；`/images/proxy` 的签名对象是它的 `url` 查询参数，见 Task 6。

**Step 3: 配置项**

`app/config.py` 加入（放在 `minio_*` 附近）：

```python
    # 图片签名 URL 的 HMAC 密钥;留空则回退 jwt_secret_key
    image_sign_secret: str = ""
    # 图片签名有效期(秒)
    image_sign_ttl_seconds: int = 3600
```

**Step 4: 签名端点**

`app/api/upload.py` 加入：

```python
from app.core.image_sign import sign_image_url


class SignRequest(BaseModel):
    urls: list[str]


@router.post("/images/sign")
def sign_images(data: SignRequest, current_user=Depends(get_current_user)):
    """把图片地址(本地路径或外链代理)换成带签名的可用 URL。"""
    out = []
    for raw in data.urls[:100]:
        if raw.startswith("/api/upload/images/proxy?"):
            # 代理接口签名绑定被代理的 url 参数
            base, _, query = raw.partition("?")
            params = dict(kv.split("=", 1) for kv in query.split("&"))
            target = unquote(params.get("url", ""))
            exp = int(time.time()) + settings.image_sign_ttl_seconds
            sig = sign_proxy(target, exp)
            out.append(f"{base}?url={quote(target, safe='')}&sig={sig}&exp={exp}")
        elif raw.startswith("/api/upload/images/"):
            out.append(sign_image_url(raw, settings.image_sign_ttl_seconds))
        else:
            out.append(raw)
    return {"urls": out}
```

（`sign_proxy` 与 `unquote`/`quote` 的 import 见 Task 6；也可把 proxy 签名并入 `image_sign.py` 作为一个 `sign_proxy_url(target, ttl)` 函数，二选一，保持单一实现。）

**Step 5: 运行与提交**

Run: `python -m pytest tests/core/test_image_sign.py -q`
```bash
git add backend/app/core/image_sign.py backend/app/config.py backend/app/api/upload.py backend/tests/core/test_image_sign.py
git commit -m "feat(rag): 图片 URL 签名工具与签名端点"
```

---

## Task 6: 图片接口校验 + SSRF 加固

**Files:**
- Modify: `rag/backend/app/api/upload.py`
- Modify: `rag/backend/app/core/image_sign.py`（加 `sign_proxy` / `verify_proxy_signature`）

**Step 1: 本地图片接口校验签名**

```python
@router.get("/images/{date_dir}/{file_name}")
def get_image(date_dir: str, file_name: str, sig: str | None = None, exp: int | None = None):
    path = f"/api/upload/images/{date_dir}/{file_name}"
    if not verify_image_signature(path, sig, exp):
        raise HTTPException(status_code=403, detail="签名无效或已过期")
    file_path = UPLOAD_DIR / date_dir / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)
```

**Step 2: 代理接口校验 + SSRF 加固**

```python
@router.get("/images/proxy")
async def proxy_image(url: str, sig: str | None = None, exp: int | None = None):
    if not verify_proxy_signature(url, sig, exp):
        raise HTTPException(status_code=403, detail="签名无效或已过期")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只支持 http/https 图片")
    _assert_public_host(url)
    ...
    # httpx 请求改为:禁用自动跟随重定向到私网、限制大小与超时
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        resp = await client.get(url, headers={"Referer": ""})
        resp.raise_for_status()
        content = resp.content
    if len(content) > settings.image_proxy_max_bytes:
        raise HTTPException(status_code=413, detail="图片过大")
```

新增 `_assert_public_host(url)`（放在 `upload.py`）：

```python
def _assert_public_host(url: str) -> None:
    """拒绝解析到内网/回环/链路本地地址的目标,防 SSRF。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        raise HTTPException(status_code=400, detail="无效的图片地址")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析图片主机")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(status_code=403, detail="不允许访问内网地址")
```

（若仓库已有「安全主机白名单」配置，优先复用它；实现前 grep `safe_host` / `allow` 确认。）

**Step 3: 加测试**

在 `tests/core/test_image_sign.py` 追加（参数化）：
- `verify_proxy_signature` 有效/过期/篡改。
- `_assert_public_host("http://127.0.0.1/x")`、`http://10.0.0.1/x`、`http://169.254.169.254/` 抛 403；`http://example.com/x` 通过（该测试依赖 DNS，若 CI 无网络则用 `monkeypatch` 打桩 `socket.getaddrinfo`）。

**Step 4: 提交**

```bash
git add backend/app/api/upload.py backend/app/core/image_sign.py backend/tests/core/test_image_sign.py
git commit -m "fix(rag): 图片接口校验签名并加固 SSRF"
```

---

## Task 7: 前端改用签名图片

**Files:**
- Modify: `rag/frontend/src/views/Chat.vue`（`:324-326`）
- Modify: `rag/frontend/src/views/Wiki.vue`（`:231-233`）

**Step 1: 改造渲染逻辑**

现状是纯前端把外链重写为 `/api/upload/images/proxy?url=...`（`Chat.vue:326`、`Wiki.vue:233`），签名必须由后端签发。改为：

1. 渲染 markdown 后，收集所有 `<img>` 的 `src`（需要处理的：以 `/api/upload/images/` 开头的，以及 `http(s)` 外链）。
2. 把外链先转成 `/api/upload/images/proxy?url=<encoded>`，与本地路径一起放进一个数组。
3. 调 `request('rag', '/api/upload/images/sign', { method: 'POST', body: { urls } })`，拿到一一对应的签名 URL。
4. 按顺序回填 `img.src`。
5. 失败时保持原样或用占位图，不要阻断正文渲染。

建议抽成一个共享工具 `frontend/src/utils/imageSign.ts`，两个页面共用，避免重复。

**Step 2: 验证**

Run: `cd frontend; npm run build`（`vue-tsc` 必须通过）

**Step 3: 手动验收**

启动前后端，确认 Chat 与 Wiki 中的图片仍能显示（说明签名链路通了），且直接访问不带 `sig` 的图片 URL 返回 403。

**Step 4: 提交**

```bash
git add frontend/src/views/Chat.vue frontend/src/views/Wiki.vue frontend/src/utils/imageSign.ts
git commit -m "fix(rag): 前端改用签名图片 URL"
```

---

## Task 8: 其余三项加固

**Files:**
- Modify: `rag/backend/app/api/pages.py`（`update_page` 目标笔记本校验）
- Modify: `rag/backend/app/api/graph.py`（`POST /rebuild` 管理员专属）
- Modify: `rag/backend/app/api/dingtalk.py`（`/sync`、`/sync-selected` 管理员专属）

**Step 1: `update_page` 校验新 notebook_id**

`pages.py:243-244` 附近，在把 `notebook_id` 写入之前加入可见性校验：目标笔记本必须存在且 `group_id` 在用户组内或为 NULL；否则 403。管理员跳过校验。

**Step 2: `POST /graph/rebuild` 管理员专属**

`graph.py:246-254` 加入与 `rebuild-entities` 一致的守卫：

```python
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
```

**Step 3: 钉钉同步管理员专属**

`dingtalk.py` 的 `POST /sync`（`:217` 附近）与 `POST /sync-selected`（`:277` 附近）加入同样的管理员守卫；`GET /docs`、`/spaces`、`/status` 保持登录可读。

**Step 4: 测试**

在 `tests/core/` 新增 `test_authz_guards.py`（用 Task 9 的 TestClient fixture），断言：普通用户 403、管理员放行（用 monkeypatch 让实际重建逻辑不执行）。

**Step 5: 提交**

```bash
git add backend/app/api/pages.py backend/app/api/graph.py backend/app/api/dingtalk.py backend/tests/core/test_authz_guards.py
git commit -m "fix(rag): 收敛图谱重建/钉钉同步/笔记改归属三处越权"
```

---

## Task 9: 测试基建、坏测试修复与端到端验收

**Files:**
- Modify: `rag/backend/tests/conftest.py`
- Delete/repair: `tests/core/test_document.py`、`test_embedding.py`、`test_generation.py`

**Step 1: 补 TestClient fixtures**

在 `tests/conftest.py` 追加（供 Task 2/4/8 使用）：

```python
@pytest.fixture
def api_client(tmp_path):
    """带 SQLite 临时库的 TestClient;get_db 指向该库。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.api import deps
    from app.models.database import get_engine, get_session, init_db

    engine = get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(engine)
    SessionLocal = lambda: get_session(engine)  # noqa: E731

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps.get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
```

并提供一个 `as_user(client, groups)` helper：通过 `app.dependency_overrides[get_current_user]` 注入一个假用户 payload（含 `id/username/groups/...`，形状对齐 `build_user_payload`）。

> 实现前先读 `app/api/deps.py` 与 `app/core/jwt_utils.py`，确认 `get_db` 与 `get_current_user` 的实际名字与导入路径，再落 fixture。

**Step 2: 处理 3 个坏测试文件**

`tests/core/test_document.py`、`test_embedding.py`、`test_generation.py` 导入了已删除的 `app.core.document/embedding/generation`，导致整包收集失败。二选一（推荐前者）：
- 删除这三个文件（它们测试的模块已不存在）；
- 或改写为针对现模块（`app/core/ingest.py`、`app/core/rag.py`、`app/core/llm.py`）的测试。

无论哪种，`python -m pytest -q` 必须能**完整收集**且无新增失败。

**Step 3: 全量验证**

Run（在 `rag/backend` 下）: `python -m pytest -q`
Expected: 收集无 error，失败数不高于改动前基线（改动前为 16 passed / 3 errors；修复后应为 0 errors）。

Run（在 `rag/frontend` 下）: `npm run build`
Expected: 通过。

**Step 4: 逐条核对验收标准**

对照设计文档 `docs/plans/2026-09-20-knowledge-authz-design.md` 的《验收标准》1–8 逐条验证，把结果写入该文档末尾的「实施结果」小节，并提交。

**Step 5: 提交**

```bash
git add backend/tests/conftest.py docs/plans/2026-09-20-knowledge-authz-design.md
git commit -m "test(rag): 补 TestClient 基建并修复收集失败的旧测试"
```

---

## 已知限制（不要在本计划内解决）

- wiki 编译仍是全局的，`group_id` 是人工发布标记，不阻止跨组内容被合并到同一页面（设计文档《残留风险》已记录）。
- 没有组注册表，`group_id` 与 OIDC/LDAP 下发的组字符串必须逐字一致。
- 未做按组分区编译、未做组管理 UI。
