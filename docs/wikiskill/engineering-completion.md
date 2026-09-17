# WikiSkill 工程完成性交付（engineering-completion.md）

> 汇总：Grader v2 工程接线、v2 离线链路验证、前端/浏览器验收、部署与迁移准备、
> 回归与交付。边界：全程无真实模型请求/付费调用；未执行生产迁移与生产启用；
> 未改动 A1–A5 预期标签、未代填 human approved。
> 分类说明：未验证 ≠ 未实现；只有 A 内不再存在可在当前环境完成的安全工程时才算
> “非模型依赖工程完成” —— 见 E 的如实列项（本轮未完成项及原因）。

## A. 已实现且离线验收通过

| 项 | 证据（文件/测试） |
| --- | --- |
| 8B 技能正文与差异展示 | stage8/8b（5+9） |
| 8C 运行控制（创建/启动/暂停/恢复/取消、CAS/启动租约/确认绑定记录） | stage8c（9） |
| 真实 worker 管理入口链路（admin→子进程/线程 worker→orchestrator→真实适配器→HTTP stub；模拟被哨兵拒绝） | stage8e_chain（1） |
| 8D 晋升/回退 + 业务编译绑定读取 + schema 缺失拒绝 + 版本冻结跨中断 | stage8d（10） |
| Grader v2 接线：注册表状态（工程可运行/未校准）、真实语义评审适配器（独立配置、预算包裹、用法 null）、评估入口与门控保守语义（needs_review/invalid 不通过不缩分母、未决不可晋升、任务 pass≠晋升、切换评分器须新实验） | stage8f（7）、7f（12） |
| 评审输入隔离（参考答案/私有反馈不进执行/维护/提议角色上下文；评审只见资料+输出+检查） | 7f 通道分离 + 8f 外泄断言 |
| 前端 | `npm run build`（vue-tsc+vite）通过；console 评估新增待审/评审请求列 |
| 部署/运维文档与配置默认安全值 | 08e/09-deployment-prep |
| 隔离 SQLite 迁移验证 & 旧功能关闭兼容 | alembic 单头解析 + 回归开关用例 |

后端回归（本轮实际运行，最后一遍完整命令见 implementation-status 审计段）：
演化 19 文件 collect=**154**、编译管线 3 文件 **57**，合计 **211 passed**
（stage8f 加入后：原 147+7）。全部离线。

## B. 依赖真实模型的待办（等模型恢复）

1. v2 真实供应商评审运行（评审模型配置后；入口 `python -m app.core.skill_evolution.cli trial-real-d preflight|create|run` 及其管理端同链路）。
   前置：`WIKISKILL_REVIEWER_*` 显式配置 + `ADMIN_REAL_ENABLED=true` + 每次 start 的
   显式确认；预算：沿用 run 记录（max_model_calls/max_tool_calls/max_seconds，
   建议默认 90/40/3600）；预期证据：stub→真实切换后 run 收敛、评审请求记录、
   usage 仍 null；停止条件：预算耗尽/租约丢失/明确服务错误即停，不自动重试扩大。
2. 阶段 7B 正式对照实验与效果报告（不宣称复现）。

## C. 需要人工决定或授权的待办

1. Grader v2 人工标签：A1–A5 已由 human-project-owner 于 2026-09-08 批准并入档；
   `used_for_development=false`。剩余不是再填标签，而是真实校准批准后才可翻转
   calibration / allow_business_promotion。
2. 真实校准完成后把注册表 v2 calibration 置 calibrated 并重开
   allow_business_promotion 的流程（含隔离验证 + 新 unseen 验收集预留）。
3. 生产启用：迁移执行、开关打开、真实晋升/绑定切换——全部需授权。

## D. PostgreSQL 业务接入（2026-09-09 已完成隔离工程验收）

1. 隔离 PostgreSQL 16.15 + pgvector 0.8.6 已验收（独立容器
   `wikiskill-pg-acceptance-20260908`，仅 `127.0.0.1:55432`，不读 `.env`、
   不占 5432、不跑生产后端/真实模型）。空库 upgrade head、P44→head、
   P51 旧绑定→head、集合 CAS/ABA/幂等/冻结/重启/备份恢复均在真实 PG 上通过。
   证据：`docs/wikiskill/postgresql-acceptance.md`、stage8u 22 passed。
   验收后容器/volume/55432 已清理。这不是生产库迁移或生产启用。

## D2. 浏览器故障验证（已完成，隔离后端停/启）
- 断连注入：隔离环境下停止后端 → 点“刷新”→ 页面显示“查询失败”提示（无
  pageerror）；恢复后端 → 再点“刷新”→ 数据行恢复、alert 无残留（data/dev8h，
  已删除）。请求拦截 API 在本浏览器运行时不可用（setRequestInterception 不支持），
  以“隔离后端停止”的等价本地方式完成——工具限制与替代均已记录。
- 空数据/无权限/恶意正文/分页/v2 待审标签/晋升阻止原因在前序轮次浏览器实测。

## E. 仍存在的工程缺陷 / 未完成项（更新）

