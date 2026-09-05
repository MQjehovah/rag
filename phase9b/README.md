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

## 八、第三批授权：真实库副本迁移/回填/恢复演练（checkpoint：7374f65 →）

**结论先行：真实库保持零修改；副本 Alembic 迁移到 head 遇到 schema 超前阻断，如实记录为待决策阻断，未做任何绕过。**

### 8.1 前置核查
- HEAD 7374f65、Alembic 唯一 head `a9b8c7d6e5f4`。
- 真实库（`backend/data/notes.db`，普通文件、非 reparse）：160,489,472 B、mtime 2026-09-02 17:58:50、
  SHA-256 `be2b6d6c…0cb66f`、`journal_mode=wal`（目录无 -wal/-shm 残留）、`quick_check=ok`、
  `alembic_version=e6f7a8b9c0d1`(P38)、无 python 服务进程使用。
- schema-aware inventory（真实库与 baseline 快照各一次，`identical=True`）：notebooks=3（均 admin scope，
  无 extra groups）、pages=145、page_chunks=5403、wiki_pages/revisions/sections=0、evidence=5403、
  图谱 117/82/214/4570/76；缺 `wiki_workspaces/notebook_workspace_bindings/
  wiki_section_evidence_bindings` 表；`wiki_pages.workspace_id` 并不存在（本报告早先描述
  为「已物理存在」属误述，见下“报告修正”）；物理超前的仅是 `pages` 的 7 个 P40 canonical
  列（`note_schema_version` 等）已存在且与迁移目标等价。
- 回填候选：3 个 notebook（candidate=3，blocked=0，同 ACL 共享 scope=1 组含 3 notebook）。

### 8.2 快照与一致性
- 会话 `backend/.phase9b_migration/drill1/…`：`source-snapshot/baseline.db` 由 SQLite 只读 backup API
  生成（源只读，未改真实库），SHA-256 `fa8f4c39…121706`；`working/work.db` 由 baseline 复制且 SHA 一致。
- real vs baseline inventory 关键计数与版本完全一致（identical=True）。
- `restore-verify`：从 immutable baseline 恢复到 `restored/restored.db`，SHA 与 baseline 完全一致、
  quick_check ok、版本 e6f7a8b9c0d1、计数无差异，exit 0（证明恢复不依赖已迁移副本）。

### 8.3 副本迁移结果（阻断）
- preflight（working 副本）ok（quick_check/fk/revision=P38）。
- `upgrade head`（working 副本）失败，exit 1：首个升级步骤
  `alembic/versions/f2a3b4c5d6e7_p40_page_canonical_note_fields.py` 执行
  `ALTER TABLE pages ADD COLUMN note_schema_version` 报 `duplicate column name: note_schema_version`
  ——真实库物理上已含该列（schema 超前于 P38 revision）。失败后副本 `alembic_version` 仍为
  e6f7a8b9c0d1（该步未部分生效）；完整 Alembic 日志留于会话 logs（未入库）。
- 按授权不修改已发布 migration、不做手工 stamp/改 revision 绕过；working 副本标记为失败产物，
  不继续反复修补。因此回填 dry-run/apply、postflight、downgrade-drill 依赖迁移后 schema，**未执行**。

### 8.4 防误写与零修改证明
- 负测：对真实库路径执行 `upgrade` / `backfill-apply` → 工具在写保护层拒绝并 exit 1（不触库）。
- 演练结束后真实库复检与开始时逐项一致：SHA-256 `be2b6d6c…`、大小、mtime、revision=P38、quick_check、
  计数全同，目录仍无 -wal/-shm；无任何真实库写入目标路径出现过。
- 结论：**迁移到 head 属待决策阻断**（需 schema 归一/列去重或等价方案后另行授权），其余安全演练
  （只读盘点、一致性快照、备份恢复验证、防误写）均通过。

### 8.5 工具与门禁
- 工具（可重复、只允许操作副本，写目标带 `--allowed-dir` + 真实库拒绝）：
  `backend/phase9b_migration/`（guard/inventory/snapshot/migrate/backfill/postflight/recovery/main/README）。
- 单测：`backend/tests/test_phase9b_migration_tools.py` → 33 passed，exit 0（合成 sqlite）。
- 真实库复制演练仅用副本完成；未跑后端全量/前端 build/9A 浏览器套件。

## 九、副本迁移兼容修复 + 完整演练（checkpoint：f690df3 → 本批，阻断解除）

