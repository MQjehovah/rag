# 知识库新版本详细执行计划 V3（剩余工作版）

> 更新日期：2026-08-19  
> 项目：`C:\Users\20474\Documents\学习Agent\gitlab-rag-feature`  
> 规则：已完成并通过回归的 P0–P7 任务已经移除；本文只保留尚未完成的工作。

---

## 0. 当前状态与剩余目标

Evidence、Card V3、统一检索、知识债务闭环、Card 图谱、Wiki V1、旧 KO 迁移、Feature Flag、ACL、前端构建与测试底座已经完成，不再列为待办。

剩余目标：

1. **产品收口**：隐藏灰度期旧入口，将 Health、Conflict、Debt 切换到 Card V3 口径。
2. **数据源平台化**：把现有钉钉同步升级为可扩展、可恢复、可审计的 Connector 平台。
3. **上线验收**：完成第二数据源试点、公司 Embedding/Reranker 联调和最终灰度切换。

## 1. 最终产品形态

### 1.1 普通用户导航

```text
AI 问答
笔记
Wiki 主题
知识图谱
```

检索、引用、反馈和知识补充请求全部通过 AI 问答完成。

### 1.2 管理员增加的入口

```text
审核工作台
知识质量
冲突治理
知识债务
数据源
检索评测
```

### 1.3 灰度兼容入口

- `/search` 改为管理员“检索评测”，不在普通导航显示。
- `/knowledge` 改为只读“旧 KO 归档”，不再承担审核与发布。
- 旧 API、旧索引和迁移映射保留到最终切换验收完成。

## 2. 剩余工作包与依赖

```text
P8 产品入口与治理口径收口
        ↓
P9 数据源平台底座
        ↓
P10 钉钉 Connector 适配
        ↓
P11 数据源管理页面
        ↓
P12 GitLab 第二数据源试点
        ↓
P13 稳定性、安全与效果验收
        ↓
P14 灰度切换与旧入口归档
```

P8、P9 可以并行；P10–P12 依赖 P9；P14 必须等待 P13 通过。

## 3. P8：导航、信息架构与治理口径收口

### 3.1 改造目标

当前顶栏把用户任务、技术实现、阶段编号和管理员工具平铺在同一级，最终版本必须按用户任务收口：

```text
AI 助手：负责“问”
知识中心：负责“看”
治理工作台：负责“管”
数据源：负责“进”
```

顶级导航最多 4 个业务入口，阶段编号、旧数据结构和调试工具不得暴露为正式产品概念。

### 3.2 最终导航和角色矩阵

#### 普通用户

```text
AI 助手
知识中心
```

#### 知识管理员

```text
AI 助手
知识中心
治理工作台
数据源
```

#### 系统管理员头像菜单

```text
个人信息
系统工具
  ├─ 检索评测
  ├─ 旧 KO 归档
  ├─ Feature Flag
  ├─ 模型健康
  └─ 迁移状态
退出登录
```

菜单隐藏只是体验层，所有管理员页面和 API 仍必须做后端权限校验。

### 3.3 旧入口映射

| 当前入口 | 最终位置 | 处理 |
|---|---|---|
| AI 问答 | AI 助手 | 保留，改名并作为默认首页 |
| 笔记 | 知识中心/原始资料 | 改名，保留事实源定位 |
| 知识问答 | 系统工具/检索评测 | 从业务导航移除 |
| 知识对象 | 系统工具/旧 KO 归档 | 只读、管理员可见 |
| 审核 | 治理工作台/待审核 | 保留 Card 级审核 |
| 知识图谱 | 知识中心/知识图谱 | 与 P5 图谱合并 |
| P5 图谱 | 知识中心/知识图谱 | 删除“P5”产品名称 |
| Wiki 主题 | 知识中心默认页 | 保留 |
| 冲突中心 | 治理工作台/冲突 | 切换 Claim/Revision/Evidence 口径 |
| 健康度 | 治理工作台/知识质量 | 切换 Card V3 指标 |
| 知识债务 | 治理工作台/知识缺口 | 管理员内部可标注“知识债务” |
| 数据源 | 顶级入口 | 仅管理员可见 |

### 3.4 最终路由

```text
/                         → /assistant
/assistant                AI 助手

/knowledge                → /knowledge/wiki
/knowledge/wiki           Wiki 主题
/knowledge/documents      原始资料
/knowledge/graph          统一知识图谱

/governance               治理概览
/governance/review        待审核
/governance/conflicts     冲突
/governance/gaps          知识缺口
/governance/quality       知识质量

/sources                  数据源

/admin/retrieval          检索评测
/admin/legacy-kos         旧 KO 归档
/admin/flags              Feature Flag
/admin/model-health       模型健康
/admin/migration          迁移状态
```

旧路由采用显式 redirect，不复制页面：

