# WikiSkill implementation-status（阶段交接记录）

## 阶段进度

- [x] 阶段 0–6（00–06 docs）
- [x] 阶段 7A：真实模型接入与效果实验准备（07-experiment-protocol.md，本文）
- [x] 正式实验数据集准备（wiki-default-v5 + 评分校准；本文）
- [x] Grader v2 设计与离线校准准备（08-grader-v2-design.md；原型不默认/不接门控）
- [x] 阶段 8A：只读实验管理入口（08a-readonly-console.md；只读，不涉效果验收）
- [ ] 阶段 7B：正式付费对照实验（等待授权；同时受模型可用性与评分可信度阻塞）
- [x] 阶段 8B–8E：工程功能交付（08b/08c/08d/08e；工程+模拟/隔离验收；真实链路与生产启用仍待授权）
- [ ] 阶段 8F（后续）：真实链路执行与正式效果/生产验收（阻塞：模型授权+人工标签）

## 本轮（7A）范围与结论

状态标记：**已实现 / 离线测试通过；真实链路未验证 / 效果未验证**（不把阶段 6 模拟
结果当作真实模型验收或效果证明）。

- 语义复核：invalid 评估暂停→resume **跳过进入下一轮**（显式记录，不重跑/不沿用
  无效分数/不无限重用，重试须新 attempt——未启用）；heartbeat 过期即失效、旧 token
  写入被拒；预算现覆盖执行（事件计数）+维护+提议（工具轮）与重试；usage 未知=null。
- 真实适配：`real_adapters.py`（执行/维护/提议/验证角色构造器，复用项目客户端体系，
  不猜 model ID，显式 real 不回退模拟、缺配置报错无密钥）；参数贯通
  adapter/gating/orchestrator（`llm_runner_override`/`executor_runner`）。
- 数据：新建 **wiki-default-v4**（22 任务：train 6 单组≥4 / val 8 / test 8，合成资料，
  manifest 记录生成/分组/哈希/暴露），v1/v2/v3 保持工程回归。
- 协议与统计：experiment7（四组 C/D 对齐、隔离 replicate 目录、dry-run 估算
  cost=null、freeze 不可覆盖、evaluate_frozen_test 只跑 test、paired_bootstrap 组内
  先平均）；CLI experiment-plan/check/dry/report。
- 校准：评分器语义不变；校准案例行为核查；如改语义须新 grader_version 并在正式实验
  前冻结；测试集被调参必须 mark_test_consumed。

## 核查基线（2026-09-07）

| 项 | 值 |
| --- | --- |
| 分支/HEAD | `feature/dingtalk-to-markdown` @ `e42b056`（未变） |
| Alembic head | P51 `d974815b4a91`（8D 新增审计表迁移，单头；未在生产库执行） |
| 数据库 | 隔离实验库；未迁移生产库、未改业务绑定、未启动生产 worker |
| 数据集 | v1/v2/v3（回归）+ v4（7A 实验） |

## 变更文件

新增：`real_adapters.py`、`experiment7.py`、`tools/gen_dataset_v4.py`、
`eval/wiki_evolution/datasets/wiki-default-v4/`（生成产物+manifest）、
`tests/test_skill_evolution_stage7a.py`（11 项）、`docs/wikiskill/07-experiment-protocol.md`。
修改：`adapter.py`（llm_runner_override）、`gating.py`（executor_runner 贯通）、
`orchestrator.py`（heartbeat 过期失效、执行模型调用预算、resume 语义注释）、
`cli.py`（experiment-*）、`implementation-status.md`。根 `.gitignore` 沿用。

## 测试结果（离线）

```powershell
python -m pytest tests/test_skill_evolution_stage7a.py -q   # 11 passed
python -m pytest …（阶段1–6 + 7A 全量）                        # 见下方全量
# 全量：阶段 1–6 与 7A 全部通过（执行时以实际输出为准，见交接命令）
```

关键离线覆盖：真实适配契约/参考答案隔离/无标记依赖、无模拟回退、配置校验无密钥、
心跳与旧 worker 拒写、invalid resume 前进不重评、执行预算计数、v4 结构与隔离、
四组协议配置、测试集不参与回路、配对 bootstrap 重复样本处理、成本未知=null。

## v5 正式实验数据集与校准（2026-09-07，离线；无模型/演化/评测）

- v1–v4 未改动。新增 **wiki-default-v5**（`tools/gen_dataset_v5.py`，确定性再生）：
  39 任务 = train 10（3 组、3 家族；cfg-t1 6 任务满足提议者 ≥4 读取）/ val 14
  （6 组、5 家族）/ test 15（5 组、5 家族）；五个来源家族——操作流程 / 配置约束 /
  版本迁移 / 故障排查 / 多资料冲突与适用范围（conf 为双资料任务）。素材全为自编
  合成（无生产/私有资料）；组→split 单射，跨集合近重复按文本相似度门禁拦截；
  manifest 记录家族、组/split、内容哈希与暴露（编写者本人看过；未进入任何角色
  上下文；不声称他人从未查看）。
- 评分校准（`tools/grade_calibration_v5.py`，未改动冻结评分器 v1）：六项人工可核验
  探针 → `docs/wikiskill/calibration-v5.md`、`eval/wiki_evolution/calibration/
  calibration-v5.json`。能区分：版本混淆、关键内容遗漏；当前不能区分（误通过/漏报）：
  合理同义表达、关键词齐全但结论错误、无证据自由补充、适用范围张冠李戴。
  语义升级需另立 grader_version（不覆盖 v1）——待人工确认。
- 测试：`tests/test_skill_evolution_stage7d.py`（2 passed：结构与暴露/哈希、校准边界）。
- 状态：D 组真实链路**待验证**；四组效果实验**未执行**；本轮未运行任何模型生成、
  技能演化或最终测试评测，未依任何成绩调参。

## Grader v2 纠偏（prototype-2，2026-09-07，离线）

- 澄清校准：逐例双列（v1 模拟 vs v2）见 `docs/wikiskill/review-grader-v2.md`；
  v1 误通过 7 / 误拒绝 2（缺陷复现归因 v1），v2 待审 10 / 正确拒绝 2，
  不再用一致率暗示准确度。
- 保守聚合契约落地（grader_v2.py prototype-2）：pass/fail/needs_review + invalid；
  任一必需 fail → 任务 fail；未决必需项 → needs_review，不晋升、不缩分母；
  C 级经通用 resolutions 解析，实现无样例特判。
- 表述收紧：数值/单位只证等价、同参数异条件多值不自动矛盾、覆盖提及≠覆盖正确、
  未提及转待审；新增反例测试。
- 结构化声明仅作设计记录（执行产物契约不变）。
- 人工审阅材料：`docs/wikiskill/review-grader-v2.md`；验收样例
  `v2-acceptance-review.json`（A1–A5，pending，未宣称已验收）。
- 测试：`tests/test_skill_evolution_stage7e.py` 6 passed（仅本文件复跑；
  历史回归**未复跑**）。v5 数据集未改动；同类型/unseen-family 分层 + 预定义总体
  主指标，不事后剔除任务。

## Grader v2（2026-09-07，仅设计+原型+校准，离线）

- 阻塞明确：v1 评分器对 同义误拒绝 / 关键词齐全但结论错误误通过 / 无依据新增事实
  不可识别 / 数值齐全但适用范围错误误通过；**v1 高分不得直接解释为质量提升**。
