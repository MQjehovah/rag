# WikiSkill 阶段 7A：真实模型接入与效果实验准备（07-experiment-protocol）

> 状态标记（如实）：**已实现 / 离线测试通过**；**真实链路未验证 / 效果未验证**。
> 本轮禁止联网真实调用；阶段 6 模拟闭环结果不视为真实模型验收或效果证明。

## 1. 真实运行前的关键语义复核

- **invalid 评估暂停后的 resume 语义（显式记录）**：invalid 评估（事件已落库、
  有效主分数=null）→ 运行暂停 `eval_invalid`；resume 时**跳过该候选进入下一轮**，
  不重跑同一候选、不沿用无效分数、不无限重用 invalid 结果、不重复晋升
  （orchestrator `_run_iteration` 的 EVAL_INVALID 恢复分支；测试
  `test_invalid_resume_advances_not_reeval`）。若未来允许重新评估，须生成可追溯的
  新 attempt（新 evaluation_id）、保留旧记录并把重试计入预算——本阶段未启用。
- **心跳与租约**：`claim` 原子领取（token+过期）；`heartbeat` 只在租约未过期时续期，
  过期后返回 False（旧 worker 不能续命）；过期后新 worker 可接管；旧 token 的所有
  关键写入经 `_fence`/`_commit_guarded` 拒绝（测试 `test_heartbeat_and_old_worker_blocked`；
  阶段 6 另有双 claim LeaseConflict/过期接管/取消边界测试）。
- **预算覆盖**：used_model_calls 现统计 执行（train/eval 轨迹 model_call 事件计数）+
  维护 + 提议（含其工具轮）与重试；usage 不可得 → None/null（不写 0）；
  max_model_calls/max_tool_calls/max_seconds 与单次超时仍执行。

## 2. 真实模型适配（离线，禁止联网调用）

- 复用项目既有模型客户端体系（OpenAI 兼容 messages→JSON；执行走项目严格客户端
  `call_wiki_llm_json`），`real_adapters.py` 提供角色 runner 构造器：
  执行者 / Maintainer / Skill Proposer（多轮工具协议逐轮 JSON）/ 验证执行者
  （同一执行配置）。**不假定 OMP 编码模型即运行时模型；不猜测供应商 model ID**
  （`resolve_real_config` 缺 URL/KEY/MODEL 即报错，且不打印密钥）。
- 显式 simulated / real：real 失败原样上抛，**绝不回退模拟**
  （`test_no_simulated_fallback_on_real_failure`）；记录各角色 model/timeout/
  retries/api_url/api_key_present/prompt 版本；usage_tokens=null。
- 离线契约测试用脱敏 stub：执行契约/参考答案隔离/无 STRICT 行为标记依赖
  （`test_executor_override_contract_and_no_reference_leak`）；真实路径不识别
  STRICT-V1 等标记。执行者仍无法访问经验 Wiki/评分参考/验证测试反馈文件。

## 3. 实验数据集与评分校准流程

- 既有 v1/v2/v3 保持不变（工程回归）；新建 **wiki-default-v4**（`tools/gen_dataset_v4.py`
  确定性生成，22 任务：train 6（单组≥4 供提议者读取）/ val 8 / test 8；
  参数化合成资料：完整性/数值/版本差异/冲突/边界/证据类；组划分防泄漏；
  `manifest.json` 记录生成方式、分组、文件哈希与暴露状态（生成前从未暴露；
  v4 未参与任何调试）。参考答案只在 `references/`（grader 私有）。
- 若后续用测试结果调参必须调用 `mark_test_consumed`（记录该测试集已被消费）。
- 主指标维持 `passed_over_total_v1`；评分器**语义不变**——新增校准案例仅在
  grader-calibration（同义词/关键词堆砌/正确关键词错误结论）中核查行为，
  不依据结果改评分器；如需改语义则新建 grader_version 并在正式实验前冻结。

## 4. 四组冻结协议

| 组 | 说明 | evolve | experience |
| --- | --- | --- | --- |
| A | 无技能（仅任务指令） | 否 | – |
| B | 固定初始技能（不进化） | 否（固定种子） | – |
| C | 多轮提议+门控，但**不维护/不读取持久经验** | 是 | none |
| D | 完整 WikiSkill（持久经验+完整循环） | 是 | full |

