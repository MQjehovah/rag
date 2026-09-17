# WikiSkill 阶段 1：可重放任务与隔离基线（01-isolated-baseline）

> 依据总方案（Obsidian Vault 知识库搭建）与 `docs/wikiskill/00-design.md`。
> 目标：离线、可重放、隔离的真实 `wiki.default` v3 编译评测环境 + 合成任务集 + 确定性评分 + CLI。
> 本轮实现的是**工程基线**（模拟模型 + 合成资料）；不构成真实模型质量基线，也未验证自进化效果。
> 记录事实：`instructions.md` 仍未注入模型请求（轨迹 meta 明确 `skill_instruction_injected=false`），指令版本化与注入留待阶段 2。

## 1. 实际变更文件与主要接口

新增（未改动任何既有业务模块/表/依赖/运行配置）：

| 文件 | 作用 |
| --- | --- |
| `backend/app/core/skill_evolution/__init__.py` | 包导出与异常 |
| `.../errors.py` | SkillEvolutionError/DatasetError/SnapshotError/TraceError/GraderConfigError |
| `.../contracts.py` | 任务契约 `TaskSpec`（task_id/dataset_version/domain/split/group_id/input_snapshot_id/instruction/grader_version/reference_ref/trigger/wiki/sources）、`DatasetSpec`、加载与校验（必填字段、split、跨 split 分组去重、文件存在性）、`load_reference`（reference 必须位于 dataset/references/ 内） |
| `.../snapshot.py` | `SnapshotStore/SnapshotContent`：保存资料全文+元数据，label 不可覆盖（内容哈希校验）、封存 seal |
| `.../runner.py` | `SimulatedModel`（6 个 profile：faithful/omit_conditions/flatten_versions/quadruple_numerics/unavailable/invalid_json）、`RecordingRunner`（请求/响应/异常/估算用量记录）、`GraphRecorder`（图谱隔离 noop 记录）、估算器 |
| `.../runenv.py` | 实验根品牌与路径边界、实验专用 SQLite engine、`reject_unsafe_db_path`（拒绝已存在库/业务库）、`register_compile_stack`（builtin skills + wiki.default v1/v2/v3 注册） |
| `.../trace.py` | JSONL 事件流 + meta + seal 哈希 + 校验/读取 |
| `.../adapter.py` | `build_snapshot_for_task`、`materialize_experiment`、`run_one`（真实 `executor.create_run(pipeline_version="3")` + `execute_run`，注入 runner，收集 candidate/事件/用量，封存）、`classify_run/classify_failure` |
| `.../grader.py` | 确定性评分器 `wiki-default-grader/v1`：phrase/value/value_in_version/absent_in_version/diff_notice/forbidden，逐项+任务级 verdict，无加权综合分 |
| `.../cli.py` | validate-dataset / run / rerun / show / report / list-profiles |
| `backend/eval/wiki_evolution/datasets/wiki-default-v1/` | dataset.json + sources/（7 份合成资料）+ references/（grader 私有参考答案） |
| `backend/tests/test_skill_evolution_contracts.py`、`test_skill_evolution_phase1.py` | 20 项验收测试 |
| `docs/wikiskill/01-isolated-baseline.md`、`implementation-status.md` | 本报告与交接记录 |

主要接口（供阶段 2 复用）：

- `adapter.run_one(root, dataset, task, *, profile, snapshot=None) -> {execution_id, run_dir, meta, candidate}`：执行一次真实 v3 编译并封存轨迹；返回的 `meta` 为完整封存 meta（task/snapshot/pipeline/model/usage/outcome/stages/候选摘要/seal）。
- `grader.grade(outcome=…, candidate=…, reference_ref=…, dataset_dir=…, task_id=…)`：读 reference（唯一读点）产出逐项检查。
- `runenv.reject_unsafe_db_path(db_path, root)`：路径边界守卫。
- `trace.verify_sealed(run_dir)` / `iter_events(run_dir)`：重放审计读取。

## 2. CLI 命令与实测输出（backend 目录、`.venv` 已激活）

```powershell
# 校验任务集（契约字段/split 分组防泄漏/快照 label-内容一致/参考覆盖）
python -m app.core.skill_evolution.cli validate-dataset --dataset eval/wiki_evolution/datasets/wiki-default-v1
```
输出：
```
dataset_version: wiki-default-v1
domain: wiki_compile.default   grader_version: wiki-default-grader/v1
tasks: 6
  train: 3 tasks, groups=['doc-family-titan810-install', 'doc-family-titan810-safety']
  val: 2 tasks, groups=['doc-family-titan810-v2v3']
  test: 1 tasks, groups=['doc-family-titan810-params']
snapshot labels: 5（内容哈希一致）
OK
```

