# WikiSkill 阶段 0 设计冻结（00-design）

> 依据：`知识库搭建/WikiSkill实施方案-OMP-DeepSeek-V4-Flash.md`（Obsidian Vault）。
> 状态：设计冻结。本文档只核查代码与产出设计，未修改业务代码、数据库、依赖或运行配置；未安装第三方 wikiskill 包；未发起真实模型调用；未执行生产迁移。
> 标注约定：**[事实]** 直接读当前代码得出（附 文件:行）；**[设计]** 本文建议；**[假设]** 未验证推断。

## 1. 当前代码基线

| 项 | 值 | 证据 |
| --- | --- | --- |
| 仓库 | `C:\Users\20474\Documents\学习Agent\gitlab-rag-feature` | |
| 分支 | `feature/dingtalk-to-markdown` | `git branch --show-current` |
| HEAD | `e42b056a5edefc531f37be91eb5577a0ed7149d0`（2026-09-05 15:38:28 +0800，「docs: phase 9c - mark B1 implemented…」） | `git rev-parse HEAD` |
| 工作区 | 无已跟踪文件修改；仅未跟踪产物：`backend/.pytest_{b,c2,c3,c,tmp,_runtime}/`、`backend/reports/`、`backend/tmp/`、`data/`、`tmp/` | `git status --porcelain` |
| 根 AGENTS.md | 描述 Notes RAG v2（pages/notebooks/search…），**未覆盖** phase 5+ 的 wiki pipeline/skills/workspace 各层，属过时文档 **[事实]**（仓库根唯一 AGENTS.md，内容与 git log phase5–9c 系列不一致） | `AGENTS.md` |
| Python | `backend/.venv`（codex primary runtime 3.12.13，`pyvenv.cfg`）；系统另有 3.11.9 | |
| 数据库默认 | `sqlite:///./data/notes.db`（config.py:60）；PG 仅条件启用（init_db 中 `dialect=="postgresql"` 分支 database.py:2473-2511） | config.py:60 |
| 迁移 | Alembic 目录 `backend/alembic/versions/`（38 个文件）；**head = P44 `a9b8c7d6e5f4`**（a9b8c7d6e5f4_p44…py:46-47，down=P43 `d3e4f5a6b7c8`）。开发/测试建库走 `init_db()`（create_all + 托管字段检查 database.py:2444-2518）；真实库落后 schema 时 `init_db` 抛 `SchemaNotReadyError`（database.py:1343, 2444-2456），须显式 `alembic upgrade` | [事实] |

## 2. 真实调用链（default Wiki 编译）

结论先行：**当前实际启用的默认编译流水线是 `wiki.default` v3**（配置默认 `wiki_pipeline_active_version="3"`，config.py:162；bootstrap 启动时把 registry active 显式设为该值，wiki_pipeline/bootstrap.py:70-86）。v1/v2 与 v3 同时注册用于精确版本恢复；v2/v3 注册本身不改 active（wiki_skilled_default.py:600-619 例外地在幂等分支设 active=v2，但 bootstrap 注册完统一覆写为配置值，bootstrap.py:85）。

### 2.1 入口 → 建任务

所有入口最终汇聚到 `executor.create_run`（executor.py:511），不显式传版本时经 registry 解析 **active**（executor.py:543-566；`get_pipeline(key,None)` → `_ACTIVE_VERSIONS`，registry.py:276-310）：

| 入口（API/事件） | 调度函数 | 触发类型 | 版本 | 证据 |
| --- | --- | --- | --- | --- |
| Page 增/改/导入（pages.py、notebooks.py、source_path_mappings、dingtalk_folder_mappings、sources/executor） | `schedule_page_refresh`（wiki_refresh_scheduler.py:527）→ `_enqueue_page_changed`(:424) → `_create_default_run`(:375) | `page_changed` | 不传 → active(v3) | :395-408 |
| Wiki dirty 重建（wiki.py refresh-dirty/refresh-page-dirty :414/:466 及启动 recover_dirty_pages :669） | `schedule_wiki_rebuild`(:546) → `_enqueue_manual_rebuild`(:476) | `manual_rebuild` | 不传 → active(v3) | :500-512 |
| 全量重建（POST /api/wiki/rebuild） | `create_batch_run`（wiki_default.py:324） | `batch_rebuild` | 不传 → active(v3) | :368-385 |
| Page 删除（pages.py delete kill-on 分支 :382/:414-419；sources/executor :572-573） | `create_page_deleted_run`（wiki_default.py:447）或 scheduler `_enqueue_page_deleted`(:724) | `page_deleted` | 不传 → active(v3) | :485-490 |
| Skill 覆盖/解锁重建（api/wiki_skills.py skill_override :193、skill_unlock :259） | `_create_skill_rebuild_run`(:143) | `manual_rebuild` | 显式读 registry active 传入（:164-167） | :132-141 |

幂等与抢占：`idempotency_key`（唯一索引，database.py:443-447）同指纹幂等返回、异指纹 409；`supersede_matching_runs`（executor.py:638）同维度抢占旧 queued/running/failed。输入内容指纹 `input_hash`（String(64) 必填）[事实]。

