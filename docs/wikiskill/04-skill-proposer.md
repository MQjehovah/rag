# WikiSkill 阶段 4：技能提议者与候选版本（04-skill-proposer）

> 依据总方案 §5.4/§6.3 与阶段 2/3 报告。完成：数据集 v2 补齐 ≥4 训练任务、
> ProposalRun/Proposal 持久化（P47）、受控多轮工具型 Proposer（create/patch/no_action）、
> 候选不可变版本原子保存、CLI 与审计导出。未实现：验证门控、自动晋升、多轮调度、
> 管理页、真实供应商（不声明真实模型提出有效技能）。

## 1. 数据集补齐方式与版本

- 新增数据集版本 **wiki-default-v2**（未改动 v1）：group
  `doc-family-titan810v2-install` 派生**独立 workspace**（ws_fa3038c24073，与 v1
  ws_c25d0a84d1d8 不同），避免跨数据集经验/轨迹自动混用；含 **4 个不同 train 任务**
  （v2-install-01..04，独立 task_id/instruction/快照 label）+ 独立 val/test 组。
- 演示/实验范围显式记录：proposer 作用域 = workspace+domain+dataset_version；经验与
  轨迹同数据集同作用域；阶段 4 不跨数据集继承经验。
- v2 validate-dataset 通过；演示根 4 条 train 执行全部已封存（校验哈希有效）。

## 2. 实际工具协议、授权与预算边界

- 角色标识 `role=wiki-propose`（独立于执行 Agent 的 wiki-synthesis 注入器，互不套用）。
- 工具集（受控 ID 参数，无路径/shell/DB/网络）：`read_index`、`read_pattern`、
  `read_skill_history`、`read_skill_version`、`read_trace`。
- 授权：全部工具只能读同一 workspace+domain+dataset 的 **train 且已封存**记录；
  val/test、跨 workspace、未授权/损坏轨迹即使 ID 已知也被拒绝（工具与入口双重校验）。
- 读取审计：`tool_events_json` 记录每次 read_trace 的 execution_id、返回长度与截断；
  `read_execution_ids_json` 累计去重；单次读取上限 20k 字符。
- 修改提案（create/patch）提交前要求 **≥4 条不同**成功读取（重复/失败不计数）；
  不足 → `prerequisites_insufficient`（明确前置状态）；no_action 不强制 4 条。
- 预算：max_model_turns=6、max_tool_calls=12、max_repairs=2；超限 → `budget_exhausted`
  （独立于 no_action 与效果拒绝）。模型消息/工具调用记录在 `model_calls_json`，
  usage=null（估算单列）。
- 未知工具 → `[tool_error]` 诊断入对话；非法输出/重复 finish → `output_invalid`；
  超时 → failed（记录原始类型）。

## 3. create / patch / no_action 实跑结果（演示根，dataset v2）

| 演示 | run_id | status | proposal | 候选版本 | reads | model/tool |
| --- | --- | --- | --- | --- | --- | --- |
| patch | propose_6245cf… | candidate_saved | prop_931e… | `default:0002` | 4 | 5 / 4 |
| create | propose_26e77e… | candidate_saved | prop_b6cc… | `default-v2:0001` | 4 | 5 / 4 |
| no_action | propose_151fc4… | no_action | prop_0d0b… | 无 | 0 | 1 / 0 |
| patch 重试 | propose_6245cf… | candidate_saved（idempotent_hit） | 同 prop_931e | `default:0002` | 4 | 0 / 0 |

- patch：基于精确父版本 `default:0001`（父内容哈希校验通过），anchor 唯一命中
  （`8. 参数与结论…` 行）追加核对步骤；父版本正文/哈希不变；PURPOSE.md 未改。
- create：新技能身份 `default-v2`（default 编译领域兼容：domain/runtime_ref 同种子），
  不覆盖既有身份；候选含完整 SKILL.md/PURPOSE.md（来源 manual，未评估）。
- no_action：保存原因与读取记录（0 条），无版本创建、无评估分数。
- **活动绑定不变证据**：三演示前后 `list_bindings()` 一致（0 条，实验/业务均未触碰）；
  候选保存 ≠ 绑定/晋升。

## 4. 候选持久化、幂等与并发

- 复用 `skill_store` 不可变版本（seq 单调、无覆盖）；新增 `add_version_uncommitted`
  供 proposer 与提案/运行行**单事务原子保存**（失败回滚无孤儿候选）。
- 幂等键 = 作用域+dataset+输入集合+配置（不含 base 快照）；重复成功提交命中既有运行
  （实测 patch 重试 idempotent_hit、版本不重复）；失败/无效/预算诊断行用分键存储，
  修复后可重试。
