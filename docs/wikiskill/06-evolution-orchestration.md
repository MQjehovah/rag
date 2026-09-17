# WikiSkill 阶段 6：多轮进化调度、预算、暂停与恢复（06-evolution-orchestration）

> 依据总方案 §7.2/§7.3 与阶段 5 报告。完成：P49 EvolutionRun/Iteration、把训练→
> 维护→提议→验证门控连接为可持久恢复的自动进化循环、租约/预算/暂停/取消/恢复、
> CLI 与运行报告。未实现：业务晋升、管理页、阶段 7 对照实验、真实供应商端到端
> （模拟闭环通过，**真实模型端到端验证仍待完成**，不声称阶段 6 的真实模型验收）。

## 1. 状态机、步骤与检查点

- 运行状态：queued / running / paused / completed / failed / cancelled /
  budget_exhausted；正常结束原因区分 `max_iterations` 与 `perfect_score`。
- 轮次步骤（检查点指针）：train → maintain → propose → eval(gate) → done；
  每步完成后持久化关联（train_execution_ids / maintenance_run_id / proposal_run_id
  +proposal_id / evaluation_id / gate_event_id）再进入下一步。
- 恢复：中断后从当前步骤重跑——各子模块幂等键保证不重复应用经验补丁、不重复创建
  候选、不重复晋升、不重复追加 gate 事件；已封存且版本一致（技能集合 hash）的训练
  结果被复用；失败/未完成任务以新 execution_id 重做；已接受的恢复正确进入下一轮
  （指针落在已完成轮次后）。
- invalid 评估：默认暂停（reason=eval_invalid，事件已落库）；恢复时该轮直接收尾
  进入下一轮（不无限重跑，也不当作效果拒绝）。

## 2. 初始化模式与经验归属

- `paper`：空技能集合（experiment 初始集合可为空）+ 空经验（显式记录）；
- `business`：显式种子集合 + 显式初始经验快照。
- 配置（train/val 清单、预算、runner、dataset、init_mode）写入运行记录，不按目录
  自动猜测；同一 Workspace 并发运行由“单运行租约 + 实验写入者固定 + 基础状态校验”
  约束，经验不跨实验可变共享（需要分支/快照时显式创建并记录来源）。

## 3. 租约、幂等、预算与取消语义

- 租约：`claim` 原子领取（queued/paused→running + token + 过期时间）；未过期租约
  拒绝第二个 worker；过期后可接管。所有关键写入经 `_fence`（token+过期）校验，
  过期旧 worker 不得回写/晋升；`resume=True` 的领取才清除暂停/取消请求。
- 幂等身份：run/iteration/step/input 派生（如 maintainer/proposer/evaluator 的
  idempotency_extra=`run:iter:step`），恢复不随机生成新键绕开已有记录。
- 预算：max_iterations / max_model_calls / max_tool_calls / max_seconds；维护与提议
  角色及重试全部计入；耗尽 → `budget_exhausted` 独立终态（不伪装成完成/no_action）；
  未知 token usage=null（估算单列）。
- 取消：安全步骤边界生效，保留已提交历史；已接受的实验版本不回滚（明确说明）。
- 暂停：安全边界停止并保留已提交结果；resume 从检查点继续。
- 外部模型调用 exactly-once 不作承诺：只记录调用窗口并把重试计入预算。

## 4. 三轮实跑链路与分数（wiki-default-v3，同一 faithful 模拟执行模型）

| 轮 | 训练 | 维护 | 提议 | 评估/门控 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 1 | 4 条（default:0001） | 建 pattern | patch STRICT-V1 | 1/2 | **accepted**（best 0→1/2） |
| 2 | 4 条（default:0002 注入） | update（追加证据） | patch STRICT-V1-ALT | 1/2 | rejected（tie） |
| 3 | 4 条 | update | no_action（先读门控影响历史） | 无评估 | done（na） |
| – | – | – | – | – | completed（max_iterations） |

第 2 轮真实训练请求使用第 1 轮接受的技能集合（default:0002 注入，轨迹 meta 可查）；
拒绝后集合与经验保持；第 3 轮 no_action 不创建评估；accept 只发生一次
（gate history 单 accepted）。另有测试：满分提前停止（perfect_score，1/1 后不再跑
第 2 轮）、暂停/恢复、取消、租约冲突与过期接管、预算耗尽、paper 空初始化路径。

## 5. CLI 命令与测试结果

```powershell
python -m app.core.skill_evolution.cli evolution-create --root … --dataset … `
      --experiment <exp> --train-tasks v3-train-01,…,v3-train-04 `
      --iterations 3 --init-mode business
python -m app.core.skill_evolution.cli evolution-run --root … --dataset … --run <run_id>
python -m app.core.skill_evolution.cli evolution-status --root … --run <run_id>
python -m app.core.skill_evolution.cli evolution-pause  --root … --run <run_id>
python -m app.core.skill_evolution.cli evolution-cancel --root … --run <run_id>
python -m app.core.skill_evolution.cli evolution-report --root … --dataset … `
      --run <run_id> --out-dir …/reports
# evolution-report.md：iter×步骤×关联 id 串联表
```

测试：
```powershell
python -m pytest tests/test_wiki_pipeline_7d.py … tests/test_skill_evolution_stage6.py -q
# 123 passed, 16 warnings in 190.27s（48 既有编译回归 + 阶段1–5 + 阶段6 新增 12）
```

## 6. 中断恢复证据（测试覆盖）

- 恢复不重复：重复门控不重复晋升、重复评估幂等（stage5 单测）+ 运行级指针推进；
- 暂停请求持久化（get_run.pause_requested=True），resume 清除并续跑至终态；
- 取消：接管后不再安排任何新步骤（current_iteration=0）即 cancelled；
- 租约：未过期双 claim → LeaseConflict；过期接管允许；cancel 在边界生效；
- budget 耗尽终态（used≥上限）且保留已提交轮次；
- 三轮链中每轮恰好 4 条训练执行、1 条 accepted、1 条 rejected、1 条 no_action。

## 7. 外部调用可能重复的已知边界

外部模型响应返回但尚未持久化时无法保证调用严格只执行一次：子模块仅在**成功返回**
后落幂等记录；重试窗口内的调用计入预算并随轨迹记录；报告中不声称外部 exactly-once。

## 8. 真实模型端到端验证的待办条件

- 配置真实 runner（维护/提议/执行三角色）与稳定端点、凭据与成本口径；
- 用同一真实模型+固定数据/评分器跑 ≥3 轮并核对接受/拒绝可复现与预算可控；
- 在真实数据与真实网络下验证租约/心跳/重试窗口（本阶段仅进程内模拟）。

## 9. 阶段 7 对照实验所需接口

- 冻结代码/数据/评分器/模型设置后，用 `orchestrator.execute` 分别驱动四组：
  无技能（FrozenSkillSet.none/empty）、初始技能（business 初始集不进化）、
  无经验 Wiki（去掉 maintain 写经验，仅保留同一轮次训练与提案）、完整 WikiSkill；
- 读出接口：`gate.get_experiment/gate_history/get_evaluation`、
  `skill_impact_summary`、`exp.list_patterns/list_revisions/list_logs`；
- 写接口：`evaluate_and_gate`（基线+候选门控）、`create_run`/`execute(resume)`；
- 结果按任务分组 bootstrap 统计（分组=group_id），成本与调用数单列。