### 2.2 队列 → worker

队列即 `knowledge_compile_runs.status='queued'` 行（无外部 broker）。消费：

- main.py startup `_start_scheduler`：`bootstrap_wiki_pipeline()`(:63) → `recover_dirty_pages()`(:77) → `run_startup_recovery()`+`start_worker()`(:90-93)；shutdown `stop_worker()`。
- worker.py `_pump_loop`(:417)：每轮 `requeue_stale_runs`(:286，lease/heartbeat 超时恢复) → `claim_next_run`(:87) → `executor.claim_by_id`（CAS queued→running、attempt 原子 +1、写 lease_token/worker_id/lease_expires_at，executor.py:749-810）→ `_run_with_lease_renewer`(:251)：启动 `LeaseRenewer` 心跳守护线程(:175，默认 30s 续租，config.py:171) → `execute_run(db, run.id)`。
- 并发上限 1（ThreadPoolExecutor max_workers=1，worker.py:470-482）；轮询 2.0s（config.py:170）；lease 300s（config.py:168）。
- 恢复语义：stale running → 关 stage 失败 → attempt<max 回 queued（不加 attempt）、attempt≥max → failed(retry_exhausted)（worker.py:286-363）；取消 queued→cancelled、running→cancel_requested（executor.py:727）；状态机常量与转移表见 state_machine.py:21-58。

### 2.3 worker → 阶段执行（v3 固定链）

`execute_run`（executor.py:1223）按 **run 创建时固化的 pipeline_version** 取流水线（注册新版本不影响旧 run，:1271-1272）；预建本 attempt 全部 StageRun 行；逐 stage：原子 `_fence_run`(:856) → `compute_stage_input_hash`(:1001-1025) →（prod 全 stage `cachable=False`，wiki_default.py:109-128 / v3 `_stage_defs_v3` 1211-1235，缓存仅 fake 演示）→ running 短事务 → `db.begin_nested()` SAVEPOINT 包住 `_safe_execute`(:1159) 的写入 → 结果契约校验（registry.py `validate_stage_result` ~:452-494；成功可带 artifact/payload/metrics；仅 `allows_publish` 的 stage 可返回 output_revision_id）→ fencing 提交；失败回滚 SAVEPOINT 丢弃孤儿写 [事实]。

v3 阶段链（wiki_skilled_default_v3.py:88-97 keys、:1209-1219 执行体映射；v3 单目标支持触发 `page_changed/manual_rebuild`，:86）：

1. `resolve_context` → `wiki_default._stage_resolve_context`（wiki_default.py:851）：解析目标/workspace/binding/scope + 确定性 input_hash。
2. `topic_route` → `_stage_topic_route`（:1076）：**LLM 主题决策**（worthy/create/update，调用点 :1228-1231，经 `_llm_runner(ctx)`）。
3. `skill_route` → `wiki_skilled_default._stage_skill_route`（wiki_skilled_default.py:280）：只读构造 SkillContext → `skill_service.decide(sctx)` **未传 llm_runner**（:315 区域）→ 确定性路由/缺省 default；产出 `skill_decision` Artifact 与 state（生产实际上恒为 default，除非锁定/迁移）。
4. `synthesize_by_skill` → `v3._stage_synthesize_v3`（v3:604）：default 分支复用 v1 合成（普通/版本化 Map-Reduce）；api_reference 分支走内存编译 `_compile_api_from_pages`(:541，纯内存、不发布)；batch/page_deleted 分派到 batch v3 / v1。
5. `validate_by_skill` → `v3._stage_validate_v3`(:651)：default 复用 v1 发布前校验（input_hash 重算/正文非空/workspace 未变，wiki_default.py:1636-1713）。
6. `publish_by_skill` → `v3._stage_publish_v3`(:1034)：default → v2 publish wrapper（v3:55-56，等价 v1 发布 + Skill 字段原子写）；api_reference → `_persist_api_revision`(:920-954) + Sections/Bindings；**这是唯一产品写 stage**（详见 2.5）。
7. `finalize_compile_outcome` → `_stage_finalize_compile_outcome`（wiki_default.py:2360）：把 Publish Manifest 结果映射为 run 成败（LLM 级失败不再虚假 succeeded）。
8. `schedule_graph` → `_stage_schedule_graph`(:2419)：按 Manifest 重建图谱。

### 2.4 模型调用（default 分支）

- stage 不直接 import 客户端；一律经 `ctx["llm_runner"]`（executor 注入，executor.py:1319-1320；进程级全局 `_LLM_RUNNER`，生产启动未 configure → None）。
- `wiki_default._llm_runner(ctx)`（wiki_default.py:151-163）：ctx runner 缺失时回退 `_default_llm_runner`(:159-165) → `asyncio.run(call_wiki_llm_json(...))`（knowledge_compiler_v3/wiki_page_builder.py:386，严格客户端：未配置/HTTP 失败抛 `LLMServiceUnavailable`，非法 JSON 返回 None）。
- default 合成的提示词是**静态模板**：`WIKI_INGEST_PROMPT`(builder:61)、`WIKI_SYNTHESIS_PROMPT`(:84)、`WIKI_BATCH_SUMMARY_PROMPT`(:104)、`WIKI_MAPREDUCE_PROMPT`(:118)、`WIKI_SYNTHESIS_VERSIONED_PROMPT`(:141)，调用点 :1121/:1207/:1252/:1272/:1294 与 topic_route :1228-1231。模板内不含任何 skill 内容。
- api_reference 分支的模型参与经 `_pipeline_api_llm_adapter`（v3:511-541，复用同一 `_llm_runner(ctx)`）只给 Markdown hint 提取（api_reference/compiler.py:392-507）。
- 共享宽松客户端 `llm_client.call_llm_json`（llm_client.py:106，失败返回 {}）只被 chat/rag/检索/intent/organize 等使用，**不在 default 编译链上**。