- 产出：`docs/wikiskill/08-grader-v2-design.md`（v2 结论-证据契约、A/B/C 可实现分级、
  模型评审适配器设计、最小实现路线、人工确认清单、v5 数据建议——推荐方案 B：保留
  ts/conf 为未见类型单独报告跨类型泛化，不挪用 v5 val/test）。
- 原型：`app/core/skill_evolution/grader_v2.py`（v2-prototype-1；单位归一化数值断言、
  版本区段归属/缺失、同参数多值矛盾、覆盖短语；C 级一律 needs_review，不静默回退；
  未接线默认评分器/门控——stage7e 断言 gating/orchestrator/adapter 无原型引用）。
- 独立校准开发集 `v2-dev.json`（12 例，预期先定，暴露已记；未用 v5 test 内容迭代）+
  `v2-dev-results.json`（12/12 match；误通过/误拒绝为有意暴露）+ `v2-human-review.json`
  （待人工骨架：S1/S3/S4/S7/S8/S10）。
- 测试：`tests/test_skill_evolution_stage7e.py` 4 passed。未运行任何模型/演化/效果实验；
  未切换评分器、未修改技能。
- 状态：D 组真实链路**待验证**；四组效果实验**同时受模型可用性与评分可信度阻塞**。

## 语义评审接口与审计（2026-09-07，离线；不接门控）

- review_store.py：绑定哈希判定（内容变更旧判定失效）、追加式审计、human 需人工
  标识、model 需 model_ref+prompt_version；判定不流入执行/提议通道。
- model_review.py：可插拔 SemanticReviewer 接口 + stub（仅测试）；配置缺失 fail
  closed，未确认 model_id 不指定、无真实请求；非法结构/服务失败不产生通过。
- v1 证据修正：开发集改调**真实 v1 引擎**（参考=v1-probe-references 代理参考，
  限制逐行标注），不再用“模拟”冒充实测；指标含 正确/误通过/误拒绝/待审率/故障率。
- 验收集 A1–A5 保持 pending，未用于开发。
- 测试：stage7e+7f 共 11 passed（本轮仅复跑这两个文件；历史回归未复跑）。
- 仍未完成：人工标签确认、真实评审校准、v2 门控接入、D 组真实链路与效果实验。

## 阶段 8A（只读控制台，2026-09-07）

- 后端 `app/api/evolution_console.py`（管理员+JWT、服务端根映射、sqlite3 mode=ro、
  无建库副作用、无模型请求、无写操作）+ 前端 `/admin/evolution` 只读页（状态分流、
  simulated/real、未知用量非 0、分页、按需轨迹、插值渲染防脚本）。
- 后端验收 `tests/test_skill_evolution_stage8.py` 5 passed；前端 `npm run build`
  已通过；浏览器验收已执行（headless，隔离临时环境）：未登录→login、非管理员→403、
  功能未启用/不可用提示、数据列表、详情 drawer（real/12-90/未知费用/未验证横幅）、
  轨迹弹窗、刷新重载、控制台无错误；未执行：空数据/请求失败 UI 分支与浏览器内
  XSS 注入（如实注明）。只读边界加固（轨迹名白名单+拒绝符号链接+根内包含校验、
  返回字段白名单脱敏）与正文差异未实现标记已加入；临时环境与 token 已删除。
- 本阶段不代表效果验收：阶段 7、Grader v2、生产晋升保持待验收。

## 关键未决问题 / 下一阶段（7B 前置）

1. 真实链路验收（下一轮优先）：真实 runner 各角色各一次，核对结构化输出、工具循环、
   失败分类、心跳/租约与预算计数（试运行授权模板见 07 文档 §9）；
2. 7B 授权清单见 07 文档 §7/§9（供应商与准确 model ID、凭据环境变量名、数据范围、
   费用/调用/时间上限、验收标准）；
3. 未解决/如实说明项：多阶段编译“逐任务内部真实请求数”与 usage/价格不可得
   （费用上限不可控声明，见 07 §8-2）；v4 “never exposed”限定为可证明范围
   （未进入任何角色上下文/调试；不证明外部人员查看，见 07 §8-3）；C 组被拦
   请求降级 pause(eval_invalid) 的语义属预期（07 §8-2）；
4. 阶段 8 管理入口未开始。

## 试运行准备（2026-09-07，本轮仅核查/计划/修正，未执行真实调用）

- 配置可用性：见 07 §9.0（智谱 BigModel glm-5.1，四角色共用；凭据已配置布尔 true）。
- §4 停止语义修正：orchestrator BudgetGuard 增加 budget_exhausted 标志与租约属主
  `_check_lease`（旧 worker token 被接管/过期 → reserve 抛 LeaseConflict，不发起新请求）；
  maintain/propose/eval/baseline 的 pause 路径在预算耗尽时改抛 BudgetExceeded →
  外层终态 RUN_BUDGET_EXHAUSTED、stop_reason 前缀 budget_exhausted（不再只留
  eval_invalid）；Actors 增加 maintainer_runner（真实 maintainer 经 guard.wrap 注入，
  与 executor/proposer 同一 envelope 覆盖）；summary 双计抑制。
- 离线测试：`tests/test_skill_evolution_stage7b.py` 增/改
  `test_preflight_stops_mid_multi_task_before_overspend`（预算耗尽显式终态、无新发送）、
  `test_old_worker_lease_lost_blocks_new_requests`（接管后旧 guard 拒绝新请求）；
  stage6/7a/7b 回归通过（23+11）。全量结果以最终交付为准。
- 待确认（用户输入）：角色是否拆分模型、max_output_tokens 值（客户端现已贯通，默认
  不发）、预算建议值（90/3600/≤3 仅为建议默认，未授权）、供应商侧额度/费用上限、
  数据范围确认（仅 v4）。
- 入口/验收（07 §8.5）：trial-real-d preflight|create|run 已实现并离线验收
  （stage7c 3 passed；HTTP stub，非真实调用）；客户端 max_output_tokens → 载荷
  max_tokens 贯通且默认行为不变；真实 runner 线程执行协程。

- 新测试文件 `tests/test_skill_evolution_stage7b.py`（10 passed）：续租/接管/停止释放、
  发送前预算与在途持久、时间截断、多任务小预算不超发、v4 组与暴露复核、整批冻结门、
  C 两轮无 Pattern 演化、D 经验利用。
- orchestrator：LongCallRenewer、BudgetGuard、experience=none 跳过维护；
  models/evolution.py + P50 迁移 `7a7a0000c0de`（reserved_in_flight_json）。
- 回归：7A/7B 合计 21 passed；阶段 1–7B 全量见交接命令输出（134+10）。


## 阶段 8A 收尾状态（2026-09-07，只读控制台，不再新增功能）

### 已完成的只读能力与实际验收证据
- 后端 `app/api/evolution_console.py`（管理员+JWT、服务端根映射、sqlite3 mode=ro、
  无建库/迁移副作用、无模型请求、无写操作）+ 前端 `/admin/evolution` 只读页。
- 后端验收 `tests/test_skill_evolution_stage8.py` **5 passed**；前端 `npm run build`
  通过；浏览器验收（headless，隔离临时环境）已执行：未登录→/login、非管理员→/403、
  功能未启用/实验库不可用提示、正常数据列表、详情 drawer（real 徽标/12-90 已用上限/
  token 与费用“未知（不显示为 0）”/未验证横幅）、轨迹弹窗、刷新重载、控制台无错误。
- 只读边界：轨迹目录名白名单 + 拒绝符号链接 + 根内包含校验；返回字段显式白名单并
  脱敏（注入 sk-*/正文/<script> 均不透出）；读取前后库哈希不变。
