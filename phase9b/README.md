# Phase 9B 第一批 — 增量一致性与故障恢复（checkpoint：573f6f2 → 本批）

验证更新/删除/版本/故障后：Wiki/Section/Evidence/图谱一致、旧发布不被失败覆盖、
重试不重复发布、进程重启后任务恢复。隔离临时 SQLite + 真实 Alembic + Fake 模型；
不迁移真实库、不跑最终全量、不扩展前端。

- 冻结契约与分工/文件所有权：见 [CONTRACT.md](CONTRACT.md)（延续 phase9a/CONTRACT 标识与隔离要求）。
- 运行说明与门禁命令（均在仓库根/backend 下，用 `backend\.venv\Scripts\python.exe`）：

| 用途 | 命令 |
|---|---|
| 增量一致性（7 用例） | `pytest tests/test_phase9b_incremental.py -q`（cwd=backend） |
| 图谱重试 + 跨进程崩溃恢复 + lease 接线（6 用例） | `pytest tests/test_phase9b_recovery.py -q` |
| 被改 executor/worker 模块定向回归 | `pytest tests/test_wiki_pipeline_core.py -q` |
| 9A 包装脚本负向自测（3 场景） | `powershell -ExecutionPolicy Bypass -File phase9b\run-live-negative.ps1` |
| 包装脚本正向全路径 | `powershell -ExecutionPolicy Bypass -File phase9a\run-live.ps1` |

## 一、版本来源判定（本轮结论：待决策阻断，未接线）

勘察确认**项目没有“明确且已有契约支持的 API version_scope 来源”**：
- `db_adapter.py:247` 恒置 `version_scope=""` → 全链 `unversioned`；parser 不读 `info.version`；
- Page/Evidence/来源导入载荷无 API 版本列；`ApiVersionNote` 仅测试实例化（无生产者）；
- default 分支的 product-version 归一（`v1→1.0`）与 api scope token 约定（`v1/v2`）不一致；
- 既有 fixture `openapi_users_v1.json/v2.yaml` 不含 `info.version`；`info.version` 是最接近的
  结构化候选，但**无项目契约规定其应映射 version_scope**。

→ 本轮**不接线、不新增推断规则、不新增 DB 字段**；沿用「/v1、/v2 不同路径 Endpoint」展示口径，
真实执行链恒 `|unversioned`。最小可选方案与缺失字段记录于
`backend/tests/test_phase9b_incremental.py` 文件头，留待后续授权审定。

## 二、增量一致性（test_phase9b_incremental.py，7 passed）

| 场景 | 结果要点 |
|---|---|
| S1 新增 `/v3/users` | 新 current 含 v3 节；/v1、/v2、/v2/audit 的 content/structure/Binding 与上一 revision 逐字段相等；旧 revision 保留；图谱 provenance 指向新 current |
| S2 改 `/v1/users` 参数 | section_key 不变、内容变化、无关 section 语义等价；历史/当前各取所需 |
| S3 删唯一来源 | archived、membership=[], dirty=False，图谱失效关系清除 |
| S4 删非唯一来源 | draft/dirty → 再 manual_rebuild → 新 revision 无 /v2/audit、旧 revision 保留 |
| S5a protected/manual（default） | 人工编辑 summary 不被重编译覆盖 |
| S5b api lock 复制 | 按实际行为断言 section_key→NULL 且 protected 保留 |
| S6 历史 + Evidence 一致性 | 历史 revision 完整；发布边界 EVIDENCE_STALE → run failed、零新 Revision、dirty 保持 |

## 三、故障与恢复（test_phase9b_recovery.py，6 passed）

1. **图谱阶段一次故障→正式 retry**：一次性故障 wrapper 委托真实 `_default_graph_runner`
   （flag 存在且目标匹配时抛一次）。run failed `GRAPH_BUILD_FAILED`，**已发布 Revision 保持
   有效**（current 不变、dirty=False、Revision 数=1）；清 flag 后正式 retry → attempt2 succeeded、
   Revision 仍=1（不重复发布）、Manifest 由 Artifact 重放、图谱 provenance 无重复/无陈旧。
2. **跨进程崩溃恢复**（真实进程边界，非 recovery helper 直调）：进程 A 真实 lifespan 启动
   worker，run 在 synthesize 阻塞；先证明 heartbeat 正常续租，再 terminate；短 lease（6s，
   经 settings env 注入）老化后启动进程 B 走真实 startup recovery+worker → attempt=2 succeeded；
   旧 running Stage 收尾 `failed(worker_lost)`/queued→`skipped`、无 ghost running、Artifact/
   Revision/dirty/图谱最终正确。
3. **lease 配置接线**（默认=现值，生产行为不变）：`config.py` 增
   `wiki_pipeline_lease_seconds/heartbeat_timeout_seconds/poll_interval_seconds/
   lease_renew_interval_seconds`（300/300/2.0/30.0）；`executor.claim_by_id` 与
   `worker.heartbeat` 同源读 lease 字段；pump 间隔与 renew 默认读对应字段。定向回归
   `test_wiki_pipeline_core.py` 114 passed。

## 四、9A 包装脚本可靠性（phase9a/run-live.ps1 修复 + 负向自测）

根因：step7 `& $Node`+`$LASTEXITCODE` 陈旧/输出未落 session/失败路径无统一清理（泄漏 server/
vite）/node 无超时/无产物门禁。修复：try/finally 统一清理（PID 对象级归属校验，清理错误不覆盖
原失败码）；node 改 .NET Process（真实 ExitCode）+ 超时 + stdout/stderr 落
`logs\accept.out/.err.log` 并回显；**产物门禁**（`SUMMARY … exitCode=0` 且 shots≥6 png 才算过）；
`PHASE9A_ACCEPT_SCRIPT/ACCEPT_TIMEOUT` 注入。负向自测 `phase9b/run-live-negative.ps1`：
A（accept exit=7）rc=7、B（exit 0 无 SUMMARY/截图）rc=8 + `ACCEPT_GATE`、C（accept 超时）rc=7 +
`ACCEPT_TIMEOUT`；三场景 8810/3020/9666 均无监听、无本任务残留 → 23/23 PASS。正向 `run-live.ps1`
15/15 + gate `summaryOk=True shots=6`，exit 0。

## 五、门禁（本批实际执行，2026-09-04，均主 Agent 复核复跑）

| 门禁 | 结果 |
|---|---|
| `test_phase9b_incremental.py` | 7 passed，exit 0 |
| `test_phase9b_recovery.py` | 6 passed，exit 0 |
| `test_wiki_pipeline_core.py`（定向回归） | 114 passed，exit 0 |
| `phase9b/run-live-negative.ps1`（负向） | 23/23 PASS，exit 0 |
| `phase9a/run-live.ps1`（正向包装） | 15/15 + artifact gate，exit 0 |

未跑：后端全量 pytest、前端 build / 三套 mock 浏览器套件（无前端源码变化）、真实库/PostgreSQL 副本迁移。

## 六、文件范围（本批提交）

- 产品（最小接线，默认不变）：`backend/app/config.py`、`backend/app/core/wiki_pipeline/executor.py`、
  `backend/app/core/wiki_pipeline/worker.py`
- 测试：`backend/tests/test_phase9b_incremental.py`、`backend/tests/test_phase9b_recovery.py`
- 脚本/文档：`phase9a/run-live.ps1`、`phase9b/CONTRACT.md`、`phase9b/run-live-negative.ps1`、本文件
