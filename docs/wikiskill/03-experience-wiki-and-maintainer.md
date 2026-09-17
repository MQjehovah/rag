# WikiSkill 阶段 3：经验 Wiki 与维护者（03-experience-wiki-and-maintainer）

> 依据总方案 §5.2/§6.2 与阶段 2 报告（含 P44 兼容性修正）。
> 完成：独立经验 Wiki（Pattern/修订/维护运行/日志/索引），授权 train 轨迹采样，
> Wiki Maintainer 结构化输出经 Schema 校验/事务原子应用/幂等/冲突保护，
> CLI 与一次性一致导出。未实现：Proposer、门控/晋升、管理页、真实供应商。
> 明确区分：**流程验证**（本阶段，模拟维护者 + 真实采样/记录/校验/事务/导出）与
> **真实模型归因能力验证**（未做，不声明）。

## 1. 阶段 2 前置兼容性复核结果（修正已合入 02 文档）

- P44 旧库：演化表从核心 `check_managed_migrations` 拆出（database.py 已还原到阶段 0
  状态，其他业务表校验不变）；演化模型使用**独立 metadata**（models/evolution.py），
  业务库 init_db/create_all 不再感知/自动 DDL；
  关闭时 P44 库可正常启动并跑通真实 default 编译（新增测试）；启用时
  `require_evolution_schema` 缺表即报错；新功能关/开两态旧库测试在临时库通过。
- 不可变技能版本复核：写入口仅新增（`_insert`），无 UPDATE/DELETE API；`get_version`
  每次重算内容哈希校验一致性（改库即损坏报错）；阶段 3 不原地改种子 PURPOSE.md——
  两轮维护后 `default:0001` 哈希/正文/PURPOSE 未变（测试断言）。

## 2. 实际改动与存储选择

新增/修改：

| 文件 | 内容 |
| --- | --- |
| `app/models/evolution.py`（重写） | 独立 metadata；7 表：技能版本×2 + Pattern/修订/维护运行/日志/索引×5；`create_evolution_schema/missing_evolution_tables/require_evolution_schema` |
| `alembic/versions/a4b5c6d7e8f9_p46_experience_tables.py` | P46（down=P45）：经验 5 表 |
| `app/core/skill_evolution/experience_store.py` | 内容/证据校验、读取（pattern/revisions/logs/index/run）、索引重建、原子 `apply_plan`、失败诊断行、导出与 Markdown 渲染 |
| `app/core/skill_evolution/trace_sampling.py` | 授权 train 枚举、采样规则、每日志截断与预算 |
| `app/core/skill_evolution/maintainer.py` | 提示词/角色 Schema、模拟维护者、受限重试、`run_maintenance` 编排（幂等键/冲突/失败诊断） |
| `cli.py` | maintain-list / maintain-preview / maintain-run / maintain-status / patterns / experience-export |
| `tests/test_skill_evolution_stage3.py` | 阶段 3 验收 10 项（第九节 1–13 覆盖） |
| `docs/wikiskill/02…` | 追加“阶段 2 兼容性修正记录” |