- 说明：合成/测试记录中的 `real` 字段只表示“该运行按 real 模式编排”，**不构成任何
  真实模型调用证据**；真实调用证据只能来自真实链路的实际出站请求与记录。

### 尚未完成
- 浏览器“空数据”与“请求失败”展示分支未在浏览器触发验证（如实注明）。
- “恶意 HTML 按文字显示不执行”以无 v-html 代码审计与后端白名单/脱敏测试为证据，
  未做浏览器内 XSS 注入验证。
- **技能正文差异展示未实现**：仅交付 diff 元数据（版本/哈希/patch 长度/action），
  页面与接口明确 `content_diff_supported=false`，不得宣称已支持正文对比。

### 待办（保持待验收，未自动恢复/执行）
- 阶段 7：四组效果实验未执行（阻塞：模型可用性 + 评分可信度）。
- Grader v2：原型与评审接口离线就绪；人工标签确认、真实评审校准、门控接入未完成。
- 真实链路：D 组未验证；效果未验证。
- 生产晋升：未开始，任何只读页面完成不构成效果达标。
- 模型恢复后的 D 组试运行入口：`trial-real-d preflight|create|run`
  （backend 目录、见 07 §8.5/§9）已就绪且离线验收；**待确认预算**：
  max_model_calls=90 / max_tool_calls=40 / max_seconds=3600 / ≤3 轮 / timeout=60 s /
  max_output_tokens（客户端已贯通，默认不发送）——以上均为建议默认值，未经授权不执行。

### 保留说明
代码与文档全部保留；未提交、未清理/删除项目文件（仅删除本轮浏览器验收用临时
data/dev8a 目录与其内 token 文件）。无模型请求、未自动恢复实验。停止。

## 阶段 8B–8E 交付（2026-09-07，工程收尾轮）

### 进度
- [x] 8B 技能正文与差异（08b）— 后端 + 前端 + stage8b 9 passed（stage8 同步 5 passed）
- [x] 8C 运行控制（08c）— control/evolution_admin + stage8c 9 passed（含真实子进程
      worker 浏览器验证：启动→completed 持久化）
- [x] 8D 晋升/回退 + 编译绑定读取（08d）— business_ops + executor 注入 + P51 迁移 +
      stage8d 8 passed（捕获编译请求验证新/旧指令版本）
- [x] 8E 工程交付（08e）— 文档 + 回归 + 构建 + 浏览器隔离验收

### 变更文件
- 新增：app/api/evolution_admin.py、app/core/skill_evolution/control.py、
  business_ops.py、alembic/versions/d974815b4a91_p51_business_events.py、
  tests/test_skill_evolution_stage8{b,c,d}.py、api/evolutionAdmin.ts、
  docs/wikiskill/08{b,c,d,e}-*.md
- 修改：config.py（admin/real/compile 三开关）、main.py（admin router）、
  evolution_console.py（正文/差异/版本目录端点 + diff_supported）、
  executor.py（编译绑定注入钩子）、models/evolution.py（business events 表）、
  EvolutionConsole.vue（正文/差异/运行控制/晋升回退 UI）、
  implementation-status.md；stage8 测试断言同步。
- 迁移：P51 d974815b4a91 单头；生产库未执行。跟踪改动仍仅 .gitignore/config/main/
  wiki_page_builder/router（其余未跟踪保留）。

### 测试（实际执行）
- 演化全量 144 passed：contracts/phase1/stage2–7f/stage8/8b/8c/8d
  （命令见 8e 文档 §7；约 3m40s）。
- 编译管线 25 + 32 passed（wiki_pipeline_7d / wiki_skill_default_v3 / equivalence）。
- 前端 npm run build 通过；浏览器隔离验收（data/dev8e 临时环境，已删除含 token）：
  新建实验对话框/真实选项灰显、模拟 create→start(子进程)→completed、晋升默认阻止
  模拟提示、缺 schema 回退“不自动迁移”提示；零未处理 pageerror。

### 遗留与如实标注
- real run 启动需授权开关 + 配置可解析（未授权）；真实链路未验证。
- Grader v2 未接门控（未注册版本自动评分被拒，不静默回退）；人工标签未填。
- 效果实验未执行；生产启用未授权未执行；PG 业务接入未验收（无环境，不以 SQLite
  结果代替）；正文语义对比未实现（diff 为文本 unified diff）。
- 模拟/合成记录的 real 字段只表示“按 real 模式编排”，不构成真实调用证据。

## 工程完成性核查轮（本会话末，问题定位+必要补缺；未新增需求）

### 1) 真实执行链路：已接通（HTTP stub 全链验证），真实供应商调用仍=等环境验证
- 补缺：worker 侧 `build_run_actors`（control.py）按 run 记录构造真实
  executor/maintainer/proposer 适配器；CLI `evolution-run` 同路径；真实模式在 worker
  侧二次把关（授权缺失/配置不可用 → 拒绝，绝不静默回退模拟）。
- 验证 `tests/test_skill_evolution_stage8e_chain.py`（1 passed）：管理员 create(real)→
  start（未确认 409、参数不一致 409、正确确认 200）→ 线程 worker 走 orchestrator →
  真实适配器把请求打到本地 HTTP stub（Authorization/model 正确、used≥1）；模拟
  SimulatedModel 被哨兵改成实例化即抛错（证明未走模拟）；run 收敛。全程离线。
- 之前文档“真实执行未实现/未接通”为**未实际调用**而非代码缺失——现文档改为
  “链路已接通并 stub 验证；真实供应商未验证”。
- 单独 trial-real-d 可用**不**作为管理入口已接通的证据（本测试从管理入口发起）。

### 2) 生产绑定：代码已实现、隔离测试通过、生产未启用（三段口径分开）
- 核查：功能关闭 → 零注入旧行为不变（含缺 schema 照常）；**功能启用 + schema 缺失
  → 晋升/回退 503 明确拒绝、编译 fail loud**（不再“静默忽略绑定却报告成功”）；
  绑定损坏 → fail loud。
- stage8d 补两测：schema_missing_enabled_fails_loud / schema_missing_disabled_old_behavior。

### 3) 版本冻结：跨中断恢复验证（补缺实现）
- 每次 compile run 首次执行把绑定“冻结”为固定版本并落盘
  （knowledge_compile_artifacts, type=evolution_binding_pin）；后续 attempt（retry/
  中断恢复）一律读冻结值；中途晋升/回退不影响该 run。
- stage8d ⑨（10 passed 内）：attempt1 失败 → 绑定回退 v1 → retry 恢复该 run 请求仍含
  冻结 v2；新建 run 才用 v1。

### 4) 模拟晋升旁路：受服务端开关约束（请求参数不能开启）
- 新增 `WIKISKILL_EVOLUTION_ALLOW_SIMULATED_PROMOTION`（默认 False）；promotion_preview
  /promote 在开关关闭时即使 allow_simulated=true 也拒绝（“旁路未在服务端开启”）。
- stage8d 拒绝测试覆盖（生产/默认配置不会被请求参数打开）。

### 5) 真实启动授权：全局开关 + 每次确认 + 参数绑定记录
- start/resume(real) 需 confirm.explicit=true 且 dataset/迭代/预算与 run 记录一致；
  resume 不接受参数变更、不重置预算、不自动扩大授权；stage8e_chain 拒绝路径覆盖。