- E1 v2 专用数据集：**已完成**（wiki-default-v2dev：train5/val4，含“温度上限 40°C”
  结构性失败与 cable8mm 占位非满分任务；stage8g）。
- E2 v2 全编排/评审/恢复：**已完成**（stage8g：语义未决→invalid 无缩分母、评审计费
  ≥3、resume 预算单调、配置/技能不漂移、v2 数据禁止 v1 引擎）。
- E3 真实模式五角色单 HTTP stub 全协议闭环：**已完成（stage8h_e3，2 passed）**。
  单一本地 stub 覆盖 executor（富内容 + 技能注入 40°C 目标行）、maintainer
  （create→update 经验结构）、proposer（受控 ≥4 次 read_trace 工具轮 → 合法 patch，
  沿用现有 patch ops schema，未修改任何生产协议）、reviewer（独立语义判定）、
  验证（候选评估同一执行链）。全部角色走真实适配器（build_run_actors + 独立评审
  配置的 ChatSemanticReviewer，网络经 BudgetGuard 包裹）；SimulatedModel /
  SimulatedMaintainer / SimulatedProposer 哨兵化（被调用即抛错）。
  真实代码评分与门控：基线有效 1/3 → 轮1 候选严格提升 2/3 → accepted（原子更新）；
  轮2 训练请求（迭代2 freeze 集合）实际携带刚接受技能版本与 content_hash，且 ≥4 个
  executor 请求正文含 40°C 目标行；轮2 候选持平 2/3 → rejected，技能指针与经验历史
  保留。评审失败路径：瞬时 500 → 有限重试成功（同一预算两次请求）；重试耗尽 →
  ReviewerError（invalid，不产生通过、不回退 v1）。
  证据（全部标为 HTTP stub 工程验证；虚构凭据 stub-key；零外部网络）：各角色 HTTP
  请求计数、proposer 工具读取轮数、真实 evaluation 行（baseline 1/3、candidate
  2/3/2/3）、accepted/rejected 事件、下一轮技能版本+正文哈希、评审重试计数。
  管理入口 → 真实 worker 授权启动 + stub 首请求见 stage8e_chain（与 E3 同 worker
  路径 build_run_actors + orchestrator.execute）。
- 已无“当前环境可继续而未完成”的既定工程缺口。剩余仅：真实供应商评审与 7B 效果
  （必须真实模型/真实校准）。A1–A5 人工标签已批（≠ 真实校准）；PG 隔离工程验收
  已完成（见 D / postgresql-acceptance.md）。

## F. 凭据绑定终审（2026-09-08；当前环境既定工程缺口清零）

- M3 结论保持通过；本轮为独立终审发现的真实模型凭据链 P2（凭据隔离与冻结缺口）。
- 修复内容：raw `override['llm_api_key']` 明确拒绝（不再出现“预检通过、发送失败”
  分裂）；executor/maintainer/proposer 与 reviewer 统一走
  `credential_binding.py`（provider→credential_env→运行时 env 值；旧兼容仅
  require=false 且只读 settings key）；reviewer 冻结/恢复/发送不再读取全局执行密钥，
  发送前做“冻结+当前 provider 映射/引用/端点/HTTPS”双重校验；密钥轮换不改配置指纹；
  旧 reviewer 冻结缺 provider 字段 fail-closed；凭据/引用/端点漂移与 HTTPS 违约均在
  httpx.post 前拒绝；空凭据不发送无 Authorization 请求。
- 证据：stage8s 23 passed（角色 10/评审 12/脱敏 1，三组哨兵 0 泄露）；五角色两 provider
  本地 stub 浏览器链 used==stub 46==46、逐角色 0 mismatch、reserved==[]、UI 双 provider
  展示无密钥；全部 skill_evolution 267 + Wiki 编译 273 + 前端 build/单测通过；alembic
  单 head 4f83c9e2a1d7。
- 当前环境既定工程缺口：清零（仅针对本凭据绑定终审范围）。其后独立审计仍发现
  训练轨迹未真实评分、实验/业务注入不等价、预算跨 resume 可重置、B5 正式批次入口
  未闭环；见 H。剩余外部阻塞：真实供应商链路与阶段 7B 效果实验（真实模型/授权 +
  真实校准）。A1–A5 人工标签已批；PG 隔离工程验收已完成。
  模拟/合成记录不构成真实链路或效果证据；真实供应商仍未验证、论文效果未验证。

## G. A1–A5 人工标签批准（2026-09-08；human approved ≠ 真实校准）

- 项目所有者已人工确认 A1–A5（reviewer=human-project-owner，2026-09-08）：
  A1=pass、A2–A5=fail。判定与逐项理由已填入
  `docs/wikiskill/review-acceptance-A1-A5.md` 各“人工判定栏”，并追加式入档
  `eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl`（15 条，材料哈希绑定、
  幂等、冲突 fail-closed），验收集元数据置 `human_labels_approved`。
- grader_registry：GRADER_V2 labels_approved=true；real_calibration=false、
  calibration=engineering_only、allow_business_promotion=false；
  require_business_promotable(GRADER_V2) 仍拒绝（正式业务晋升仍被服务端阻止）。