```text
/review       → /governance/review
/p5-graph     → /knowledge/graph?view=entity
/wiki         → /knowledge/wiki
/graph        → /knowledge/graph?view=document
/conflicts    → /governance/conflicts
/health       → /governance/quality
/debts        → /governance/gaps
/search       → /admin/retrieval
```

旧 `/knowledge` 将被知识中心占用；旧 KO 组件必须先迁到 `/admin/legacy-kos`，再启用新的 `/knowledge` 父路由。该步骤不可颠倒。

### 3.5 前端代码结构

禁止继续在 `App.vue` 中手写全部菜单和权限判断，改为配置驱动：

```text
frontend/src/
├─ layouts/
│  ├─ AppShell.vue
│  ├─ KnowledgeLayout.vue
│  ├─ GovernanceLayout.vue
│  └─ AdminLayout.vue
├─ navigation/
│  ├─ config.ts
│  ├─ permissions.ts
│  └─ types.ts
├─ components/navigation/
│  ├─ PrimaryNav.vue
│  ├─ SectionNav.vue
│  ├─ MobileNavDrawer.vue
│  ├─ Breadcrumbs.vue
│  └─ UserMenu.vue
└─ router/
   ├─ index.ts
   ├─ guards.ts
   └─ legacyRedirects.ts
```

导航项配置示例：

```typescript
interface NavigationItem {
  key: string
  label: string
  route: string
  icon?: string
  order: number
  roles?: Array<'user' | 'knowledge_admin' | 'system_admin'>
  featureFlag?: string
  badge?: 'review_count' | 'debt_count' | 'source_error_count'
  children?: NavigationItem[]
}
```

同一配置同时驱动桌面导航、移动抽屉和面包屑，避免三处菜单不一致。

### 3.6 路由权限实现

路由 `meta` 统一定义：

```typescript
interface RouteMeta {
  requiresAuth: boolean
  requiredRoles?: string[]
  featureFlag?: string
  navGroup?: 'assistant' | 'knowledge' | 'governance' | 'sources' | 'admin'
  title: string
}
```

执行顺序：

1. 从 `localStorage` 恢复 token。
2. token 存在但用户为空时调用 `/api/auth/me`。
3. 未认证跳转 `/login`，并保存合法的 `redirect`。
4. 校验角色，权限不足进入 403 页面，不跳回首页掩盖错误。
5. 校验 Feature Flag，关闭的功能进入 404/功能未启用页。
6. 更新页面标题、面包屑和导航选中状态。

禁止从 JWT 前端解码结果直接信任管理员角色；角色以 `/api/auth/me` 返回为准。

### 3.7 知识中心实现

知识中心使用父布局和二级导航：

```text
知识中心
├─ Wiki 主题（默认）
├─ 原始资料
└─ 知识图谱
```

任务：

- Wiki 使用现有 Wiki 页面，不重复开发数据层。
- 原始资料复用 Editor/Page 列表，页面名称不再叫个人“笔记”。
- 图谱合并旧 Graph 与 P5 Graph：同一页面切换 `document/entity/community` 三种视图。
- 三种图谱视图共用实体详情抽屉、Card/Evidence 来源和 ACL。
- URL 保存当前 view、筛选和选中实体，刷新后状态可恢复。
- 页面之间使用 Card、Evidence、Page ID 跳转，不复制详情组件。

### 3.8 治理工作台实现

治理首页不是简单链接集合，而是可执行队列：

```text
待审核 Card
高风险变更
未解决冲突
高频知识缺口
Evidence 覆盖异常
数据源同步异常
```

四个子页面：

- **待审核**：Card Proposal、Revision Diff、Evidence 对照和批量审核。
- **冲突**：Claim、Revision、Evidence、来源版本冲突。
- **知识缺口**：高频无答案、用户请求、关联 Pending Card、重新验证。
- **知识质量**：Card V3、Evidence、Graph、Wiki、模型和数据源健康指标。

治理首页聚合 API 应一次返回概览，避免前端并发请求所有列表接口。

### 3.9 系统工具实现

- 旧 KO 归档强制只读，隐藏创建、编辑、发布和批量操作。
- 检索评测显示 Query Rewrite、各路召回、Fusion、Reranker 和降级原因。
- 模型健康显示真实 `used/degraded/error_code`，不展示 Secret。
- Feature Flag 修改保留审计人和更新时间。
- 系统工具不进入普通导航和站内业务搜索结果。

### 3.10 响应式与可访问性

#### 桌面端（>1280px）

- 顶部显示 2–4 个一级入口。
- 二级导航在页面内部展示，不把所有子页面放回顶栏。
- 数量徽标只显示需要处理的事项，最大显示 `99+`。

#### 平板/窄屏（768–1280px）

- 一级入口收进菜单按钮或侧抽屉。
- 当前页面标题和用户头像保持可见。
- 二级导航可以横向滚动，但正文不能产生整页横向滚动。

#### 手机（<768px）

- 使用全高导航抽屉。
- 治理表格切换卡片列表。
- 所有按钮最小点击区域 44×44px。