- C/D 除 experience 机制外共享初始化/任务/执行模型/提议模型/工具权限/门控/停止规则
  （`default_protocol_config` 保证）；C 保留 非 Wiki 信息（gate 历史/提案历史/技能
  历史）与 D 对齐（`keep_non_wiki_feedback=True`），防止历史摘要成隐藏经验 Wiki。
- paper 模式 C/D 均从空技能+空经验开始；business 另立、不混合统计；A/B 为基线，
  不同初始化的差异不得全部归因于经验 Wiki。每 replicate 独立目录/数据库/产物，
  禁止继承先前演化结果；无随机 seed（如实记录：复制/缓存重放不计独立运行）。
- 每完整组与无经验组各计划 ≥3 次独立运行（RECOMMENDED_RUNS）。

## 5. 实验入口与报告（离线可用）

CLI：`experiment-plan` / `experiment-check` / `experiment-dry` / `experiment-report`
（计划与估算）以及正式可恢复批次入口：
`experiment-batch-create`（校验数据集/grader/种子/隔离路径并写 plan）、
`experiment-batch-run`、`experiment-batch-resume`、`experiment-batch-status`。
协议固定：A 无技能不进化；B 固定可解析初始技能不进化；C 可进化且 experience=none；
D 与 C 除 experience=full 外一致。`build_plan(..., dataset_version)` 写入每个
`protocol.json`。全部 replicate 冻结并校验内容/成员哈希/所属 run 后才统一
`evaluate_batch_test`（v2 缺 reviewer 不回退 v1）。模块另提供 `estimate_budget`
（公式=rounds×（train_tasks + 1 + (1+4) + val_tasks））、`freeze_skill_set`（不可覆盖）、
`evaluate_frozen_test`（**只跑 test split**）、
`paired_bootstrap`（按来源组重采样；组内 replicate 先平均，不把重复运行当独立样本）。

测试成绩不得用于挑选最好随机运行；成本缺失/无效运行如实报告。

## 6. 离线测试结果

```powershell
python -m pytest tests/test_skill_evolution_stage7a.py tests/test_skill_evolution_stage8y_formal_batch.py -q
# 7a 计划/隔离契约 + 8y 正式批次离线入口（本地 stub，非真实效果）
```

## 7. 待办与授权（进入阶段 7B 的最小需求）

真实链路验收待办（下一轮优先）：用真实 runner 跑通执行/维护/提议/验证各一次并核对
结构化输出、工具循环、失败分类、租约心跳与预算计数。

进入阶段 7B 正式付费实验需用户确认的最小授权：
1. 各角色供应商与**准确 model ID**（执行/维护/提议/验证可同或分，须给定）；
2. 凭据配置方式（环境变量/密钥文件；不要求在聊天中提供密钥值）；
3. 可发送给供应商的数据范围（仅自建合成资料 v4；生产资料需另行授权）；
4. 试运行与正式实验各自的费用/调用/时间上限；
5. 业务改善与关键回归的验收标准。

预算估算方式（无价格时金额=NULL，不编造）：按 任务数×轮数×角色 的调用估算
（见 `estimate_budget` 输出公式），拿到有效单价后可由调用数换算；当前
token usage 不可得，一律 null/unknown。


## 8. 阶段 7A 前置补审结论（2026-09-07 补）

状态：**已实现 / 离线测试通过；真实链路与效果仍未验证**。

1) **长调用心跳**（orchestrator `LongCallRenewer`）：真实调用等待期间由守护线程按
   interval 调 `heartbeat` 续租，正常 worker 不被接管；`stop()`/租约丢失线程自动退出
   （资源释放）。证据：`tests/test_skill_evolution_stage7b.py` 中
   `test_renewer_keeps_lease_blocks_takeover_then_release`（续租期竞争 claim 抛
   LeaseConflict；停续租显式过期后新 worker 接管；旧 token 写终态被拒；stop 后线程
   非存活）、`test_renewer_gives_up_on_lost_lease`。调用链：
   execute→claim→LongCallRenewer(heartbeat 每 ≤5s)→步内长模型调用期间持续续租；
   异常/return 经 finally renewer.stop()。