- 并发/分支：按固定父版本生成新 seq 分支，不覆盖他人版本；内容重复可经
  `find_version_by_content` 关联（`duplicate_of_version_id`），重复原因随提案记录。
- 候选从不标记 accepted/rejected：门控结论属阶段 5；输出非法记 `output_invalid` 等
  独立状态。本阶段保存新技能候选**不宣称多技能执行已被支持**（阶段 2 只验证单技能
  注入）——阶段 5 评估候选技能集合前需先扩展注入为多技能集合并做多技能捕获测试。

## 5. 测试命令与实际结果

```powershell
python -m pytest tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py `
                  tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py `
                  tests/test_skill_evolution_stage2.py tests/test_skill_evolution_stage3.py `
                  tests/test_skill_evolution_stage4.py -q -p no:cacheprovider
# 101 passed, 16 warnings in 102.26s
#   = 48（既有编译回归）+20（阶段1）+11（阶段2）+10（阶段3）+12（阶段4）
```

阶段 4 验收覆盖（第九节 1–13）：
1) <4 条不同轨迹不能提交修改（prereq 状态，无候选）；
2) 重复/失败读取不增加有效计数；
3) val/test、跨 workspace、未授权、损坏轨迹拒绝；
4) 未读取证据 / 不在快照的 Pattern 引用拒绝；
5) create 合法生成候选且活动绑定不变、重复提交幂等；
6) patch 从精确父版本生成候选，父版本不变；
7) 补丁失配/歧义/错误父哈希 → output_invalid，无部分写入；
8) no_action 不生成版本；
9) 保存失败（身份冲突/校验失败）无孤儿候选/运行行；
10) 重复提交不重复生成版本（幂等命中）；
11) 未知工具/非法输出/超时/预算耗尽分类正确（tool_error 诊断、TimeoutError→failed、
    bad output→output_invalid、预算→budget_exhausted）；
12) 多轮工具链可审计（5 次模型调用、4 次 read_trace、审计事件 4 条）；
13) 提议运行不产生新编译 run（不触发发布/外部网络）；
14) P47 head 迁移后 require 通过、全部既有回归通过（101）。

## 6. CLI 示例与结果

```powershell
python -m app.core.skill_evolution.cli propose-check --root eval/wiki_evolution/runtime `
      --dataset eval/wiki_evolution/datasets/wiki-default-v2
# PRECHECK_OK（≥4 条不同训练轨迹可提交修改）

python -m app.core.skill_evolution.cli propose-run --root … --dataset … --profile patch
python -m app.core.skill_evolution.cli propose-run --root … --dataset … --profile create
python -m app.core.skill_evolution.cli propose-run --root … --dataset … --profile no_action
python -m app.core.skill_evolution.cli propose-status --root … --run-id propose_6245cf…
python -m app.core.skill_evolution.cli propose-tools --root … --run-id propose_6245cf…
python -m app.core.skill_evolution.cli propose-diff --root … --proposal-id prop_931e0954a534404bab5a
python -m app.core.skill_evolution.cli propose-export --root … --proposal-id prop_931e… `
      --out-dir eval/wiki_evolution/runtime/export-cand
# 导出候选 SKILL.md/PURPOSE.md；文件已存在时拒绝覆盖（不覆盖已有版本）
```

## 7. 尚未验证的真实模型能力

- 真实供应商多轮 ReAct 提议未接通（脚本化 SimulatedProposer 只验证协议/分派/校验/
  存储流程）；不声称真实模型能提出有效技能或带来效果收益；
- 多技能候选集合的执行与效果未验证（阶段 5 前置接入要求见 §4）；
- accepted/rejected 门控结论、效果回写均未实现（阶段 5）。

## 8. 阶段 5 接入所需：候选技能集合、评估与结果回写接口

- 候选技能集合：从 proposals（action create/patch、candidate_version_id、
  duplicate_of）读取候选版本（`skill_store.get_version`），组成实验技能集合并
  扩展 `injector` 为多技能冻结集合（阶段 2 单技能 → 集合），在阶段 1 隔离环境跑
  val/test 执行；
- 评估：复用 grader v1 + 分类（成功/内容失败/基础设施），候选集 vs 基线集逐任务
  对比；
- 结果回写：阶段 5 新增 accepted/rejected 结论实体（不写本阶段 proposals 之外字段），
  绑定切换（experiment kind）原子更新，并记录技能 impact（替代 skill-impact.md
  占位）；提案原因/Pattern/证据直接来自 `evolution_proposals` 与本文档 §2 审计记录。