### 2.5 发布边界（业务写）

编译成功即自动发布，无独立审核步（wiki.py approve/reject/preview 全 410 废弃，:557-589；人工发布走 wiki_lifecycle.publish_wiki_revision :47-89，与编译发布是两条路）。发布 stage 直接写业务表：

- `WikiPage`（status→published、dirty=false、current_revision_id、skill 字段）、`WikiRevision`（status=published）、`WikiSection`（含版本字段）、`WikiVersionSource`、`Page.wiki_dirty/wiki_compiled_content_hash` 清理；api_reference 另写 `WikiSectionEvidenceBinding`；`schedule_graph` 阶段写图谱表。写入函数：`_publish_wiki_from_entry`（wiki_default.py:663-684）、`_create_wiki`(:639-673)、`_append_revision/_append_versioned_revision`（builder:675-695/:754-781）、v3 `_persist_api_revision`(v3:920-954)/`_write_auto_section`(:823-866)/`_write_protected_section`(:874-912)。resolve/topic/synthesize/validate 阶段只产出 DTO 候选（dto.py），写库最多 Artifact。

普通用户只读 published+published 当前 revision（wiki.py `_visible_wiki_or_404` :240-253、list 过滤 :316-330）。

## 3. 关键发现

1. **[事实] default 技能指令未注入模型请求（核心阻塞）。** `instructions.md` 在启动加载时只做大小/UTF-8 校验（wiki_skills/loader.py:178-183、:305），内容即丢弃：`SkillDescriptor`（schemas.py:148-159）只有 `instruction_resource` 文件名；registry 仅存 descriptor/runtime/active（registry.py:46-53，纯进程内存，无 DB）；API 白名单序列化不返回指令全文（api/wiki_skills.py `_serialize_skill_summary`）。路由 LLM 提示词明确「不发送 instructions/schema」（router.py:16-18、:67-116）；default 合成提示词为静态模板（见 2.4）。**结论：无论 default 还是 api_reference，skills 的 instructions.md 都没有进入任何模型请求**；skill.yaml 只决定「选择哪个 skill」，不影响生成内容。
2. **[事实] skill Runtime 是占位适配层，生产不调用。** `DefaultSkillRuntime`（builtin/default/runtime.py）extract/plan/render/validate 全返回占位；default 内容走 Phase-5/7C 静态 Python 流（v3 注释明确「不复制整套 wiki_page_builder」）。api_reference 由 `ApiReferenceRuntime` 转发纯函数（builtin/api_reference/runtime.py:29-33）。
3. **[事实] 生产技能选择是确定性的。** skill_route 调 `service.decide(sctx)` 不传 llm_runner（wiki_skilled_default.py:315 区域；service.decide 默认 llm_runner=None，service.py:100-104），router 缺省走确定性/default fallback（router.py:552-563 的 LLM 分支只在显式注入 runner 时可达）。每 wiki 选择结果持久化在 `wiki_pages.content_skill/skill_version/skill_selected_by/skill_locked/skill_decision_json`（database.py:552-557），无 workspace 级绑定表。
4. **[事实] 版本固定与恢复能力已具备（流水线侧）。** run 创建时固化 `pipeline_version`（executor.py:566/603），执行按精确版本取流水线（:1271）；v1/v2/v3 并存注册，bootstrap fail-closed 校验（bootstrap.py:56-67）；测试证明「v2 注册后 queued v1 run 仍按 v1 执行」（test_wiki_skill_pipeline.py:148-164）。**技能/经验侧没有等价机制**（registry 纯内存、无不可变版本、无 DB 指针）。
5. **[事实] 阶段摘要不足以重放完整轨迹。** Artifact 只落 JSON 摘要（decision/manifest/输入列表，artifact_type 见 database.py:507 注释），`payload_json` 不存提示词/输入全文；`input_hash` 是内容哈希不是快照；来源 Page 正文在业务 DB 可变。要支持「固定输入快照 + 可重放轨迹」，必须新增快照与轨迹存储。
6. **[事实] 可复用的离线执行范式已成熟（测试即证据）。** 测试模式：临时 SQLite + `init_db` + 进程内注册 v1/v2/v3 + `executor.configure_external_runners(llm_runner=fake, graph_runner=noop)` + `create_run` + `execute_run`，全程无网络/无真实模型（test_phase5_equivalence.py:200-262 FakeWikiLlm、test_wiki_skill_default_v3.py:310-458、test_wiki_pipeline_7d.py、test_phase9a_integration.py 含 recorder.llm_runner :118-218）。本轮在 `backend/.venv` 复跑 `test_wiki_pipeline_7d.py + test_wiki_skill_default_v3.py`：**48 passed in 6.60s**。
7. **[事实] 存在进程级全局共享的注册表与 runner。** pipeline/skill Registry、`_LLM_RUNNER/_GRAPH_RUNNER`（executor.py:63-80）、kill-switch TTL 缓存都是**进程内全局**。演化实验若与 API/worker 同进程运行会互相污染 → 演化执行必须独立进程（CLI/独立 worker）。
8. **[事实] kill switch**：`wiki_pipeline_default_enabled` 默认 True（config.py:152），DB-first（RuntimeFeatureFlag 行覆盖，feature_flags.py:24-28），关闭时不建 run、保持 dirty、绝不回退旧 builder（scheduler 模块 docstring :218-220）。可用作实验期保护性开关（另见风险 R1）。
9. **[事实] 检索评测先例**：`eval_retrieval.py` 是注入式检索评测（Recall@10 + 引用准确率），评测集 `backend/eval/p4_eval_set.json`（10 例，含 permission_isolation 例）——是「离线评测集 + 确定性度量」的现有模板，但不是 Wiki 编译质量评测。
10. **[事实] 无任何实验/评测/预算配置与表**（config.py 无 eval 类字段；无 skill 内容表、无绑定表、无 pattern/proposal/evolution 表）。

