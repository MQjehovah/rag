# Phase 9A 隔离真实后端联调 —— 冻结契约（Shared Contract）

> 由主 Agent 冻结。Agent A（后端隔离/harness/链路测试）与 Agent B（AdminRunsPanel
> 反例/浏览器联调脚本）都必须严格按本文件的标识符与行为实现，不得自行改名。
> 本文件是全链路的唯一事实来源；遇到矛盾以本文件为准，改动须回到主 Agent。

## 0. 目标

把 Phase 1–8 的“mock 浏览器验收”提升为：浏览器 → 真实 FastAPI 路由/Service/ORM/ACL/
Pipeline/发布/图谱 → 隔离临时 SQLite（真实 alembic upgrade head）。不连真实业务库、
LDAP、远程 Connector、真实模型。不部署、不迁移真实库、不进入 Phase 9B。

## 1. 隔离环境（先证明，再启动）

- 每次运行创建任务会话目录（未入库，已 gitignore）：`<repo>\.phase9a\<session>\`
  子目录：`db\`（SQLite）、`logs\`、`shots\`、`records\`（LLM/graph 调用记录）、`pids\`。
- SQLite 绝对路径：`<repo>\.phase9a\<session>\db\phase9a.db`
- 数据库初始化：必须**实际执行 `python -m alembic upgrade head`**（cwd=`backend/`，
  子进程 env `DATABASE_URL=<sqlite 绝对路径>`），不使用 create_all 代替。
  迁移单 head 以仓库实际链为准（P44 `a9b8c7d6e5f4`），不硬编码猜测。
- **启动前证明**：任何启动/迁移动作前断言 DB 绝对路径的 resolve 前缀严格位于
  `<repo>\.phase9a\<session>\db` 下；否则非零退出并拒启动。绝不指向
  `backend/data/notes.db` 或任何真实库。**禁止修改仓库 `.env`**。
- 后端子进程 env 必须显式覆盖置空外部模型地址：`LLM_API_URL=`、`LLM_API_KEY=`、
  `EMBEDDING_API_URL=`、`RERANKER_API_URL=`、`PDF_VISION_ENABLED=false`，
  使任何“未配置替身时的回退”都不命中真实客户端。
- 前端 dev 通过进程环境变量 `VITE_DEV_PORT` / `VITE_API_PROXY_TARGET` 控制
  （vite `loadEnv` 会合并 process.env），**不新增/不修改 `.env.*` 文件**。

### 端口（可用同名 env 覆盖；本文件固定默认值）
| 项 | 默认 |
|---|---|
| 后端 uvicorn | `PHASE9A_BE_PORT=8810`（绑定 127.0.0.1） |
| 前端 vite | `VITE_DEV_PORT=3020`，代理 `VITE_API_PROXY_TARGET=http://127.0.0.1:8810` |
| CDP 调试端口 | `PHASE9A_CDP_PORT=9666` |
| LLM/graph 记录文件 | `PHASE9A_RECORD_FILE=<session>\records\calls.jsonl` |
| 故障注入开关文件 | `PHASE9A_FAULT_FLAG=<session>\records\fault.flag`（存在=使含标记的默认编译失败） |

