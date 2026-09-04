# Phase 9B — 增量一致性/故障恢复 + 第二批授权闭合（版本接线、配置校验）

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

## 一、版本来源判定（先为待决策阻断；后经授权实现为项目扩展字段）

勘察结论（作为历史记录）：项目原本**没有“明确且已有契约支持的 API version_scope 来源”**——
`db_adapter.py` 恒置 `version_scope=""`、parser 不读 `info.version`、Page/Evidence/导入载荷无
API 版本列；`info.version` 无契约授权，URL/文件名/标题不得推断版本。

随后授权落地（见文末“第二批授权闭合”）：以**项目专用 OpenAPI 顶层扩展**
`x-wiki-version-scope` 作为显式来源；缺失维持 unversioned，非法声明来源级阻断，仍不做
info.version/路径/文件名推断，Markdown 仍 unversioned。改声明 = 来源内容变化，参与 hash/重验。

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

## 五、门禁（第一批实际执行，2026-09-04，均主 Agent 复核复跑）

| 门禁 | 结果 |
|---|---|
| `test_phase9b_incremental.py` | 7 passed，exit 0 |
| `test_phase9b_recovery.py` | 6 passed，exit 0 |
| `test_wiki_pipeline_core.py`（定向回归） | 114 passed，exit 0 |
| `phase9b/run-live-negative.ps1`（负向） | 23/23 PASS，exit 0 |
| `phase9a/run-live.ps1`（正向包装） | 15/15 + artifact gate，exit 0 |

未跑：后端全量 pytest、前端 build / 三套 mock 浏览器套件（无前端源码变化）、真实库/PostgreSQL 副本迁移。

## 六、文件范围（前两批提交）

- 产品（最小接线，默认不变）：`backend/app/config.py`、`backend/app/core/wiki_pipeline/executor.py`、
  `backend/app/core/wiki_pipeline/worker.py`
- 测试：`backend/tests/test_phase9b_incremental.py`、`backend/tests/test_phase9b_recovery.py`
- 脚本/文档：`phase9a/run-live.ps1`、`phase9b/CONTRACT.md`、`phase9b/run-live-negative.ps1`、本文件

## 七、第二批授权闭合（checkpoint：35eb3e3 → 本批）

### 7.1 OpenAPI 显式版本范围接线（Agent A）
项目专用 OpenAPI 顶层扩展字段 `x-wiki-version-scope`（示例见
[CONTRACT-version-config.md](CONTRACT-version-config.md) §5）：
- 作用：该份 OpenAPI 文档内**全部 Endpoint**；在现有 `parse_openapi` 顶层解析路径**一次读取**
  （`db_adapter` 不再独立解析全文，`version_scope=""` 仍由 adapter 传入）。
- 缺省：无声明 → unversioned；内部显式 version_scope 参数为空 → 用声明；二者都明确 → 规范化后
  相同接受、不同受控失败（`VERSION_SCOPE_CONFLICT`）；非法声明（null/非字符串/空白/非法字符）→
  来源级受控失败（`VERSION_SCOPE_DECLARATION_INVALID`，publishable=False，坏来源不被静默丢弃）。
- 规范化沿用 `identity.normalize_version_scope`（`" V1 "`→`v1`）；不读 URL/文件名/标题/openapi/
  info.version；Markdown 仍 unversioned。
- 链路：声明 → 文档全部 Endpoint 的 section_key/Evidence Binding 用同一生效 scope →
  `merge_documents` 按 endpoint_id 隔离（同 method/path 不同声明各自成节、各自用本来源证据）。
- 声明变更 = 来源内容变化：参与 Page/source hash、编译输入与发布前重验（只改声明不同步
  Evidence hash → `EVIDENCE_STALE` 零发布）。
- 新增测试 `backend/tests/test_phase9b_version_scope.py`（35 passed）：同 `/shared` method/path
  双声明 v1/v2 隔离、改 v1 不动 v2、无声明 unversioned、非法声明（JSON+YAML）来源级阻断、
  内部参数×声明相同/冲突、规范化大小写、声明变更 hash 未同步零发布。

### 7.2 Pipeline 时间配置校验（Agent B）
- 默认值保持：lease=300、heartbeat_timeout=300、poll=2.0、renew=30.0。
- 校验：lease/heartbeat_timeout 正整数；poll/renew 有限正数（`math.isfinite` 拒绝 NaN/±Inf）；
  模型级 `renew <= min(lease, heartbeat_timeout) / 3`；env 字符串由 pydantic 正常解析；
  非法配置在 `Settings` 构造时抛 `ValidationError`（启动配置阶段失败，worker 不启动）。
- executor `claim_by_id` 与 worker `heartbeat` 继续读**同一** `wiki_pipeline_lease_seconds`
  （同源函数对象）；上一轮 `try/except Exception → 回退默认` 已删除——非法值不被吞，读处抛错；
  不再维护模块级时长常量，全部读已验证 settings。
- 新增测试 `backend/tests/test_phase9b_config.py`（36 passed）：默认值/合法短配置通过；
  lease≤0、poll/renew=0、NaN、±Inf、非法字符串、renew 超预算等反例均抛 `ValidationError`。

### 7.3 门禁（主 Agent 复核复跑，一次组合去重）
```
cd backend
.\.venv\Scripts\python.exe -m pytest tests/test_phase9b_version_scope.py tests/test_phase9b_config.py ^
  tests/test_api_reference_parser.py tests/test_api_reference_identity.py tests/test_api_reference_db_adapter.py ^
  tests/test_api_reference_merge.py tests/test_api_reference_runtime.py tests/test_api_reference_schemas.py ^
  tests/test_phase9b_recovery.py::test_cross_process_crash_recovery tests/test_wiki_pipeline_core.py -q
```
结果：**363 passed，退出码 0**。未跑后端全量/前端 build/9A 浏览器联调。

### 7.4 本批文件范围
- 源码：`backend/app/config.py`、`backend/app/core/wiki_pipeline/executor.py`、
  `backend/app/core/wiki_pipeline/worker.py`、`backend/app/core/wiki_skills/api_reference/parser.py`、
  `backend/app/core/wiki_skills/api_reference/compiler.py`
- 测试：`backend/tests/test_phase9b_version_scope.py`、`backend/tests/test_phase9b_config.py`
- 文档：`phase9b/CONTRACT-version-config.md`、本文件