```powershell
# 使用 fake runner（默认 faithful）执行：train 全部任务
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1 --split train
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-002 --profile omit_conditions
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-005 --profile flatten_versions
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-001 --profile unavailable
```
```powershell
# 查询 / 重放 / 报告
python -m app.core.skill_evolution.cli show --root eval/wiki_evolution/runtime --execution-id <id>
python -m app.core.skill_evolution.cli rerun --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1 --execution-id <id> --profile faithful
python -m app.core.skill_evolution.cli report --root eval/wiki_evolution/runtime --output eval/wiki_evolution/runtime/report.md
```
`rerun` 实测（对 faithful 001 重放）：
```
rerun exec 6d43a323f99a4d599aab0d848bad5298 task=wiki-default-001 (原 execution c9cc26ffc36742d4982248a7bf5b3f46 未改动)
```

## 3. 测试命令与实际结果

```powershell
python -m pytest tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py `
                  tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py `
                  -q -p no:cacheprovider
# 68 passed, 15 warnings in 25.17s
# （48 = 阶段 0 基线回归仍通过；20 = 阶段 1 新增验收）
```

新增验收覆盖（要求 1–10）：真实 v3 阶段链与产物断言、业务库字节不变（settings.database_url 指向业务库的守卫下运行仍不写业务库）、资料篡改后旧快照重放输出一致、同快照重跑独立 execution_id 且各自封存、模型消息不含参考文本、成功/内容失败/模型失败/遗漏四类结果分类、无未注入外部调用（对真实 LLM 客户端与默认 runner 打桩断言零触发；`usage_tokens is None`）、版本归属与 diff_notice、跨 split 分组重复检测、错误库路径/损坏快照/封存失败不静默（单元层）。

## 4. 合成案例逐项评分结果（真实运行）

评分器 `wiki-default-grader/v1`；verdict 只按逐项通过与否，无加权综合分。12 次运行（root 已 gitignore）：

| execution(前12位) | task | profile | run | published | verdict | checks |
| --- | --- | --- | --- | --- | --- | --- |
| c9cc26ffc367 | wiki-default-001（train） | faithful | succeeded | True | pass | 8/8 |
| d7575378e929 | wiki-default-002（train） | faithful | succeeded | True | pass | 8/8 |
| 2e1d66c69aab | wiki-default-003（train） | faithful | succeeded | True | pass | 6/6 |
| 22ef85841ebb | wiki-default-002 | **omit_conditions** | succeeded | True | **fail** | 6/8 |
| 427351c7d121 | wiki-default-003 | **quadruple_numerics** | succeeded | True | **fail** | 3/6 |
| f5c115d2efc0 | wiki-default-004（val） | faithful | succeeded | True | pass | 8/8 |
| e1590f2ebf4e | wiki-default-005（val） | faithful | succeeded | True | pass | 8/8 |
| 1c8b3d0ef5df | wiki-default-005 | **flatten_versions** | succeeded | True | **fail** | 5/8 |
| d3ca520c447c | wiki-default-006（test） | faithful | succeeded | True | pass | 5/5 |
| 2c416fa76434 | wiki-default-001 | **unavailable** | failed | False | fail（infra_or_model） | 3/8 |
| 983a89c2ef8d | wiki-default-001 | **invalid_json** | failed | False | fail（content_compile） | 3/8 |
| 6d43a323f99a | wiki-default-001（rerun） | faithful | succeeded | True | pass | 8/8 |

代表性失败点（确定性检出，非猜测）：

- 002-omit_conditions：`p1/p2 phrase` 失败 —— 正文缺少“适用于 Titan 810 电池模组标准机壳型号”“关闭电源并断开高压回路”（模拟模型遗漏适用/前置条件）。
- 003-quadruple_numerics：`p2/p3 value` 失败 —— 缺 60V、1 MΩ；`forbidden` 命中 `240V`（模拟幻觉数值混入正文）。
- 005-flatten_versions：`absent_in_version` 两次失败 —— 28 N·m 混入 2.0、25 N·m 混入 3.0；`diff_notice` 失败 —— 3.0 同版本 28/32 差异未标注。
- unavailable / invalid_json：真实流水线级失败，code 分别为 `SERVICE_UNAVAILABLE`（分类 infra_or_model）、`INVALID_RESPONSE`（分类 content_compile）；published=False，无半成品候选标成功。