只清理本任务启动的进程（PID 记录于 `<session>\pids\`）。不结束用户已有 node/python 服务。

## 2. 服务端模型替身（真实进程内，须有调用记录）

Server 启动脚本（Agent A 的 `backend/phase9a/server.py`）在 `import app.main` 前后配置：
- **LLM runner**：`executor.configure_external_runners(llm_runner=rec, graph_runner=graph_rec)`
  - `rec(messages, context="", timeout=120.0)`：
    - 每次调用把一行 JSON 写入 `PHASE9A_RECORD_FILE`（字段：`seq,ts,context,len(prompt 首条内容)`，绝不记录正文/凭据）。
    - 故障注入：若 `PHASE9A_FAULT_FLAG` 文件存在 且 任一 prompt 含 `PHASE9A_FAIL_MARKER` → 抛 `RuntimeError("phase9a injected failure")`（走 STAGE_EXCEPTION）。
    - 阻塞点：若 prompt 含 `PHASE9A_BLOCK_MARKER` → 每秒检查一次 flag 文件，直到 flag 文件**消失**再返回（上限 60s，用于 cancel 语义；仅集成测试用）。
    - `context == "wiki-synthesis"` 且 prompt 不含上述标记 → 返回 `{"summary":"Phase9A 摘要","content":"Phase9A 聚合正文。" + "x"*60}`。
    - `context == "wiki-ingest-page"` → 返回 `{"worthy": True, "ops": []}`（live 不使用 page_changed）。
    - `context == "api-reference-compile"` → 返回 `[]`（live api 源为 OpenAPI，不应被调；若被调说明链路异常，返回空数组安全降级）。
    - 其余 → `{"worthy": True, "ops": []}`。
  - `graph_rec(**kw)`：先记一行记录（含 `kind` 目标字段），再**委托真实生产图谱实现**
    `wiki_default._default_graph_runner(**kw)`（同步真实建图），把返回值透传。
- **Embedding**：按 conftest 同款确定性 fake（类级替换 `EmbeddingService.encode/encode_batch`，
  sha256 种子伪向量，维度取 settings.embedding_dimensions）。LLM context 记录在 server 进程内。
- 任何 runner 被调用都写记录文件；集成测试断言“default wiki 至少命中一次 wiki-synthesis”。

## 3. 冻结标识（DB 种子与全部断言共用）

### 3.1 身份（全部 `is_local=True`，bcrypt，密码 `Phase9a!2026`）
| username | groups | 角色 |
|---|---|---|
| `phase9a-admin` | `["__local_admin__"]` | admin（全部可见/可管理） |
| `phase9a-editor` | `["eng","editors"]` | wiki_editor（LDAP map=editors）；业务组 eng |
| `phase9a-reader` | `["eng"]` | reader；仅见 acl 含 `eng` 的 active workspace/wiki |

server env：`LDAP_GROUP_MAP_WIKI_EDITOR=editors`、`LDAP_GROUP_MAP_ADMIN=`（空）。
鉴权一律走 `POST /api/auth/login` → Bearer（生产 auth 代码，无旁路）。

### 3.2 工作区 / Notebook / Page / Evidence / Wiki
- `ws-eng`：`key="phase9a-eng"`，`name="Phase9A 工程知识库"`，`acl_scope={"groups":["eng"]}`
  - 绑定 Notebook `nb-eng`（`group_id="eng"`，名“工程手册”）
  - 绑定 Notebook `nb-hidden`（`group_id="eng2"`，名“隐藏来源”，仍绑定到 ws-eng，用于不可见来源）
- `ws-sales`：`key="phase9a-sales"`，`name="Phase9A 销售知识库"`，`acl_scope={"groups":["sales"]}`
  - 绑定 Notebook `nb-sales`（`group_id="sales"`）

| Page | notebook | 内容要点 | Evidence(active, source_doc_hash=page.content_hash, content_hash 64hex, locator `{"section":"all"}`) |
|---|---|---|---|
| `p-default` | nb-eng | 纯中文长文（≥400 字，**不含语义版本号/数字点**，介绍“Phase9A 编译使用说明”） | 不需要 |
| `p-api` | nb-eng | 内容 = OpenAPI 3 JSON fixture（见 §4.1，/v1、/v2 不同路径 Endpoint + 参数 + media + 错误码） | `ev-api` |
| `p-hidden` | nb-hidden | OpenAPI 3 JSON（单端点 `GET /v2/audit`） | `ev-hidden` |
| `p-fail` | nb-eng | 含标记 `PHASE9A_FAIL_MARKER` 的长文本 | 不需要 |
| `p-sales` | nb-sales | 纯中文长文（“Phase9A 销售报价流程”） | 不需要 |

| WikiPage | workspace | title | skill | sources | 状态 |
|---|---|---|---|---|---|
| `w-eng-default` | ws-eng | `Phase9A 系统使用说明` | default:1 locked | `["p-default"]` | dirty=True, acl=ws acl, status=draft |
| `w-eng-api` | ws-eng | `Phase9A 用户接口参考` | api_reference:1 locked | `["p-api","p-hidden"]` | 同上 |
| `w-sales-default` | ws-sales | `Phase9A 销售流程` | default:1 locked | `["p-sales"]` | 同上 |
| `w-fail` | ws-eng | `Phase9A 故障重试主题` | default:1 locked | `["p-fail"]` | 同上 |

所有 Page 设 `wiki_dirty=True`；所有 WikiPage 初态**无** current_revision_id。
以上均为 fixture 允许预建的“来源/壳”；**Wiki Revision/Section/Binding 绝不预填**，必须由真实 Pipeline 发布产生。

### 3.3 编译触发（正式入口）
- seed 后以 admin 身份调用 `POST /api/wiki/refresh-page-dirty` → 对每个 dirty Wiki 建
  `manual_rebuild` run（active v3）。worker（真实进程内 pump）自动领取执行。
- 期望：`w-eng-default`、`w-eng-api`、`w-sales-default` → succeeded（Revision+Section+Binding 落库、
  wiki.dirty=False、current_revision_id 回填、Manifest 落库）；`w-fail` → 首次 failed
  （fault flag 存在），旧 current_revision 不变（None）、wiki.dirty 保持 True。
- 然后删除 fault flag，`POST /api/wiki-compile/runs/{run_id}/retry` → 再次 succeeded。

## 4. 关键资料与断言要点

### 4.1 API OpenAPI fixture
- 文件（提交）：`backend/tests/fixtures/api_reference/phase9a_users_api.json`
- 至少含：`GET /v1/users`、`GET /v2/users`（查询参数 `limit`、路径参数 `id`、响应 media
  `application/json` schema、`401/403/404` 等 ≥400 响应作为业务错误码、`components/schemas`）。
- 经真实 v3 api_reference 编译发布后，wiki `w-eng-api` 的 published Revision Sections 须包含
  `api_endpoint|get|/v1/users|unversioned` 与 `api_endpoint|get|/v2/users|unversioned`
  （/v1、/v2 为**不同路径 Endpoint 展示**，各自成节；标题含对应路径），每条 endpoint
  Section `validation_status="pass"`，`structure_json` 含
  `display.endpoint.parameters/responses.media_types/error_codes` 白名单投影。
  口径：真实 executor 链路的 `db_adapter` 当前把 `ApiSourceDocument.version_scope` 恒置为
  `""` → 归一 `unversioned`，因此「同一 Endpoint 的 version_scope 隔离」在生产执行链上
  尚未被本链路验证、不作为本轮覆盖项；本轮不新增版本推断规则，版本相关能力留待 9B
  按原计划审定（本契约仅记录 /v1、/v2 路径维度的展示验证）。
- 图谱：run 的 Manifest `graph_targets` 含 `{"kind":"wiki","wiki_page_id":...}`；publish 后
  schedule_graph stage 以真实图谱实现同步建图；图谱 API（`/api/v4/graph/subgraph`）按对应身份
  可查到与 wiki 目标相关的节点/边（不能只停留在“已调度”）。

### 4.2 必须的 HTTP/权限断言（admin/editor/reader 三个真实 token）
1. 目录与详情：reader 见 `w-eng-default`/`w-eng-api`（acl eng）且为 published；editor/admin 可见；
   reader 对 `ws-sales`/`w-sales-default` 一律 404；workspace 列表不含 ws-sales；`/api/wiki` 列表不含其 wiki。
2. Evidence：reader 打开 `w-eng-api` 中来自 `p-hidden` 的 Section（如 GET /v2/audit）→ HTTP 200、
   `total=0`（不可见来源不计入 total、items 空）；admin 同 Section `total>=1`；reader `GET /api/evidence/{ev-hidden}` → 404，admin → 200。
3. 无 Binding 的 Section（如 default wiki 的 summary/facts 或历史上无 binding 的 api Section）→ 返回空 total=0，
   不回退整 Page 证据。
4. 历史 Revision：admin 以 `GET /api/wiki/{id}?revision_id=<旧 published/superseded>` 可看；
   reader/editor 非授权历史 → 404（普通读者仅 published+current）。诊断 `GET /api/wiki/{id}/diagnostics?revision_id=`
   对应请求的 revision（返回的 revision_id 与请求一致）。
5. 权限：
   - reader：diagnostics → 403；`/api/wiki-compile/runs*` → 403。
   - editor：diagnostics（可见 wiki）→ 200；`/api/wiki-compile/runs*` → 403（无管理员 Run 权限）。
   - admin：`/api/wiki-compile/runs?workspace_id=ws-eng` 列表只含 ws-eng 的 run，**不混入** ws-sales。
   - 跨 Wiki/Revision/Section ID 直接请求（属于另一 wiki 的 revision/section）→ 404。
6. D 失败/重试：`w-fail` 首次 run status=failed（阶段时间线真实）、再 retry → succeeded，
   Manifest 新 Revision 发布成功；浏览器面板能看到真实 Run/Stage。

### 4.3 图谱“完成”判定（不得以调度代替）
publish succeeded 后：查询 DB `V4GraphEntity/V4GraphRelation` 出现与目标 wiki/page 相关的行，
并经图谱 API 对该身份返回相关节点；graph 调用记录文件证明 `graph_rec` 收到该 wiki 目标。

## 5. Agent 分工与文件归属（同文件单一写入者）

### Agent A —— 后端隔离与链路
- `backend/phase9a/__init__.py`（空）
- `backend/phase9a/fixtures.py`（冻结标识常量 + OpenAPI 内容构造/读取，被 seed/test/server 复用）
- `backend/phase9a/bootstrap_db.py`（路径守卫 + alembic upgrade head）
- `backend/phase9a/seed.py`（幂等种子：users/workspaces/bindings/notebooks/pages/evidence/wikis/runtime flag 不需行）
- `backend/phase9a/server.py`（守卫 + 模型替身 + uvicorn）
- `backend/tests/fixtures/api_reference/phase9a_users_api.json`
- `backend/tests/test_phase9a_integration.py`（pytest：alembic head 临时库 + TestClient 真实路由 +
  executor 真实 v3 pipeline；覆盖 §4 全部后端断言 + cancel 语义 + call-record 断言）
- 门禁：仅跑该测试文件；`backend/.venv/Scripts/python.exe -m pytest tests/test_phase9a_integration.py -q`
- 最后产出：一次性启动脚本或 `phase9a/run-live.ps1`（主 Agent 集成时用，见 §7）

### Agent B —— 前端面板反例 + 浏览器联调脚本
- `frontend/src/components/wiki/AdminRunsPanel.vue`：最小修复（同 run getRun 在途去重/待刷新标记；
  不重写组件、不提高轮询间隔）
- `frontend/tests/wiki-section-evidence/`：新增慢详情持续反例（命名 `RA3`），必要时扩展 mock-server
  控制键与运行时间线
- `frontend/tests/live9a/accept.mjs` + `README.md`（真实后端浏览器验收，CDP 驱动，结构复用 wiki-section-evidence）
- 门禁：改动 AdminRunsPanel 后只跑 `wiki-section-evidence` 套件（含 RA3）；`npm.cmd run build` 一次。
  live9a 脚本不在 A 的后端就绪前实跑。

### 主 Agent —— 集成与共享
- `.gitignore`、`phase9a/CONTRACT.md`、`phase9a/run-live.ps1`（或等效 orchestrator）、
  最终 `phase9a/README.md`、集成验收（启动后端 → seed → 触发编译 → live9a 浏览器跑通 → 收尾清理）。
- live9a 需要先：backend server（8810）+ seed + 编译全部成功后，浏览器才访问。

## 6. 运行顺序与退出码
- 后端 pytest：失败即非零退出。
- live 联调脚本：任何启动失败/空结果/断言失败/驱动异常 → 非零退出；
  截图只有实际可见才声明已查看。
- 日志/截图/数据库/缓存/凭据一律只写入 `<repo>\.phase9a\`，不入库。

## 7. 集成 orchestrator（主 Agent）
`phase9a/run-live.ps1` 职责（仅供参考框架，最终以主 Agent 合成为准）：
1. 建 session 目录，启动前验证 DB 绝对路径位于 `<session>\db`。
2. `python phase9a/... bootstrap_db`（alembic upgrade head，cwd=backend）。
3. seed（backend venv python）。
4. 启动后端 server（记录 PID）；health check 就绪。
5. 触发 `refresh-page-dirty`；轮询 runs 至全部终态；断言 expected 状态（fault 逻辑开）。
6. 启动前端 vite（记录 PID）；启动 headless Chrome CDP。
7. 运行 live9a accept.mjs 断言真实后端内容/权限/面板。
8. 收尾只杀本任务 PID；输出摘要；退出码。
