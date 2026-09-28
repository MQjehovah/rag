# RAG 用户与权限管理设计（RBAC）

日期：2026-09-28
状态：已确认，待实施
关联：`docs/plans/2026-09-20-knowledge-authz-design.md`（可见性隔离，已完成）；本设计在其之上补「功能权限 RBAC + 用户/组管理」

## 背景

rag 目前只有 `__local_admin__` 一个粗粒度管理员标记（SSO roles 含 `admin` → 登录时补该组名）：

- **菜单不设防**：「数据与管道」组（数据源/编译管道/编译模板/知识图谱/嵌入模型）对所有登录用户可见，仅部分按钮按 `isAdmin` 隐藏；路由无守卫
- **无用户管理**：用户来自 LDAP/SSO 自动建号 + 环境变量创建的本地管理员；无列表/新建/禁用/重置密码入口
- **无角色体系**：功能权限全部硬编码为 `__local_admin__` 判定（约 50 处），无法把「数据源管理」「嵌入模型管理」等权限单独授予某个角色
- **无组注册表**：`notebooks.group_id`/`wiki_pages.group_id`/`pipelines.group_id`/`compile_templates.group_id` 都是自由文本组名，易拼错；成员关系在 `user_groups`（登录时全量同步）

## 目标

1. 「数据与管道」菜单组对**无管理权限的普通用户整组隐藏**，路由加守卫，后端按权限键拦截
2. 新增「角色 → 权限键」RBAC：角色可配模块级管理权限；内置 `admin`（`*`）不可改删
3. 新增用户管理：列表/搜索、新建本地账号、启用/禁用、重置密码（仅本地）、编辑资料、分配角色与组
4. 新增组管理（轻量注册表）：组列表（来源/成员数/引用数）、新建、删除（有引用拦截）、成员增删（本地可改，LDAP/SSO 同步成员只读）
5. SSO/LDAP 管理员继续可用：保留 `__local_admin__` 标记语义（等效 `*`），无感兼容

## 非目标（YAGNI）

- 不做资源级 ACL（每个页面/笔记本直接授权到用户）；资源可见域仍按组（沿用 knowledge-authz 语义）
- 不做组重命名级联（改资源表自由文本太重；v1 只做注册表约束新建/删除）
- 不改 token 结构（权限每请求从 DB 解析，见下）
- 不合并 LDAP/SSO 的组同步逻辑（登录同步仍是权威）

## 权限模型（增量式 RBAC）

- **有效权限** = 用户所有角色权限键的并集；若 `groups` 含 `__local_admin__`（SSO admin/LDAP 管理员/LDAP_GROUP_MAP_ADMIN）→ 并集补 `*`
- **`get_current_user` 已每请求查库组装 payload**（`build_user_payload`），在其中解析 roles/permissions ⇒ 授权变更**即时生效**，无需重登，也不改 JWT 结构
- **判定助手**：`app/core/security.py` 新增
  - `has_permission(user, key)`：`*` 或 key 命中
  - `require_permission(key)`：FastAPI 依赖，不足 403
- **现有 `__local_admin__` 调用点迁移**：按模块替换为对应权限键；无法细分的（如所有权旁路）保留 `has_permission(user, "*")`（等价现状）

### 权限键目录（v1）

| 键 | 名称 | 覆盖范围 |
| --- | --- | --- |
| `sources.manage` | 数据源管理 | `/api/sources*` 增删改、Jira 同步 |
| `pipeline.manage` | 编译管道与模板 | `/api/pipelines*`、`/api/compile-templates*` 增删改/运行 |
| `embedding.manage` | 嵌入模型管理 | `/api/embeddings*` 档案增删改、重建索引 |
| `graph.manage` | 知识图谱管理 | `/api/graph*` 重建/社区摘要；图谱页浏览（菜单可见性） |
| `wiki.admin` | Wiki 空间管理 | `/api/wiki/spaces*` CRUD、全库重建 |
| `notebook.manage` | 笔记本管理 | `/api/notebooks*` 增删改/组可见域 |
| `page.manage` | 全库页面管理 | 他人页面/回收站清理等管理操作 |
| `user.manage` | 用户管理 | `/api/admin/users*` |
| `role.manage` | 角色管理 | `/api/admin/roles*` |
| `group.manage` | 组管理 | `/api/admin/groups*` |
| `*` | 全部权限 | 仅内置 admin |

目录以代码常量维护（含分组/名称/说明），`GET /api/admin/permissions` 输出给角色编辑页。

## 数据模型（幂等建表）

```
roles       id, name(unique), display_name, permissions(JSON list[str]),
            is_system(bool), created_at, updated_at
user_roles  id, user_id, role_id, UNIQUE(user_id, role_id)
groups      id, name(unique), source('local'|'ldap'|'sso'), created_at
            （注册表：登记可用组名；成员关系仍在 user_groups）
```

- 启动 seed：内置 `admin` 角色（`["*"]`，is_system=true）；若 env 创建的本地产管理员无任何角色 → 幂等授予 `admin` 角色
- `groups` 注册表首次启动时从既有 `user_groups.group_name` 与资源表 `group_id` 去重回填（标记 source='local'），保证下拉可用；`__local_admin__` 等内部标记不回填
- 迁移方式沿用仓库惯例：`init_db` create_all + 启动幂等回填（无 Alembic）

## 后端 API

