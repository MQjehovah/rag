# 知识库权限设计（rag）

日期：2026-09-20
状态：已确认，待实施
关联：阶段二 · Part B（另见 dashboard 的 `docs/plans/2026-09-20-usage-view-design.md`）

## 背景

rag 的隔离模型目前只覆盖到「笔记本」一层，Wiki / GraphRAG / 图片等多处没有按组过滤。调研结论：

- 全库**唯一的隔离列**是 `notebooks.group_id`（单个自由文本组名，无外键、无组注册表）。`pages` 没有 `created_by`，可见性完全靠 `notebook_id → notebooks.group_id`；`notebook_id IS NULL` 视为公共（`api/search_common.py:10-22`）。
- **`wiki_pages` 完全没有归属列**：`api/wiki.py:36` 是 `db.query(WikiPage).all()`，详情 `:67`、来源 `:76`、编辑 `:98` 也都不设限，任何登录用户可见并可编辑任何 wiki 页。
- **wiki 编译是全局的**：`core/wiki.py:362-366` 取全部笔记，`:312-314` 取全部过期笔记，`_persist` 按标题 upsert 并做合并（`:219-242`），所以一个 wiki 页可能同时蒸馏了多个组的笔记。
- **GraphRAG 全局回退未过滤**：`api/chat.py:196` 的 `search_communities(db, emb)` → `core/graphrag.py:202-236` 加载**全部** `GraphCommunity` 按余弦排序，无任何可见性条件。
- **`_get_kb_context` 泄露**：`api/chat.py:389-394` 在保存笔记/导入时返回**全部**笔记本与最近 100 条笔记标题。
- **图片接口无鉴权**：`api/upload.py:91`(`GET /images/{date_dir}/{file_name}`) 与 `:99`(`GET /images/proxy`) 没有 `Depends(get_current_user)`。
- **非管理员可破坏全局图谱**：`api/graph.py:246-254` 的 `POST /graph/rebuild` 只要求登录，而 `core/graph.py:80` 会先 `delete()` 全部 `GraphEdge`。
- **`update_page` 越权**：`api/pages.py:243-244` 校验了当前笔记本，但**没有校验传入的新 `notebook_id`**。
- 组来源：OIDC `groups` claim（`core/jwt_utils.py:133-144`，接受 `list[str]` 或逗号串）或 LDAP CN（`core/auth.py:80-103`）；本地管理员固定为 `"__local_admin__"`（`api/auth.py:37`）。**没有组注册表，也没有组管理 UI/API**。
- 既有设计文档已明确「本期不做」：`docs/plans/2026-09-05-rag-oidc-dashboard-design.md:68-72`「RAG wiki/search 按组收紧权限（现状：无过滤则全员可见）」。

## 目标

1. Wiki 按组可见/可编辑，管理员可人工指定归属。
2. 堵住 GraphRAG 全局回退、`_get_kb_context`、图片接口、全局图谱重建、`update_page` 五处同类漏洞。
3. 不改变现有「笔记本按组」语义，不引入组注册表。

## 非目标（YAGNI）

- **不按组分区编译 wiki**（见「残留风险」）。
- 不引入组注册表 / 组管理后台。
- 不改笔记/搜索的既有隔离（已按 notebook 组生效）。
- 不做 token-exchange / Keycloak 迁移。

## 设计

### B1 数据模型

`wiki_pages` 新增：

```
group_id  String(255)  NULL  索引
```

语义与 `notebooks.group_id` 保持一致：**NULL = 公共（所有登录用户可见）**，非 NULL = 仅属于该组字符串的用户可见。

迁移：优先使用仓库现有的建表/迁移方式；若没有 Alembic，则用启动时幂等 `ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS group_id VARCHAR(255)`（PostgreSQL）+ `CREATE INDEX IF NOT EXISTS`，并保留 SQLite 开发回退分支。实现前先确认仓库的既有做法。

### B2 统一可见性 helper

在 `app/api/` 的公共处新增（与 `search_common.get_visible_page_ids` 并列）：

```python
def visible_wiki_filter(user: dict):
    """返回 WikiPage 的可见性条件;本地管理员返回恒真条件(true())。"""
    if "__local_admin__" in user["groups"]:
        return true()
    return or_(WikiPage.group_id.is_(None), WikiPage.group_id.in_(user["groups"]))
```

应用于：
- `GET /api/wiki`（`api/wiki.py:34-54`）
- `GET /api/wiki/{page_id}`（`:61-87`，含 `:76` 来源笔记查询）
- `PUT /api/wiki/{page_id}`（`:89-109`：先判可见，不可见返回 404 而不是 403，避免泄露存在性）

### B3 人工指定归属（管理员）

- 新增 `PUT /api/wiki/{page_id}/group`，body `{"group_id": "研发部" | null}`，**仅 `__local_admin__`**；返回更新后的页面。
- `GET /api/wiki` 与详情响应体增加 `group_id` 字段，供前端展示。
- `frontend/src/views/Wiki.vue`：管理员（已有 `isAdmin`）在页面详情处显示分组选择器，可设/清空。

### B4 GraphRAG 全局回退按组过滤