用量记录（示例，每 run 一次模型调用）：faithful 001：`in=734 out=429 est_tokens=582 usage_tokens=None usage_measured=false`；unavailable：`calls=1 err=1 usage_tokens=None`（错误调用不计 token，也非 0）。

## 5. 数据库、文件与外部调用隔离措施

- **数据库**：每 execution 在 `runs/<execution_id>/experiment.db` 全新创建；入口 `ensure_experiment_root` 写入品牌文件，已有目录无品牌即拒绝；`reject_unsafe_db_path` 拒绝已存在库文件与解析后等于 `settings.database_url` 的路径；代码从不读取/回退 `settings.database_url` 建实验库（测试把 settings.database_url 指向业务库后运行，业务库字节不变）。发布产物只落在实验库。
- **文件**：实验根必须显式传入；快照/轨迹/输出全部位于实验根内；reference 只能位于 `dataset/references/`，越界即拒；来源文件解析后必须位于 dataset 内。
- **外部调用**：不启动 worker pump、不调用 scheduler/API；模型调用经 `executor.configure_external_runners` 注入 `RecordingRunner(SimulatedModel)`，图谱阶段注入 `GraphRecorder`（只记录不写，避免 `_default_graph_runner` 按 settings 打开业务库）；任何未注入的默认真实路径被触发会因未配置 LLM 抛 `LLMServiceUnavailable` 或以断言失败显形——测试对 `call_wiki_llm_json`/`_default_llm_runner` 打桩断言零调用。
- **进程**：实验与 API/生产 worker 不同进程（CLI 或独立测试进程）；进程级注册表/runner 只在本进程生效。

## 6. 快照与重放边界

- 快照存**资料全文与元数据**（doc_id/title/content/file/product_version），不是只存哈希；同一 label 内容不一致不可覆盖；封存 seal 校验防篡改。
- 重放语义：从 `root/snapshots/<label>/` 恢复输入（不经 dataset 原始文件）；同一快照重跑生成新 execution_id、新 run 目录、各自 seal，历史不覆盖；实测篡改 dataset 源文件后按快照重放，输出正文与快照哈希与旧执行完全一致。
- “可重放”= 恢复相同输入与配置再次执行并复现确定性结果（真实模型输出不承诺逐字一致）；重跑 runner 确定性（faithful 输出稳定）。
- 执行记录含：任务/数据集/快照、pipeline(runtime)与模型配置标识、`skill_instruction_injected=false`（阶段 1 事实）、每次模型请求与响应/异常（多次调用全部记录，非只最后一次）、阶段结果/错误分类/输出快照/评分、execution_id ↔ CompileRun/StageRun/Artifact（meta 记录 run_id/artifact_types/stages）。事件与 meta 封存后不可追加；无密钥/鉴权头进入记录。

## 7. 尚未验证的真实模型能力（如实说明）

- 真实供应商链路未接通（无凭据/授权），SimulatedModel 为确定性占位——不宣称任何真实模型质量或自进化效果。
- 模型失败只验证到 `SERVICE_UNAVAILABLE`/`INVALID_RESPONSE` 两条注入路径；真实网络错误/重试/超时行为未覆盖。
- token usage 无供应商实测（`usage_tokens=null`），成本口径为估算字符/token。
- 指令注入未实现（阶段 2），本基线无法回答“技能内容是否带来收益”。
- 6 个合成案例仅用于工程验收，不宣称泛化；评分权重未校准，无综合分。

## 8. 阶段 2 指令版本与注入的接入位置

1. **注入点**（00-design §4.3 冻结）：
   - default 分支单一出口：`wiki_pipeline/pipelines/wiki_default.py:_llm_runner(ctx)/_default_llm_runner`（现 :151-165）——包一层 InstructionInjectingRunner：按 run 绑定的指令版本在 `messages` 前插入 system 消息，长度超限 fail closed，同时把最终 messages 写轨迹；
   - api_reference 分支出口：`wiki_skilled_default_v3.py:_pipeline_api_llm_adapter`（:511-541）同法包裹。
2. **指令版本存储与绑定**：新增 DB 表（Alembic P45，down=`a9b8c7d6e5f4` + MANAGED 清单）：不可变 `skill_instruction_versions`（content/content_hash/parent_version/source）与生效指针；builtin `default/instructions.md`（builtin/default/instructions.md）导入为种子 v1；进化只写候选行。
3. **接入验证**：捕获模型请求断言两个指令版本消息确实不同；关闭绑定/缺失版本时回到阶段 1 基线行为（用本报告记录的 12 次运行为回归参照）；实验 execution 创建时固化指令版本哈希进 meta（在 `skill_instruction_version` 字段落地）。
