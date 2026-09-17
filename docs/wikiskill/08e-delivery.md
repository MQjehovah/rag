# 阶段 8E：工程交付与完整验收（08e-delivery.md）

> 汇总 8B–8D 工程功能与运维/交付说明，并按 A–F 口径分列状态。
> 状态标记：**已实现 / 离线及模拟验收通过；真实链路、效果、Grader v2 接入、
> 生产启用均待授权/待验证** —— 只对实际执行的测试报告通过。

## 1. 功能开关 / 受控实验根 / 权限

| 开关（后端 .env / 环境变量，示例值） | 含义 |
| --- | --- |
| `WIKISKILL_CONSOLE_ENABLED=true` | 只读控制台（默认 false） |
| `WIKISKILL_CONSOLE_ROOTS={"lab":"D:/data/wiki-experiments/lab"}` | 服务端受控实验根映射（只接受 id→绝对目录；客户端永不传路径） |
| `WIKISKILL_EVOLUTION_ADMIN_ENABLED=true` | 运行控制/晋升写端点（默认 false，与只读开关独立） |
| `WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED=false` | 真实模式服务端授权（默认 false）；**仍需每次 start/resume 的显式确认且参数与 run 记录一致** |
| `WIKISKILL_EVOLUTION_ALLOW_SIMULATED_PROMOTION=false` | 模拟晋升旁路（仅隔离测试可开；请求参数不能开启） |
| `WIKISKILL_BUSINESS_COMPILE_ENABLED=false` | 业务编译读取作用域绑定注入（默认 false → 旧行为不变）；开启但 schema 缺失 → 编译拒绝 |

权限：全部读/写端点要求 JWT + 管理员（`__local_admin__`）；页面路由
`/admin/evolution` 限 system_admin。所有写接口只接受 根标识 + 库内标识，服务端校验
作用域与参数（数据集目录白名单、版本白名单、workspace 存在且 active）。

## 2. 实验创建 / 操作 / 查看 / 晋升 / 回退手册（操作员）

1. 管理员登录 → 系统工具 → 演化实验只读控制台（页面状态会分流：未启用/不可用/无数据/
   查询失败/有数据）。
2. 新建实验：选数据集（评分器未就绪版本禁选）、模式（模拟可执行；真实选项灰显并注明
   未授权）、经验模式（full/none）、迭代与三类预算 → 创建后实验与 run 处于 queued。
3. 运行控制：queued → 启动；running → 暂停/取消；paused → 恢复（恢复沿用原 run，
   不重置预算、不新建 run）；取消不删除记录、不回退已接受版本；终态由持久记录保存，
   页面刷新/服务重启后仍可查询。
4. 查看：实验 drawer（运行模式/状态/停止原因/已用与上限/token 费用“未知”）；技能
   版本正文（不可信文本展示、完整性校验、超长截断）与任意两版本正文差异；轨迹摘要；
   门控/评估/经验模式摘要。
5. 业务晋升：实验 drawer → 输入业务 workspace_id → 晋升预览（来源实验/精确版本/
   评估有效性/模拟或真实证据）→ 无阻止项才可执行晋升；模拟证据需勾选显式旁路且
   永远标记为“非真实效果”。
6. 技能回退：同区域“回退到历史版本”（沿审计链回到此前明确生效的版本）。
   **回退不自动恢复已发布 Wiki**：Wiki 内容恢复是既有 Wiki Revision 的独立操作。

## 3. 故障与恢复步骤（运维）

- 预算耗尽：run 终态 `budget_exhausted`（stop_reason 前缀 budget_exhausted）；
  调整预算需要新建实验或人工编辑（不自动重跑）。
- 模型不可用：simulated 运行不依赖模型；real 运行在配置缺失/未授权时启动被预检拒绝
  （不静默降级模拟）。
- 租约丢失（worker 崩溃/网络）：lease 过期后新 worker 可接管继续（orchestrator 既有
  语义）；start/resume 会 409“正在启动/被占用”直到租约过期或状态收敛。
- 服务重启：状态在实验库持久记录；重启后刷新页面即可继续查询/操作；运行中的 run 由
  worker 进程独立执行，不受 API 服务重启影响（进程内无长同步等待）。
- worker 启动失败：run 保持 queued（可重试）；子进程日志写
  `<root>/runs/_console/<run_id>.log`。

## 4. 数据保留 / 备份 / 恢复

- 实验根目录（`skill_store.db` + `runs/` 封存执行 + `snapshots/`）即完整数据目录：
  备份=拷贝该目录（停止写入后一致性拷贝）；恢复=放回同一绝对路径或更新根映射。
- 业务库 evolution 表属业务数据库 schema（需迁移 P51 与既有 P45–P50）；业务绑定/
  审计事件随业务库备份。技能版本不可变（内容哈希校验），不接受原地修改。
- 审计事件 evolution_business_events 为 append-only（幂等键去重），是回退依据。

## 5. 模型恢复后的真实验收执行清单（未执行）

1. `python -m app.core.skill_evolution.cli trial-real-d preflight|create|run`
   （backend 目录；预算/模型 ID 经 07 §9 授权后填写；试运行最多 3 轮）；
2. 真实编译链路验证：promote real 运行接受版本到隔离业务库 → 捕获真实编译请求确认
   指令版本生效 → rollback 后新请求恢复历史版本；
3. Grader v2 人工标签（A1–A5 worksheet）→ review_store 入档 → 真实评审校准 →
   门控接线决策（当前 v2 未就绪，任何未注册评分器自动评分被拒，不静默回退）；
4. 阶段 7B 正式对照实验（预算授权后）；
5. 效果实验与配对统计报告（未达标须区分机制/效果）。