## 4. 架构接入方案（设计）

### 4.1 边界裁定

- **业务 Wiki 与经验 Wiki**：业务侧维持现状（wiki_workspaces/wiki_pages 族）。经验 Wiki（patterns/日志/影响）不进 `Base` 业务库、不进检索索引、不接 chat；阶段 1 用**独立 SQLite 文件**承载实验数据（数据+快照+轨迹+经验草稿），阶段 3+ 再按需引入业务可见表（迁移+托管清单）。
- **技能迁移 vs 技能进化**：现有 `default→api_reference` 迁移（proposal/locked，batch v3）是**内容技能间一次性迁移**，其 SkillDecision/locked 语义可复用，但不是自动进化闭环；进化 = 同 skill 的指令版本迭代（candidate→验证→accept/reject→binding 指针）。两者共用 Skill 字段与版本精确性约束，但状态机分开记录。
- **内容校验 vs 效果门控**：现有 validate_by_skill/validate_default 是发布前合法性校验（结构/正文/input_hash 一致性）——属「内容校验」，继续作为任务硬性检查的一部分；「效果门控」（分数提升才接受、验证集独立评估）是进化层新逻辑（evaluation + gating），与内容校验分离。

### 4.2 版本维度分离（对应方案 §5.3 要求）

| 维度 | 载体（现状） | 目标（设计） |
| --- | --- | --- |
| Runtime 版本 | 代码 + builtin 目录（loader 启动校验；registry 进程内存 key/version） | 保持代码随部署版本；`runtime_bundle` 常量记录进 run/experiment 快照。**不可由进化修改**。 |
| 指令版本（可进化） | 仅 builtin `default/instructions.md`（未注入、未版本化） | DB 表 `skill_instruction_versions`：不可变行（skill_id, version, content, content_hash, parent_version_id, source=builtin|candidate, proposal_id 可空）+ `workspace_domain_bindings` 生效指针。builtin v1 = 启动导入的种子版本；进化只写新候选行。 |
| 模型/评分器 | llm_client 读 settings；无评分器 | experiment 配置快照冻结 `model_ref/grader_ref/prompt_ref`；grader 版本化文件。 |

在途 run 在 claim/创建时**固定指令版本集合**（content_hash 链记入 run 行或轨迹），不随 binding 指针漂移——与现有 pipeline_version 固化语义一致（executor.py:566/1271）。

### 4.3 指令注入点（阶段 2，先于任何进化）

**[设计]** 在模型调用单一汇聚点注入，不改业务提示词模板：

- default 分支：`wiki_default._llm_runner(ctx)`/`_default_llm_runner`（wiki_default.py:151-165）是 topic_route 与全部合成的唯一同步 runner 出口 → 在此包一层 `InstructionInjectingRunner`：按 run 的指令版本解析文本，在 `messages` 前插入 `{"role":"system","content":<指令>}`（OpenAI 兼容端点均支持），并设长度上限（建议配置 `wiki_skill_instruction_max_chars`，超限 fail closed）；同时把最终 `messages` 快照写入轨迹（满足阶段 2 验收「两个版本在捕获到的请求中确实不同」）。
- api_reference 分支：`_pipeline_api_llm_adapter`（v3:511-541）为另一出口，同样包裹。
- 关闭功能（无 binding/无指令版本）时保持现行为（零注入、旧编译不变）——阶段 2 验收。
- 直接改静态模板（builder:61-167）的方式**否决**：提示词站点多、会引入与技能绑定的耦合与回滚面。

### 4.4 隔离评估的实现方式（阶段 1）

复用真实编译核心 + 隔离副作用：