可访问性要求：键盘可操作、Esc 关闭菜单、焦点返回触发按钮、ARIA label/expanded/current 正确、颜色不是唯一状态表达。

### 3.11 按顺序执行的任务

#### 阶段 A：基础设施，不改变现有页面

- [ ] `P8-FE-01` 抽离 router、guards 和 navigation 配置。
- [ ] `P8-FE-02` 建立 AppShell、PrimaryNav、MobileNavDrawer、UserMenu。
- [ ] `P8-FE-03` 实现 requiresAuth/requiredRoles/featureFlag 守卫。
- [ ] `P8-FE-04` 增加 403、404、功能未启用页面。
- [ ] `P8-BE-01` 为所有管理员 API 补齐统一 `require_admin` 依赖。

#### 阶段 B：先迁移旧入口，再创建新父路由

- [ ] `P8-FE-05` 将 KoList 移至 `/admin/legacy-kos` 并改为只读。
- [ ] `P8-FE-06` 将 Search 移至 `/admin/retrieval`，名称改为检索评测。
- [ ] `P8-FE-07` 建立旧路由 redirect 和访问日志。
- [ ] `P8-FE-08` 建立 `/assistant`，根路由重定向到 AI 助手。

#### 阶段 C：知识中心

- [ ] `P8-FE-09` 创建 KnowledgeLayout 和二级导航。
- [ ] `P8-FE-10` 迁移 Wiki、Editor/Page 到新路由。
- [ ] `P8-FE-11` 合并 Graph/P5Graph 为三视图页面。
- [ ] `P8-FE-12` 统一 Card/Evidence/Page 详情跳转和 URL 状态。

#### 阶段 D：治理工作台

- [ ] `P8-BE-02` 新增治理概览聚合 API。
- [ ] `P8-BE-03` 新增 Card V3 知识质量指标 API。
- [ ] `P8-BE-04` 冲突扫描扩展到 Claim/Revision/Evidence。
- [ ] `P8-BE-05` 旧 KO 指标标记 legacy，不计入 V3 主指标。
- [ ] `P8-FE-13` 创建 GovernanceLayout 和概览。
- [ ] `P8-FE-14` 迁移审核、冲突、债务和健康页面。
- [ ] `P8-FE-15` “健康度”改名“知识质量”，“知识债务”改名“知识缺口”。
- [ ] `P8-FE-16` “创建 KO”替换为“创建 Card Proposal”。

#### 阶段 E：系统工具与数据源入口

- [ ] `P8-FE-17` 创建 AdminLayout 和系统工具菜单。
- [ ] `P8-FE-18` 将 Flag、模型健康、迁移状态接入系统工具。
- [ ] `P8-FE-19` 预留 `/sources` 顶级入口，仅管理员和 Feature Flag 开启时显示。

#### 阶段 F：响应式、测试和灰度

- [ ] `P8-FE-20` 完成三档响应式布局和键盘操作。
- [ ] `P8-TEST-01` 单测导航过滤、角色判断、Feature Flag 和 badge。
- [ ] `P8-TEST-02` 路由测试未登录、普通用户、知识管理员、系统管理员。
- [ ] `P8-TEST-03` E2E 覆盖登录、刷新、深链接、旧路由重定向和退出。
- [ ] `P8-TEST-04` 视觉测试覆盖 1440、1024、768、390px。
- [ ] `P8-TEST-05` 检查构建分包，Editor/Graph 不进入 AI 助手首屏包。

### 3.12 验收标准

- 顶栏业务入口最多 4 个，不出现“P5”“知识对象”等内部术语。
- 普通用户只看到 AI 助手和知识中心。
- 管理员刷新任意深层路由后，身份、菜单和当前页面不丢失。
- 普通用户直接访问 `/governance`、`/sources`、`/admin/*` 返回 403。
- 旧路由全部有明确 redirect，不出现循环跳转和空白页。
- Graph/P5 Graph 合并后功能、数据和来源追溯不减少。
- 知识质量不再把旧 KO 数量当作 V3 主指标。
- Conflict、Gap 的动作全部指向 Card/Claim/Evidence。
- 390px 宽度无整页横向滚动，导航可键盘操作。
- AI 助手首次加载不下载 Editor、Graph、治理页面代码。
- Feature Flag 可恢复旧入口，回滚不需要数据库回退。

## 4. P9：数据源平台底座

### 4.1 目标架构

```text
企业系统
→ Connector 增量拉取
→ NormalizedSourceItem
→ 幂等、删除和 ACL 校验
→ Page/Asset
→ Evidence
→ Card Proposal
→ 人工审核
→ Published Card
→ Retrieval / Graph / Wiki
```

所有来源必须进入现有 Evidence→Card 主链路，禁止绕过 Evidence 直接生成可信 Wiki 或发布知识。

### 4.2 明确不做