### 6) 测试清单映射（本核查轮实际运行）
```powershell
# backend/ 目录
python -m pytest tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py ^
  tests/test_skill_evolution_stage2.py tests/test_skill_evolution_stage3.py ^
  tests/test_skill_evolution_stage4.py tests/test_skill_evolution_stage5.py ^
  tests/test_skill_evolution_stage6.py tests/test_skill_evolution_stage7a.py ^
  tests/test_skill_evolution_stage7b.py tests/test_skill_evolution_stage7c.py ^
  tests/test_skill_evolution_stage7d.py tests/test_skill_evolution_stage7e.py ^
  tests/test_skill_evolution_stage7f.py tests/test_skill_evolution_stage8.py ^
  tests/test_skill_evolution_stage8b.py tests/test_skill_evolution_stage8c.py ^
  tests/test_skill_evolution_stage8d.py tests/test_skill_evolution_stage8e_chain.py ^
  tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py ^
  tests/test_wiki_skill_default_equivalence.py -q
# 结果：204 passed（演化 18 文件 collect 147 + 编译管线 57）
```
- 计数口径：此前“全量 144/148”只覆盖当时的文件集合（无 stage8b/8c/8d/8e_chain 或
  stage8d 更少用例）；本核查轮文件集合更大（演化 18 文件 collect=147）。是否遗漏以
  逐文件清单对照，不靠总数。
- **未运行（如实列出）**：PostgreSQL 业务接入验收（无环境）；生产库 alembic upgrade
  （未授权）；真实供应商调用与 7B 效果实验（未授权/等模型）；Grader v2 人工标签与
  真实校准（等人填）；浏览器“空数据/请求失败/XSS 注入”分支（隔离环境后端正常）；
  迁移脚本已在 alembic heads 解析（单头）但未在任何真实库执行。

### 分类收尾（不把未验证写成未实现，反之亦然）
- 已实现且离线/隔离验证：8B 正文/差异、8C 运行控制与真实 worker 接入（stub）、
  8D 晋升/回退 + 编译绑定读取 + 版本冻结 + 模拟旁路服务端门 + 真实启动确认；
- 未实现：生产绑定切换、生产 schema 迁移、正文语义对比、Grader v2 自动评分接入；
- 等真实环境验证：真实供应商链路行为与效果、Grader v2 人工标签/校准、阶段 7B 效果
  实验、生产启用。模拟/合成记录 real 字段不构成真实调用证据。

## Grader v2 工程接线轮（本会话末二；08f / 09 / engineering-completion）

### 实现
- 评分器注册表 grader_registry.py：v1(mechanism,not_applicable,可业务晋升仅机制) 与
  v2-prototype(ready=工程可运行, calibration=engineering_only, allow_business_promotion=
  False —— 服务端强制，非文档提示)；control.check_dataset_grader/业务晋升均读它。
- 语义评审适配器（model_review）：ReviewerConfig 独立显式配置（model/prompt/api，
  绝不沿用执行模型；缺 → fail closed）；ChatSemanticReviewer 真实实现（提示词版本
  wiki-default-review/prompt-v1；评审输入仅 资料+输出+检查，无参考答案/身份/分数；
  check_review_result 结构校验；故障/非法/引用错误 → invalid 不回退 v1；
  BudgetGuard.wrap 包裹网络发送 → 评审请求与重试共同消耗预算；usage=null）。
- 评估入口 review_eval.grade_task_v2 + gating review=v2 接线（run_baseline/
  evaluate_and_gate 透传 reviewer；needs_review/invalid 任务 → valid=False、不通过、
  不缩分母、不可晋升；任务 pass≠晋升；v2 要求数据集评分器=v2 —— 不同 grader_version
  分数不可直接比较，切换须新实验重建基线；无 v2_spec 明确拒绝不退回 v1）。
- orchestrator：Actors.reviewer_factory + 真实评审发送 guard 包裹 + 评审计数回写
  used_model_calls（非 external 时）。
- console /evaluations 增加 pending_tasks/pending_count/review_requests（无迁移，
  由既有 JSON 推导）；前端评估列“待审 N 任务 / 评审请求”。

### 测试（实际执行，离线）
- stage8f（7）+ stage7f（12 更新 real 工厂契约）；演化全量最终 211 passed
  （19 演化文件 154 + 管线 57，collect 数与通过数一致）；npm build 通过。
- 浏览器（隔离 data/dev8g，已删含 token）：25 实验分页 20+5/Total25、评估“待审 2
  任务”+invalid_reason“不通过、不缩分母、不可晋升”、恶意 `<script>` 正文按文本展示
  无执行无 console error、晋升预览默认阻止原因可见。

### 边界 / 未完成（engineering-completion.md A–E）
- 未执行真实供应商评审；未执行生产迁移/启用；A1–A5 未改未代填；人工评审身份不可
  由请求参数写入。
- E（可在当前环境继续，本轮未闭环，故不宣称“全部非模型依赖工程完成”）：
  v2 专用带 v2_spec 的开发数据集缺失（现有 v1–v5 无 v2_spec 块 → gating v2 全轮
  run 无法在真实任务集端到端跑）；真实模式五角色单 stub 全协议闭环未搭建；
  UI 故障注入式浏览器自动化未复跑。其余 A 项均已实现并离线/隔离验收。
- 部署与运维准备见 09-deployment-prep.md（迁移顺序/备份恢复/日志脱敏/数据保留/
  模拟旁路服务端限制/PG 外部阻塞）。

## 剩余缺口收尾轮（本会话末三）：v2 开发数据集 + 编排/评审/校准/故障验证

- v2 专用开发数据集：`tools/gen_dataset_v2dev.py` → `eval/wiki_evolution/datasets/
  wiki-default-v2dev`（train5/val3，grader=wiki-default-grader/v2-prototype-2；每任务
  references[task_id].v2_spec 覆盖 主体-谓词-宾语/数值单位(°C,V,mm)/版本区段归属
  (v1.0,v1.1)/coverage/声明式矛盾；全部为新写开发资料，未用 A1–A5 与既有 test 样例）。
- 评分器校准状态拆分（grader_registry）：`labels_approved` 与 `real_calibration`
  为两个独立状态；只建人工标签参考标准不自动置 calibrated；解除晋升限制需对应
  评分器/模型/提示词版本的校准证据+明确批准（本轮未代填）；stage8f 校验两态独立
  与保护。
- v2 反向保护：v2 数据集未配 review=v2 → run_baseline 执行前拒绝（不允许 v1 引擎
  评分 v2 数据）。
- 测试（离线）：`stage8g` 5 passed（数据集/spec、非法或缺失 spec 明确拒绝、私有评分
  不外泄、orchestrator v2 全编排：语义未决→基线 invalid 无晋升分数不缩分母、评审
  计费≥3、中断 resume 预算不重置且配置不漂移、新 worker 接管）；`stage8f` 7 更新后
  通过；7f 12、8c/d 等受影响子集 32 passed。
- 浏览器故障验证（隔离 data/dev8h，已删）：停后端→“查询失败”提示（无 pageerror）；
  启后端→点刷新→数据恢复、无 alert 残留。请求拦截 API 当前浏览器运行时不支持
  （setRequestInterception 不可用），以隔离后端停/启等价方式完成。
- 最终回归：全部相关文件重跑（含 stage8g）→ 见本文件“最终回归”段输出（211+5）。
- 剩余工程缺口（详 engineering-completion.md E3）：真实模式五角色单 stub“接受→
  下一轮注入”闭环仍需按 proposer/maintainer 协议固化测试夹具（proposer 工具轮 +
  patch ops 格式、maintainer create/update_patterns schema；proposer.py:505-640、
  patch_package、maintainer 顶部字段）；离线模拟无“技能提升质量”的机制
  （SimulatedModel.faithful 仅支持 追加核对清单/删除条件要点/模拟不可用），故 v2
  严格改进只能在真实输出差异夹具下构造。此项在当前环境可继续，但未在本轮内收敛；
  PG 无环境保留为外部阻塞。