1. **真实逻辑复用**：实验 CLI 在独立进程内：`skill_service.register_builtin_skills()` + 注册 v1/v2/v3（等价 bootstrap 前段）→ 在**实验专用 SQLite** 上 `init_db` → 按任务物化快照（Page/Notebook/WikiWorkspace/Binding/可选 Evidence）→ `executor.create_run(db, pipeline_key="wiki.default", pipeline_version="3", trigger_type="manual_rebuild"|"page_changed", ...)` → `execute_run(db, run.id)`。这与生产 worker 走同一 `execute_run`/stage 实现（test_phase5_equivalence.py 同范式）。
2. **不写业务 Wiki / 不触碰生产库**：实验 DB 文件独立（`backend/eval/wiki_evolution/runs/<id>/eval.db`）；进程内不启动 worker、不调用 scheduler/API、不改 `settings.database_url`；发布 stage 的产品写在实验 DB 内，评分读实验 DB。额外保护：入口断言当前进程非 API 进程且实验 DB 路径 != 任何业务 DB。
3. **固定输入快照**：任务文件（`sources/*.md` + 任务 JSON）只读；物化时按 `content_hash` 生成不可变 Page 内容并记录 `snapshot_id`；每次运行从快照文件重建实验 DB（新建文件、不覆盖旧 run）。
4. **固定技能/模型/评分器版本**：技能集合 = binding 指针 + 指令版本哈希；模型 = experiment 配置 `model_ref`（离线=注入 runner 标识；真实=settings 的 url/model 快照）；grader = `grader_ref` 版本化文件。
5. **执行者/提议者权限隔离**：实验 DB 内只物化任务允许资料；验证答案与 reference 放独立文件区（`references/`），**不写入实验 DB、不进入 runner 可读路径**；CLI 进程只加载自己数据集目录；后续接 API 时复用 `access_control`（wiki_editor/admin，api/wiki_skills.py:45-60 模式）。隔离靠文件/DB 边界实现，不靠提示词。

### 4.5 逻辑目录（物理实现）与内容演进对照

方案 §3.2 的 `<evolution-workspace>` 落地为 `backend/eval/wiki_evolution/`：

```text
backend/eval/wiki_evolution/
  datasets/wiki-default-v1/          # 任务 JSON + 来源材料（train/val/test 分组文件）
  snapshots/<snapshot_id>/           # 物化输入快照（不可覆盖，追加+seal hash）
  runs/<evolution_run_id>/           # 每 run 独立实验 DB + 轨迹 jsonl + 输出
  references/                        # grader 参考答案（评分器私有）
  graders/wiki-default-grader-v1.py  # 确定性评分器（版本化）
  experiences/                       # 阶段 3+ pattern/修订导出
  skills/                            # 阶段 2+ 指令版本导出（SKILL.md/PURPOSE.md）
```

Raw 追加写、技能版本不可变、回退只改指针——遵循方案 §3.2 不变量；不用 `git reset --hard` 回退。

## 5. 数据结构映射

### 5.1 现有表直接映射（阶段 1 复用，零 DDL）

| 现有对象 | 位置 | 复用方式 |
| --- | --- | --- |
| `KnowledgeCompileRun` | database.py:388-447 | 任务执行的 run 主记录：pipeline/version 固化、input_hash、lease、attempt、status。实验 DB 中每任务一行。 |
| `KnowledgeCompileStageRun` | :449-491 | stage 级执行记录（attempt/status/metrics）→ 轨迹事件骨架。 |
| `KnowledgeCompileArtifact` | :493-525 | decision/manifest 等结构化产物（**不含 prompt/输入全文**，需补轨迹）。 |
| `WikiPage/WikiRevision/WikiSection/WikiVersionSource/Page` | :528-728、:206-263 | 实验 DB 内「候选输出」物化载体（评分读这里）；业务库不写。 |
| `WikiWorkspace/NotebookWorkspaceBinding/Notebook` | :328-386、:145-172 | 实验 DB 的 scope 容器与路由（page_changed 触发依赖 binding）。 |
| pipeline/skill Registry、bootstrap | registry.py、bootstrap.py | 独立进程内注册；不共享 API 进程。 |
| `executor.create_run/claim_by_id/execute_run`、worker.LeaseRenewer | executor.py:511/:749/:1223、worker.py:175 | 实验执行直接调用（不经 pump）。 |
| `RuntimeFeatureFlag/feature_flags` | database.py:1104、feature_flags.py | 实验入口可选保护开关（如 `wiki_evolution_enabled`，阶段 6 前不新增）。 |
| `access_control`、`wiki_workspace.service` | access_control.py:98-660、wiki_workspace/service.py | 后续 API 化时做读写权限与可见性（现 CLI 阶段靠文件/DB 边界）。 |
| `eval_retrieval.py` | app/core/eval_retrieval.py | 仅参考其「评测集+确定性度量」模式，度量本身不适用于编译质量。 |

### 5.2 逻辑实体 → 新增存储（设计，标注引入阶段）

