# Phase 9C 主交付说明（本地代码交付）

> 本文件是最终本地交付的入口文档；不复制阶段报告，只链接并汇总。
> 阶段契约/演练记录：`phase9a/CONTRACT.md`、`phase9a/README.md`、
> `phase9b/CONTRACT.md`、`phase9b/README.md`、`phase9b/CONTRACT-version-config.md`。
> 计划来源：`<Obsidian Vault>/知识库搭建/智能Wiki知识编译管道详细执行计划-Claude版.md`
> （2026-09-01，Claude 版，逐阶段执行）；仓库阶段契约与上述 Obsidian 计划一致，本文件以此为准。

## 1. 当前功能与主要调用链

统一模型：外部源 → `RawSourceItem` → Converter Registry → `CanonicalNote` → Page →
确定性路由 `WikiWorkspace`（每 Notebook 独立默认 Workspace，同 ACL 不自动合并）→
Topic Router → Auto Skill Router（default / api_reference）→ Knowledge IR + Section
Blueprint → Section 生成 + Evidence 覆盖验证 → 原子发布 `WikiRevision/Section/Binding` →
真实图谱 + 编译产物（CompileRun/Stage/Artifact/Manifest）维护。

主要调用链：
- 单目标 v3：`resolve_context → topic_route → skill_route → synthesize_by_skill →
  validate_by_skill → publish_by_skill → finalize_compile_outcome → schedule_graph`。
- default 分支复用 v1/v2 合成（v3/v2 等价性有专项测试）；api_reference 分支走
  `db_adapter → compile_api_reference → _persist_api_revision`（原子发布 + Evidence 重验）。
- OpenAPI 显式版本：文档顶层 `x-wiki-version-scope` → 该文档全部 Endpoint 的
  section_key / Evidence Binding 使用同一 scope（缺省 unversioned；非法声明来源级阻断）。

## 2. 本地分支、验收代码 SHA、迁移 head

- 分支：`feature/dingtalk-to-markdown`（本地，无 push）。
- 迁移唯一 head：`a9b8c7d6e5f4`（P44；仓库链单 head，经 `alembic` 实跑与测试断言）。
- “被测试代码 SHA”：Phase 9C 全量 pytest 运行对象对应的提交见文末（先于文档提交的
  源码/测试 checkpoint）。文档提交 SHA 见文末，二者分别列出避免混淆。

## 3. 环境 / 依赖 / 配置 / 启停

- 后端：`backend\.venv`（Python，见 requirements.txt）；启动 `uvicorn app.main:app
  --host 127.0.0.1 --port 8000`（cwd=backend）；测试 `python -m pytest`（pytest.ini 已
  `-p no:cacheprovider`）。
- 前端：`frontend`（Vite 5/vue-tsc）；`npm run dev` 端口 3000（`vite.config.ts`），代理
  `/api → http://127.0.0.1:8000`；`npm run build` 内含 vue-tsc 类型检查。
- 配置：`backend/.env`（由 `.env.example` 复制，不要提交）。外部模型 URL 为空时各能力
  安全降级；测试会话由 `backend/tests/conftest.py` autouse 隔离（确定性 Fake embedding、
  清空 LLM/reranker/vision URL）。
- Docker Compose：PostgreSQL(pgvector)/backend/frontend（未在本批启动）。
- 隔离联调启停见 `phase9a/run-live.ps1` 与 `frontend/tests/live9a/README.md`
  （只清理本任务 PID，端口 8810/3020/9666）。

## 4. x-wiki-version-scope 示例与无声明行为

```yaml
openapi: 3.1.0
x-wiki-version-scope: v2   # 项目专用扩展：作用于本份文档全部 Endpoint
info: { title: "...", version: "..." }
paths: { ... }
```
- 缺省：无声明且无内部显式 version_scope → `unversioned`（系统缺省，不宣称资料明示版本）。
- 非法声明（null/非字符串/空白/字符集/超长）：来源级受控失败（publishable=False，
  坏来源不静默丢弃）。
- 冲突：内部显式 version_scope 与文档声明都明确且规范化后不同 → 受控失败；相同接受。
- 不做 URL/文件名/标题/`openapi`/`info.version` 推断；Markdown 仍 unversioned。
- 规范化沿用 `identity.normalize_version_scope`。详见 `phase9b/CONTRACT-version-config.md`。

