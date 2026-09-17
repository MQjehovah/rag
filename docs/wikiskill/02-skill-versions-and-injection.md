# WikiSkill 阶段 2：技能包、不可变版本与指令注入（02-skill-versions-and-injection）

> 依据总方案 §5.3/§6.1/§7.2 与阶段 0 设计 `00-design.md`（含文末阶段 2 更新）。
> 完成：default 分支的指令版本存储（不可变）、Workspace/domain 绑定（实验/业务分离）、
> 确定性的单边界注入与记录；未实现经验维护者/提议者/门控/晋升/管理页。
> 记录事实：指令此前未注入（阶段 1 meta `skill_instruction_injected=false`）；本阶段起
> 实验可显式启用注入；**业务绑定默认不启用**，生产/旧流程不受影响。

## 1. 实际注入调用链与避免重复注入的方式

已核实调用关系（阶段 0/1 复核一致，无机械全包装）：

- default 编译阶段所有模型调用都经 executor ctx 的 llm_runner：
  stage → `wiki_default._llm_runner(ctx)`（wiki_default.py:151-157）→ ctx 注入 runner
  （实验路径 `configure_external_runners`，executor.py:69-80）或默认真实 runner
  （`_default_llm_runner` → `call_wiki_llm_json`）。
- 用途由 context 标签区分（builder 字符串）：内容生成 `wiki-synthesis`
  （含版本化）、`wiki-batch-summary`、`wiki-mapreduce`；主题路由 `wiki-ingest-page`
  （topic_route）不是内容生成；skill_route 当前确定性不调模型；API Reference 域本阶段
  不支持。

**选择边界**：不在 `wiki_default._llm_runner`/`_default_llm_runner`/`_pipeline_api_llm_adapter`
内部逐个包装（会重复/无处解析绑定），而在**实验执行注入的 runner 最外层做一次包装**
（`injector.SkillInjectingRunner`），并满足：

- 只对 `CONTENT_CONTEXTS` 内容生成调用插入冻结指令；`wiki-ingest-page` 等原样透传
  （reason=context_not_content，事件可见）；
- 每次调用只注入一次（injector 无状态，只在其 __call__ 内组装一次消息）；
- 注入后消息再进入 `RecordingRunner` → 捕获的 model_call 就是**注入后实际发送**的消息；
- API Reference 绑定由 `skill_store` 在包加载/绑定/冻结阶段直接拒绝（domain/runtime_ref
  校验），不会出现“绑定后静默忽略”；
- 生产 worker 未接线技能绑定（业务注入默认关闭）——无绑定行即功能关闭，行为等同阶段 1。

运行时序（记录语义）：

```
execution 启动（适配器）
  ├─ plan = FrozenSkillSet（mode none/empty/versions；启动即冻结）
  ├─ meta.skills / skill_instruction_version / set_hash 写入 trace
  └─ SkillInjectingRunner(plan, RecordingRunner(SimulatedModel), emit)
       └─ 每次内容生成调用：skill_injection 事件(injected/version_ids/set_hash/reason)
          → 组装 [system:指令] + 原消息 → RecordingRunner 记录注入后消息 → 模型替身
```

## 2. Runtime 版本 / 指令版本关系

| 维度 | 载体 | 谁可改 |
| --- | --- | --- |
| Runtime（受控代码） | 进程 Registry：builtin skills runtime_key + wiki.default 流水线版本；技能包内 `runtime_ref: wiki.compile.default.runtime/v1` 作兼容标识 | 代码/部署（进化不可改） |
| 指令版本（可进化） | `evolution_skill_versions`：SKILL.md/PURPOSE.md 全文、content_hash、seq、parent_version_id、source_type | 只新增（seq 单调）；无 UPDATE/覆盖 API |

比较与排序一律用整数 `seq`（同 skill 内单调递增），不依赖字符串大小；
`version_id` 形如 `default:0001`（`skill_id:seq%04d`）仅为可读标识。

## 3. 存储与迁移

- 复核 Alembic head：P44 `a9b8c7d6e5f4`（无任何文件把它作为 down_revision）→ 新增
  **P45 `b5c6d7e8f9a0_p45_skill_evolution_tables.py`**（down=`a9b8c7d6e5f4`）。
- 两表（`app/models/evolution.py`，注册在共享 Base metadata）：
  `evolution_skill_versions`（不可变版本 + 内容哈希 + seq 唯一 + 自引用 parent FK）、
  `evolution_skill_bindings`（kind experiment/business、workspace_id、domain、
  set_kind skill/empty；skill 形态必须成对、empty 形态全 NULL；唯一 scope；FK RESTRICT）。
- MANAGED 清单：`MANAGED_EVOLUTION_TABLES` + `_check_evolution_schema`（列/类型/
  nullable/主键/唯一约束/FK），旧库（非空、停在 P44）`init_db` 抛 schema_not_ready，
  升到 head 后检查通过（临时库验证；**未迁移任何生产/业务库**）。
