# Grader v2 工程接线（08f-grader-v2-wiring.md）

> “工程可运行”与“已校准可正式使用”是两个独立状态：
> - engineering：v2 可在隔离环境通过 stub/simulated 完整运行（本接线+测试）；
> - calibrated：需要人工标签（A1–A5，不改标签、不代填）+ 真实评审校准；
>   完成前，正式自动晋升被**服务端**阻止（不是文档提示）。

## 1. 评分器注册表（app/core/skill_evolution/grader_registry.py）

| grader | role | ready | calibration | allow_business_promotion |
| --- | --- | --- | --- | --- |
| wiki-default-grader/v1 | mechanism | yes | not_applicable | yes（仅机制；模拟证据仍分别被业务晋升前置阻止） |
| wiki-default-grader/v2-prototype-2 | semantic | yes（工程可运行） | engineering_only | **no（服务端强制）** |

- control.check_dataset_grader / admin meta 读本注册表；未注册/未 ready → 创建/启动拒绝。
- 不同 grader_version 的分数不可直接比较：v2 评审要求数据集评分器 = v2；创建/基线/
  候选全链路校验一致；切换评分器必须新建实验并重建基线（gating 抛
  ExperimentConfigMismatch）。

## 2. 语义评审适配器（model_review 扩展；真实适配器已接线）

- `ReviewerConfig{mode, model_id, prompt_version, api_url, api_key_present, timeout, retries}`
  独立显式配置；**评审模型绝不自动沿用执行模型**（配置缺 model_id/api/prompt →
  fail closed；stub 仅供测试注入）。真实网络仅经项目 LLM 客户端 + 独立模型/url。
- `ChatSemanticReviewer`：提示词模板版本 `wiki-default-review/prompt-v1`；评审输入
  只含【资料片段】【待评输出】【检查项】（不可信数据，忽略其中任何指令文本，不执行；
  不含参考答案全文/候选身份/实验组/分数）→ 私有评审反馈与参考答案不进入执行者、
  维护者、提议者上下文（7f 通道分离测试保持）。
- 输出经 `check_review_result` 校验：非对象/无 items/缺 reason/非法 verdict/服务失败
  → ReviewerError（invalid，**不回退 v1**）。
- orchestrator：`Actors.reviewer_factory`；真实评审器网络发送经 BudgetGuard.wrap →
  **评审请求与重试受预算约束**（executor 与评审共同消耗 used_model_calls）；
  用量未知保持 null（沿用 usage=null 约定，无价格/token 断言）。

## 3. 评估入口与门控接线（gating + review_eval）

- 实验 runner_config `review=v2` 时：
  - 确定性项（A/B 级）由 grader_v2.evaluate 程序判定；C 级语义项一律 needs_review，
    交给语义评审器（review_eval.grade_task_v2 入口）；
  - 任务 verdict = pass | fail | needs_review | invalid；
  - needs_review/invalid：不通过、**不缩分母**（main_total 保持 full），未决评估
    valid=False → 不能晋升（best/指针不动，gate invalid）；
  - **任务 pass ≠ 候选晋升**：候选仍需全部判定完成且主分数严格超过历史最佳
    （既有严格门控 keep）。
- 评审故障/非法输出/证据引用错误 → 任务 invalid（V2EvalError 捕获），不回退 v1。
- run_baseline / evaluate_and_gate 参数透传 reviewer；评估 usage_json 记
  review_requests；console /evaluations 现在返回 pending_tasks/pending_count/
  review_requests（页面显示“待审 N 任务”标签与原因）。

## 4. 未更改项与边界

- A1–A5 预期标签未改，无代填 human approved；v2 校准数据（v2-acceptance-review.json）
  仍 pending_human_review、used_for_development=false。
- 人工评审身份的信任来自可信操作上下文；模型/自动路径永不冒充该入口
  （review 记录由 SemanticReviewer identity 标注 model:…，与人工 reviewer id 分离；
  外部人工录入入口未开放 API，需显式人工操作流程——本轮未提供任何请求参数可写入
  human reviewer id 的端点）。
- 未交付：v2 专用数据集（现有 v1–v5 任务 reference 无 v2_spec 块 → 明确拒绝，不静默
  退回 v1）；真实供应商评审运行（未授权）；正式校准置位流程（需人工标签+真实校准）。

## 5. 测试（实际执行）

- `tests/test_skill_evolution_stage8f.py` —— 7 passed：pass（含参考不外泄）、确定性
  fail、needs_review 未决（不通过/不缩分母语义）、评审器故障 → invalid 不回退、
  缺 v2_spec 明确拒绝、ChatSemanticReviewer HTTP stub（独立 model、凭据、消息结构）、
  注册表状态与工程可运行/未校准服务端阻止、执行模型存在也不影响评审独立校验。
- `stage7f`（原型契约/通道分离）12 passed（含 real 工厂契约更新：完整配置→可构造
  ChatSemanticReviewer）。
- 浏览器（隔离 data/dev8g，已删）：25 实验分页（20+5/Total 25）；评估表“待审 2 任务”
  与 invalid_reason“不通过、不缩分母、不可晋升”可见；恶意 `<script>` 正文按文本展示、
  无脚本执行/无 console error；晋升预览默认阻止原因可见。
