# Phase 9B 第一批：增量一致性与故障恢复 —— 冻结契约

> 主 Agent 冻结（基线 573f6f2）。Agent A（增量一致性 + 版本来源判定）与
> Agent B（图谱失败重试 + 跨进程恢复 + 包装脚本可靠性）严格按本契约实现，
> 同文件单一写入者；共享文件不得两 Agent 同时编辑。日志/截图/DB/缓存/凭据一律
> 只写入 `.phase9a\` 或 pytest 私有 tmp（均 gitignore），不入库。

## 0. 目标与范围（不进入 9B 第二批/9C，不跑后端全量，不跑前端 build/三套 mock）

验证更新、删除、故障后：Wiki/Section/Evidence/图谱一致、旧发布不被失败覆盖、
重试不重复发布、进程重启后任务恢复。不做真实库副本迁移、不跑最终全量、不扩展前端。

## 1. 隔离要求（延续 9A，全部强制）

- 实际 `alembic upgrade head`（单 head a9b8c7d6e5f4）初始化全新临时 SQLite；
- DB 绝对路径必须位于允许根下（pytest 用自身 tmp/conftest 重定向目录；跨进程用
  `<repo>\.phase9a\<session9b>\db` 并在启动前断言，server.py 守卫已具备）；
- Fake 模型/外部源有调用记录；不连真实 notes.db/LDAP/远程 Connector/真实模型；
- ACL、发布事务、Evidence、图谱、Run 状态机走真实实现；不改正式 `.env`；
- 只终止本任务记录进程；不清理用户已有 tmp/reports/data/.pytest_*；
- 不修改既有迁移；不 amend、不 push。

## 2. 版本来源判定（Agent A 结论性产出，不做接线）

勘察结论（已完成，冻结）：**项目不存在“明确且已有契约支持的 API 版本(version_scope)
来源”**：
- `db_adapter.py:247` 恒置 `version_scope=""`→unversioned；
- `parser.py` 不读 `info.version`；Page/Evidence/导入载荷无 API 版本列；`ApiVersionNote`
  仅测试实例化（无生产者）；`versioning.py` 的 product-version 语义(归一 1.0)与 api
  scope token(v1) 不一致；phase9a/CONTRACT §4.1 恰好书面写明“版本能力留待 9B 审定”。
- `info.version` 是最接近的结构化候选，但**无项目契约规定**其应映射 version_scope；
  现有 fixture openapi_users_v1.json/v2.yaml 不含 info.version。

→ 本轮**不接线、不新增推断规则、不新增 DB 字段**；沿用 `/v1`、`/v2`「不同路径
Endpoint」展示口径，真实执行链恒 `|unversioned`。Agent A 产出一份“缺失字段/现有语义/
最小可选方案/待决策阻断”记录（写入其测试文件头或报告，不修改 CONTRACT 本身）。

## 3. Agent A —— 增量一致性（backend/tests/test_phase9b_incremental.py 新建）

复用 phase9a Env 风格（alembic head 临时库 + active v3 + builtin skills + 真实
TestClient/executor）。可 import phase9a.fixtures（只读常量/Recorder），**不修改**
phase9a/*.py、test_phase9a_integration.py、app 源码。测试内自带最小 fixture 助手。
发布一律经真实 Pipeline（`refresh-page-dirty`/`create_page_deleted_run` 建 run +
`executor.execute_run`），**不预填 Revision/Section/Binding**。

覆盖与断言（断言当前 Revision/Section/Binding/membership/dirty/Manifest/真实图谱，
不能只看 HTTP 200 或 run succeeded）：
1. **新增 Endpoint（api）**：w-eng-api 来源 spec 增加 `/v3/users`（同步 Page.content_hash
   与 EvidenceItem.source_doc_hash/content_hash），触发 manual_rebuild →
   新 current_revision 出现 `api_endpoint|get|/v3/users|unversioned`，原 `/v1`、`/v2`
   Endpoint section 的 structure/display/绑定不被串改；旧 revision 原样保留；
   Manifest/wiki_page_ids/dirty 正确；真实图谱重建到新 revision（无旧 provenance 残留）。
2. **修改参数（api）**：改 `/v1/users` 参数 → section_key 不变、该 section content/structure
   变化、无关 section（如 /v2/users 或 overview）语义不变（structure 内容等价断言）。
3. **删除唯一来源**：default wiki（单来源）→ `create_page_deleted_run` 删除 →
   status=archived、source_page_ids=[]、dirty=False、旧 current_revision 仍可查但 wiki
   不再 published；图谱清除该 wiki 的失效当前关系。
4. **删除非唯一来源**：api wiki 删 p-hidden → status=draft、dirty=True、membership 剩
   p-api、旧 current_revision 不变；随后 manual_rebuild → 新 revision **不再含**
   `/v2/audit` section，旧 revision 仍保留；图谱当前状态无 audit 关系；不影响 ws-sales。
5. **protected/manual 内容**：default wiki 人工编辑（manual/protected）某 section 后再
   重编译 → 该 section 内容不被合成覆盖，其它 facts 更新；api protected（lock）复制语义
   至少断言一次（新 revision 保留 protected section，不静默覆盖）。
6. **当前 vs 历史 + Evidence 一致性**：两次发布后历史 revision/sections 完整保留；
   修改 api 来源但不同步 evidence hash → run failed（EVIDENCE_STALE/PAGE_STALE）、
   零新 Revision、dirty 保持（不预填、不造假）。

图谱断言用真实 graph（graph runner = 记录并委托 `_default_graph_runner`），查
V4GraphEntity/V4GraphRelation provenance 指向**当前** revision；删除/归档后无失效当前关系。

门禁：`python -m pytest tests/test_phase9b_incremental.py -q`。报告“已有覆盖→本轮缺口”表、
场景×真实路径×DB 断言结果、数量与退出码。

## 4. Agent B —— 故障与恢复 + 包装脚本（改动见下，均为单一写入者）

### 4.1 图谱阶段一次故障 → 正式 retry（在真实建图边界注入）
- 不在测试内把 graph runner 永久替换为成功：用一次性子类 wrapper，**委托真实
  `_default_graph_runner`**，仅对指定 wiki 目标 + flag 存在时抛一次；清 flag 后继续委托真实。
- 断言：run failed（GRAPH_BUILD_FAILED）、**已发布 Revision 保持有效**（current_revision_id
  不变、wiki.dirty=False、revision_count 不增）；经正式 `POST .../retry` → succeeded；
  重放 schedule_graph（Manifest 读 Artifact）重建图谱；**Revision 数量不增加、不重复发布、
  图谱无重复关系**。

### 4.2 跨进程崩溃恢复（必须进程边界）
- 新增产品最小改动（默认值不变=现值，纯配置接线，非语义改动）：
  `backend/app/config.py` 增字段（默认=现值）：
  `wiki_pipeline_lease_seconds=300`、`wiki_pipeline_heartbeat_timeout_seconds=300`、
  `wiki_pipeline_poll_interval_seconds=2.0`、`wiki_pipeline_lease_renew_interval_seconds=30.0`；
  `executor.py` claim 写入的到期长度与 `worker.py` heartbeat 推进长度**同源读 lease 字段**
  （不得不同步，否则短 lease 被 heartbeat 顶回 300s）；pump 间隔与 LeaseRenewer 默认间隔读对应字段。
  全部默认=现值 → 生产行为不变。
- 跨进程测试（新文件 `backend/tests/test_phase9b_recovery.py`）：
  独立后端进程必须由**真实 lifespan startup 启动 worker**（server.py，无提前 start_worker）；
  允许测试专用短 lease/renew（经上述 settings/env 注入，同步合理配置），**不等固定 300s**；
  先证明正常 heartbeat 能续租（不 stale），再 kill 进程验证真正失联恢复。
  流程：进程A启动→受控阻塞点（复用 Recorder block marker+flag；默认进程在 LLM/Stage 运行中
  阻塞）→轮询 DB 确认 run running 且 Stage 已开始、attempt=1→terminate 进程A→移除 flag、
  老化窗口后（短 lease）→进程B启动走真实 startup recovery+worker→run 被 requeue 消费到
  succeeded（attempt=2）。断言：旧 running Stage 收尾为 failed(worker_lost)/skipped、无 ghost
  running、attempt/max_attempts 正确、Artifact/Revision/dirty/图谱最终正确。
  **不得直接调用 recovery helper 代替本项跨进程验收。**
- fencing/幂等：复用既有确定性测试（test_wiki_pipeline_core.py 的
  claim/lease/requeue/heartbeat/lost-lease/supersede 系列已覆盖），只补跨进程所需断言。
- 门禁：跑新 recovery 文件 + 定向回归被修改模块：
  `python -m pytest tests/test_phase9b_recovery.py tests/test_wiki_pipeline_core.py -q`
  （core 是被改 executor/worker 模块的直接回归）。

### 4.3 9A 包装脚本可靠性（phase9a/run-live.ps1 修改 + 负向自测）
最小修复（不动产品源码）：
- 整脚本 `try/finally`（或 trap）：成功/异常/超时都必须清理本任务 PID（仅本次启动的
  vite/server 树；PID 归属校验，不用陈旧 pid 文件误杀）；清理错误不得覆盖原失败码；
- step7 的 node 改 `Start-Process -Wait -PassThru`，stdout/err 重定向到 session
  `logs\accept.out.log/.err.log` 并回显尾部；不再依赖 `& node`+`$LASTEXITCODE` 陈旧值；
- **产物门禁（防假绿）**：node 返回 0 之外还必须 `logs\accept.out.log` 含
  `SUMMARY ... exitCode=0` 且 session `shots\` 存在 6 张 png；否则判失败；
- node 调用加超时（超时则杀 accept 树并失败）；
- 失败路径先回显 server/vite/accept 日志尾部再退出；
- run-live.ps1 支持 env `PHASE9A_ACCEPT_SCRIPT`（覆盖 step7 脚本，默认
  `frontend/tests/live9a/accept.mjs`）以便注入失败/空结果自测。
- 负向自测（至少一次实跑，新文件 `phase9b/run-live-negative.ps1` 或等价文档脚本）：
  (a) accept 子进程非零退出 → wrapper 非零、无 PHASE9A_LIVE_OK、无本任务进程残留
      （8810/3020/9666 无监听）；
  (b) accept exit 0 但无 SUMMARY/无截图 → wrapper 靠产物门禁判失败；
  (c) 中途失败（如编译触发失败）→ wrapper 非零且清理后端/vite。
  记录“根因→修复→自测结果”，不把重跑成功归因为环境。

## 5. 冻结标识（沿用 9A CONTRACT §3；新测试仅在其会话内使用同名/派生标识）
- ws-eng/ws-sales；nb-eng/nb-hidden/nb-sales；p-default/p-api/p-hidden/p-fail/p-sales；
  w-eng-default/w-eng-api/w-sales-default/w-fail；ev-api/ev-hidden；
  phase9a-admin/editor/reader（密码 Phase9a!2026）。
- 端口（避免与既有占用冲突）：Agent B 跨进程 recovery 用 `PHASE9A_BE_PORT=8821`
  （或独立 env），run-live 仍 8810/3020/9666。负向自测用独立 session。

## 6. 文件所有权（单一写入者）
| 文件 | 写入者 |
|---|---|
| `phase9b/CONTRACT.md`、`phase9b/README.md` | 主 Agent |
| `backend/tests/test_phase9b_incremental.py`（新建） | Agent A |
| `backend/app/config.py`、`executor.py`、`worker.py`（lease 接线，默认不变） | Agent B |
| `backend/tests/test_phase9b_recovery.py`（新建） | Agent B |
| `phase9a/run-live.ps1`、`phase9b/run-live-negative.ps1`（新建，可选等价） | Agent B |
| `phase9a/fixtures.py`、`test_phase9a_integration.py`、`.env*`、迁移 | 任何 Agent 都不得改 |

## 7. 验收与交付
- Agent A/B 各自门禁绿后再交主 Agent 集成复核（复跑两个新文件 + 各自定向回归一次）。
- 交付：可重复测试、运行说明（phase9b/README）、场景 DB 结果/数量/退出码、版本来源
  结论（待决策阻断记录）、崩溃恢复时间线与“图谱重试不重复发布”证据、包装脚本负向自测结果。
- 完成后主 Agent 仅提交上述明确文件 + phase9b 文档，本地 checkpoint（不 amend/push）。