## 6. 关闭新功能时的兼容验证（实际执行）

- 8D 编译注入在 `wikiskill_business_compile_enabled=false` 时零注入、业务编译照常
  （stage8d ①）；8A 控制台读路径 feature off → 503 页面提示“功能未启用”，无副作用；
  8C 写端点独立开关 off → 503；迁移缺失 → 编译不注入（兼容约定）且晋升/回退给出
  显式 503，不自动迁移。

## 6a. 真实/绑定/冻结语义核查结论（本轮审计）

- 真实执行链路**已接通**（非仅“未实际调用”）：管理入口 create→start（授权+确认）→
  worker（子进程 CLI evolution-run 或测试线程同路径 build_run_actors）→ orchestrator →
  executor/maintainer/proposer 真实适配器 → HTTP 客户端。验证用本地 HTTP stub 整链
  打通（见 08c 验收 + stage8e_chain）；真实供应商调用仍是**未验证**（待授权/模型恢复）。
- 生产业务绑定：代码与隔离测试均完成（见 08d）；**生产未启用**（开关默认 False、无
  迁移执行、无绑定写入）。功能开启且 schema 缺失 → 晋升/回退 503 明确拒绝、编译
  fail loud，绝不静默忽略绑定却报成功。
- 版本冻结：compile run 首次执行时冻结绑定（pin 落盘）；中断恢复/重试仍用原版本；
  中途晋升/回退不影响该 run（stage8d ⑨ 覆盖）。编译代码与文档不可恢复已发布 Wiki。
- 模拟晋升旁路：请求参数不能开启；默认配置 allow_simulated=true 依旧被拒绝
  （stage8d 测试 + 08d §2）。
- 真实启动授权：全局开关 + 每次显式确认 + 参数绑定 run 记录；恢复不切换模型/数据/
  预算、不自动扩大授权（08c §1；stage8c 校验 + stage8e_chain 1/2 拒绝路径）。

## 7. 回归与验收（实际执行）

- 演化全量（contracts / phase1 / stage2–7f / stage8 / 8b / 8c / 8d）：**144 passed**。
- 业务编译管线：`test_wiki_pipeline_7d.py` 25 passed；
  `test_wiki_skill_default_v3.py` + `test_wiki_skill_default_equivalence.py`
  32 passed。
- 前端 `npm run build`（vue-tsc + vite）通过。
- 浏览器验收（隔离临时环境 data/dev8e，headless；环境与 token 已删除）：新建实验
  对话框（真实选项灰显“未授权启动”、数据集显示评分器用途）；模拟创建 → queued →
  启动（真实子进程 worker）→ completed（perfect_score 持久化，页面刷新后仍显示
  completed/终态）；晋升预览显示“默认阻止晋升（模拟模式）”，回退在缺 schema 业务库
  给出“不自动迁移生产库”明确提示；无未处理 pageerror。
- PostgreSQL：目标部署适配未验证（无 PG 环境）；SQL 为标准方言但**不以 SQLite 结果
  代替 PG 验收**，如实列为未验收。

## 7a. 测试清单映射（本轮实际运行；命令见 implementation-status 审计段）

| 组 | 文件 | 计数 |
| --- | --- | --- |
| 演化全量 | contracts, phase1, stage2–7f, stage8, 8b, 8c, 8d, 8e_chain | 见执行输出（约 154） |
| 编译管线 | test_wiki_pipeline_7d, wiki_skill_default_v3, equivalence | 25+32 |
| 前端 | npm run build（vue-tsc+vite） | 通过 |
| 未运行 | PostgreSQL 业务接入、生产库迁移、真实供应商调用、浏览器空数据/请求失败分支 | 见下 |

范围说明：历史“全量 144/148”等计数只含当时已存在的阶段文件；“本轮数量更大”是因
新增 stage8b/8c/8d/8e_chain 文件（不是任何既有文件被删/跳过）。判断是否遗漏以文件
清单逐条对照为准，不靠总数。未运行项在本文档与 implementation-status 明确列出。

## 8. 最终交付口径（A–F）

- **A 工程功能**：8B 正文与差异、8C 运行控制（含真实模式 worker 全链接入与每次启动
  确认/参数绑定）、8D 晋升/回退 + 编译绑定读取 + 版本冻结，均已实现并离线/隔离验证；
  未实现：生产绑定切换（生产库未迁移/未启用）、正文语义对比（无 v2 接入自动评分）。
- **A′ 分类（不把未验证写成未实现）**：已实现且离线/隔离验证 —— 上述全部工程能力 +
  HTTP stub 验证的管理入口→真实适配器链路 + 版本冻结（stage8e_chain / stage8d）；
  未实现 —— 生产绑定切换、生产 schema 迁移、正文语义对比；等真实环境验证 ——
  真实供应商调用效果与行为、Grader v2 人工标签/真实校准、阶段 7B 效果实验、生产启用。
- **B 离线/模拟验收**：上述后端回归 + 构建 + 浏览器隔离验收全部通过；Grader v2
  原型离线校准已跑（run_calibration_v2），未接入任何自动评分路径。
- **C 真实链路**：待模型恢复验证（入口与清单见 §5，未执行）。
- **D Grader v2**：原型 + 评审存储 + 离线校准完成；人工标签未填（A1–A5 仍
  pending_human_review）、真实校准未跑、正式门控未接线（v2 未就绪即拒绝使用，不回退）。
- **E 效果实验**：未执行；不宣称论文效果已复现。
- **F 生产启用**：未授权、未执行（未迁移生产库、未改业务绑定、未启用生产编译注入）。