## E3 收尾（stage8h_e3 通过；文档/回归已更新）
- `tests/test_skill_evolution_stage8h_e3.py` —— **2 passed**（真实模式五角色单 HTTP
  stub 两轮闭环 + 评审瞬时失败重试/耗尽）；v2dev 数据集扩为 train5/val4（新增
  vd2-val-fail2 结构性占位非满分，避免满分提前停止）。
- 回归：全部 24 个相关文件重跑（含 stage8h）见最终输出；前端 npm run build 通过。
- engineering-completion.md：E1/E2/E3 已闭环；剩余仅 真实模型 / 人工标签与校准批准 /
  PG 无环境 三类外部阻塞。

## 审计轮（val4/分母 4 + 管理单链 worker）

### 1. v2dev val 与评估分母
- 使用**当前冻结数据集**（非旧 val3）：wiki-default-v2dev，fingerprint
  `ec4de033f4cf1143974cb26e1955c2f33bac010300b1e5de0a87f680a9a14b1a`
  （dataset.json + references/* + sources/* 逐字节哈希）。
- 实验 val 清单 = 数据集全部 4 项：vd2-val-pass / vd2-val-fail /
  vd2-val-versioned / vd2-val-fail2；**评估分母 main_total=4（不缩分母）**。
- stage8h_e3 与 stage8h_mgmt 均断言 baseline 1/4、candidate 2/4、2/4（持平拒绝），
  per-task verdicts 存储在 evaluation.per_task_results_json（task_id + verdict）。
- 之前报告中的“3”来自当时数据集为 3 val 时的运行记录；数据集随后扩到 4 val 后
  已把实验 val 清单同步为全部 4 项并复跑（非为对齐报告删任务）。此条审计无缺陷。

### 2. 统一端到端：管理 API → 独立 worker（子进程）单链（stage8h_mgmt，1 passed）
- 同一次运行：POST /experiments（review=v2, real）→ start（先 409 未确认，再显式
  确认 200，返回 pid）→ 真实独立子进程 worker（evolution-run + build_run_actors：
  executor/maintainer/proposer real + 独立评审 ChatSemanticReviewer，全部经本地
  HTTP stub，虚构凭据 stub-key）→ 两轮：轮1 接受（1/4→2/4 严格提升）、轮2 注入
  新技能正文并持平拒绝 → 同一 run_id 全链证据（worker 日志存在、角色请求计数
  executor22/reviewer12/maintainer2/proposer10、evaluation 行、accepted/rejected、
  迭代2 freeze 版本+content_hash 等于 current）。
- 覆盖区分：管理入口+worker 进程级真实请求（mgmt 测试、stage8e_chain 已有）与
  编排器/协议级闭环（stage8h_e3）现在为**同一 stub 实现**上的组合证据：
  - 单链（同一运行内）：stage8h_mgmt —— 管理→确认启动→子进程 worker→两轮闭环；
  - 协议组合（编排器直连，同一 stub 与 actor 装配）：stage8h_e3；
  两测试共享同一 _E3Handler/stub 路由（动态加载复用，无两份漂移）。
- 审计 JSON 输出在测试通过时打印（kind=http-stub-management-single-chain，含
  dataset_fingerprint/val_task_ids/run_id/worker_log/role_http_counts/per_task）。
- 修正项：worker spawner 被 stage8c 全局残留为线程执行器 → 在 control 增加显式
  `SUBPROCESS_SPAWNER` 别名，stage8c env fixture 结束后与 mgmt 测试均恢复到
  独立子进程 spawner；25 文件终版回归 **219 passed**。

## 审计修复轮（2026-09-08；audit-remediation.md）
- 已修复并离线验证：语义评审 JSON 解析 NameError/脆弱解析/尾随文本（严格 fullmatch
  契约）、item 类型/未知/重复/缺失校验、位置与引文可追溯契约（NONEXISTENT:999 拒绝；
  缺失内容判定允许无位置）、移除评审输入静默 [:20000] 截断（超限明确
  ReviewerInputLimitError=invalid）、评审记录保留（identity/非秘密配置指纹/attempts/
  error，只入评估记录）与有限重试计数。测试：tests/test_skill_evolution_stage8i.py
  （新增反例集）。
- 修复过程中同步把测试 HTTP stub 的 check 抽取限定到检查段（无协议改动）；受影响
  11 文件批 86 passed；stage8i/7f/8f/8g/8h_e3/8h_mgmt 通过。
- 仍存在的当前环境工程缺陷（审计 B1–B5，未在本轮完成，根因见 audit-remediation.md）：
  业务晋升证据判定（配置 real ≠ 实际证据；未绑定集合哈希/评估/指纹/批准）、多技能
  集合晋升与注入（schema 迁移）、晋升/回退 CAS 与幂等键语义、完整配置冻结与评审
  max_tokens 透传、阶段 7 正式接口批次强化。工程-completion 文档中 E 声明据审计
  更正为 B1–B5 仍开放。
## B4 完成轮（2026-09-08 续）
- config_freeze.py + real_adapters cfg 绑定发送 + control 冻结落库/确认指纹/
  旧记录拒绝 + Reviewer max_tokens 透传；stage8j（3 passed：settings 漂移不生效、
  指纹确认 409、legacy fail-closed），e3/mgmt/8e_chain/8c/8i 21 passed。
- B1（晋升证据绑定）、B2（多技能集合）、B3（CAS/幂等键）、B5（正式实验接口批次）
  仍未完成：范围大且相互依赖（B2/B3 需集合哈希 schema；B1 需 B2 集合绑定 +
  批准状态；B5 需冻结批次校验与 v2 分派）。无外部阻塞；后续轮实施。
## B3 完成轮（2026-09-08 续）
- business_ops：promote/rollback 增加 expected_set_hash（CAS，失配 409 binding_conflict，
  不后写覆盖）、请求级幂等键+内容指纹（同键同内容重放返回原结果且不重复写事件；
  同键不同内容 422 idem_content_conflict；新操作键允许回退后再晋升同一集合，不再
  按 工作区+目标版本 永久去重）；绑定切换经条件 UPDATE（expected_version 比较），
  审计事件同事务；审计写入失败 → 整体回滚（无半写）。business_state 暴露
  current_set_hash；admin API PromoteBody/RollbackBody 增加对应字段。
- 反例测试 tests/test_skill_evolution_stage8k.py —— 4 passed：v1→v2→回退v1→新键再
  晋升v2+重放、同键不同内容拒绝、陈旧预期哈希 409、审计失败整体回滚。
- B1/B2/B5 及 B4 联调（预览确认/浏览器）仍未完成（无外部阻塞；后续轮实施）。
## B2 检查点（2026-09-08；audit-remediation-checkpoint-b2.md）
- 已落地：P52 迁移（bindings.rev/set_hash/members_json，单 head 653bbcf9847b）、
  EvolutionSkillBinding 三列 + required 映射、business_ops.binding_set_hash。
- 兼容回归 46 passed（stage2/4/8b/8d/8k）。生产库未迁移。
- 未完成（当前窗口不足，非外部阻塞）：promote/rollback/绑定/审计/注入/freeze 的
  完整集合语义 + rev+set_hash CAS + stage8l 反例 + API/前端集合字段。详见检查点文档。

## B2 只读解析完成轮（2026-09-08；test_skill_evolution_stage8l.py）
- resolve_business_binding_set：统一完整业务绑定集合读取（rev/kind/members/
  set_hash/canonical_set_hash/legacy）；members_json=NULL 旧单技能/旧空集合兼容；
  空集合与旧记录明确区分；逐成员版本存在/内容哈希/声明一致性(含 seq)/domain/
  runtime/重复/顺序((skill_id,seq) 升序)/存储哈希全量校验，任一失败整体抛错。
- resolve_business_binding 单技能兼容（多成员集合 → binding_set_multi_member，
  不静默压首成员）。P51 缺列库按旧行语义读。
- 12 passed。此前 46 回归通过。
## B2 晋升写路径 + 集合 CAS 完成轮（2026-09-08；stage8m.py + P53 迁移）
- 唯一转换入口 canonical_binding_members（实验注入顺序=(skill_id,seq) 等价证明：
  逐成员 skill_id/version_id/content_hash 一致）；promote 逐成员校验→复制→
  _materialize_set_binding（rev+set_hash CAS；首绑唯一约束兜底）→审计（完整
  from/to members+hash+rev）同事务；指纹/重放（同键同内容重放、不同内容拒绝）；
  旧 NULL 单成员行受保护升级物化；有效集合历史 chain 使回退写路径与晋升一致。
- P53 迁移 4f83c9e2a1d7（events from/to members/set_hash/rev 六列，单 head）；
  stage8m 16 passed + 链上 alembic upgrade head 验证列齐全；46/60 回归通过。
## 业务闭环整改包（当前轮；stage8n.py + API/资格/环境）
- M1 幂等重放修复：promote/rollback 幂等键查重提前到 CAS 预期校验之前；请求指纹
  覆盖 完整目标成员+作用域+预期修订/集合状态+证据引用；首绑显式令牌
  (FIRST_BIND_REV=0, NO_BINDING_HASH)（≠空集合哈希）；成功请求携带原（已过期）
  预期令牌重放返回原结果，不产生 binding_conflict；同键不同请求（含预期不同）
  idem_content_conflict。DB 层护栏使用请求原始预期修订（写入前不重读最新 rev
  顶替）。回退按审计集合历史（完整成员，多成员可恢复，兼容旧单版本事件，合法
  初始无绑定→显式空集合行 rev+1）；同键回退重放不因历史变化自动改算目标。
- M1 旧写入口：skill_store.bind 统一物化 members_json/set_hash/rev（切换 rev+1），
  不再只改 version_id 留下过期列；P52 缺列写一律 fail closed（binding_p52_required，
  删除 legacy 写分支/_cas_apply_binding）。
- M2 编译注入：freeze/_apply_pin 保存完整 members+set_hash；逐成员重读校验
  （缺失/损坏/声明不一致 fail loud）；旧单技能 pin 兼容；render_set_block 与实验
  一致的指令正文/排序/长度上限。同一 run 跨中断冻结不变、新 run 读新集合。
- M2 晋升资格：promotion_env（production|isolated-test）配置；production 一律
  拒绝业务晋升（真实供应商/校准/批准独立待办）→ K 反例；isolated-test 下
  模拟证据需 allow_simulated+服务端旁路；真实证据（completed+real+有调用+数据集
  一致）须命中 accepted 门控事件精确集合锚点；预览与执行共用 _eligibility_report，
  执行时重校验；preview 返回 target_members/exp_set_hash/preview_fingerprint，
  API promote 支持 target_members（目标变化→拒绝重预览）、expected_rev、
  explicit_cas；rollback 同。测试记录永不标 effect_verified。
- 反例测试 test_skill_evolution_stage8n.py —— 16 passed：E（过期预期重放返回原
  结果）、F（同键不同请求/不同预期拒绝）、G（显式令牌语义 + 预览后目标变化拒绝）、
  B（多成员回退完整恢复 + 初始无绑定）、重复同键回退不自动改算、skill_store 物化
  一致性、P52 缺列 fail closed、J（real 空 run / 无锚点真实证据拒绝、有锚点可晋
  升且 effect_verified=False）、K（production+全部旁路开关仍拒绝）、并发显式令牌
  单赢、A/C（双技能晋升后编译注入全部指令且顺序正确；同 run 跨中断冻结、新 run
  读新集合）。
- 受影响回归（stage2/4/8b/8c/8d/8e_chain/8h_e3/8j/8k/8l/8m/8n）在本轮验证中。
- 未完成（明确交接点）：M3 前端一次性接线（EvolutionConsole 晋升/回退带预期令牌
  与幂等键、冲突“请重新预览”、start/resume 无网络配置预览→确认→启动协议）、
  浏览器验收 L、真实两轮 worker 闭环复跑、PG 验收；B5 正式实验接口未扩展；
  生产资格本包不开放。下一执行步骤见 audit-remediation.md 结尾。

## M3 最终验收轮（2026-09-08；stage8p.py + endpoint 名单）
- 凭据绑定：settings.wikiskill_allowed_llm_endpoints + 发送前 ensure_endpoint_allowed
  （executor/maintainer/proposer 经 _cfg_http_chat；reviewer 经 _send_http）；
  任意地址零发送；冻结端点不随 settings 漂移改绑。
- 真实子进程授权链通过（stage8p，6 passed）：create→start-preview（脱敏）→确认
  指纹→start→真实 Popen worker→HTTP stub 命中→收敛；漂移/未确认/重复 start 拒绝
  且零出站。受影响回归 137 passed（stage2/4/7f/8b/8c/8d/8e_chain/8f/8h_e3/8h_mgmt/
  8i/8j/8k/8l/8m/8n/8o/8p）。
- 未完成：双轮（accept→注入新集→tie reject）确定性门控子进程断言、浏览器人工
  走查、运行中暂停→恢复的预算保持子进程重放、PG 验收；B5 与真实供应商/校准批准
  保持开放（见 audit-remediation.md D9）。

## M3 浏览器整改复验收尾（2026-09-08；A–F 已执行，含回归暴露缺陷修复）

- 真实浏览器复验（全新隔离根 `backend/reports/m3-browser-remediation-20260908-143805`，
  28760/28761/28763 仅 127.0.0.1）：A 权限（anon→login?redirect、reader→403、admin 可进
  入）通过；B v2 real queued（grader v2/runner review=v2/real=true/val 4 项、创建期 stub
  0 请求）通过；C start→pause→resume→completed：start 与 resume POST 均带 8 项完整确认
  字段且 200，独立子进程 worker，paused 跨刷新稳定，completed 后 used=65 == 四角色 stub
  总数（31/16/3/15），暂停/恢复增量 27→38 精确一致，reserved==[]，预算不重置不扩大；
  D 502 丢响应→同键同 body 重放（workspace/rev/hash/key 全同）只发生一次业务切换
  （rev+1、审计事件+1、members 回退一层），新操作新键；E 双浏览器 CAS：旧令牌 409、
  旧 pending 作废、不覆盖 B、重新预览新键成功；F 多技能集合完整展示、正文 `<script>`
  文本化不执行、无 v-html、模拟与未校准 v2 晋升被服务端阻止、标题去只读、费用未知
  非 0、回退失败与晋升失败文案分离、刷新恢复、无 console/pageerror。
- 界面缺口补全：P1-2 结果未知提示/同键重试按钮/回退专用错误条渲染；数据集下拉绑定
  @change 使 v2 自动携带 review（修前 422 已留证）；确认构造拒绝非整数迭代。
- 回归暴露并修复存量缺陷（此前子集回归未覆盖）：real_adapters 兼容分支密钥存在性改读
  override 优先；orchestrator.execute 内部分支已落盘 paused 时不再二次 _pause
  （LeaseConflict 修复）。
- 最终测试（复跑通过，无 skip/xfail）：stage8r 5；前端纯函数 12；全部 skill_evolution
  32 文件 244 passed（477.67s）；Wiki 编译管线 10 文件 273 passed（177.98s）；
  compileall/import 通过；alembic 单 head 4f83c9e2a1d7；vue-tsc + vite build 通过。
- 外部保留项（不变）：真实供应商链路与阶段 7B 效果实验、A1–A5 人工标签与真实校准、
  PG 环境验收。生产库/生产绑定未触碰；本轮服务已全部关闭。

## 凭据绑定终审 P2（2026-09-08；M3 结论保持通过）

- 独立终审发现并修复真实模型凭据链最后一个工程缺口（不重执 M3）：
  - executor 兼容路径曾接受 `override['llm_api_key']` 预检但发送仍读 settings key →
    现 raw override key 一律 `raw_credential_override_forbidden`（不预检通过、不回显
    key），兼容模式唯一凭据来源 = settings.llm_api_key（仅 require_provider_binding=false）；
  - reviewer 冻结/恢复现写入并恢复 provider_id、credential_env、allowed_endpoints、
    enforce_https；`ChatSemanticReviewer._send_http` 不再直读 settings.llm_api_key，
    改走公共 credential_binding（provider 模式只读 credential_env；旧兼容仅 settings
    key；缺失/漂移/HTTPS 违约均 httpx.post 前 fail-closed）；
  - 公共语义单点 `credential_binding.py`：executor/maintainer/proposer/reviewer 共用
    provider/引用/端点/HTTPS 校验与凭据解析，防止双写漂移；
  - ReviewerConfig.fingerprint 纳入 provider/引用/规范化端点/enforce_https/模板哈希
    （env 值轮换不变）；旧冻结缺 reviewer provider 字段 → provider-required 环境
    `frozen_missing`。
- 反例/集成/回归（实际结果）：stage8s 23 passed；targeted 14 文件 89 passed；全部
  skill_evolution 33 文件 267 passed（492.71s）；Wiki 编译 10 文件 273 passed
  （192.62s）；compileall/import 通过；alembic 单 head 4f83c9e2a1d7；前端 vue-tsc +
  vite build + test:unit 12 通过。
- 五角色两 provider 浏览器冒烟（报告根 backend/reports/m3-credential-browser-20260908-
  160156）：UI 双确认显示 executor-fixture/reviewer-fixture 且无密钥；stub 逐角色认证
  0 mismatch；used==stub 46==46、reserved==[]；哨兵 0 泄露。
- 外部保留项不变：真实供应商链路/7B 效果、A1–A5 人工标签与真实校准、PG 环境验收。

## 凭据端点授权 P1（2026-09-08；安全修复与最终验收）

- 漏洞根因：`credential_binding.endpoint_in` 使用裸字符串 startswith / host-only 判定，
  放行同 host 非授权路径、`/v1`→`/v10` 前缀碰撞与 http↔https 跨 scheme；frozen 空集
  默认放行；userinfo/fragment/反斜杠/点路径/百分号编码逃逸未处理。
- 修复：结构化 URL 授权（scheme + hostname + effective port + path prefix 分段边界 +
  query 策略 + 编码/逃逸拒绝）；frozen 与当前映射非空且双集合命中；enforce_https 的
  `https_required` 优先抛出；resolve 预检与发送校验分层（预检 host+path，发送严格全
  身份）。四角色共用同一发送校验（executor/maintainer/proposer 经 real_adapters 委托、
  reviewer 经 ChatSemanticReviewer 同一 cb.validate_frozen_send），无旁路。
- 反例/测试（先红后绿）：旧实现 26 个拒绝用例失败 → 修复后 stage8s 100 passed；
  关键链路单次 6 文件 21 passed（mgmt/e3/8j/e_chain/8f/8q，管理 API→独立 worker→本地
  stub、四角色 provider 正确、used==出站、reserved 空、暂停恢复冻结、0 外部网络）；
  最终一次全量：evolution 33 文件 344 passed（459.43s）+ Wiki 编译 10 文件 273 passed
  （142.79s）+ alembic 单 head 4f83c9e2a1d7；0 failed/0 errors/无新增 skip/xfail。
- 改动范围：credential_binding.py、real_adapters.py（最小，链契约证明必需）、stage8s、
  两份文档；未动前端/迁移/生产配置。
- 未运行前端测试与浏览器验收（任务不涉及前端/API 请求体/UI）。无真实模型请求、无
  生产库/生产绑定变更、无真实凭据、无临时服务端口残留。
- 外部保留项不变：真实供应商链路与阶段 7B 效果实验、A1–A5 人工标签与真实校准、PG
  环境验收。本项不构成真实供应商链路验证或论文效果复现。

## 端点授权最后补丁（2026-09-08；三处遗漏修复，专项/关键链验证，不重复全量）

- 遗漏与根因：① `endpoint_declared` 未比较 hostname → 跨 host（含根路径授权）预检
  通过；② allowed 带 query 时仍走路径前缀规则 → query 授权可扩张到子路径；③ 多层
  百分号编码仅单层 unquote → 双重编码点路径/斜杠/反斜杠/控制字符可逃逸。
- 修复（credential_binding.py）：endpoint_declared hostname 严格相等；allowed 带
  query → 路径与 query 均精确相等（禁子路径/前缀/追加）；路径逐段有界（8 层）逐层
  percent-decode 至稳定 + malformed escape 拒绝（`%`/`%2`/`%GG`），收敛后校验解码段
  不形成 `/`、`\`、NUL/控制字符与 `.`/`..`；超层 fail-closed；query 不解码。
- 反例（stage8s F 节 36 项，先红后绿：修复前 19 failed）；专项 stage8s 136 passed
  （21.52s，0 failed/0 errors，无 skip/xfail）。
- 关键链路：stage8q + stage8h_mgmt 单次 pytest 8 passed（62.88s）——https_required
  错误顺序不回归、管理 API→独立 worker→本地 stub 正常、四角色 provider/凭据/端点无
  错配、used==出站、reserved_in_flight 空、拒绝反例 0 HTTP、无模拟兜底、无外部网络。
- 按任务决策不重复后端全量与 Wiki 全量（复用上一轮 344+273+alembic 单头结果）；未运行
  前端测试（不涉及前端/API/UI）。静态检查：编译通过、范围合规、无 TODO/调试/sleep/
  skip/xfail、无密钥字面量、无残留端口进程。
- 无真实模型/凭据、无生产迁移/绑定变更。外部保留项不变（真实供应商/7B、A1–A5 人工
  标签与真实校准、PG 环境验收）。

## A1–A5 人工标签批准与离线核验（2026-09-08）

- 人工确认（项目所有者 Codex 会话，reviewer=human-project-owner）：A1=pass、
  A2–A5=fail（每样例 2 检查项 + overall，共 15 条判定）。
- 入档：追加式 `eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl`（15 行；
  每行绑定 task+source_excerpt+candidate_text+checks/disputes/category/ground_truth
  哈希；重复 apply 幂等；同 check 冲突 verdict fail-closed）；工具
  `tools/approve_grader_v2_labels.py --check/--apply`；验收集顶层状态
  `human_labels_approved`、used_for_development=false、
  reserved_for_future_independent_acceptance=false、
  independent_acceptance_consumed=true；approval_record_hash=
  `9d0ee74e69156cd80ab2cd7302041010ba40c7f39356793006b31c6758741459`；samples 逐字
  未变（规范摘要 96e07ae8…）。
- registry：GRADER_V2 labels_approved=true；real_calibration=false、
  calibration=engineering_only、allow_business_promotion=false 保持不变；
  require_business_promotable 仍拒绝；/meta 机器可读展示 labels_approved/real_calibration
  并注明“人工标签批准、真实校准未完成、晋升阻止”。
- 测试（最小受影响，未跑全量）：stage8t(14)+7f+8f+8r+7e → 37 passed（21.93s），
  0 failed/0 errors、无 skip/xfail；py_compile 通过；alembic 单 head 4f83c9e2a1d7。
- 性质：human approved（人工标签批准与离线核验）；未用 A1–A5 修改评分器；不是真实
  校准。外部保留项：真实供应商/7B 效果实验、真实校准证据（人工标签已批但未校准）。
  PG 隔离工程验收见下一节。

## 隔离 PostgreSQL/pgvector 接入验收（2026-09-09；stage8u）

- 隔离环境：未用项目 `docker-compose.yml`、未读 `backend/.env`、未占 5432。
  容器 `wikiskill-pg-acceptance-20260908`（标签 `wikiskill.acceptance=true` /
  `run=20260908`）仅绑定 `127.0.0.1:55432`；库 `wikiskill_acceptance`；
  镜像 `pgvector/pgvector:pg16`；PostgreSQL 16.15 + pgvector 0.8.6。
  URL 形态 `postgresql+psycopg2://wikiskill_test:***@127.0.0.1:55432/wikiskill_acceptance`。
  未启动生产后端/worker/真实模型，未访问供应商。
- 迁移：单 head `4f83c9e2a1d7`。空库 upgrade head、P44→head、P51 旧绑定→P52/P53/head
  均成功且幂等。未改 P45–P53，未新增 P54，生产库未执行。
- PG 专项 `tests/test_skill_evolution_stage8u_postgresql.py`：**22 passed**
  （32.84s，显式 URL，0 skip）。覆盖集合绑定、双连接 CAS/ABA/首绑、请求级幂等、
  绑定与审计同事务、编译冻结、重启恢复、备份恢复一致、Grader v2 正式晋升仍拒绝。
- 第三层最小回归（一次）：8u+8d+8k+8l+8n+8p+8t+8m(P53 两项) **86 passed / 85.72s**。
  未跑 evolution 全量、Wiki 编译全量、前端、浏览器、真实模型。
- 真实缺陷与修复：P25/26/28 在 PG 上 `recreate=always` 会 DROP PK 被 FK 挡住 →
  `drop_columns_compat`；P31/P34 布尔字面量；P32.5 HAVING 别名与 PK inspector；
  PG `IntegrityError` 事务 aborted 后按幂等键重放（`business_ops`）。SQLite 未暴露。
- 备份：`backend/reports/pg-acceptance-20260909-091915/`（无口令）；
  dump sha256 `f207aef65b4d69bd…`；原库/恢复库计数与 `rev/set_hash` 一致后删恢复库。
- 清理：容器与带本任务标签的 volume 已删；55432 已关闭；未 prune、未删镜像、
  未关 Docker Desktop。
- 文案：`v2-acceptance-review.json` 仅改顶层 note（与 `human_labels_approved`
  对齐；samples 未改）。
- 外部保留项仅限：真实供应商校准、阶段 7B 效果实验。本项不是生产验收或效果证明。

## Phase9B 运维 head 常量最终同步（2026-09-09）

- 验收收尾发现：`phase9b_migration.migrate.ALEMBIC_HEAD_EXPECTED` 与
  `DEFAULT_DRILL_REV` 仍停在 P44/P43，会导致 P53 副本 preflight/recovery
  `head_matches` / roundtrip 误报。不是业务逻辑或迁移语义问题。
- 已改为显式固定值：`ALEMBIC_HEAD_EXPECTED=4f83c9e2a1d7`（P53），
  `DEFAULT_DRILL_REV=653bbcf9847b`（P53 的直接父版本 P52）。不得再声称 P44
  是当前 head。
- 防复发：`TestOpsHeadGate` 用 ScriptDirectory 核对唯一 head、父 revision 与
  常量一致；P53 空库 upgrade 后 preflight/postflight/restore-verify/downgrade-drill
  不再因 P44 误报；真实库写保护未弱化。
- 定向 `test_phase9b_recovery.py` 仍走 `phase9a/bootstrap_db.py` 升 head：该脚本
  原先硬编码 P44 会拒绝当前链。已改为读取同一 `ALEMBIC_HEAD_EXPECTED`，无业务/
  模型/迁移语义变更。
- 定向测试：`test_phase9b_migration_tools.py` + `test_phase9b_recovery.py` +
  `test_phase9b_migrate_compat.py`。未启动 Docker/PG，未重跑 PG 专项。

## Alembic current-head 测试夹具最终统一（2026-09-09）

- 四个遗留夹具已统一到 `phase9b_migration.migrate.ALEMBIC_HEAD_EXPECTED`
  （`_HEAD_REVISION = 4f83c9e2a1d7`）：`test_phase9a_integration.py`、
  `test_phase9a_startup.py`、`test_phase9b_incremental.py`、
  `test_phase9b_version_scope.py`。不再用 `_P44_REVISION` 表达当前 head。
- `test_phase9a_startup.py` 的 `_P43_REVISION` 仍保留，专用于 schema-not-ready
  历史库反例；未把它升到 head，未弱化 `SchemaNotReadyError`。
- 本轮仅跑 4 条夹具代表测试；未重跑 PostgreSQL、55 项运维回归或任何全量。
  未改业务/运维/迁移/数据库。

## 非模型依赖闭环整改包（2026-09-09；M1–M4 + R1–R4）

> **非模型工程缺口清零（独立复审整改 R1–R4）。** 上轮 33/484 passed 不得作为本轮证据。

- R1：训练 grade 绑定 `candidate_sha256` / `outcome_sha256` / `output_sha256` /
  `execution_id` / `trace_seal` / `meta_sha256` / `grader_version` /
  `reviewer_identity` / `reviewer_config_fingerprint`（及可选 `prompt_version`）。
  崩溃点 A 只补 grade（executor=0）；崩溃点 B 不重复 executor/reviewer。
- R2：正式 real 批次复用 `config_freeze` + `build_run_actors`；CLI
  `experiment-batch-test`。本地 stub：C executor=5 proposer=1；D executor=5
  maintainer=1 proposer=1；SimulatedModel/Maintainer/Proposer/PolicyProposer=0；
  v2 reviewer 独立 HTTP 1 次。
- R3：frozen set 必填字段与 manifest 缺一/篡改 fail-closed；run/resume/test 前重核。
- R4：崩溃接管保守结算；三次接管 remaining 20→15→13→11→9；`max_wall_seconds`
  为 invocation 硬上限。
- 定向 48 passed；受影响回归 236 passed / 654.05s；集中回归 **505 passed, 0 failed,
  22 skipped, 190 warnings in 805.59s**。Alembic 单 head `5a94d0e3b2c8`。
- 明确保留：真实供应商验证、Grader v2 真实校准、正式 7B 效果实验。不得宣称论文
  效果已复现。前端未改。