### 9.1 一次审查：baseline 实际 schema vs P40–P44（差异表）
| 迁移 | 对象 | baseline 物理状态 | 处理 |
|---|---|---|---|
| P40 | pages 7 列（canonical 契约字段） | **已存在且等价**（VARCHAR(64/127)/TEXT，nullable） | 兼容跳过；缺失才创建（真实库曾在此 duplicate 失败） |
| P41 | wiki_workspaces / notebook_workspace_bindings（表+CHECK+FK+索引） | 不存在 | 原样创建 |
| P41 | wiki_pages.workspace_id + FK + ix 索引 | 不存在 | 创建；已存在则兼容补齐 FK/索引 |
| P42 | knowledge_compile_* 三表（+CHECK/索引） | 不存在 | 原样创建 |
| P43 | wiki_pages 6 个 skill 列 + 2 CHECK | 不存在 | 创建；已存在兼容补齐（CHECK 先校验数据） |
| P44 | wiki_sections 6 列 + 2 CHECK + 部分唯一索引 | 不存在 | 创建；已存在兼容补齐/跳过等价索引 |
| P44 | wiki_section_evidence_bindings（表） | 不存在 | 原样创建 |

只修改实际需要兼容路径的 P40/P41/P43/P44（辅助函数独立于应用模型/schema guard：
`backend/migration_compat.py`；PG 非 sqlite 分支保持原语义，未实测如实声明）。
规则：缺失→创建；已存在且等价→保留；已存在不等价→受控失败；列存在不代表迁移完成
（FK/CHECK/索引仍补齐）；新增约束前不自动清洗违规数据（违反即失败、数据保持）；
不改业务值、不触发 worker/编译/外部调用；不改 revision/down_revision、不分叉、不 stamp。

> 报告修正（本轮，以 baseline 实际 schema 输出为准）：此前（§8.1 与 f690df3 摘要）称
> `wiki_pages.workspace_id` 已物理存在属误述——对 baseline 逐列 `PRAGMA table_info` 复核，
> `wiki_pages` 不含 `workspace_id`；真实库物理超前的仅是 P40 的 7 个 `pages` canonical 列。
> 原因未再猜测；本节差异表（§9.1）与其后所有 drill 均以该实际 schema 输出为准。

### 9.2 结果（真实数据副本演练，2026-09-05）
- 真实库不变：SHA-256 `be2b6d6c…0cb66f`、大小/时间/alembic P38/quick/计数前后一致。
- baseline 副本（immutable）→ 新 working copy：迁移 **upgrade head 成功（exit 0）**，
  `alembic_version=a9b8c7d6e5f4`，preflight quick/fk 通过，`init_db` schema guard OK。
- 数据摘要：baseline vs migrated `EQUAL=True`（pages 145 行 canonical 字段逐值、
  行数与 evidence/图谱等计数完全一致；非敏感字段摘要，无正文/凭证）。
- Workspace 回填（admin ACL 3 notebooks，同 ACL 各自独立默认 workspace）：
  dry-run candidate=3（plan_hash `02e7bf…`）；apply #1 创建 binding=3；apply #2 幂等创建=0；
  blocked/unknown=0。
- postflight：`passed=true`；active binding 唯一、published wiki 无（真实 wiki=0，如实）、
  数量未减少；**合成反例**在事务内建 notebook/ws/binding/wiki/revision/section/evidence/
  evidence-binding 后 rollback（counts_after==before，rolled_back=true），与真实数据分开。
- downgrade-drill（head → P38 → head）：exit 0；预存在表（notebooks/pages/evidence…）行数
  零减少（defects 空）；P41/P42/P44 迁移新增表（含回填的 workspaces/bindings 3 行）随降级消失
  = 预期（roundtrip 仅比较预存在表，delta 空）；quick/fk 全程通过；re-upgrade head exit 0。
  原始物理超前状态的恢复以 immutable baseline restore 为准，二者分别报告、不混称无损回滚。
- restore-verify：从 baseline 恢复副本 SHA 与 baseline 完全一致（`fa8f4c39…121706`）、quick/
  版本/计数一致，exit 0（不依赖已迁移 working）。

### 9.3 门禁（本批实际执行）
- `backend/tests/test_phase9b_migrate_compat.py`：5 passed，exit 0
  （干净 P38→head、部分新列提前等价→补齐成功、类型/nullable 不等价受控失败、列存在但
  FK/索引补齐、违反新 CHECK 的数据→失败且不被清理）。
- `backend/tests/test_phase9b_migration_tools.py`：33 passed，exit 0（guard/main/recovery 回归）。
- 未跑后端全量 pytest、前端 build/浏览器套件、真实库任何写入。

### 9.4 剩余阻断/未验证
- PostgreSQL 迁移路径未实测（非 sqlite 分支保留原语义）。
- 真实库 Wiki=0：workspace 一致性/图谱回填无历史样本；合成反例已单独标记。
- 下一批如需把回填写入“标准 head schema”后的长期数据归并/删除矩阵，另行授权。