- 演化库初始化路径（显式）：`root/skill_store.db` 由 `skill_store.session_for(root)`
  创建（独立 engine + create_all）；实验执行库与技能库分离——只在 `session_for`
  中建演化表，不在业务 ORM 里“加表即支持实验”。业务库若要启用这些表，须显式
  `alembic upgrade head`（本阶段不执行）。

## 4. 种子技能来源与人工整理差异

- 位置：`backend/eval/wiki_evolution/skills/seed-default-v1/{SKILL.md,PURPOSE.md}`。
- 来源：builtin `default/instructions.md`（仅内容结构声明：summary/facts/版本块/
  protected Section/Map-Reduce）+ `skill.yaml` 元数据 + 编译产物契约（源码核实：
  WIKI_SYNTHESIS_*/WIKI_MAPREDUCE 提示词的 JSON 结构、diff_notice、版本标签规则）。
- 人工整理差异（如实记录，不宣称与旧 instructions.md 等价）：新增适用/不适用条件、
  十步操作流程、JSON 输出契约显式化、禁止事项；PURPOSE 注明来源与演化历史，
  无 Pattern 引用（经验 Wiki 未建），`source_type=builtin_seed`，未标记“已通过进化验证”。

## 5. CLI 示例与实际结果

命令（backend 目录、`.venv`）：

```powershell
# 导入并校验种子（幂等；同内容同 source 复用既有版本）
python -m app.core.skill_evolution.cli seed --root eval/wiki_evolution/runtime
python -m app.core.skill_evolution.cli seed --root eval/wiki_evolution/runtime
#   → seed ok: skill=default version=default:0001 hash=039f65a36c2aedca source=builtin_seed
#     （第二次 seed 幂等，不再新增版本）

# 新增不可变版本（同内容也新建 seq，绝不覆盖）
python -m app.core.skill_evolution.cli add-version --root eval/wiki_evolution/runtime `
      --skill-dir eval/wiki_evolution/skills/seed-default-v1 `
      --parent-version default:0001 --source manual_seed
#   → new version: default:0002 (hash=039f65a36c2aedca)

# 查看版本（按 seq 排序，不依赖字符串）
python -m app.core.skill_evolution.cli versions --root eval/wiki_evolution/runtime

# 绑定（experiment 生效；business 默认不启用）与查询
python -m app.core.skill_evolution.cli bind --root eval/wiki_evolution/runtime `
      --workspace ws-demo-a --version-id default:0001
python -m app.core.skill_evolution.cli bind --root eval/wiki_evolution/runtime `
      --workspace ws-demo-b --version-id default:0002
python -m app.core.skill_evolution.cli bindings --root eval/wiki_evolution/runtime

# 用显式版本 / 绑定 / 空集合执行
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime `
      --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-001 `
      --binding-workspace ws-demo-a
#   → exec … task=wiki-default-001 run=succeeded … skills=versions:default:0001 inj_events=1 inj_true=1
python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime `
      --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-003 `
      --empty-skills
#   → skills=mode:empty inj_events=1 inj_true=0（显式空集合可执行）

# 按原执行版本/内容哈希重放（不自动用最新；版本缺失/哈希失配明确失败）
python -m app.core.skill_evolution.cli rerun --root eval/wiki_evolution/runtime `
      --dataset eval/wiki_evolution/datasets/wiki-default-v1 --execution-id a0274373ad37481eb2b91fce0b1cdacf
#   → rerun skills: frozen original versions ('default:0001',) (hash 校验通过)

# 查询/报告
python -m app.core.skill_evolution.cli show --root eval/wiki_evolution/runtime --execution-id <id>
python -m app.core.skill_evolution.cli report --root eval/wiki_evolution/runtime `
      --output eval/wiki_evolution/runtime/report.md
```

实测捕获消息（`model_call.messages[0]`）首行：
`# 技能指令 default（版本 default:0001）` / `…（版本 default:0002）` —— 版本不同，
实际发送的 system 指令文本不同；每条内容生成调用恰好 1 个 `injected=true` 事件。

## 6. 测试命令与结果

```powershell
python -m pytest tests/test_wiki_pipeline_7d.py tests/test_wiki_skill_default_v3.py `
                  tests/test_skill_evolution_contracts.py tests/test_skill_evolution_phase1.py `
                  tests/test_skill_evolution_stage2.py -q -p no:cacheprovider
# 78 passed, 15 warnings in 46.51s
#   = 48（既有编译回归） + 20（阶段 1） + 10（阶段 2 新增）
```

阶段 2 验收（第八节 1–11）对应用例：版本差异进入捕获请求 / 单次注入且原约束保留 /
记录为注入后消息（`test_distinct_versions_reach_model_and_single_injection`）；
绑定切换 vs 在途冻结（`test_binding_switch_affects_new_runs_not_inflight`）；
重放原版本 & legacy 无指令重放（`test_rerun_uses_original_version_not_latest`、
`test_legacy_history_replays_without_instruction`）；空集合可执行；缺失/损坏/哈希失配/
不兼容域与 Runtime 明确失败；不可变与重复导入语义；workspace/实验/业务绑定互不串用、
功能关闭行为保持；临时库迁移（P44 旧库 fail closed → head 通过）与 MANAGED 检查。