### 角色（`role.manage`）
- `GET /api/admin/permissions` — 权限键目录（分组/名称/说明）
- `GET /api/admin/roles` — 角色列表（含使用人数）
- `POST /api/admin/roles` — 新建（name/display_name/permissions）
- `PUT /api/admin/roles/{id}` — 编辑（is_system 禁改）
- `DELETE /api/admin/roles/{id}` — 删除（is_system 禁删；有用户使用时提示先解绑）

### 用户（`user.manage`）
- `GET /api/admin/users?query=&page=&page_size=` — 列表（用户名/邮箱/显示名/本地或SSO/启用状态/角色/组）
- `POST /api/admin/users` — 新建本地账号（username/password/display_name/email/roles[]/groups[]）
- `PUT /api/admin/users/{id}` — 编辑资料（display_name/email/is_active）
- `POST /api/admin/users/{id}/password` — 重置密码（仅本地账号）
- `PUT /api/admin/users/{id}/roles` — 全量设置角色
- `PUT /api/admin/users/{id}/groups` — 全量设置组（仅本地账号；SSO/LDAP 用户的组来自登录同步 → 只读）
- 自我保护：
  - 不能禁用自己；不能移除自己的最后一个管理员来源（角色 or `__local_admin__`）
  - SSO 管理员标记（`__local_admin__`）只读展示「来自 SSO」，不可在界面移除

### 组（`group.manage`）
- `GET /api/admin/groups` — 列表（名称/来源/成员数/被引用资源数）
- `POST /api/admin/groups` — 新建（注册名）
- `DELETE /api/admin/groups/{id}` — 删除（被资源或成员引用时 409 拦截）
- `GET /api/admin/groups/{id}/members` — 成员（本地/同步来源标记）
- `PUT /api/admin/groups/{id}/members` — 增删成员（仅对本地账号生效；同步用户的成员关系由登录同步维护）

### 认证
- `/api/auth/me` 响应增加 `roles: [{name, display_name}]` 与 `permissions: string[]`（只增字段，兼容旧前端）

## 前端

- auth store：解析 `roles`/`permissions`，导出 `hasPerm(key)` 助手
- 菜单（`App.vue` 数据化）：
  - 「工作区」不变
  - 「数据与管道」：无任何相关权限（`sources/pipeline/embedding/graph/wiki/notebook/page` 中无一命中且非 `*`）→ **整组隐藏**；自定义管理角色按命中子项显示
  - 新增「系统管理」组：用户管理（`user.manage`）、角色权限（`role.manage`）、组管理（`group.manage`）
- 路由（`main.ts`）：管理路由挂 `meta.perm`，无权限跳回 `/`
- 新页面：
  - `/admin/users`：搜索/分页表格、新建本地账号、编辑资料、启用禁用、重置密码、角色多选、组展示（本地可改/SSO 只读）
  - `/admin/roles`：角色列表 + 权限键分组勾选；内置 admin 只读展示；删除前提示使用人数
  - `/admin/groups`：组列表（来源/成员数/引用数）、新建/删除、成员管理抽屉
- 既有页面 `isAdmin` 判定改用 `hasPerm`（Embeddings/Wiki 等）；Embeddings 只读列表随「数据与管道」组一并仅管理权限可见

## 兼容与迁移

- 新表随 `init_db` 自动创建；启动幂等 seed（admin 角色、组注册表回填、本地管理员补角色）
- 管理员角色的自动补授是**单向**的：`__local_admin__` 标记消失不会自动回收已授予的 admin 角色，需在用户管理界面手动收回
- 现有 SSO/LDAP 管理员：登录时仍生成 `__local_admin__` → 等效 `*`；用户管理页显示「SSO 管理员」徽标（不可移除）
- 所有既有接口入参不变；`/api/auth/me` 只增字段
- 前端旧缓存 token 无需失效（权限每请求解析）

## 测试

后端 pytest：
- 权限并集/`*` 通配/`__local_admin__` 兼容
- 数据与管道各端点：无权限 403、对应权限键放行
- 用户管理 CRUD：新建本地账号可登录、禁用后 401/403、重置密码生效、SSO 用户组只读、SSO 管理员标记不可移除
- 角色 CRUD：内置 admin 禁改删；删除使用中角色拦截
- 自我保护：不能禁用自己、不能移除最后一个管理员来源
- 组：注册表回填、删除被引用组 409、成员增删仅本地账号生效

前端：`npm run build`（vue-tsc）通过；手工验收：
1. 普通用户看不到「数据与管道」与「系统管理」菜单，直访路由回首页
2. 管理员全菜单可见；授予自定义角色后按权限项显示
3. 用户/角色/组三个页面操作即时生效（另开页面验证被改用户权限变化）

## 验收标准

1. 普通用户菜单无「数据与管道」「系统管理」；直访 `/sources` 等被重定向
2. 新建角色勾选 `sources.manage` 后，该角色用户可见「数据与管道 → 数据源」并可操作；其余管理项不可见且后端 403
3. 用户管理可创建本地账号、禁用、重置密码；被禁用用户登录/旧 token 均 401
4. 管理员标记可由界面授予（本地角色）/收回；SSO 来源管理员不可收回且徽标明示
5. 组管理可新建/删除/增删本地成员；被资源引用的组删除被拦截
6. `pytest` 无新增失败；`npm run build` 通过
