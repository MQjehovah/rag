# WikiSkill 阶段 5：候选评估、严格门控与实验技能集合切换（05-evaluation-and-gating）

> 依据总方案 §5.5/§7.1 与阶段 4 报告。完成：最小实验实体（P48）、多技能集合契约、
> 隔离验证集评估（整数主分数）、严格门控（>接受 / ==持平拒绝 / <退化拒绝 /
> invalid 不晋升）、原子指针切换与幂等、skill-impact 只读历史与导出。
> 未实现：多轮自动调度、业务晋升、管理页、真实供应商（流程验证，不声明真实质量提升）。

## 1. 主分数定义与有效性规则

- 主分数 = **通过全部必需检查的任务数 / 验证任务总数**（`passed_over_total_v1`），
  整数比较（避免浮点持平误判）；逐项保留在 per_task（诊断用），不做未校准加权。
- 任务通过定义：执行 succeeded 且已发布 Revision 且 grader 判定 pass（全部硬性+要点检查）。
- 有效性规则：空/重复/非 val 任务清单拒绝；任何基础设施失败
  （`infra_or_model`）→ evaluation `valid=false, invalid_reason=…`（保守，不晋升）；
  数据集/评分器/配置错误直接抛错（无有效分数）；禁止跳任务/缩分母。
- 门控只用 **split=val**；test 不参与候选选择/参数/best_score（阶段 7 独立评测）。

## 2. 多技能集合构造与注入

- `FrozenSkillSet`（阶段 2 扩展）：支持 空/单/多技能；每 skill_id 最多一个版本；
  成员顺序固定（skill_id, seq）；集合哈希 = sha256({"members":[{skill_id,version_id,
  content_hash}]…})；单条 system 消息内按序拼接、每技能注入一次；
  总长 > 40k 明确报错（不截断）。
- 候选集合：create=基础集合新增一个新技能（同名冲突拒绝）；patch=按 proposal 精确
  替换基础集合中的父版本（父不在基础集合 → 拒绝）；其余成员与顺序固定；
  一轮只改一个技能，验证的是完整候选集合；no_action 不建评估、不切集合、不伪造分数。

## 3. 实验实体与作用域

- `evolution_experiments`：experiment_id（区分同 Workspace 不同实验）、workspace/
  domain、dataset_version、grader_version、runner/pipeline/Runtime 版本、
  初始/当前/历史最佳集合、best_score（整数）、baseline/best 评估 id、
  `status_rev`（并发乐观比较）。实验各自维护活动集合，互不覆盖；业务绑定不变。
- 阶段 4 候选须显式关联实验且父版本 ∈ 实验当前集合，否则拒绝
  （CandidateMismatch——不默认为最新集合后直接晋升）。

## 4. 基线与门控实跑结果（wiki-default-v3，同 runner 配置 faithful）

| 步骤 | 得分 | 门控 | 说明 |
| --- | --- | --- | --- |
| baseline（初始集 default:0001） | 0/2 | – | 有效基线，best=0/2 |
| strict 候选（default:0002，含 STRICT-V1 指令） | 1/2 | **accepted**（strict: > best） | best→1/2，rev 2→3 |
| tie 候选（default:0003，内容不同行为相同） | 1/2 | rejected（tie == best） | rev 不变 |
| weak 候选（default:0004，省略条件要点） | 0/2 | rejected（regression < best） | rev 不变 |
| force 候选（default:0005，模拟服务不可用） | 0/2 | **invalid**（基础设施失败） | 不晋升，事件落库 |

每个结果可追溯到 execution_id 与 grader（per_task 记录）；事件/评估存于 P48 表。

## 5. 原子性、幂等与作用域证据

- 接受单事务：校验 status_rev → gate 事件 → 更新 current/best 集合与 best_score →
  rev+1；事件 accepted rev_before→after = n→n+1；reject/invalid 不改集合、rev 不变。
- 重复 gate（同提案再次请求）幂等：先查既有门控事件（by proposal 候选版本）短路，
  不再跑评估、不重复晋升；评估本身按 idempotency_key 幂等。