| 方案实体 | 载体 | 引入阶段 | 关键列/内容 |
| --- | --- | --- | --- |
| ExecutionTrace | 实验 DB 新表（独立 metadata，**不挂业务 Base**，阶段 1 无生产 DDL） | 1 | execution_id↔run_id、task/dataset/split、input_snapshot_id、skill_set_hash、instruction_hash、runtime/pipeline/grader/model refs、事件（调用序、消息快照、用量、错误分类）、seal_hash、task_score/checks。事件明细落 run 目录 jsonl（追加写）。 |
| 输入快照 | 文件 `snapshots/<id>/` + 哈希注册 | 1 | 物化输入；不可覆盖；seal 后只读。 |
| SkillInstructionVersion / DomainBinding | 业务库新表（**Alembic P45，挂 Base + MANAGED 清单**） | 2 | 见 §4.2；候选不可自动影响业务 binding。 |
| Experience Pattern / 修订 | 实验/经验库新表或文件 | 3 | pattern_id/scope/domain/现象/原因假设/建议/适用/反例/evidence_execution_ids/修订号/状态（observed/supported/contradicted）。 |
| Proposal | 业务或实验库新表 | 4 | action/parent 版本+内容哈希/补丁/动机/Pattern 引用/已读轨迹。 |
| Evaluation / EvolutionRun | 实验库新表 | 5-6 | 配置快照、best_score、轮次、预算、租约、门控结论、幂等键。 |
| 评分器 | 文件 + 版本常量（`wiki-default-grader/v1`） | 1 | 确定性；权重表显式「待校准」。 |

阶段 1 约束：**不向 `models/database.py` 的 `Base` 加表**（避免任何库 `create_all` 时自动建表，含生产库启动）；轨迹表用独立 `MetaData`，CLI 在实验 DB 上显式建。阶段 2 起新增业务表必须：alembic 新版本（`down_revision="a9b8c7d6e5f4"`）+ `MANAGED_*` 清单 + 兼容测试（tmp DB `alembic upgrade head` 后 `init_db` 不抛，参照 test_j3_graph.py:672-717 模式）。

## 6. 首个任务与评分契约（default，阶段 1 落地）

### 6.1 任务 JSON 示例（不含私有资料）

```json
{
  "task_id": "wiki-default-001",
  "dataset_version": "wiki-default-v1",
  "domain": "wiki_compile.default",
  "split": "train",
  "workspace_scope": "eval-scope-titan-810",
  "input_snapshot_id": "snapshot-001",
  "group_id": "doc-family-titan-810-install",
  "title": "Titan 810 电池模组安装与适用条件",
  "instruction": "根据资料生成 Wiki：必须覆盖适用条件、前置条件与操作步骤；标注来源证据；不引入资料外事实",
  "source_docs": [
    {"doc_id": "d1", "title": "Titan 810 安装手册（节选）", "content_path": "sources/titan810-install-excerpt.md"},
    {"doc_id": "d2", "title": "Titan 810 安全公告（节选）", "content_path": "sources/titan810-safety-excerpt.md"}
  ],
  "expected_points": [
    {"id": "p1", "kind": "condition", "label": "适用电池类型/型号范围"},
    {"id": "p2", "kind": "prereq",  "label": "安装前断电/资质要求"},
    {"id": "p3", "kind": "step",    "label": "安装关键步骤与扭矩/力矩值"},
    {"id": "p4", "kind": "value",   "label": "关键参数（电压/温度限值）与来源一致"}
  ],
  "grader_ref": "wiki-default-grader/v1",
  "reference_ref": "grader-only-reference-001",
  "hard_checks": ["no_unauthorized_source", "published_revision_exists", "nonempty_content", "required_headings", "no_version_mixing"]
}
```

`reference_ref` 指向 grader 私有答案文件，只被评分进程读取；执行进程与模型上下文均不接触。训练答案仅训练后可供提议者（阶段 4）；验证/测试答案永不给提议者。

### 6.2 输入 / 输出 / 预期要点

- **输入**：任务 JSON + 固定来源材料（本仓库自建中文演示材料，无私有内容）；物化为实验 DB 中的 Page（固定 doc_id、固定正文、固定 ACL scope）。
- **输出**（实验 DB 内）：run 状态 succeeded；`wiki_pages` 一条 published 候选；`wiki_revisions` 一条 published revision；sections/内容正文；Artifact（decision/manifest）；外加轨迹 jsonl。
- **预期要点**：6.1 中 `expected_points` 为人工可核验清单；评分器输出每点：命中/缺失/证据支持/来源引用（是否可回溯到 d1/d2 指定 doc）。
- **版本化例外**：若来源带版本标记，正文不得把版本参数错误提升为通用（沿 WIKI_SYNTHESIS_VERSIONED_PROMPT 规则，builder:141-166）；与 v1 校验语义一致。

### 6.3 评分维度与权重（未经验证项明确标注）