- 状态边界：已实现/已离线核验：人工标签入档与核验；隔离 PostgreSQL 工程验收。
  需要真实供应商：真实评审校准证据（labels 批准不自动推出 calibrated）、阶段 7B
  效果实验。A1–A5 未用于修改评分器实现或阈值；不构成论文效果复现；不是生产验收。

## H. WikiSkill 非模型依赖闭环整改（2026-09-09；M1–M4 + R1–R4）

> **非模型工程缺口清零（2026-09-09 独立复审整改 R1–R4）。** 不得将上轮 33/484
> passed 当作本轮补修证据。本轮关闭复审四项真实缺陷；未验证真实供应商、
> Grader v2 真实校准、正式 7B 效果；不得宣称论文效果已复现。

关闭独立审计发现的四项非模型工程缺口（全程离线，无真实模型/凭据/生产 worker）：

| 项 | 根因 | 修复 | 反例 |
|---|---|---|---|
| M1 | 训练只记 execution_id；无 grade 被当成 success | `train_grading.py` 复用冻结 v1/v2；pass→success / fail→quality_failure / 缺失损坏→unknown | stage8v（上轮 11 passed） |
| M2 | 实验只注入 SKILL.md+CONTENT_CONTEXTS；业务注入 PURPOSE+全 context | 唯一 `render_execution_skill_text` / `build_injected_messages`；业务 `_apply_pin` 走同一 runner | stage8w 6 passed（本轮未重做） |
| M3 | `max_seconds` 每次 execute 重置；全局 tool 事后累加 | P54 `used_active_seconds`；`reserve_tool`/`finish_tool` 执行前消费 | stage8x（上轮 7 passed） |
| M4 / B5 | B 组 evolve=true；dataset_version 未写入；无完整可恢复批次入口 | A/B evolve=false；B 可解析种子；`experiment-batch-*`；全冻结后统一 test | stage8y（上轮 9 passed） |
| **R1** | grade 未绑定 candidate/outcome/output/seal/reviewer；评分后、iteration 写回前崩溃会丢 execution_id 并重跑 | 规范哈希字段；`grade_is_reusable` 逐项比较；`interpret_grade_kind` 重算核对；稳定 `execution_id` + `train-task-state/v1` 先提交再执行 | stage8v：candidate/outcome 变化不复用；篡改 output/seal→unknown；崩溃点 A executor=0；崩溃点 B executor=0 reviewer=0 |
| **R2** | 正式 real 批次无 model_ref/冻结入口；`run_batch` 不走 `build_run_actors` | `--real-config` 仅非秘密配置；create 冻结指纹；run 确认后 `control.build_run_actors`；新增 `experiment-batch-test` | stage8y：CLI real stub 链 C executor=5/proposer=1，D executor=5/maintainer=1/proposer=1；哨兵全 0；v2 reviewer 1 次 HTTP |
| **R3** | `verify_frozen_set` / 数据集校验 fail-open | 必填字段缺一即拒绝；manifest 绑定 dataset/source/reference/protocol/budget；run/resume/status/test 前重核 | stage8y：删除 members_hash/run_id/experiment_id/schema/set_hash/mode 拒绝；source/reference/protocol/manifest 篡改拒绝 |
| **R4** | `claim()` 覆盖旧 active 起点；`max_wall_seconds` 只影响 heartbeat interval | 过期接管先 `min(now, previous_lease_expires_at)` 结算；`max_wall_seconds` 为本次 invocation 硬上限 | stage8x：三次接管 remaining 只减不增（20→15→13→11→9）；invocation=2 / run=10 与 invocation=100 / run=3 均 0 次底层请求 |

- Alembic：单 head **`5a94d0e3b2c8`**（P54）。未改 P25–P53；未新增 P55。P54 实现列未改，沿用 stage8x 的 P53→P54 / 空库→head / downgrade→P53→P54 SQLite 路径。
- 本轮定向（修复后）：stage8v 18 + stage8x 13 + stage8y 17 = **48 passed**（其中 8v+8x+8y 一次跑 **47 passed / 297.00s**，随后补 v2 reviewer **1 passed / 1.18s**）。修复前新增反例曾 **18 failed, 29 passed / 205.70s**。
- 受影响回归：**236 passed / 0 failed / 654.05s**。
- 集中回归（skill_evolution contracts/phase1/stage* 39 文件 + wiki_pipeline_7d + wiki_skill_default_v3 + wiki_skill_default_equivalence）：**505 passed / 0 failed / 22 skipped / 190 warnings / 805.59s**。stage8u 按设计 skip。首次集中跑中 `test_first_bind_concurrent_unique_conflict` 在全量负载下 SQLite 竞态闪失 1 次，隔离重试 3/3 通过后整包重跑转绿；该用例本轮未改实现。
- 本包关闭的是上述非模型工程缺口。明确未验证：真实供应商链路、Grader v2 真实校准、正式 7B 效果实验。不得宣称论文效果已经复现。
- 前端未改。本轮未启动 PostgreSQL/Docker、未跑 `npm`、未访问外部供应商、未提交/合并。