2) **预算发送前拦截**（`BudgetGuard` 包真实 runner）：reserve 在发送前落库在途标记；
   超上限/剩余时间≤0 直接抛 BudgetExceeded，请求不发出；成功/明确失败 finish 计数，
   异常或崩溃保留“model-unknown”占用（保守“已预留/状态未知”），恢复读 DB 不丢；
   单请求 timeout 截断到剩余时间；内部重试每次调用都走同一 envelope，不能绕过计数。
   **粒度说明（如实）**：硬保证是“每个真实出站请求发送前检查且绝不超发”（测试
   `test_preflight_stops_mid_multi_task_before_overspend`：cap=2 时 stub 仅被调用 2 次，
   后续全部发送前拦截，恢复后立即 budget_exhausted 无新发送）；被拦请求在适配层降级
   为 infra 事件并 pause（eval_invalid），属预期语义而非执行后补算。多阶段编译内部
   请求数不在发送前可知，列入 unknown。模型调用上限与货币费用上限分开：usage/价格
   不可得时 cost=None，不宣称可控费用（`estimate_budget.unknown`）。
3) **数据分组/暴露（措辞修正）**：wiki-default-v4 由单一参数化模板族合成（每组每任务
   独立文件、内容互不相同、无跨集合复用——测试逐任务文本唯一）；train 1 组
   doc-family-v4e-train（6 互异任务 ≥4 满足提议者读取）→“6 单组≥4”= 一个互异来源组内
   6 个任务。局限：同族参数化、非生产文档，不得据此宣称广泛业务收益。
   “never exposed”修正为可证明范围：**该目录生成后未进入过任何执行/维护/提议/验证
   角色上下文、未用于阶段 0–6 调试或回归（manifest 记录）**；无法证明外部人员/其他
   生成模型查看过文件本身。
4) **反向隔离与冻结门**：维护/提议上下文仅挂 train 执行（`test_proposer_and_...
   _never_see_test_tasks`：config.train_task_ids 与 test 无交集）；参考答案仅 grader
   读取；验证反馈按冻结契约开放。最终测试前必须整批冻结：`experiment7.ensure_all_frozen`
   （测试：A 冻结后仍缺 B/C/D 即拒绝），不能先看一组成绩再调另一组。
5) **C 组可运行**：experience=none 时 orchestrator 跳过 MAINTAIN 步、不写/不读持久
   Pattern；候选/技能提议不强制 Pattern 引用（pattern_ids 可空，无伪造绕过）。
   `test_c_protocol_two_rounds_without_patterns`：两轮真实提议+门控完成、
   maintenance_run_id=None、Pattern 0 条、gate 历史保留（与 D 的非 Wiki 反馈权限一致）；
   `test_d_protocol_uses_previous_round_experience`：D 两轮后存在持久 Pattern。
6) 新增 P50 迁移（evolution_runs.reserved_in_flight_json）；实验/回归库均离线，无生产
   迁移/绑定变更。

## 8.5 试运行入口与离线验收（trial-real-d，2026-09-07）

- CLI：`trial-real-d preflight|create|run`（`cli.py`）。preflight 不发请求（无联网探测）；
  create 建独立实验+run 并打印 `run_id`；run/resume 复用同一 run（`resume=True` 清
  暂停标志），预算存 run config_json 持久化，恢复不重置 used、不建替代实验；
  禁止 test 评估/生产绑定变更/自动切换供应商/模型/模拟。
- fail-closed：real 模式 executor/maintainer/proposer 任一缺配置 → 在首请求前报错
  （resolve_real_config 逐角色校验；EVAL 复用 executor）。orchestrator 已接
  maintainer_runner/executor_runner/proposer_factory 三路真实 runner，均经 BudgetGuard。
- 输出限制贯通：`max_output_tokens`（可选）→ ModelConfig → runner →
  `call_wiki_llm_json(max_output_tokens=…)` → 载荷 OpenAI 兼容字段 `max_tokens`
  （默认不发，保持既有行为）；供应商拒绝（HTTP 4xx）→ LLMServiceUnavailable 明确失败，
  不静默移除后继续发送。真实 runner 在既有事件循环内改用线程执行协程（防嵌套
  asyncio.run）。usage 保留策略：客户端不回传则记录 null，不为试运行开发计费系统。
- 停止语义：预算耗尽 → 外层终态 RUN_BUDGET_EXHAUSTED、stop_reason 前缀
  budget_exhausted（内部 BudgetExceeded 转 infra 时不再只留 eval_invalid）；
  租约丢失后旧 worker 不得发起新请求（guard `_check_lease`）。
- 语义：no_action 是正常提议结果（不视为失败/不提前停）；基线满分可正常提前结束；
  completed 与链路覆盖分开报告——无真实候选验证则该段明确标记“未验证”，不伪造候选。
