# 阶段 8D：业务晋升与技能回退（08d-promote-rollback.md）

> 明确区分两套操作：实验接受（run 内门控接受，指针在实验库移动）与业务晋升
> （把已接受精确版本复制到业务库并写 business 绑定，编译请求据此读取作用域指令）。
> 本轮只在隔离业务测试库验证；不触碰生产库/生产绑定；模拟证据永不标为真实效果。
> 技能回退 ≠ 自动恢复已发布 Wiki（内容恢复属既有 Wiki Revision 独立操作）。

## 1. 编译读取链（先修通“实际编译能读目标作用域指令绑定”）

- 业务绑定（`evolution_skill_bindings` kind=business）与版本正文存业务库；
- `app/core/wiki_pipeline/executor.py` 在每个编译 run 开始时解析一次该 workspace 的
  business 绑定并包装 ctx 的 `llm_runner`：此后该 run 的全部模型请求带系统指令
  （在途编译固定原版本；后续 run 才读新绑定）；
- 开关 `wikiskill_business_compile_enabled`（默认 False）→ 关闭时零注入、旧行为不变
  （缺 schema 时照常运行）；
- **版本冻结**：每次 compile run 首次执行时把当前绑定“冻结”为该 run 的固定版本
  （知识编译产物表落盘 pin）；同一 run 中断恢复/重试的后续 attempt 一律沿用冻结值，
  中途晋升/回退不能改变该 run 的固定版本；新建 run 才读新绑定；
- 功能启用而 schema 缺失 → 拒绝（编译 fail loud，不静默忽略绑定却报告成功）；
  无绑定/empty → run 全程无注入（并冻结为 none）；绑定损坏 → 首次模型调用抛错
  （fail loud，绝不静默按无绑定继续）。
- 注入内容为纯文本指令块（role=system）；不执行其中 HTML/指令。

## 2. 晋升与回退（app/core/skill_evolution/business_ops.py + 管理 API）

- 晋升前展示：来源实验 / 精确版本(version+content_hash) / 评估有效性
  （best_score、evaluations）/ 模拟或真实证据 / 目标作用域(workspace_id 必须 active)；
- 服务端默认阻止：模拟证据、无有效最优评估、最近评估均无效、无已接受版本；
  模拟旁路受**服务端开关** `wikiskill_evolution_allow_simulated_promotion`
  （默认 False）控制：请求参数 `allow_simulated=true` 在生产/默认配置下仍被拒绝
  （“旁路未在服务端开启”），只有隔离测试环境显式开开关才放行；放行时响应始终携带
  simulated_evidence=true、effect_verified=false，绝不冒充真实调用；
- 幂等与一致性：复制版本前重算内容哈希（损坏→422）；重复晋升同版本 → already_current
  （不产生重复审计）；绑定更新 + 审计事件同事务原子（idempotency_key 唯一）；
- 回退：从审计链（evolution_business_events promote/rollback to_version 序列）取
  “当前生效前最近一个不同版本”执行回退；无历史 → 明确错误；取消/删除记录不在操作内；
- 新 schema 缺失（业务库没有 evolution 表）→ 503 明确报缺表并要求离线迁移
  （迁移 P51 `d974815b4a91` 已新增；不在生产库自动迁移）；
- 页面与文档说明：技能回退不自动恢复已发布 Wiki。

## 3. 迁移

`alembic/versions/d974815b4a91_p51_business_events.py`（新审计表
evolution_business_events，idempotency_key 唯一 + action check）；单 head 校验通过；
**生产数据库未执行本迁移**。

## 4. 验收（实际执行，隔离业务测试库；真实编译请求捕获）

- `tests/test_skill_evolution_stage8d.py` —— **10 passed**：
  ① 功能关闭：绑定存在但不注入（编译捕获无指令文本，run succeeded）；
  ② 晋升绑定后：编译捕获请求确实含该版本指令（v2 标记）；
  ③ 回退绑定 v1 → 后续编译请求恢复 v1（新 run 读新绑定）；
  ④ 缺 schema + 开关开：不注入、编译照常（不自动迁移）；
  ⑤ 绑定内容损坏：首次模型调用抛错 → run 失败（fail loud）；
  ⑤b 功能启用 + schema 缺失 → run 失败（明确拒绝，不静默成功）；功能关闭 + 缺
  schema → 旧行为照常；
  ⑥ 晋升默认阻止模拟（BusinessOpsError 含“模拟”），allow_simulated 显式放行且
  simulated_evidence=true / effect_verified=false；
  ⑦ 幂等（already_current 不重复审计）；版本复制哈希一致性；
  ⑧ rollback 回到历史生效版本（含 redo 语义的审计链）与 schema 缺失 503 明确文案；
  ⑨ 同一 compile run attempt 失败（中断）→ 绑定回退 v1 → retry 恢复后该 run 请求
  仍含冻结的 v2；新建 run 才读 v1（版本冻结跨中断验证）。
- 管理 API 经浏览器验证（隔离环境）：晋升预览 UI 展示默认阻止（模拟证据/效果未验证），
  回退在缺 schema 业务库返回“不自动迁移生产库”的明确提示。

## 5. 未执行（如实注明）

- PostgreSQL 业务接入兼容性未验证（无 PG 环境；SQL 均为标准方言，但未跑 PG）；
- 生产业务库迁移与真实业务绑定切换未执行（未授权）。