- 作用域：接受只改目标实验；同 workspace 第二实验保持自身集合；业务绑定（experiment/
  business）前后一致（测试断言）。拒绝后候选版本与经验历史仍在（版本 0003–0005 保留）。

## 6. skill-impact（真实历史只读）

- `read_skill_history`（proposer 工具）现在附带 gate impact 汇总：
  decision/reason/score/best/experiment/dataset/grader/版本——不含验证答案、
  逐任务参考值或私有轨迹（字段白名单测试）。
- `skill-impact.md` / `report.md` 由权威事件导出（非第二写入源）；
  格式非法/评估无效/效果被拒绝分别呈现为 output_invalid / invalid / rejected。

## 7. CLI 示例（已实跑）

```powershell
python -m app.core.skill_evolution.cli gate-experiment --root … --dataset eval/wiki_evolution/datasets/wiki-default-v3 `
      --workspace ws_gate5 --versions default:0001 --val-tasks v3-val-a,v3-val-b
python -m app.core.skill_evolution.cli gate-baseline  --root … --dataset … --experiment exp_9b01… --profile faithful
# baseline score 0/2（有效）
python -m app.core.skill_evolution.cli gate-candidate --root … --dataset … --experiment exp_9b01… --proposal <strict>
# accepted（1/2 > 0/2）
python -m app.core.skill_evolution.cli gate-candidate … --proposal <tie>   # rejected（==）
python -m app.core.skill_evolution.cli gate-candidate … --proposal <weak>  # rejected（<）
python -m app.core.skill_evolution.cli gate-candidate … --proposal <force> # invalid（基础设施）
python -m app.core.skill_evolution.cli gate-status  --root … --workspace ws_gate5
python -m app.core.skill_evolution.cli gate-history --root … --experiment exp_9b01…
python -m app.core.skill_evolution.cli gate-export  --root … --experiment exp_9b01… --out-dir …/report
# → report.md + skill-impact.md（含 accepted/rejected/invalid 事件）
```

门控入口对 test 数据集、空/重复任务清单、配置不一致、无效基线拒绝并给出明确错误。

## 8. 测试命令与实际结果

```powershell
python -m pytest tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py `
                  tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py `
                  tests/test_skill_evolution_stage2.py tests/test_skill_evolution_stage3.py `
                  tests/test_skill_evolution_stage4.py tests/test_skill_evolution_stage5.py `
                  -q -p no:cacheprovider
# 111 passed, 16 warnings in 120.80s（既有回归 + 阶段1–4 + 阶段5 新增 10）
```

阶段 5 验收（第十节 1–13）：空/单/多集合冻结注入与哈希；create/patch 保持其它成员；
基线与候选同任务同配置；提升接受/持平拒绝/退化拒绝/无效不晋升（world5 端到端事件）；
任务缺失/重复/空/非 val 拒绝；父版本不在基础集合拒绝；接受原子且 rev 单调、
重复 gate 幂等（不重复晋升）；拒绝后候选/经验/差异保留；影响摘要字段白名单；
同 workspace 第二实验与业务绑定不变；P44 关闭态与阶段 1–4 回归通过。

## 9. 模拟验证边界（如实）

- 演示/测试全部为 SimulatedModel（同一实现与配置），候选差异来自**注入技能指令
  携带的行为标记**（STRICT-V1 等），非按 baseline/candidate 标签预设分数、非评分
  参考进 runner、非直接写高分；
- 不声明真实质量提升；真实供应商、多轮自动调度、业务晋升、管理页未做。

## 10. 阶段 6 多轮编排所需接口

- 读：`gate.list_experiments/get_experiment/gate_history/get_evaluation`、
  `skill_impact_summary`、`experience_store.*`（提议者读经验）、`trace_sampling`（读轨迹）；
- 写：`evaluate_and_gate`（baseline 初始化 + 候选门控，幂等/rev 冲突保护）、
  `create_experiment`；候选来源 `proposer.run_proposer`；
- 每轮编排：读 best_score/current 集合 → proposer（≥4 轨迹）→ add_version 候选 →
  候选集合构造 → val 评估 → 门控 →（接受时）新 best；预算/取消状态在阶段 6 引入
  EvolutionRun 级调度（不在本阶段实现）。