- 预算：90 请求/3600s/≤3 轮与 timeout=60s 是**待确认建议默认值，非已授权**，也不保证
  完成全部轮次；估算≠硬上限（一次编译可能多次请求）；费用无法可靠估算且货币金额
  不可严格自动停止 → 建议供应商侧额度/告警（费用告警不是硬上限）。
- 离线验收：`tests/test_skill_evolution_stage7c.py`（3 passed，HTTP stub 非真实调用）：
  preflight 零请求/密钥不泄漏/角色 fail-closed；create+run+resume 同 run、预算与实验
  不重置不新增、used_model_calls==HTTP 请求数（guard 1:1）、executor/maintainer/
  proposer 各有真实请求、模拟实现被调用即抛错；实际载荷含 max_tokens、httpx timeout、
  4xx 拒绝明确失败、默认载荷无 max_tokens。

## 9. 真实链路试运行授权配置模板（待确认，不填猜测值/不输出密钥）

### 9.0 配置可用性核查（2026-09-07，只读 .env，未打印/持久化密钥）
- 加载方式：`app/config.py`（pydantic BaseSettings，`env_file=".env"`）字段
  `llm_api_url/llm_api_key/llm_model`；真实角色解析 `real_adapters.resolve_real_config(role,
  override)`：显式 override 优先，否则沿用这三个 settings；无 per-role 独立字段。
- 凭据状态（布尔，见下方最终交付）：backend/.env 存在；llm_api_url/key/model 均已配置。
- 供应商/模型（来自现有配置的非密钥项）：host `open.bigmodel.cn`（智谱 BigModel，
  OpenAI 兼容），model `glm-5.1`；四个角色当前共用同一组设置（EVAL 复用 executor 通道）。
- 客户端与预算覆盖：`wiki_page_builder.call_wiki_llm_json`（L386-432）= 每次 runner 调用
  恰好 1 次 `httpx.AsyncClient(timeout=timeout).post`，载荷仅 model/messages/stream，
  **无内部 HTTP 重试、不发送 max_tokens**；`BudgetGuard.wrap` 位于真实 runner 边界
  （orchestrator 对 executor/maintainer/proposer 均 wrap），故“每次 runner 调用=1 HTTP=
  每次发送前检查+计数”，重试（orchestrator attempts≤2、proposer max_repairs/max_turns、
  maintainer 单次）每尝试都过 envelope；多阶段内部请求数与 max_tokens 缺口见 §8-2/§9.1。
- 工具上限粒度：仅 proposer 引擎存在工具调用 —— 每次 proposer 运行内 max_tool_calls
  （引擎逐轮计数）+ 步前全局 guard 检查（used_tool 上限），非逐工具 HTTP 前置。

- 角色模型（各角色可同可分，需给出**准确 model ID**，禁止猜测/自选默认）：
  执行者 EXEC_MODEL（供应商/ID）、Maintainer MAINT_MODEL、提议者 PROPOSER_MODEL、
  验证执行者 EVAL_MODEL（默认=EXEC_MODEL，若分开须授权）。
- 凭据环境变量名：如 {ROLE}_API_URL / {ROLE}_API_KEY / {ROLE}_MODEL
  （或统一 LLM_API_* 单组），密钥只经环境变量注入，绝不入聊天/日志/记录。
- 允许发送的数据范围：默认仅 wiki-default-v4 合成资料；生产/私有文档须另授权。
- 最大出站请求数、最长时间：由 run budget 给出（max_model_calls/max_tool_calls/
  max_seconds，dry-run 预估值与上限分开展示）；单请求 timeout、重试次数与 max
  output_tokens（supplier 侧 max_tokens）须授权值。
- 费用估算依据：无单价/usage → cost=NULL（未知项列出：token_usage、cost、真实失败
  重试、逐任务内部请求数）；若有单价授权则按 estimate_budget.parts×单价估算。
- 计划端到端步骤与停止条件：1) 每个角色真实调用各一次核对结构化输出/工具循环/失败
  分类/租约/预算计数（budget 上限=试运行值）→ 2) C/D 各 1 次真实单轮冒烟 → 3) 试运行
  batch（A/B/C/D 各 1 run，val 门控+冻结）→ 4) 正式对照（C/D ≥3 独立 run，整批冻结后
  才 evaluate_frozen_test）→ 5) 停止条件：任一 run 达预算/时间上限、真实 API 连续
  失败超阈值、或费用估算超授权值即中止并回报。每步前须本授权单。