## 5. 实际数据库升级步骤（上线前，仍需单独授权在真实库执行）

仅允许在副本上执行；工具位于 `backend/phase9b_migration/`（写目标必须 `--allowed-dir`，
真实 `backend/data/notes.db` 一律拒绝）。
1. 一致性备份：SQLite backup API → immutable baseline（先核对无活动 WAL、大小/mtime/hash
   稳定）；`restore-verify` 验证 SHA 与基线一致。
2. 只读 inventory（schema-aware）：`python -m phase9b_migration.main inventory --db <copy>`；
   输出 revision/表/列/ACL/回填候选/blocked/orphan 摘要。
3. 兼容迁移：`upgrade --db <working>`（P40–P44 兼容逻辑只对“等价对象”跳过；真实库
   `alembic_version` 当前 P38，需先在副本跑通 head 并核对 quick/fk/schema guard/数据摘要）。
4. 回填 dry-run：`backfill-dryrun` → 检查计划 hash 与计数。
5. 批准计划后 apply：`backfill-apply --plan-hash <hash>`（单事务、幂等；第二次 created=0；
   与 dry-run 后库状态不一致会拒绝）。
6. postflight：active binding 唯一、blocked 未放行、同 ACL 各自默认 Workspace、
   Wiki.workspace_id 与来源 binding 一致、published Wiki 无 NULL workspace、关键计数不减少。
默认每 Notebook 独立默认 Workspace，**同 ACL 不自动合并**（ACL 只做权限等价校验，不作
Workspace identity）。

## 6. 失败处置

- Run：`POST /api/wiki-compile/runs/{id}/retry|cancel`（真实状态机；queued→cancelled、
  running→cancel_requested；终态 409）。
- worker 恢复：进程内 pump 自动消费 queued；崩溃后真实 startup `run_startup_recovery`
  requeue stale（短 lease 演练见 `tests/test_phase9b_recovery.py`）。
- 图谱重试：schedule_graph 失败（GRAPH_BUILD_FAILED，retryable）不回滚已发布 Revision；
  正式 retry 从 Artifact Manifest 重放、不重复发布。
- schema 不兼容（同名列/约束不等价、约束违规数据）：迁移/编译受控失败并停止；副本视为
  失败产物，从 immutable baseline 重建，不手工改 revision。
- 备份恢复：`restore-verify`/restored 副本与 baseline SHA 一致即原始状态恢复依据。

## 7. 数据保护（必须理解）

- `downgrade`（head→P38 之类）会移除迁移新增的表/列及其中数据（含迁移后回填的
  Workspace/Binding 行）——这不是“无损回滚”。
- “原始物理超前 schema”状态的恢复，只能以 **immutable baseline 的恢复验证**为准；两种
  结果分别报告，不混称无损。

## 8. 覆盖核对、最终测试与离线对比（含局限）

详细 20 项→证据映射见下方清单（测试名均真实存在）。链路类型：真实生产调用链（executor
真实 v3 发布 + 临时 SQLite + Fake 模型）/ 临时 SQLite 集成 / Fake 模型替身 / mock 接口 /
单测直调。
1 新来源 Raw→Canonical→Workspace→Topic→Skill→Revision：分段覆盖（sources executor：
  `test_phase2_executor_conversion.py:122/:155`；wiki.compile：`test_wiki_skill_default_v3.py:310`、
  `test_phase5_pipeline_core.py:205`、`test_phase9a_integration.py:350/:436`）。
  局限：无“connector raw 进库后同一测试内连续触发 wiki.compile 到 Revision”的连续用例（记录缺口，见 §10）。
2 Markdown passthrough：`test_canonical_note_converters.py:72/87/93/107`、
  `test_phase24_semantics.py:347`（直通/保留分隔符/剥 frontmatter）。
3 PDF/Office 共享转换 + 失败不建页：`test_canonical_note_converters.py:357/:465`、
  `test_phase2_executor_conversion.py:155`（failed/blocked 无 Page 行）。
4 同 ACL 不同 Workspace 隔离：`test_wiki_workspace_routing.py:370/:604/:225`、
  `test_wiki_pipeline_core.py:703/:872`、`test_phase9a_integration.py:547`。