**存储选择**：权威源 = 实验控制库 `root/skill_store.db`（独立 evolution metadata），
技能版本与经验同库事务隔离；Pattern/修订/日志/索引全部为 DB 行。Markdown 仅由
`experience-export` 生成**单次一致快照**（同一只读会话导出 index.md/logs.md/patterns/*.md），
不是另一套可写权威源（无回写路径）。经验按 workspace+domain 隔离、不进业务 RAG
（业务库无演化表、检索不查询这些表）。

## 3. 作用域、原子性、幂等与历史保留

- **作用域**：授权 = `split=='train'` + 同 `dataset_version/domain` + 同 workspace；
  val/test 与跨 workspace 的 ID 直接拒绝（即使已知）；轨迹必须已封存且哈希有效。
- **原子性**：`apply_plan` 单事务完成「运行行→create/update 模式与修订→日志→索引重建
  →applied 收尾」，任一步失败整体回滚；非法输出只写 failed 运行行（诊断），
  模式/修订/日志/索引零部分写入。
- **幂等**：键 = scope+dataset+输入执行集合+配置（base 只用于事务内冲突比较）；
  重复提交命中既有 applied 行，不重复建模式/追加日志（测试断言 1 模式、日志不重复）。
- **冲突**：基础索引哈希/模式头在事务内复核；过期基础 → `MaintenanceConflictError`，
  不覆盖较新经验（测试：stale base 不产生新模式/不覆盖索引）。
- **历史保留**：模式修订 seq+1 追加、不可变；新证据修正后旧修订仍可读（测试 seq1/2
  并存）；状态 observed→supported（冲突证据→contradicted），单次观察不自动提升。

## 4. 轨迹采样与日志规则

- 默认失败 ≤5、成功 ≤3；不足用实际数量不凑数；确定性排序（created_at, execution_id）；
  采样配置与所选输入记入运行行。
- 基础设施/模型失败（infra_or_model）默认不进入技能缺陷证据池；`--include-infra`
  显式包含（测试断言默认排除/显式包含）。
- 每条日志默认上限 15,000 字符：记录 `truncated` 与 `original_chars`、保留执行摘要/
  阶段/评分/输出开头；不修改 Raw；总量超预算 → 明确报错暂停（不静默删历史）。
- 阶段 1/无指令执行：技能身份标记 unknown/not_injected（prompt 含
  `skill_instruction=unknown/not_injected`），不为它推断技能版本。

## 5. 维护者调用与记录

- role=wiki-maintain，经 runner 接口（messages+context+timeout），有限重试（≤2 次），
  结构/大小/允许字段校验；usage=null 不伪造成本；消息与角色、输入集合、估算记录在
  维护运行行 `model_calls_json`。
- 执行 Agent 的技能注入与维护者调用互不混用（维护者不在 CONTENT_CONTEXTS，注入器只
  作用于编译内容生成；维护者 runner 无技能注入包装）。
- 非法输出/超时：记录 failed 运行行（error_code/message），经验 Wiki 不变（测试）。
- 输出字段白名单（顶层/模式字段/证据 id），pattern_id 只接受受控标识；
  证据 id 必须 ∈ 本轮授权集合；更新目标存在且 base_revision_id == 当前修订，
  补丁未知字段/歧义失配拒绝。

## 6. 实际运行命令与结果

```powershell
python -m app.core.skill_evolution.cli maintain-list --root eval/wiki_evolution/runtime --dataset eval/wiki_evolution/datasets/wiki-default-v1
# workspace=ws_c25d0a84d1d8 authorized train executions=3
#   108cd048… wiki-default-001 kind=success
#   6373f5a3… wiki-default-002 kind=quality_failure（omit_conditions）
#   559595ff… wiki-default-002 kind=quality_failure（invalid_json）

python -m app.core.skill_evolution.cli maintain-preview --root … --dataset …
# chosen=3 … log_total_chars=2196 (cap/条=15000)

python -m app.core.skill_evolution.cli maintain-run --root … --dataset …           # 第一轮 create
# → applied created_patterns=["pat_40db6ec1ba97a954"] model_calls=1
python -m app.core.skill_evolution.cli maintain-run --root … --dataset … `
      --model-profile update --pattern pat_40db6ec1ba97a954                        # 第二轮 update
# → applied updated_patterns=[…] model_calls=1

python -m app.core.skill_evolution.cli patterns --root … --workspace ws_c25d0a84d1d8 --pattern pat_40db6ec1ba97a954
python -m app.core.skill_evolution.cli experience-export --root … --workspace ws_c25d0a84d1d8 --out-dir eval/wiki_evolution/runtime/export-demo
# → exported 3 files（index.md/logs.md/patterns/pat_40db6ec1ba97a954.md）
```

## 7. 两轮维护前后的模式差异（真实运行）

| | 第一轮（create） | 第二轮（update） |
| --- | --- | --- |
| 运行 | maint_0fd4… applied | maint_a952… applied |
| 模式 | pat_40db6ec1ba97a954（observed→supported） | 同一模式，seq 1→2 |
| 现象 | 失败样本正文缺适用/前置要点（证据=两个失败执行） | 保留，证据并入（同集去重） |
| 原因假设 | `（待验证）技能指令未显式要求逐条覆盖条件要点…` | `新增证据后修正的假设：（第二轮补充证据）`（marker 来自模拟输出，仅演示流程） |
| 支持证据 | 6373f5…, 559595…（2） | 同上 2 条 |
| 历史 | seq1 可读 | seq1+seq2 均可读 |
| 种子 default:0001 | hash 039f65a36c2aedca | 未变（正文/PURPOSE 未改） |

> 模拟维护者的“修正假设”文本仅供流程验证；不构成真实质量/归因结论。

## 8. 作用域/原子/幂等/历史证据（测试名）

- `test_val_test_and_cross_workspace_rejected`、`test_corrupt_unsealed_trace_rejected`；
- `test_invalid_evidence_or_patch_no_partial_write`（零部分写入 + failed 诊断行）；
- `test_idempotent_and_conflict`（重复提交不重复、stale base 冲突不覆盖）；
- `test_two_rounds_and_seed_immutability`（历史保留 + 种子哈希/正文不变）；
- `test_export_matches_revisions`（导出与 DB 修订一致）；
- `test_model_error_keeps_diagnostics_only`、`test_maintainer_request_content_and_truncation`、
  `test_sampling_rules_no_padding`、`test_infra_not_skill_evidence`。

## 9. 测试命令与实际结果

```powershell
python -m pytest tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py `
                  tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py `
                  tests/test_skill_evolution_stage2.py tests/test_skill_evolution_stage3.py `
                  -q -p no:cacheprovider
# 89 passed, 16 warnings in 62.92s
#   = 48（既有编译回归）+ 20（阶段 1）+ 11（阶段 2，含 P44 兼容新用例）+ 10（阶段 3）
```

## 10. 尚未验证的真实模型能力（如实）

- 真实供应商维护者调用（无凭据/资料授权）：本阶段全部为 SimulatedMaintainer，
  证明的是采样/调用记录/Schema 校验/事务应用/导出流程，非真实模型提炼质量；
- 经验模式对技能指令改进的真实收益未测（阶段 4+ 提议者/门控）；
- usage 仍为 null（估算口径）；上下文超限策略为显式暂停，未做压缩系统。

## 11. 阶段 4 提议者可用的读取接口与数据契约

- 读取接口（只读，供提议者受控使用）：
  - `experience_store.list_patterns(db, ws, domain)` / `get_pattern` / `list_revisions`
    （当前内容 + 全历史 + 证据 execution_id 与状态）；
  - `experience_store.get_index(db, ws, domain)`（问题/原因/建议索引，与已应用修订一致）；
  - `experience_store.list_logs(db, ws, domain)`（演化日志，含 run_id/seq）；
  - `trace_sampling.list_authorized_train_meta`（train 授权执行，role 侧只读）；
  - 维护运行模型调用记录在 `evolution_maintenance_runs.model_calls_json`
    （角色/消息/估算/usage=null）。
- 数据契约：Pattern 修订载荷 JSON（phenomenon/cause_hypothesis/suggestion/applicability/
  supporting_execution_ids/conflicting_execution_ids）、索引条目
  （pattern_id/title/status/problem/cause/suggestion）、日志文本；scope=workspace+domain。
- 提议者阶段建议以这些读取接口为输入，输出 create/patch/no_action 提案并写入技能新版本
  （`skill_store.add_version`），再进入验证门控——不在本阶段实现。