`search_communities(db, emb, top_k, visible_page_ids=None)`：
- `api/chat.py:196` 传入已算好的可见页面 id 集（`search_common.get_visible_page_ids`）。
- `core/graphrag.py`：先取可见页面关联的 `graph_entities.id` 集合，再只保留 `member_ids` 与该集合相交的 `GraphCommunity`，然后再排序取 top_k。
- 社区数量少，Python 侧过滤可接受。

> 说明：该过滤是**最佳努力**，不是严格边界。实体按名称全局共享、社区又是对全图跑 Louvain 得到的，因此可见社区的摘要可能掺入他组页面贡献的关系；严格按组隔离需要按组做实体归属/社区划分，超出本次范围。

### B5 `_get_kb_context` 按组过滤

`api/chat.py:389-394`：notebook 限定为 `(group_id IN user.groups) OR group_id IS NULL`；page 限定为 `notebook_id IS NULL OR notebook_id IN 可见 notebook`。`__local_admin__` 不过滤。

### B6 图片签名 URL

背景：前端把外链图片重写为 `/api/upload/images/proxy?url=...` 并用 `<img>` 加载（`Chat.vue:326`、`Wiki.vue:233`），**浏览器无法为 `<img>` 附带 Authorization 头**，所以不能简单加鉴权。

方案：
1. 新增签名工具：HMAC-SHA256，密钥取 `settings.image_sign_secret`，缺省回退 `jwt_secret_key`。签名内容含 URL 与过期时间戳。
2. 新增 `POST /api/upload/images/sign`（需登录），入参 `{urls: string[]}`，返回 `{url: string}[]`——对本地图片路径与需要代理的外链分别签发带 `sig` 与 `exp` 的完整 URL。有效期 1 小时（可配）。
3. `GET /images/{date_dir}/{file_name}` 与 `GET /images/proxy` 校验 `sig`+`exp`；不合法返回 403。
4. `/images/proxy` 另加：目标主机白名单/黑名单校验、**禁止解析到内网/回环/链路本地地址（防 SSRF）**、响应大小上限、超时。
5. 前端：`Chat.vue` / `Wiki.vue` 渲染 markdown 时，先收集需要展示的图片 URL，批量调 `POST /api/upload/images/sign`，再用返回的签名 URL 赋给 `<img src>`。取消现有的纯前端重写逻辑。

### B7 其余三项

- `api/pages.py:243-244`：`update_page` 校验传入的 `notebook_id` 属于当前用户可见范围，否则 403。
- `api/graph.py:246-254`：`POST /graph/rebuild` 改为**管理员专属**（与 `rebuild-entities`/`rebuild-communities` 一致）。
- `api/dingtalk.py:217-305`：`POST /sync`、`/sync-selected` 改为**管理员专属**（读取类 `GET /docs|/spaces|/status` 保持登录可读）。

### B8 测试

新增：
- wiki 可见性：普通用户看不到非本组页面（列表不含、详情 404）、看得到本组与公共；管理员全见；编辑非可见页面 404。
- `PUT /api/wiki/{id}/group`：管理员可设/清空；普通用户 403。
- `_get_kb_context`：普通用户只拿到本组 + 公共。
- 签名：有效通过、过期 403、篡改 403、无签名 403；`/images/proxy` 拒绝内网地址。
- `POST /graph/rebuild` 普通用户 403；`update_page` 传入无权 notebook_id → 403。

同时修复现有 3 个 import 已删模块的坏测试文件（`tests/core/test_document.py`、`test_embedding.py`、`test_generation.py`）——至少让 `pytest` 能整体收集，不新增失败。

## 验收标准

1. 普通用户 `GET /api/wiki` 不含非本组页面；直接访问其 id 返回 404；编辑返回 404。
2. `__local_admin__` 可见并可编辑全部页面，且能设置 `group_id`。
3. 聊天在本地检索不足触发 GraphRAG 全局回退时，返回的社区摘要仅来自可见内容。
4. 保存笔记/导入接口返回的笔记本与笔记标题列表按组过滤。
5. 无 `sig`/过期/篡改的图片请求返回 403；带合法签名的 `<img>` 正常显示；代理拒绝内网目标。
6. `POST /api/graph/rebuild` 与 `POST /api/dingtalk/sync*` 对普通用户返回 403。
7. `update_page` 指向无权笔记本返回 403。
8. `pytest` 无新增失败，前端 `npm run build`（含 `vue-tsc` 类型检查）通过。

## 残留风险（重要，需运维知晓）

- **人工贴标签不等于内容隔离**：wiki 编译仍是全局的（`core/wiki.py:362-366`），一个被标为 A 组的 wiki 页仍可能包含 B 组笔记蒸馏出的内容。`group_id` 是**发布标记**，正确性依赖管理员判断。要根治必须按组分区编译（本期不做）。
- **公共内容默认可见**：`group_id IS NULL` 视为公共，包括钉钉/Jira 导入创建的笔记本（`group_id=NULL`）与其衍生内容。上线后需尽快给敏感内容指定归属。
- **组名是自由文本**：没有任何注册表或校验，`group_id` 与 OIDC/LDAP 下发的组字符串必须逐字一致，否则会「看不见自己的内容」。建议文档中明确组名来源。