5 多 Notebook 同 Workspace：`test_wiki_workspace_routing.py:332`、
  `test_phase9a_integration.py:436`（nb-hidden/nb-eng 同 ws-eng 双来源共存）。
6 API Skill 高置信选择：`test_wiki_skill_api_reference_register.py:79`、
  `test_wiki_skill_router.py:162/:176`、`test_wiki_skill_default_v3.py:310`。
7 低置信回退 default：`test_wiki_skill_router.py:207/:222/:233/:244/:255/:267/:424`。
8 新 Endpoint 新增 Section：`test_phase9b_incremental.py:477`、`test_api_reference_display.py:380`、
  前端 `live9a` L3。
9 参数变化只影响相关 Section：`test_phase9b_incremental.py:568`、
  `test_phase9b_version_scope.py:667`。
10 删除唯一来源：`test_phase9b_incremental.py:649`、`test_wiki_pipeline_7d.py:152`、
    `test_phase52_deletion.py:415`。
11 删除非唯一来源：`test_phase9b_incremental.py:712`、`test_phase52_deletion.py:444`。
12 LLM 失败保持 dirty/旧发布版：`test_phase9a_integration.py:350`（首败）、
    **新增 `test_phase9a_integration.py::test_v3_llm_failure_after_publish_keeps_old_revision_and_dirty`**
    （真实 v3 链：已发布→二次编译 LLM 失败→current 不变/dirty/零新 Revision）；
    legacy 直调 `test_wiki_v4.py:304/:336`。
13 Converter 失败不创建错误 Page：同 3（`test_phase2_executor_conversion.py:155`）。
14 Validator 失败不发布：`test_api_reference_validator.py:80-166`、
    `test_api_reference_runtime.py:178/:185/:495/:509`、`test_wiki_skill_default_v3.py:507-537/:934`、
    `test_phase9b_incremental.py:943`。
15 队列/重启/heartbeat：`test_wiki_pipeline_core.py:256/:1099-1259/:1612/:2265/:2620`、
    `test_phase9b_recovery.py::test_cross_process_crash_recovery`（进程边界）、
    `test_wiki_v4_final.py:138/:269`。
16 protected/manual 保留：`test_phase9b_incremental.py:786/:870`、
    `test_wiki_skill_default_v3.py:731`、`test_wiki_skill_default_equivalence.py:296`。
17 Skill 迁移失败保持原 Skill：`test_wiki_skill_migration_v3.py:589/:727/:506/:685`、
    `test_wiki_skill_router.py:338`、`test_wiki_skill_api.py:342-400`。
18 图谱失败独立重试不重复 Revision：`test_phase9b_recovery.py:441`、
    `test_wiki_skill_default_v3.py:1049`、`test_phase52_deletion.py:503`。
19 Workspace/Wiki/Evidence 权限：`test_phase9a_integration.py:485-615`（admin/editor/reader +
    历史/诊断/run/Evidence 来源过滤）、`test_wiki_8c_section_evidence.py:255/:392`、
    `test_wiki_workspace_api.py:160/:178`、`test_access_control_api.py:305`、live9a L4a-L6d。
20 历史 Revision 回滚：`test_p6_wiki_api.py:99`、`test_p6_wiki_lifecycle.py:76`、
    `test_phase9b_incremental.py:943`、`test_phase9a_integration.py:583-615`（admin 200 /
    reader·editor 404）。

### 离线质量对比（既有测试，不调真实模型）
- default 流水线版本等价：`test_wiki_skill_default_equivalence.py`（v1 vs v2 全场景快照等价：
  普通/版本化/protected/多源/not_worthy/LLM invalid/unavailable/stale/page_deleted）与
  `test_wiki_skill_default_v3.py:424`（v3 ≡ v2 单目标）。
- api_reference 保真/防虚构/防泄漏：`test_api_reference_display.py`（投影白名单、参数顺序、
  media_types、hash 不符丢 display、防猜测类型、oversize/overdepth 无 display）、
  `test_api_reference_runtime.py`（不猜事实、伪造 method/status/error code/未知 evidence
  拒绝、exception 路径不泄漏 token）、`test_api_reference_renderer.py`（不泄漏 evidence id）、
  `test_api_reference_schemas.py:378-474` + merge/parser 的 coverage 门禁。