## 7. 新旧执行记录的重放语义

- 新记录：meta.skills 冻结（mode/version_ids/content_hashes/set_hash），每次模型调用
  的注入状态在 events（skill_injection + model_call 注入后消息）。
- 重放默认：用原记录 version_ids + 原 content_hashes 校验后冻结（`freeze_versions`），
  绝不自动使用当前最新版本；版本缺失 → SkillStoreError；内容哈希与原记录不一致
  （库被篡改）→ SkillStoreError（不静默替换）。
- 阶段 1 历史（无 skills 字段/mode=none）：按旧“无指令”语义重放（FrozenSkillSet.disabled），
  不自动补入种子技能；可审计记录注入事件 injected=false reason=disabled。
- 无法恢复的配置（原版本缺失/损坏）明确失败并报告边界，不做“近似重放”。

## 8. 已验证范围 / 未验证范围

已验证（离线/隔离，模拟模型 + 合成资料）：
- 注入边界正确性（只对内容生成调用注入一次；topic/skill 路由与 api 适配不注入）、
  捕获消息含注入、版本冻结与绑定切换/重放语义、空集合、不可变与失败路径、
  迁移兼容、阶段 1 与既有编译回归全绿（78 passed）。
未验证：
- 真实供应商链路与真实模型上的注入效果（无凭据/资料授权）；“指令使真实模型输出更好”
  未声明、未测量；
- 业务绑定启用路径（默认关闭，worker 未接线）；多技能集合注入（阶段 2 为单技能域）；
- 权重/效果门控、经验维护者、提议者、晋升（阶段 3+）。

## 9. 阶段 3 经验 Wiki 接入位置

- 数据：`pattern/修订` 需要新表或独立经验库（沿 `evolution_skill_bindings` 同库追加或
  独立 `experience_store.db`，建议独立文件 + 独立 metadata，避免污染业务 Base）；
  维护者输出补丁经程序校验后写入；索引与日志可先落文件（JSONL）。
- 接线：复用本阶段冻结与 trace 事件模型——经验条目引用 `execution_id` 与轨迹
  `skill_set_hash`/事件；维护者仅读授权 execution 事件（`trace.iter_events`），
  建议新增受控读取接口而不是直读文件系统。
- 记录：本轮 SKILL/PURPOSE 已含“演化历史/关联经验”占位（种子注明无 Pattern），
  阶段 3 把其变为真实引用并校验作用域/证据执行 id。


---

## 阶段 2 兼容性修正记录（2026-09-07，阶段 3 前置复核）

复核发现并修正两处：

### 1. P44 旧库启动兼容性（原实现违反“关闭不影响旧功能”）

原实现把两张演化表加入 `MANAGED_EVOLUTION_TABLES` 并在 `check_managed_migrations`
核心分支校验：P44 旧库（合法业务库）即使 WikiSkill 未启用也会因缺演化表
schema_not_ready —— 与“新功能关闭时不影响旧编译”冲突，且 create_all 可能静默建表。

修正（最小拆分）：
- 演化模型移到**独立 metadata**（`app/models/evolution.py`，不注册业务 Base）：
  业务库 init_db/create_all 不再感知演化表 → 不自动 DDL；
- `database.py` 还原为阶段 0 状态（核心 Schema 校验不变，其他业务表检查原样保留）；
- 启用路径显式校验：`create_evolution_schema(engine)`（实验库）与
  `require_evolution_schema(engine, context=...)`（启用时缺表即报错，不静默退化；
  业务库启用须先 Alembic 迁移）。
- 新增 P46（`a4b5c6d7e8f9`）经验 Wiki 5 表，head=P46；演化 schema 含全部 7 表。

测试：`test_p44_legacy_db_feature_off_compile_runs`（P44 旧库 init_db 通过，
require 报 evolution_schema_not_ready，功能关闭下跑通真实 default 编译链）；
`test_migration_head_and_lagging_db_compat`（head 后 require 通过、无缺失表）。
临时库验证；未迁移任何生产/业务库。

### 2. 不可变技能版本复核（补充“无覆盖 API”之外的证据）

- 写入口核查：`skill_store` 对版本表只有 `_insert`（新增），无 UPDATE/DELETE API；
  `import_seed`（同内容同来源幂等复用）与 `add_version`（始终新 seq）都不会改写旧行。
- 读取一致性：`get_version` 每次读取重算内容哈希并与入库哈希比对，失配（含直接改库）
  抛 `SkillStoreError`，不做静默替换。
- 阶段 3 保证：种子 PURPOSE.md/正文不做原地修改；经验关联（证据/修订）只写入
  独立 pattern/revision 表；需要改变技能包时创建新版本（新 seq）。

验证结果：修正后 `tests/test_skill_evolution_stage2.py` **11 passed**（含新增 P44 兼容
与 require 用例）。