| 维度 | 实施 | 权重 |
| --- | --- | --- |
| 硬性合法性 | 确定性程序（8.4 验收驱动）：run succeeded、恰好一次发布、evidence/引用有效、无越权、结构合法、无跨版本混写 | 否决项（任一失败 = 任务失败） |
| 内容完整性 | 结构化要点命中（`expected_points` 覆盖）+ 必要 heading 出现 | **待校准**（初始 1.0，冻结在 grader v1，校准前不用于跨实验比较） |
| 内容正确性 | 关键值与结论与来源字段比对（确定性提取关键值）；语义判断单列，不进自动分 | **待校准** |
| 回归 | 基线与候选逐任务对照（同任务历史分数差值） | 不设权重（报告项） |
| 成本 | 调用次数、输入/输出字符（真实 token 用量暂缺，见 §9 未决 U1） | 单列，不混入质量分 |

- `grader_version="wiki-default-grader/v1"` 在阶段 1 冻结并随评分器文件哈希记录；**分数权重未做校准前，不得声称效果提升**；LLM 评审如启用，固定评审模型+提示词+盲评+人工抽样校准并报告误差（方案 §4.3），阶段 1 默认不启用。
- 硬性失败计任务失败；数据损坏/服务不可用单独标记为评估无效，不计入晋升证据。

### 6.4 分组防泄漏

- 按 `group_id`（原始文档家族/主题/产品版本族）分组：同一 doc-family 的全部任务只落在单一 split；划分清单与内容哈希随 dataset_version 冻结。
- 首次规模 20–40 例（train/val/test 建议 6/2/2 起步，按试运行波动再扩），train 同时含成功/失败/边界样例。
- 测试集结果一旦用于修改方案即不再是「未见测试集」，声明相应降级；不复制生产库作测试库；不访问真实外部业务写接口。

## 7. 阶段 1 具体文件与函数变更清单（设计）

新增文件（不修改既有业务代码）：

| 文件 | 内容 |
| --- | --- |
| `backend/eval/wiki_evolution/datasets/wiki-default-v1/*.json` + `sources/*.md` + `split-manifest.json` | 任务与材料、分组与哈希 |
| `backend/app/core/skill_evolution/__init__.py`、`schemas.py` | 任务/数据集/轨迹/评分 DTO（frozen + JSON-safe，风格对齐 wiki_skills/schemas.py） |
| `backend/app/core/skill_evolution/snapshot_store.py` | 快照写/读/封存（追加写、seal hash、路径越界拒绝） |
| `backend/app/core/skill_evolution/task_adapter.py` | `materialize_task(db, task) -> snapshot`（建 Page/Notebook/Workspace/Binding）；`run_task(task, cfg) -> execution`：独立进程内注册 builtins+pipelines→实验 DB init_db→物化→`create_run(…pipeline_version="3"…)`→`execute_run`；**不调用 scheduler/API** |
| `backend/app/core/skill_evolution/trace_store.py` | 从 run/stage/artifact + runner 捕获消息写轨迹（独立 metadata 表 + jsonl） |
| `backend/app/core/skill_evolution/grader_default.py` | `wiki-default-grader/v1` 确定性评分（要点/证据/硬性检查），读 `references/` |
| `backend/app/core/skill_evolution/cli.py` | `init-dataset / run / evaluate / export`（方案 §10 契约子集；先覆盖阶段 1） |
| `backend/tests/test_skill_evolution_phase1_*.py` | 阶段 1 验收测试（见 §8） |
| `docs/wikiskill/implementation-status.md` | 阶段交接记录（随每阶段更新） |

**复用而非复制**：真实编译核心（executor.create_run/execute_run + wiki.default v3 stages + loader/registry/bootstrap 注册段）与测试离线范式；**不建新的"模拟生成函数"**。不在新目录复制整套 builder。

## 8. 阶段 1 验收测试（设计）

确定性测试（风格对齐 test_phase5_equivalence.py / test_wiki_pipeline_7d.py）：

1. 同任务两次运行（固定输入 + 固定 runner）结果 DB 语义快照一致（重跑可复现）；旧快照不被新输入覆盖（seal 后写失败）。
2. 评分参考不进入模型上下文：捕获 runner 收到的全部 messages，断言不含 reference 内容/路径/答案字段。
3. 无生产写入：实验运行期间 `settings.database_url` 未被修改、未调用 scheduler/schedule_* /create_batch_run 的 API 包装；实验 DB 行数/写入范围断言。
4. 分组防泄漏：任一同 `group_id` 任务不跨 split；split-manifest 哈希校验通过。
5. 硬性检查矩阵：缺 heading、空正文、来源外事实注入样例 → 对应任务判失败（而非 run 异常）。
6. mock 与真实模型结果分别标记：`runner_kind=offline|real` 进入轨迹与报告。
7. 关闭功能路径：不注入指令版本时执行 `wiki.default` v3 行为与基线测试一致（阶段 2 门禁预埋：本阶段先固化基线样例）。

## 9. 实施风险与处理（对应方案任务书第 6 点核对）