- protected 保真与不可发布阻断：A 节 protected 等价 + display:673 + runtime 系列 + stage
  零发布系列。
- 口径说明：仓库内没有“default skill 与 api_reference skill 对同一份资料产出内容等价”的
  对照（两者产出不同 skill 语义属预期）；A 节“等价”指 default 管线 v1/v2/v3 版本等价。
  以上仅验证管线约束与展示保真，**不证明真实模型提取质量/延迟/成本**；本报告不提供虚构
  百分比指标。

## 9. 已知限制

- 本批及此前均在 SQLite 上实际验证；**PostgreSQL 路径未实测**（迁移 PG 分支保持原语义、
  docker-compose 未启动）。
- 真实库 `notes.db` 当前 Wiki=0：历史 Wiki 的验证基于合成资料；真实数据副本演练仅到
  migration/backfill/postflight 层（Wiki 相关为事务内合成反例，单独标记）。
- 真实模型质量/费用未评估（全部 Fake/预录确定性输出）。
- 当前真实库**尚未升级**（仍 P38 且物理部分超前），应用上线前须先完成正式迁移
  （见 §5 与 §11）。
- 连续全链（raw→Revision 同一用例）与真实 v3 高并发 soak 未建用例（见 §10 缺口）。

## 10. 覆盖缺口（如实）

- B1 单一连续“connector raw → CanonicalNote → Page → page_changed CompileRun → Revision”
  用例缺失（两套 executor 各自有覆盖）；建议最小用例：raw md/pdf → converter → upsert →
  wiki.compile；坏文件断言无 Page、无 run、无 Revision。**本轮未实现**。
- B3 真实 v3 product pipeline 多 worker 高并发 soak 未建（现有并发为 fake pipeline +
  跨进程单 run）。**本轮未实现**。
- B2 已在本批补反例并通过（见第 8 项 12 的粗体测试）。

## 11. 上线前仍需执行的步骤（需单独授权）

1. 真实库正式迁移（需授权）：一致性备份→副本 inventory→兼容迁移→回填 dry-run→批准计划
   apply→postflight；PostgreSQL 目标时先做 PG 副本验证。
2. 启动 PG 相关服务与 pgvector，做一次 PostgreSQL 真实链路验收（当前未实测）。
3. 上线前回归：真实资料小批量（含 1 个 API Reference 资料）触发正式编译到 Revision，
   并在界面核验目录/结构化章节/Evidence。
4. 添加新 Skill 的最小标准流程（上线态）：在 `wiki_skills/builtin/<skill>` 注册 key/版本/
   loader/registry → 通过既有 Skill 契约单测（loader/registry/router/signals 模式参照 default
   与 api_reference）→ 以 editor 身份 `POST /api/wiki/{id}/skill-override
   {skill_key,skill_version,lock,recompile}` 触发真实编译 → 用 diagnostics/Evidence 抽屉核验
   → 汇入 `bootstrap` 内置注册并跑 Phase 级验收。
5. 切流量/回滚预案：以 immutable 备份 + 副本演练为准，不接受 downgrade 作为回滚手段。

## 12. 最终门禁记录

- 后端全量 pytest（一次，`.venv`）：**2454 passed / 2 skipped / 0 failed**，exit 0，
  时长 ≈16:31；skip 为 `test_wiki_skill_loader.py`（当前平台无法建立外部连接、安全跳过）；
  warnings 均为既有 deprecation（无运行异常；故障注入日志与后台异常已按“通过 + 仅 deprecation”
  区分）。完整日志保留在临时目录（未入库）。
- 前端 `npm run build`：成功，exit 0（vue-tsc 已含）。
- 复用的既有验收证据：Phase 8 前端 mock 套件、9A live9a、9A/9B 集成、9B 副本迁移演练
  均按“链路未变不复跑”原则引用既有记录（见 §1 文档链接与本表），未重复执行。
- 数据/模型隔离：全量 pytest 未引用真实 notes.db（仅注释与工具测试的假 “notes.db” 文件
  名）；conftest autouse 隔离外部模型；无回退真实客户端。