- 不复制对方项目的内存 `_tasks`、`_statuses`。
- 不使用裸 `asyncio.create_task` 作为正式任务系统。
- 不在哈希去重前调用 LLM。
- 不让 LLM 判断删除、ACL 或是否保存原始事实源。
- 不把账号、Token、Secret 明文返回前端。
- 不在长数据库事务内执行网络、解析、Embedding 和 Card 编译。

### 4.3 Connector 契约

```text
backend/app/sources/
├─ base.py
├─ registry.py
├─ schemas.py
├─ service.py
├─ worker.py
├─ dingtalk.py
├─ gitlab.py
└─ filters.py
```

```python
class SourceConnector(Protocol):
    key: str
    name: str

    async def test_connection(self) -> ConnectionTestResult: ...
    async def discover(self) -> list[SourceScope]: ...
    async def iter_changes(self, cursor: dict | None) -> AsyncIterator[SourceChange]: ...
    async def fetch_item(self, external_id: str) -> NormalizedSourceItem: ...
    async def fetch_attachments(self, external_id: str) -> list[SourceAttachment]: ...
    async def fetch_acl(self, external_id: str) -> SourceACL: ...
```

Connector 只负责外部通信与标准化，不直接操作 Card、Wiki 或图谱。

### 4.4 标准化模型

```text
NormalizedSourceItem
├─ connection_id
├─ source_type
├─ external_id
├─ external_version
├─ title
├─ content
├─ content_type
├─ content_hash
├─ source_url
├─ source_path
├─ source_updated_at
├─ deleted
├─ acl_scope
├─ metadata_json
└─ attachments
```

- `external_id` 在连接内稳定唯一。
- `content_hash` 根据标准化正文计算。
- 无版本号时使用更新时间与哈希组合。
- ACL 无法解析必须 fail closed。
- LLM 输出不能覆盖原始标题、正文、URL 和版本。

### 4.5 数据模型

#### SourceConnection

`id, connector_key, name, enabled, config_json, secret_ref, target_notebook_id, default_acl_json, cursor_json, last_success_at, last_error_at, created_by, created_at, updated_at`

约束：`connector_key + name` 唯一；`config_json` 只能保存非敏感配置。

#### SourceItem

`id, connection_id, external_id, external_version, content_hash, metadata_hash, source_url, source_path, page_id, state, acl_json, source_updated_at, last_synced_at, last_error, retry_count, created_at, updated_at`

约束：`connection_id + external_id` 唯一；`state` 为 `active/skipped/deleted/error`。

#### SourceSyncRun

`id, connection_id, mode, status, cursor_before_json, cursor_after_json, discovered_count, created_count, updated_count, unchanged_count, deleted_count, failed_count, cancel_requested, heartbeat_at, started_at, finished_at, error_summary, created_by`

同一连接只允许一个 `queued/running` 任务。

#### SourceSyncError

`id, run_id, external_id, stage, error_code, error_message, retryable, retry_count, created_at, resolved_at`

`stage` 为 `discover/fetch/parse/persist/index/compile`。

### 4.6 任务执行机制

第一阶段采用“数据库任务表 + 单独 Worker”，不引入重型消息队列：