| 风险 | 核实结论 | 处理建议 |
| --- | --- | --- |
| R1 生产发布耦合 | 编译即自动发布：publish stage 直接写业务 Wiki/Revision/Section（wiki_default.py:1714、v3:1034） | 实验永远运行在独立实验 SQLite；实验进程不启动 worker/pump；CLI 不 import scheduler 入口；入口断言数据库 URL 指向实验文件；实验期可把 kill switch `wiki_pipeline_default_enabled` 关闭作为第二道闸（不依赖它）。 |
| R2 指令未注入 | 见发现 1：instructions.md 未进任何请求 | 阶段 2 先完成 §4.3 注入与「捕获请求断言」；在注入打通前不进入进化（方案 §13 明示）。 |
| R3 旧版本恢复 | 流水线侧已具备（精确版本执行、v1/v2/v3 并存、测试覆盖）；技能/经验侧无 | 技能版本落不可变 DB 行 + 生效指针；回退=改指针，禁止 git reset；在途 run 创建时固化版本集合；候选失败只回退技能选择不删 Raw/经验。 |
| R4 数据库迁移冲突 | head P44 `a9b8c7d6e5f4`；init_db 会 create_all（新 Base 表会出现在任何库）；托管清单守卫 | 阶段 1 不加 Base 表（独立 metadata）；阶段 2 起新表一律 P45 alembic（down=P44）+ MANAGED 清单 + 「upgrade head → init_db 不抛」兼容测试；不向生产库自动执行任何升级。 |
| R5 跨 Workspace 数据访问 | 编译侧：create_run 目标校验/workspace 状态校验（executor.py:422-477、service/routing fail-closed）；读侧：可见性按 acl_scope/组（access_control.py:424-436, 505-660） | 实验数据与业务数据不同库；reference/答案独立文件区不物化进实验 DB；执行者读取面 = 物化器显式注入的 doc_ids；CLI 期无 API 面，后续 API 用现有 can_* 权限。 |
| R6 模型调用预算 | llm_client/call_wiki_llm_json 无 usage/token 回传、无重试退避（单次 httpx，llm_client.py:83-137）；进化循环会放大成本 | 每 run/轮次记录调用次数与字符量；EvolutionRun 加预算与取消字段（阶段 6）；真实 token 计量需扩展客户端或按消息记录估算（U1）；多轮循环在 CLI 内串行、显式 max_iterations/预算，达限按方案状态机 budget_exhausted 收口。 |
| R7 进程级全局注册/runner 污染 | Registry 与 runner 均为进程内全局（executor.py:63-80、registry.py:46-53） | 演化实验在独立进程跑；不内嵌 API 进程；每进程只做一次 bootstrap；实验报告记录注册快照哈希。 |
| R8 依赖重试语义与进化轮次混淆 | 现有 retry/supersede 是任务级故障恢复，非演化重试 | 演化轮次状态机（queued/running/paused/succeeded/failed/cancelled/budget_exhausted，方案 §7.2）新表记录；不把演化步骤混入 KnowledgeCompileRun 的重试语义。 |

## 10. 未决问题

- U1（必要）：模型调用不返回 token usage（llm_client.py / builder:386 均未取 `usage`），真实成本口径需扩展客户端或明确定义估算方法 → 影响成本度量与预算，阶段 1 用调用次数+字符数先行。
- U2（必要，阶段 6+）：真实链路模型凭据与配置（`llm_api_url/llm_api_key/llm_model`）与真实资料使用授权当前不可用/未授权 → 本阶段及后续 mock/离线验收照常，真实链路单列。
- U3（评分器，阶段 1 内决定）：`expected_points` 的命中判定细则与「语义正确性」是否引入固定 LLM 评审（需选定评审模型）——权重校准前所有自动分仅作内部对照，不宣称效果。
- U4（论文对齐）：执行者/提议者模型分工与论文采样的字符预算（方案 §6.2/6.3）需在真实模型确定后按上下文窗口冻结配置。
- U5：api/wiki.py 的 `preview` 等端点已 410 废弃（:579-589），「离线预览候选而不发布」无现成 API 面 → 阶段 1 用实验 DB + publish 落实验库的方式等价满足（不产生业务写）。


---

## 阶段 2 接入点更新（设计修订，2026-09-07，依据 02-skill-versions-and-injection.md）

§4.3 原设计建议在 `wiki_default._llm_runner` / v3 `_pipeline_api_llm_adapter` 两处包裹。
阶段 2 实现核验后改为**单一实验边界注入**，差异与依据：

1. **边界**：`injector.SkillInjectingRunner` 只包在实验执行注入的 ctx runner 最外层
   （configure_external_runners 处），按 CONTENT_CONTEXTS（wiki-synthesis /
   wiki-batch-summary / wiki-mapreduce）过滤；不触碰 wiki_default._llm_runner /
   _default_llm_runner / _pipeline_api_llm_adapter 内部（避免重复包装且无处解析绑定）。
2. **理由**：default 分支所有模型调用都汇入 ctx runner 单点；在函数内部包装需要为
   runner 引入 skill-set 解析，污染生产路径；实验边界 + 显式启用（业务默认关闭）保持
   旧流程不变。
3. **覆盖**：default 内容生成全部上下文经此单点注入；wiki-ingest-page 等主题/技能路由
   调用透传（事件 reason=context_not_content）；API Reference 域在包加载/绑定/冻结层
   直接拒绝（不做静默忽略）。
4. **记录**：注入事件与注入后 model_call 消息成对出现；meta 记录冻结技能集合
   （mode/version_ids/content_hashes/set_hash）。