1. API 创建 `SourceSyncRun(status=queued)` 并立即返回。
2. Worker 原子抢占任务并改为 `running`。
3. 每处理一批更新计数和 `heartbeat_at`。
4. 重启后将超时的 `running` 任务重新入队。
5. 取消只设置 `cancel_requested=true`，Worker 在安全检查点停止。
6. 每个 SourceItem 独立短事务，单条失败不回滚整批。
7. PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`；SQLite 使用短事务和唯一运行约束。

只有多实例和任务规模超过数据库 Worker 能力后，再评估 Celery、Dramatiq 或 Temporal。

### 4.7 幂等规则

| 情况 | 动作 |
|---|---|
| 首次发现 | 创建 Page，生成 Evidence，运行 Card 编译 |
| 内容哈希未变 | 只更新检查时间，不调用解析、Embedding、LLM |
| 仅 ACL/标题变化 | 更新元数据和 ACL，不重算正文向量 |
| 正文变化 | 更新 Page，旧 Evidence stale，生成新 Evidence 和 Card Revision Proposal |
| 源端删除 | SourceItem deleted，Page 归档，Evidence stale，索引移除 |
| 删除后恢复 | 恢复 active，按当前版本重跑主链路 |
| 单条失败 | 记录 SourceSyncError，其他条目继续 |

### 4.8 ACL 与安全

- Connector 输出统一 `acl_scope`，映射现有 Notebook/UserGroup 权限。
- 数据源连接、任务和错误详情仅管理员可见。
- SourceItem、Page、Evidence、Card、Graph、Wiki 使用相同可见范围。
- 多来源 Card 使用最严格范围，公开来源不得扩大私有来源权限。
- Secret 使用环境变量、Secret Manager 或 `secret_ref`，不进日志和 API。

### 4.9 LLM 边界

- 原文保存、变更、删除和 ACL 判断不使用 LLM。
- Parser 和规则优先做确定性清理。
- GLM-5.1 只用于可选标题优化、Claim 和 Card Proposal。
- 调用模型前必须完成哈希去重。
- 模型结果必须能追溯 SourceItem、Page 和 Evidence。

### 4.10 后端任务

- [ ] `P9-BE-01` 新增四张数据源表和 Alembic 迁移。
- [ ] `P9-BE-02` 定义 Connector Protocol 与标准 Schema。
- [ ] `P9-BE-03` 实现 Registry，导入模块时不得启动任务。
- [ ] `P9-BE-04` 实现 SourceSyncService、幂等和状态机。
- [ ] `P9-BE-05` 实现数据库 Worker、心跳、恢复与取消。
- [ ] `P9-BE-06` 接通 `sync_page_evidence()` 和 `compile_page_to_cards()`。
- [ ] `P9-BE-07` 实现删除、恢复、ACL 与附件变化处理。
- [ ] `P9-BE-08` 增加结构化日志：connection/run/item/stage。
- [ ] `P9-BE-09` 增加 `SOURCE_HUB_ENABLED` Feature Flag。

### 4.11 API

```text
GET    /api/sources
POST   /api/sources/connections
GET    /api/sources/connections/{id}
PATCH  /api/sources/connections/{id}
POST   /api/sources/connections/{id}/test
POST   /api/sources/connections/{id}/sync
POST   /api/sources/runs/{id}/cancel
GET    /api/sources/runs
GET    /api/sources/runs/{id}
GET    /api/sources/runs/{id}/errors
POST   /api/sources/runs/{id}/retry-failed
GET    /api/sources/items
GET    /api/sources/items/{id}
```

所有写接口要求管理员权限，响应不得包含密码、Token 和完整 Secret。

## 5. P10：现有钉钉同步 Connector 化

### 5.1 原则

- 不重写已经稳定的下载、转换、Manifest、删除检测和来源保护。
- Connector 作为适配层调用现有 `DingTalkSyncService`。
- 保留 `/api/dingtalk/*` 到统一页面验收完成。
- 两套入口必须共享任务锁，禁止重复同步。

### 5.2 执行步骤

1. 为现有钉钉空间创建 SourceConnection。
2. 将 Manifest document ID 映射为 SourceItem.external_id。
3. 回填现有 Page 的 SourceItem.page_id，不重新下载和编译。
4. 将空间、文档、下载和删除事件包装为 SourceChange。
5. 复用现有转换结果生成 NormalizedSourceItem。
6. 哈希未变化直接记 unchanged。
7. 内容变化调用 Page 导入和 Evidence/Card 主链路。
8. 删除先归档并 stale 证据，不立即物理删除。
9. 对比新旧入口文档数、哈希、失败项和删除项。
10. 验收后将 Editor 钉钉弹窗改为跳转统一“数据源”。

### 5.3 任务与验收

- [ ] `P10-BE-01` 实现 DingTalkConnector 适配器。
- [ ] `P10-BE-02` 编写 Manifest→SourceItem 回填脚本。
- [ ] `P10-BE-03` 保证新旧入口共享运行锁。
- [ ] `P10-BE-04` 增加 ACL fail-closed 测试。
- [ ] `P10-BE-05` 覆盖删除、恢复、加密和附件失败。
- [ ] `P10-FE-01` Editor 同步入口跳转统一页面。

验收要求：不新增重复 Page；未变化文档不重算；既有来源元数据保留；删除同步到 Page、Evidence、索引和 Card Source。

## 6. P11：数据源管理页面

### 6.1 页面范围

- Connector 类型、连接名称和状态。
- 非敏感配置摘要和连接测试。
- 增量、回填、全量对账。
- 任务进度、心跳和各类计数。
- 失败条目、错误阶段和单项重试。
- 最近同步历史。
- SourceItem→Page→Evidence→Card 追溯。

### 6.2 任务与验收

- [ ] `P11-FE-01` 创建管理员数据源列表。
- [ ] `P11-FE-02` 创建配置抽屉且不回显 Secret。
- [ ] `P11-FE-03` 创建运行记录和进度详情。
- [ ] `P11-FE-04` 创建失败项查看和重试入口。
- [ ] `P11-FE-05` 创建 SourceItem 追溯抽屉。
- [ ] `P11-FE-06` 三种同步模式提供明确说明和风险提示。

验收要求：刷新和后端重启后状态不丢；普通用户不可访问；取消在安全检查点生效；前端不存明文密钥。

## 7. P12：GitLab 第二数据源试点

### 7.1 范围

第二 Connector 用来证明抽象可复用。首版只接入高价值、低噪声内容：

1. Repository 中的 Markdown、AsciiDoc 和文本说明。
2. GitLab Wiki。
3. 已关闭且含解决方案的 Issue 放到第二阶段。
4. Merge Request 讨论暂不进入首版。

无法暂时取得 GitLab Token 时，可用 LocalDirectoryConnector 做契约测试替身，但正式验收仍需真实第二来源。

### 7.2 安全与 ID

- Token 只授予 `read_api` 和必要的 `read_repository`。
- 不允许 Connector 修改 GitLab 内容。
- external_id：`project:{project_id}:repo:{ref}:{path}`。
- Wiki external_id：`project:{project_id}:wiki:{slug}`。
- Issue external_id：`project:{project_id}:issue:{iid}`。

### 7.3 增量策略

- Repository 比较 commit SHA、blob ID 和内容哈希。
- Wiki 比较 slug、更新时间和哈希。
- Issue 使用 `updated_after` 游标并保留重叠窗口。
- 定期全量对账远端 ID，识别删除和移动。
- 路径移动且内容哈希相同时记录 move，不重复生成 Card。

### 7.4 任务

- [ ] `P12-BE-01` 实现连接测试、分页和限流。
- [ ] `P12-BE-02` 实现 Repository 文档同步。
- [ ] `P12-BE-03` 实现 Wiki 增量同步。
- [ ] `P12-BE-04` 实现项目/组权限到内部 ACL 映射。
- [ ] `P12-BE-05` 实现 429、5xx、网络中断退避恢复。
- [ ] `P12-BE-06` 实现删除、路径移动和默认分支切换。
- [ ] `P12-FE-01` 增加 GitLab 配置和同步范围选择。

### 7.5 试点与验收

- 选择 1 个权限明确的测试项目，最多 50 个 Markdown/Wiki。
- Card Proposal 只生成 draft，不自动发布。
- 人工抽查 10 个 SourceItem 的原文、Evidence、Card 和 ACL。
- 相同 commit 重复同步 3 次，不新增 Page/Evidence/Embedding/Card Proposal。
- 修改只影响对应 Page、Evidence、Revision 和派生层。
- 删除后检索不再返回旧内容。
- 无权限用户无法通过 Chat、Search、Graph、Wiki 和直接 API 访问。

## 8. P13：测试、性能、安全与模型验收

### 8.1 自动化测试

- [ ] 每个 Connector 运行相同的契约测试。
- [ ] 覆盖标准化、哈希、external_id、ACL 和 Secret 脱敏。
- [ ] 覆盖 SourceItem 幂等、删除、恢复和移动。
- [ ] 覆盖 SyncRun 合法/非法状态转换。
- [ ] 覆盖心跳超时恢复、取消和单条失败继续。
- [ ] FakeConnector 跑通 Page→Evidence→Card Proposal。
- [ ] 覆盖钉钉新旧入口并发触发。
- [ ] 覆盖 GitLab 分页、429、5xx、空页面和二进制文件。
- [ ] 覆盖同步中途重启和 ACL 全链路隔离。

### 8.2 性能与数据库

- 网络、解析、Embedding 和 LLM 不得持有写事务。
- 默认批量 20、单连接并发 2，配置必须有上限。
- SQLite 保持 WAL、busy_timeout 和短事务。
- 同步时连续调用 Page、Chat、Search，P95 不超过基线 20%。
- 20 个 SourceItem 连续失败不能阻塞其他条目。
- 不允许出现 `database is locked` 或永久 `running` 任务。

### 8.3 日志与指标

日志字段：`connection_id, run_id, external_id, stage, duration_ms, result, retry_count`。

指标：最近成功时间、同步延迟、阶段耗时、新增/更新/跳过/删除/失败、Embedding/LLM 次数、哈希去重节省次数、Card Proposal 数量和 NO_CHANGE 比例。

### 8.4 公司 Embedding/Reranker 验收门

当前公司服务在线，但 `/v1/models` 没有已启动模型。管理员启动后：

1. 确认 Embedding/Reranker 实际 UID。
2. 确认 Embedding 维度 1024、Reranker score 字段和排序方向。
3. 补齐试点数据缺失向量。
4. 对比纯 BM25 与 Dense+Reranker 指标。
5. 记录 P50/P95、错误率和降级次数。

模型不可用时必须安全降级；可用后的效果如果不优于 BM25，不得只为“使用模型”而启用。

### 8.5 Embedding/Reranker 降级语义修复

#### 当前问题

对方新版本及当前项目都需要避免以下误判：

- Embedding 批处理失败后用空向量占位，但上层无法明确知道 Dense 路是否真正执行。
- Reranker 失败后返回全部 `1.0`，上层仍可能把候选标记为 `reranker` 来源。
- “服务不可达、模型未启动、UID 错误、协议错误、维度错误”被统一记录成普通异常，无法治理。
- 页面仍能依靠 BM25 返回结果，容易让使用者误以为公司模型已经参与。

#### 统一返回契约

Embedding 不再只返回 `list[list[float]]`，增加批次状态：

```python
class EmbeddingBatchResult:
    embeddings: list[list[float]]
    used: bool
    degraded: bool
    model_uid: str
    dimensions: int | None
    latency_ms: int
    error_code: str | None
    error_message: str | None
```

Reranker 不再用统一分数伪装成功：

```python
class RerankResult:
    results: list[dict]
    used: bool
    degraded: bool
    model_uid: str
    latency_ms: int
    error_code: str | None
    error_message: str | None
```

只有 `used=true` 时才允许：

- 将候选来源标记为 `dense` 或 `reranker`。
- 在检索 Trace 中记录该模型参与评分。
- 计算 Dense/Reranker 命中率和延迟。

失败时执行明确降级：

```text
Embedding 失败
→ dense_used=false
→ 跳过向量路
→ 保留 BM25/关键词/图谱召回

Reranker 失败
→ reranker_used=false
→ 保留 RRF/Fusion 原排序
→ 不生成虚假的 relevance_score=1.0
```

#### 错误分类

统一 `error_code`：

| error_code | 含义 | 是否重试 |
|---|---|---|
| `NETWORK_UNREACHABLE` | DNS、连接、Socket 权限失败 | 是 |
| `TIMEOUT` | 请求超时 | 是 |
| `MODEL_NOT_LOADED` | 服务在线但模型列表无 UID | 否，等待管理员启动 |
| `MODEL_UID_INVALID` | 配置 UID 不存在 | 否，修改配置 |
| `AUTH_FAILED` | 401/403 | 否，检查凭证 |
| `BAD_REQUEST` | 请求协议或字段不兼容 | 否，修复客户端 |
| `RATE_LIMITED` | 429 | 是，指数退避 |
| `SERVER_ERROR` | 5xx | 是 |
| `INVALID_RESPONSE` | 返回缺少 data/results/index/score | 否 |
| `DIMENSION_MISMATCH` | 向量维度不是配置值 | 否，禁止写入 |

服务返回 400 时必须保留脱敏后的错误正文，用于区分 `MODEL_NOT_LOADED` 与普通 `BAD_REQUEST`。

#### 启动与运行时健康检查

新增模型健康状态：

```text
GET /api/admin/model-health
```

响应至少包含：

```json
{
  "embedding": {
    "configured": true,
    "model_uid": "...",
    "available": false,
    "dimensions": null,
    "last_success_at": null,
    "last_error_code": "MODEL_NOT_LOADED"
  },
  "reranker": {
    "configured": true,
    "model_uid": "...",
    "available": false,
    "last_success_at": null,
    "last_error_code": "MODEL_NOT_LOADED"
  }
}
```

- 后端启动时只做轻量探测，不因公司模型不可用阻止应用启动。
- 探测结果设置短时缓存，禁止每个用户请求都调用 `/v1/models`。
- 运行时请求失败应更新健康状态和降级计数。
- 管理员页面显示“正常/已降级/模型未启动/配置错误”，普通用户不显示内部地址和错误正文。

#### 实施任务

- [ ] `P13-MODEL-01` 定义 EmbeddingBatchResult 与 RerankResult。
- [ ] `P13-MODEL-02` Embedding 空向量改为显式降级状态，Dense 路只处理合法维度向量。
- [ ] `P13-MODEL-03` Reranker 失败时保留 Fusion 原排序，不返回虚假统一分数。
- [ ] `P13-MODEL-04` 只有真实模型调用成功才写入 `dense/reranker` 来源和 Trace。
- [ ] `P13-MODEL-05` 实现 HTTP/协议/模型/维度错误分类和脱敏日志。
- [ ] `P13-MODEL-06` 新增模型健康检查服务、缓存和管理员 API。
- [ ] `P13-MODEL-07` 增加调用次数、成功率、降级率、P50/P95 和错误码指标。
- [ ] `P13-MODEL-08` 管理员“知识质量”页面增加模型运行状态卡片。

#### 测试与验收

- [ ] 模型列表为空时识别为 `MODEL_NOT_LOADED`，Search/Chat 仍能使用 BM25。
- [ ] UID 错误时不写入空向量，不覆盖已有有效向量。
- [ ] 返回维度不是 1024 时拒绝入库并记录 `DIMENSION_MISMATCH`。
- [ ] Reranker 返回 400/500/超时后结果顺序与 Fusion 降级顺序一致。
- [ ] 降级结果的 `sources` 和 Trace 中不出现 `reranker`。
- [ ] 模型恢复后无需重启即可在健康缓存过期后重新启用。
- [ ] 管理员可以看到最近成功时间和错误分类，普通用户看不到内部服务信息。
- [ ] 评测报告必须同时记录 `dense_used`、`reranker_used` 和降级原因，禁止只记录最终 Recall。

## 9. P14：灰度、切换与回滚

### 9.1 Feature Flag

```text
SOURCE_HUB_ENABLED
DINGTALK_CONNECTOR_ENABLED
GITLAB_CONNECTOR_ENABLED
SOURCE_CARD_COMPILE_ENABLED
LEGACY_SEARCH_VISIBLE
LEGACY_KO_VISIBLE
```

### 9.2 灰度顺序

1. 数据源页面先仅管理员可见。
2. 钉钉双入口只做结果比对，不执行双重同步。
3. 新入口先同步 10 篇钉钉文档。
4. GitLab 试点不超过 50 篇。
5. 连续 7 天无重复、越权和不可恢复任务。
6. 将统一数据源入口设为默认。
7. 隐藏旧钉钉弹窗、旧 Search、旧 KO 导航。
8. 旧 API 与映射表至少保留一个验收周期。

### 9.3 回滚

- 关闭 `SOURCE_HUB_ENABLED` 后 Worker 不再抢占新任务。
- 已完成的 Page/Evidence/Card 不删除，可按 SyncRun 定位影响范围。
- 钉钉恢复旧入口，但不能与新任务并发。
- GitLab 关闭后停止拉取，已同步内容按管理员决策保留或归档。
- 回滚不删除 SourceItem、SyncRun 和错误审计记录。

## 10. 排期

| 周期 | 工作 | 交付物 |
|---|---|---|
| 第 1 周 | P8 + P9 Schema/契约 | 最终导航、V3 治理口径、迁移、Connector 契约 |
| 第 2 周 | P9 Worker + P10 | 持久任务、恢复/取消、钉钉适配、回填脚本 |
| 第 3 周 | P11 | 数据源连接、进度、错误、追溯页面 |
| 第 4 周 | P12 | GitLab Repository/Wiki、50 文档试点 |
| 第 5 周 | P13 | ACL、删除、重启、性能、公司模型评测 |
| 第 6 周 | P14 | 默认入口切换、旧入口隐藏、回滚演练 |

单人开发时 P8、P9 不并行，按 6 周执行，不通过跳过测试压缩周期。

## 11. Definition of Done

- 至少两个真实 Connector 通过同一契约测试。
- 重复同步不生成重复 Page、Evidence 或 Card Proposal。
- 未变化内容不重复调用 Embedding 和 LLM。
- 内容更新生成新 Evidence 和 Card Revision Proposal。
- 源端删除同步清理检索并保留审计记录。
- 重启后任务可恢复或明确失败，不永久显示“同步中”。
- ACL 在 Page、Evidence、Card、Retrieval、Graph、Wiki 一致。
- 密钥不进入 API、日志、数据库明文字段和前端存储。
- 普通用户不能访问数据源管理和错误详情。
- 同步期间页面、搜索和问答无明显卡顿。
- 新 Card 只生成 draft，数据源不能直接发布知识。
- 回滚演练通过，旧钉钉入口可由 Feature Flag 恢复。

## 12. 实施参考

### 对方项目

- `D:\workspace\Claudework\rag\rag\backend\app\sources\base.py`：最小 Connector 接口。
- `D:\workspace\Claudework\rag\rag\backend\app\sources\__init__.py`：插件注册表。
- `D:\workspace\Claudework\rag\rag\backend\app\api\sources.py`：统一 API；不要照搬内存任务状态。
- `D:\workspace\Claudework\rag\rag\backend\app\core\ingest.py`：参考统一入库思想，不照搬直接 Page/Wiki 方式。
- `D:\workspace\Claudework\rag\rag\frontend\src\views\Sources.vue`：管理页面交互参考。

### 当前项目必须复用

- `backend/app/api/dingtalk.py`
- `backend/app/core/dingtalk_sync_service.py`
- `backend/app/core/dingtalk_storage.py`
- `backend/app/core/evidence_ingest.py`
- `backend/app/core/knowledge_compiler_v3/pipeline.py`
- `backend/app/core/feature_flags.py`
- `backend/app/core/retrieval/pipeline.py`

### 官方资料

- [Vue Router 路由元信息](https://router.vuejs.org/guide/advanced/meta.html)
- [Vue Router 导航守卫](https://router.vuejs.org/guide/advanced/navigation-guards.html)
- [WAI-ARIA Disclosure Navigation Menu](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/examples/disclosure-navigation/)
- [Web Content Accessibility Guidelines](https://www.w3.org/WAI/standards-guidelines/wcag/)
- [GitLab REST API](https://docs.gitlab.com/api/)
- [Repository Files API](https://docs.gitlab.com/api/repository_files/)
- [GitLab Wikis API](https://docs.gitlab.com/api/wikis/)
- [GitLab REST 分页](https://docs.gitlab.com/api/rest/#pagination)
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)
- [SQLAlchemy 事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)
- [PostgreSQL SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html)

## 结论

剩余建设只完成两件事：

1. 将现有 V3 能力收口成一致的用户与管理员产品形态。
2. 建立可靠的数据源采集平台，让钉钉、GitLab 和后续系统统一进入 Evidence→Card→审核→发布→问答闭环。

```text
企业数据源
→ 可恢复增量同步
→ 原始 Page/Asset
→ Evidence
→ Card Proposal
→ 人工审核
→ Published Card
→ Retrieval / Graph / Wiki
→ 用户问答与知识债务
→ 反向推动来源和 Card 更新
```
