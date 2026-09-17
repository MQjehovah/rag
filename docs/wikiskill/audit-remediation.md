# WikiSkill 审计修复（audit-remediation.md）

范围说明：本轮按审计问题逐项复核并修复；每项列出 审计问题 → 根因 → 修改 →
反例测试 → 实际验证 → 剩余限制。未完成项如实标注并给根因，不把“未验证”写成
“未实现”，也不把已修复写成仍存在。

## A. 已修复且离线验证

### A1 语义评审输出解析（NameError + 脆弱解析） — 已修复
- 审计问题：`_json.JSONDecodeError` 未定义 NameError；尾随文本/未闭合经脆弱括号扫描
  可能被吞；截断被静默应用。
- 根因：重构时把局部 `import json as _json` 移除后 `except _json.JSONDecodeError`
  残留；解析用非字符串感知括号扫描。
- 修改（app/core/skill_evolution/model_review.py 重写）：
  * 模块级单一 `import json`；`_parse_json_object` 只接受“完整 JSON 对象”或“单个
    整块 fenced JSON（fullmatch 前后仅空白）”，拒绝尾随文本/未闭合，删除括号扫描；
  * 移除 `build_review_prompt` 的 `[:20000]` 静默截断与 review_eval `_source_texts`
    的 `[:20000]`；完整内容送审，超 `REVIEW_INPUT_LIMIT`（默认 60k）抛
    `ReviewerInputLimitError`（按 invalid，不截断；分块未启用=最小可靠方案）；
  * 来源不可读/越界 → 明确 V2EvalError（不再注入“不可读”占位文本）。
- 反例测试：`test_skill_evolution_stage8i.py` —— 尾随垃圾、未闭合、非对象、空、
  fenced 非法均拒绝；NameError 回归（错误路径只抛 ReviewerError）；长文本超限拒绝
  且完整内容在提示词内（无截断证据：源文本完整出现并以最后检查行收尾）。
- 验证：`python -m pytest tests/test_skill_evolution_stage8i.py -q` → 通过（含下同
  新增用例）。

### A2 评审 item 校验（类型/未知/重复/缺失/位置/引文） — 已修复
- 修改：`check_review_result` 扩展 —— 逐 item 类型检查、verdict/reason 合法、
  `expected_check_ids` 提供时禁止未知/重复并强制全覆盖（缺失不得默认 pass）；
  新契约字段 `source_loc`/`cand_loc`（`src:N[#s-e]`/`sec:N[#s-e]`）与可选 `quote`
  做可追溯校验：未知/越界/NONEXISTENT → ReviewerError；quote 须出现在引用范围。
  “缺失内容”类判定允许无位置（不伪造输出位置）；旧字段
  `source_evidence_loc`/`candidate_loc` 保留为不透明备注（不参与可追溯校验）。
  文档口径：位置存在只证明可追溯，不代表语义支持正确。
- 反例测试：8i —— 未知/重复/缺失、类型错误、无 reason、非法位置（NONEXISTENT:999、
  src:999、越界、语法）、引文不匹配、合法“缺失内容无位置”判定。
- 验证：`pytest test_skill_evolution_stage8i.py` 通过。

### A3 评审记录保留与尝试计数 — 已修复
- 修改：`ChatSemanticReviewer` 记录 `last_attempts/last_error`（每次请求均计入同一
  预算；耗尽 → ReviewerError）；`review_eval` 在结果中保存 `review_records`
  （reviewer_identity、config_fingerprint（非秘密）、attempts、last_error）只进评估
  记录；配置指纹 `ReviewerConfig.fingerprint()` 不含密钥（endpoint 仅 host）。
- 反例测试：8i —— 瞬时失败后重试成功 `last_attempts==2`；耗尽时 last_attempts==1 且
  last_error 有值；`review_records` 存进 grade 结果；token `review_records` 不出现在
  执行/维护/提议/gating/orchestrator 等优化角色模块（扫描断言）。
- 验证：`pytest test_skill_evolution_stage8i.py tests/test_skill_evolution_stage7f.py
  tests/test_skill_evolution_stage8f.py` 通过。

### A4 受影响回归（实际执行）
- `pytest tests/test_skill_evolution_stage8h_e3.py tests/test_skill_evolution_stage8h_mgmt.py
  tests/test_skill_evolution_stage8g.py` → 8 passed（单 stub 两轮闭环、管理单链 worker、
  v2 编排/评审全链在严格解析+位置契约下仍通过——测试 stub 的 id 抽取限定到检查段，
  无协议改动）。
- 涉及 model_review/review_eval 的既有套件（7f/8f/8g/8h/8i、stage8、wiki_pipeline_7d）
  批量通过（见最终输出）。

## B. 当前环境仍存在的工程缺陷（未在本轮完成；根因明确）

### B1 业务晋升证据判定（审计二）
状态：**未修复（保留原行为）**。
根因/影响：`business_ops.promotion_preview` 用“是否存在任一 real 配置 run”推断
模拟/真实；证据未与“待晋升技能集合哈希 + 精确评估 + 数据集指纹 + grader_version +
执行/评审配置指纹 + 接受事件 + 批准”绑定；v1 mechanism 在 `allow_business_promotion`
=True 下可被用于业务晋升资格。修复需要引入 受控证据来源边界/批准状态 与精确集合
绑定校验（见审计清单 2.1–2.7）。已在 control 的 grader_registry 保留
labels/real_calibration 双状态与 allow_business_promotion 门，但业务晋升资格判定
未收紧；需在 business_ops 增加 绑定集合哈希+评估+接受事件+批准 的 fail-closed 校验
与负向测试（real 空 run/无关 run/错误集合/过期评估/未校准/模拟旁路/预览后目标变化）。
下一轮实施顺序建议：先定义 `PromotionEvidence` 绑定记录并迁移（含多技能集合），
再收紧判定与幂等 CAS。

### B2 多技能集合晋升与注入（审计三）
状态：**未修复（保持单成员 members[0] 语义）**。
根因/影响：`_accepted_target`/binding 列（version_id/skill_id 单值）仅支持单技能；
编译注入按单绑定版本包装。完整集合（多成员+固定顺序+集合哈希）、全有或全无复制、
集合长度上限、回退完整集合、跨中断冻结完整集合均未实现；schema 需按 Alembic 现
head 新增迁移（集合表或 JSON 绑定）并在隔离库验证旧单技能兼容。属较大结构变更，
未在本轮完成。

### B3 晋升/回退 CAS 与幂等（审计四）
状态：**未修复（保留现有幂等语义）**。
根因/影响：绑定更新无“预期当前集合哈希/修订”CAS，后写可覆盖并发结果；幂等键按
“workspace+目标版本”永久去重会阻止“晋升→回退→再次晋升同版本”。需要：请求携带
幂等键 + 预期集合哈希，服务端同事务 CAS+审计，冲突 409；同键同请求重放返回原结果、
同键不同内容拒绝。含 B2 的集合哈希 CAS；未在本轮完成。

### B4 冻结完整运行配置与预算授权（审计五）
状态：**已修复（本轮，stage8j 3 passed + 链路回归 21 passed）**。
修改：新增 `config_freeze.py`（RoleFrozen/评审冻结快照/模板哈希/非秘密指纹，
api_key 仅存引用）；real 角色 runner 与评审器一律从冻结配置构造，HTTP 载荷
model/endpoint/max_tokens 全部来自冻结（real_adapters 改用 cfg 绑定发送，不再读
当前 settings）；首次授权 start 时冻结落库，start/resume 校验 config/reviewer
指纹（缺失/漂移 409）；非受控旧记录明确拒绝（frozen_missing，不以默认补齐）；
评审请求实际透传 max_tokens（wikiskill_reviewer_max_output_tokens）；管理 API
ActionBody/run 视图携带指纹。反例测试 stage8j：创建后 settings 漂移仍用冻结模型
（载荷不含 EVIL）、评审载荷 max_tokens=512、错误指纹 409、旧记录构造 fail-closed。
根因/剩余：见下。
根因/影响：`create/build_run_actors` 从创建时 settings/run 配置读取角色模型/评审
配置；未持久化“非秘密配置快照（角色 provider/endpoint host/model/prompt 模板哈希/
参数/预算指纹）”供 worker 与 resume 严格复用；输出 token 上限未传入真实语义评审
载荷（评审走通用 chat，未带 max_tokens）；start/resume 确认只比对数据集与预算，未
绑定完整配置指纹；旧记录缺冻结字段无“需迁移/不可恢复”标记。本轮未完成；真实执行
被授权前不触发网络。

### B5 阶段 7 正式实验接口（审计七）
状态：**非模型工程缺口清零（2026-09-09 R2/R3；不得将上轮离线 9 passed 当作本轮证据）**。
- 上轮已落地：A/B `evolve=false`、B 可解析种子、`dataset_version` 写入 protocol、
  CLI create/run/resume/status、全冻结后 test。本轮补修：real 走 `build_run_actors`
  与冻结指纹确认；frozen/manifest 必填字段删除即拒绝；dataset/protocol/budget
  字节变化在 run/resume/test 前拒绝；新增 `experiment-batch-test`。
- 反例：`tests/test_skill_evolution_stage8y_formal_batch.py`（本轮 17 passed，
  含 CLI 本地 HTTP stub 全链与 v2 reviewer）。
- 限制：本轮只验证本地 HTTP stub 与模拟哨兵禁回退；真实供应商、Grader v2 真实校准、
  正式 7B 效果实验未执行，不得宣称论文效果已复现。

## C. 依赖真实模型/人工批准/外部环境的验收（未执行、保留）
- 真实供应商评审/执行与 7B 效果实验（需真实模型与授权）。
- Grader v2 人工标签（A1–A5）与校准置位/批准（人工；未代填/未伪造）。
- PostgreSQL 业务接入验收（本地无 PG；未擅自安装；不以 SQLite 结果代替）。

## D. 业务技能完整闭环整改包（2026-09-08 后续轮；与前文 B1–B3 历史更正对应）

> 前文 B1–B3 “未修复” 声明对应早期轮次；其后已分别落地（见
> implementation-status.md：B2 只读解析轮 → B2 晋升写路径/集合 CAS 轮 →
> 本包）。本节记录本包的根因/修改/反例/验证与限制，不再与前文矛盾。

### D1 成功请求重放（审计最新发现：成功后原请求重发 → binding_conflict）
- 根因：promote/rollback 先做 CAS 预期（rev/hash）校验、再查幂等记录；成功后原
  请求（携带已过期的预期令牌）重发会先命中 binding_conflict。
- 修改（business_ops.promote/rollback）：鉴权/作用域后先按幂等键识别已完成操作
  （请求指纹含 完整目标成员 + 作用域 + 预期修订/集合状态 + 证据引用），命中即返回
  历史结果（members/set_hash/rev），不重写、不要求旧预期仍等于当前；同键不同
  请求内容/预期 → idem_content_conflict。回退同键重放不因历史变化自动改算目标。
- 反例：stage8n test_replay_after_success_with_stale_expected_returns_original、
  test_same_key_different_expected_rejected、rollback same-key 等 → passed。

### D2 集合 CAS 预期令牌与防 ABA（显式首绑语义）
- 根因：无绑定行没有可比较的状态列；缺字段曾被当作“任意状态”。
- 修改：首次绑定显式令牌 (expected_rev=0, expected_set_hash=NO_BINDING_HASH)，
  与空集合哈希/任意版本哈希严格区分；正式写入口（API explicit_cas=True）缺令牌
  → 422 cas_token_incomplete；DB 层护栏用请求原始预期修订（写入前不重读最新 rev
  顶替）；成功 rev+1；A→B→A 后旧 rev 请求 409；首绑由真实唯一约束
  ux_evolution_binding_scope 收敛（IntegrityError→409）。
- 反例：stage8n test_explicit_cas_token_semantics / concurrent / 8m I/H → passed。

### D3 完整回退（审计集合历史）
- 修改：rollback 从审计事件重建有效集合状态链（完整成员，兼容旧单版本事件，
  不编造）；目标 = 链上最近不同状态（支持多成员、合法初始无绑定 → 显式空集合行
  rev+1）；写路径 = _materialize_set_binding（rev+1 + members/set_hash），同事务
  审计完整 from/to。
- 反例：stage8n test_rollback_multi_and_initial_none / rollback_to_initial_none。

### D4 原子性与旧写入口
- promote/rollback：复制+绑定+审计同事务，任一步失败整体回滚（8m 反例保留）。
- skill_store.bind 统一物化 members_json/set_hash/rev（切换 rev+1），不再留下
  过期列；P52 缺列写一律 binding_p52_required fail closed（删除 legacy 写分支）。
- 反例：stage8n test_skill_store_bind_materializes_consistent /
  test_business_promote_fails_closed_without_p52。

### D5 完整集合编译注入（B2 项 4）
- freeze/_apply_pin 存完整 members+set_hash，逐成员重读校验；旧单技能 pin 兼容；
  render_set_block 与实验一致的指令正文/排序/长度上限。
- 反例：stage8n A（双技能晋升后新编译请求含全部指令、顺序正确）、C（同 run 跨
  中断冻结不变、新 run 才读新集合）。

### D6 可信晋升资格（B1 项，最小可靠闭环）
- 根因：配置 real=true 只说明配置；证据未与精确集合/接受事件/评估/数据集/grader/
  执行配置绑定；生产缺环境门。
- 修改：settings.wikiskill_promotion_env（production|isolated-test）；production
  一律拒绝本包业务晋升（真实供应商/人工校准/批准独立待办）；isolated-test：
  模拟证据需请求 allow_simulated + 服务端隔离旁路；真实证据（completed+real+
  有调用+数据集一致）须命中 accepted 门控事件精确集合锚点（evolution_gate_events
  next_set 成员一致）。预览与执行共用 _eligibility_report，执行时重校验，目标变化
  拒绝（不悄悄改目标）；测试/模拟记录永不 effect_verified=True；v1 机制与未校准
  v2 仍不可正式晋升（grader_registry 门保持）。
- 反例：stage8n J 系列 + K（production + 误设旁路 + allow_simulated=true 仍拒绝）。
  正向用例经受控夹具建立资格（真实 run + 精确 gate 锚点），未删除保护条件。

### D7 预览/执行一致（M2.3）与 API 接线（部分）
- preview 返回 target_members/exp_set_hash/preview_fingerprint + 业务绑定
  expected_rev/expected_set_hash（API 端点合并 business_state）；promote body 增
  expected_rev/target_members、rollback body 增 expected_rev，端点 explicit_cas。
- 前端未接线（EvolutionConsole 仍无预期令牌/幂等键/确认流）→ 见“限制”。

### 验证命令（本包）
- 反例与回归：tests/test_skill_evolution_stage8{l,m,n}.py 及
  stage2/4/8b/8c/8d/8e_chain/8h_e3/8j/8k 受影响集（本包执行结果见报告）。
- alembic heads = 4f83c9e2a1d7（P53，单 head；未改动既有迁移）。

### 限制（如实标注，非“收尾完成”）
- M3 前端一次性接线（晋升/回退带预期令牌与幂等键、冲突“请重新预览”、技能回退不
  恢复已发布 Wiki 提示、start/resume 无网络配置预览→确认→启动协议）未落地；
- 浏览器验收 L、真实两轮 worker（管理 API→worker→本地 HTTP stub）闭环复跑未执行；
- PG 验收见独立文档（本轮未重复启动 PostgreSQL/Docker）；B5 四组正式实验接口已在
  2026-09-09 离线闭环（stage8y）；生产业务晋升资格未开放；
- control 的 start/resume 仍“先冻结后确认、resume 强制指纹、cli/worker 入口可绕
  过确认”结构（stage8j 语义保留），完整“先预览配置→确认→启动”最小协议未实现。
- 下一执行步骤：① EvolutionConsole promote/rollback 确认流（expected tokens、
  idempotency key、409→重新预览、回退说明文案）与 businessState 调用；② control
  start/resume 无网络预览→确认→启动（含 cli/worker 入口旁路收口）；③ 浏览器验收
  + 两轮 stub 闭环复跑；④ docs 同步与独立复审。

### D8 M3 接线轮（本轮增量；前端/启动确认的最小可靠闭环）
- 前端（EvolutionConsole.vue + api/evolutionAdmin.ts）：
  * 晋升预览接入 businessState（当前绑定 no_binding/rev/集合哈希展示）；预览展示
    完整目标成员、目标集合哈希、证据模式与阻止原因（阻止时页面无任何可绕过选项，
    已移除“允许模拟晋升”勾选，请求不再携带 allow_simulated）；
  * doPromote 提交 expected_rev/expected_set_hash/target_members/idempotency_key；
    新预览→新键；网络失败保留预览与键（重试复用同请求）；409/422 冲突文案要求
    重新预览且不自动覆盖；
  * 回退走确认对话框并注明“技能回退只影响后续编译，不恢复已发布 Wiki”；回退目标
    由服务端审计链固定（页面不重选）；
  * runAction：real start/resume 先拉 start-preview（无网络）→ 展示角色/端点
    host/输出限制/预算/数据范围/双指纹 → 用户确认后带 explicit_confirm 与
    config/reviewer 指纹提交；simulated 保持原有路径并显式标注不升级。
- 后端 control/API/CLI：
  * control.start_preview：无网络预览（simulated 无角色/指纹；real 由当前受控配置
    现算预期冻结块，仅返回 endpoint host + api_key_present 布尔，无密钥值、无探测）；
  * control.start 改为“确认指纹 == 预览时现算指纹”才允许冻结（漂移 → frozen_mismatch，
    且不静默冻结新配置；随后冻结记录再与确认指纹复核）；
  * control.worker_start_gate：CLI evolution-run 与 worker 共享的启动资格门
    （real 须 授权开关+freeze_eligible+持久化 frozen；simulated 独立显式、不升级）；
    已接入 cli.cmd_evo_run（首个 HTTP 请求前拒绝并返回 code）；
  * API 新增 GET /runs/{run_id}/start-preview。
- 反例测试 tests/test_skill_evolution_stage8o.py —— 5 passed：simulated 无网络预览
  与门；real 预览不泄漏密钥；预览后漂移 start 拒绝且未冻结；匹配指纹 start 冻结并
  spawn + 门通过；real 未冻结门拒绝；CLI 子进程缺授权在首个网络请求前拒绝（rc!=0）。
- 回归：8j/8k/8m/8n/8o（44 passed）、8d/8e_chain/8h_e3/8h_mgmt（14 passed）、
  前端 npm run build（vue-tsc）通过。
- 限制：浏览器人工验收（预览/确认/冲突/网络重试逐项走查）、真实两轮
  管理 API→worker→本地 HTTP stub 闭环复跑（真实授权路径）、PG 验收仍未执行；
  B5 正式实验接口保持开放；生产业务晋升资格保持 fail closed。

### D9 M3 最终验收轮（凭据端点绑定 + 真实子进程授权链路）
- 凭据/端点受控绑定（最小修复）：新增
  settings.wikiskill_allowed_llm_endpoints；发送路径（real_adapters._cfg_http_chat、
  model_review._send_http）在发起 HTTP 前执行 ensure_endpoint_allowed（名单空 →
  仅允许 settings.llm_api_url / wikiskill_reviewer_api_url 的 host；名单显式 →
  仅命中名单）。任意地址 + 全局密钥 → endpoint_not_allowed 且 HTTP 计数为 0；
  冻结 runner 不受 settings 漂移改绑（仍发冻结端点，且仍过名单校验）。
- 反例 tests/test_skill_evolution_stage8p.py —— 6 passed：任意端点零发送、受控
  默认端点正常带 Bearer 发送、显式名单覆盖默认、评审发送同名单、冻结后漂移仍发
  冻结端点、以及真实独立子进程全链（管理员 create → 无网络 start-preview（脱敏）
  → 确认指纹 start → 真实 Popen 子进程 cli evolution-run（worker_start_gate）
  → 本地 HTTP stub 收到真实 auth/model → run 收敛；预览后漂移 409 且零出站；
  未确认 409；重复 start 409）。
- 受影响回归（本轮含守卫）：stage2/4/7f/8b/8c/8d/8e_chain/8f/8h_e3/8h_mgmt/8i/
  8j/8k/8l/8m/8n/8o/8p —— 137 passed。迁移无新增（head=4f83c9e2a1d7）。
- 限制（如实）：① 确定性“第一轮 accepted → 第二轮注入新集合 tie rejected”双轮
  门控结果需协议级 stub 驱动 orchestrator 的 maintainer/proposer/eval 两轮，尚未在
  真实子进程链上构造（本轮完成“真实子进程授权链 + 命中 stub + 收敛”，未含该双轮
  结果断言）；② 浏览器人工逐项走查未执行；③ resume 的“运行中暂停后恢复沿用原
  冻结配置与剩余预算”以既有 8j/control 服务语义为准，未在本轮真实子进程上重放；
  ④ PG 验收、B5、真实供应商、人工校准批准仍独立待办。

### D10 M3 凭据—端点受控绑定（provider/config ID + 引用 + 端点 三关联）
- 修复：settings.wikiskill_credential_providers（JSON：provider_id →
  credential_env（非秘密 env 引用）+ endpoints(scheme+host+port+路径) +
  allow_insecure）+ wikiskill_default_provider。resolve_real_config provider 分支
  显式关联；ModelConfig/RoleFrozen 只存 provider_id/credential_env/allowed_endpoints/
  enforce_https（无密钥）；发送前 _validate_provider_binding 校验映射仍在、引用未
  换、端点仍在冻结与当前映射内；密钥按引用（env）发送时解析（支持同引用轮换），
  不再无条件读全局 key；正式端点默认 HTTPS（http 非 allow_insecure 拒绝）；
  httpx follow_redirects=False。兼容模式（未配 providers）保留旧语义（仍受
  allowed 名单约束）。评审发送沿用端点名单守卫。
- 反例 tests/test_skill_evolution_stage8q.py —— 6 passed：两供应商 key 互不串用、
  引用不匹配发送前拒绝（零 HTTP）、冻结后映射改变拒绝（provider_mapping_changed）、
  同引用轮换不改变冻结 model/endpoint、HTTPS 默认要求 + 记录无密钥、冻结块无密钥。
- 回归：8j/8p/8f/8e_chain/8q —— 23 passed（冻结指纹随新字段重算，既有真实子进程
  授权链与 reviewer/actor 路径保持）。
- 未完成（如实）：M3 项二（真实子进程“两轮 accept→tie-reject”门控断言，复用
  e3 协议夹具的 manager+Popen 版本）、项三（子进程 pause/resume 受控阻塞与预算
  保持）、项四（浏览器逐项验收）本轮未执行；B5/PG/真实供应商/人工校准保留。

### D11 M3 正式 real provider 门禁（本轮执行）
- 修复：settings.wikiskill_require_provider_binding（默认 True）+
  wikiskill_reviewer_provider；control.require_real_provider_binding 接入
  start/resume/freeze_run_runtime_config/build_run_actors(real)/worker_start_gate：
  providers 缺失、默认 provider 未注册（executor/maintainer/proposer）、review=v2
  评审 provider 未绑定 → 首个 HTTP 请求前 ControlError real_provider_binding_required；
  旧记录可读，不满足契约的真实启动拒绝并提示配置/新建。模拟模式不变。
- 夹具：8e_chain/8h_e3/8h_mgmt/8j/8o/8p 注入虚构 provider（FK_E2E env 引用，
  allow_insecure stub 端点）；mgmt 子进程 child env 同步 provider 配置。
- 验证：stage8e_chain/8h_e3/8h_j/8o/8p/8q（17+…通过）与 8h_mgmt（管理 API →
  Popen 独立 worker 真实两轮）通过；stage8q 6 passed（凭据绑定反例）。
- 未完成：项二“同一 run 以 e3 协议 handler 经 manager+Popen 断言 baseline 1/4→
  2/4 accepted→持平 rejected”的单独测试、项三 pause/resume 阻塞 stub 测试、
  项四浏览器操作验收未写/未跑（mgmt 与 e3 分别为该链路两轮的既有近似覆盖，
  不冒充二/三/四完成）；B5/PG/真实模型/人工校准独立保留。

### D12 M3 集成验收 A/B/C（本轮实际状态）
- A（manager+Popen 同一 run 两轮）：扩展现有 stage8h_mgmt 单链测试 —— 增加
  start-preview→确认完整指纹/评审指纹→start（返回真实 PID）、重复 start 409
  （无双 worker）、各角色 HTTP 计数断言（实测 executor22/reviewer12/maintainer2/
  proposer10）、auth 全为 Bearer stub-key、持久预算 model_calls≥1；baseline
  1/4、candidate [2,2]/4、gate accepted→rejected、iteration2 冻结==current
  版本/哈希。→ 1 passed（28s），同 run_id 贯穿。
- B（pause/resume）：新增 test_management_subprocess_pause_resume_same_run
  （同设施、独立 run、真实 Popen）。当前失败：worker 收到 pause 请求后未在
  180s 内收敛为 paused（stage8h_mgmt.py `assert paused`，约 190s 超时）；
  需核查 orch 在独立子进程路径的 pause_requested 检查边界（正常终态路径已通）。
- C（浏览器）：未执行。

### D13 M3 B 修复完成（根因+回归）与 A/B 交付
- 根因（证据驱动，两处应用缺陷）：
  1) orchestrator.execute 对 _run_iteration 返回 paused/cancelled 仅 `return
     get_run(...)`，未把 run 级状态持久化为 paused/cancelled 且不释放租约 ——
     诊断证据：iteration1 已 paused、worker 已退出(pid_alive=false)而 run_row
     仍 status=running、pause_requested=1、lease 悬挂。
  2) control._require_real_confirm(resume=True) 把 config_fingerprint/
     reviewer_fingerprint 判为“额外参数变更”而拒绝恢复，但 resume 又强制要求
     指纹（自相矛盾）。
- 修复：orchestrator.execute 对 paused → `_pause`（写 run paused+释放租约），
  cancelled → `_set_terminal(RUN_CANCELLED)`；control 恢复时排除两个指纹键
  （仍逐一校验 dataset/budget/iteration 不变）。
- 测试协调：事件屏障改为 accepted 落库后立即暂停（50ms 轮询、快照移至暂停后），
  消除亚秒级轮 2 竞态；断言 events==1（无 rejected 提前）、暂停后 stub 计数 3s
  静止、resume 后 completed、冻结指纹与 executor 冻结字段不漂移、预算不减少、
  事件 [accepted,rejected] 不重复、轮次=2、无双 worker。
- 结果：stage8h_mgmt A+B —— 2 passed（63s）；受影响回归 stage8c/8e_chain/8h_e3/
  8h_j/8o/8p/8q —— 34 passed。预算核对：A used.model_calls≥1；B 恢复前后
  used_final≥used_before>0（本地 stub 角色请求计数由 handler role_counts 统计，
  实测 executor22/reviewer12/maintainer2/proposer10，全部计入 persistent budget
  按 guard 计数路径）。
- 未完成：C 浏览器操作验收未执行（非外部阻塞；执行窗口耗尽）。

### D14 预算精确核对（A/B，已通过）+ C 状态
- 依据：executor/maintainer/proposer/reviewer 四角色均经 BudgetGuard.wrap
  （executor 在 execute 顶层；其余在调用点），成功 200 → finish_model(sent_known=True)
  计数一次；无异常即无“未知发送/预留”残留。
- A 新断言：used_model_calls == sum(role_counts)（实测角色计数 executor22/
  reviewer12/maintainer2/proposer10，总计随用例输出）；reserved_in_flight==[]。
- B 新断言：used_final == 最终 stub 总数；恢复增量 used_delta == 出站增量
  （counts_delta）；预算累计不清零。
- 结果：stage8h_mgmt A+B —— 2 passed（53s，精确核对成立）。
- C（浏览器操作验收）：未执行（执行窗口耗尽；非外部阻塞）。续接命令：
  隔离后端(flags+providers+合成实验记录) + 前端 dev + 本地 stub + 浏览器工具按
  清单逐项并保存脱敏请求（注意“服务端成功响应丢失”用隔离代理丢弃首响应实现）。

### D15 M3 P1 浏览器整改（代码+自动化通过；浏览器复验待续）
- P1-1/2/3 根因与修复见 m3-browser-acceptance-20260908.md“整改后”节；前端纯函数
  evolutionConfirm.ts；后端 v2 create 门禁 + meta 机器字段；反例
  test_skill_evolution_stage8r_browser_remediation.py（5 passed）；受影响
  stage8r/8h_mgmt/8n/8p/8c/8d/8j/8k 55 passed；前端构建通过。
- 未完成：真实浏览器 A–F 操作复验（隔离夹具 audit_m3_browser.py 已就绪，
  需以新端口/新根驱动后逐项取证）。

### D16 M3 浏览器复验完成（真实浏览器 A–F 全部执行并通过对账）
- 环境：全新报告根 `backend/reports/m3-browser-remediation-20260908-143805`（前端
  28760/后端 28761/stub 28763，仅 127.0.0.1）；audit_m3_browser.py 参数化
  （M3_REPORT_ROOT/M3_BUSINESS_DB/M3_EXPERIMENT_ROOT/M3_BACKEND_PORT/M3_STUB_PORT/
  M3_STUB_URL，默认布局不变）；全新 business.db + lab；虚构用户/凭据；无任何外部网络
  模型请求（审计内 host 仅 127.0.0.1:28763）。
- 本轮补齐的界面缺口（P1-2 代码在前轮已就位但模板未渲染）：回退专用“上次回退结果
  未知，可重试原请求”提示 + “重试上次回退（同键同请求）”按钮 + “回退操作失败”独立
  错误条（与“晋升操作失败”分离）。
- 本轮新修复两处浏览器/回归暴露缺陷：
  ① EvolutionConsole 数据集下拉缺 @change → v2 不带 review 创建 422（复现→绑定
  onDatasetChange→复验通过；422 记录保留为 v2 门禁真实证据）；
  ② buildRealStartConfirm 迭代需正整数（防 2.5 类输入）。
- 全量回归暴露并修复两个存量后端缺陷：
  ① real_adapters 兼容分支密钥存在性未读 override.llm_api_key（provider 重构漂移）；
  ② orchestrator.execute 对内部分支已落盘 paused 的结果二次 _pause → LeaseConflict；
     改为 run 仍 running 才落盘一次。
- 测试证据（复跑通过）：stage8r 5；前端纯函数 12（node:test 本地打包运行）；全部
  test_skill_evolution_*.py 244 passed（477.67s）；Wiki 编译管线 10 文件 273 passed
  （177.98s）；compileall/import 通过；alembic 单 head 4f83c9e2a1d7；vue-tsc + vite
  build 通过。
- 浏览器判定：A 权限/403/控制台入口通过；B v2 real queued + 落库契约通过（grader v2、
  runner review=v2、mode=real/real=true、val 4 项、创建期 stub 0 请求）；C
  start(全 8 确认字段)→pause→两次刷新稳定→resume(同冻结指纹+全 8 字段)→completed，
  used=65==四角色 stub 总数、暂停/恢复增量 38==38、reserved==[]、预算未重置扩大；
  D 502 丢响应→同 body/同 rev/同 hash/同 key 重放仅一次业务切换（rev+1、审计+1）→新
  操作新键；E 双浏览器 CAS：旧令牌 409、无覆盖、重新预览新键执行成功；F 多技能完整
  展示、`<script>` 文本不执行、无 v-html、模拟与未校准 v2 晋升服务端阻止、标题去只读、
  费用未知非 0、刷新恢复、无 console/pageerror。
- 详细逐项与数值见 m3-browser-acceptance-20260908.md“整改复验与最终完成”及报告根内
  acceptance-summary.json/pause-state.json/final-reconcile.json/environment-summary.json。
- 外部保留项不变：真实供应商链路与 7B 效果、A1–A5 人工标签与真实校准、PG 环境验收。

### D17 凭据绑定终审 P2（2026-09-08 独立终审；executor/reviewer 凭据隔离）
- 背景：M3 结论保持通过；本项为独立终审发现的真实模型凭据链 P2，非 M3 复执。
- 根因一（executor 兼容路径）：`resolve_real_config` 接受 `override['llm_api_key']` 完成
  凭据存在性校验但发送时 `_resolve_credential` 只读 `settings.llm_api_key` → override-only
  预检通过、真实请求无该凭据。
- 根因二（reviewer 绑定缺口）：`reviewer_frozen_from_live`/`build_reviewer_frozen` 未写入
  /恢复 provider_id、credential_env、allowed_endpoints、enforce_https；
  `ChatSemanticReviewer._send_http` 直读 `settings.llm_api_key`；评审端点只走宽泛
  allowlist 而无“冻结+当前 provider 映射”双重校验；reviewer provider 通过门禁却可能
  实际使用全局执行密钥（M3 stub 同 key 掩盖）。
- 修复（公共语义单点）：新增 `backend/app/core/skill_evolution/credential_binding.py`
  （受控 provider 解析/注册校验/env 引用/规范化端点/冻结+当前映射双重校验/HTTPS/
  凭据运行时解析/raw override 拒绝）；executor/maintainer/proposer/reviewer 共用。
- 安全契约（统一优先级）：provider 模式 `provider_id → map → credential_env → env 值`，
  绝不回退 `settings.llm_api_key`；旧兼容仅限 `require_provider_binding=false` 且只读
  settings key；raw `override['llm_api_key']` 一律 fail-closed（`raw_credential_override
  _forbidden`，错误不回显 key）；provider/引用/端点漂移与 HTTPS 违约在 httpx.post 前拒绝；
  env 值轮换不改配置指纹。
- reviewer：ReviewerConfig 增 provider_id/credential_env/allowed_endpoints/enforce_https，
  fingerprint 纳入 provider/引用/规范化端点/enforce_https/模板哈希（值轮换不变）；
  冻结记录写入非秘密绑定字段；旧冻结缺字段在 provider-required 环境 `frozen_missing`
  （不以当前 settings 补齐/不回退全局 key）；发送前与当前 map 复核；缺失凭据/端点漂移
  /HTTPS 违约在 httpx.post 前 ReviewerError fail-closed。
- 反例：`tests/test_skill_evolution_stage8s_credential_binding.py` 23 passed（A 角色 10 项、
  B reviewer 12 项、C 脱敏 1 项；哨兵 EXECUTOR/REVIEWER/EVIL-GLOBAL 全扫描为 0 泄露）。
- 集成：五角色两 provider 本地 stub 浏览器链（新报告根
  `backend/reports/m3-credential-browser-20260908-160156`，executor-fixture/
  reviewer-fixture 不同 env key，settings.llm_api_key=EVIL-GLOBAL）：UI start/resume 双
  确认均显示两 provider、无密钥；worker 逐角色认证 0 mismatch（executor 22/maintainer 2/
  proposer 10/reviewer 12）；used==stub 46==46、恢复增量一致、reserved==[]；frozen 记录
  含 provider 绑定字段无 key；审计/DB/记录哨兵扫描 0 泄露。
- 受影响回归（本轮实际复跑）：targeted 14 文件 89 passed（167s）；全部 skill_evolution
  33 文件 267 passed（492.71s）；Wiki 编译 10 文件 273 passed（192.62s）；compileall/
  import 通过；alembic 单 head 4f83c9e2a1d7；前端 vue-tsc + vite build 通过；test:unit 12
  passed。
- M3 结论未变（仍通过）；stage7a 用例按新契约更新（raw override 由“合法”改“必须拒绝”）。
- 外部保留项不变：真实供应商链路与阶段 7B 效果实验、A1–A5 人工标签与真实校准、PG
  环境验收。日志/数据库/响应/报告均无密钥值；测试仅本地 stub；无生产库/绑定变更。

### D18 凭据端点授权 P1（2026-09-08 安全修复；反例先行→最小修复→专项→关键链→单次全量）
- 根因：`credential_binding.endpoint_in` 采用 `url.startswith(allowed)` 或 host-only
  判断 → 同一 host 任意路径被放行；`/v1` 前缀碰撞匹配 `/v10`；跨 scheme（http↔https）
  同 host 被接受；frozen_endpoints 为空时默认放行；userinfo/fragment/反斜杠/点路径/
  百分号编码逃逸未处理；允许本地 HTTP 时因 host 相同错误接受跨 scheme 变化。旧实现
  复现：三组已确认用例均错误返回 True。
- 安全影响：provider-bound 真实凭据可能被发送到“同 host 但非授权路径/不同 scheme/
  不同端口”的恶意或错误端点；`/v10` 类路径歧义可能让请求逃逸受控 path prefix。
- 新 URL 授权契约（`credential_binding._url_parse_strict/_path_segments/
  _match_authorized`）：
  - 仅接受绝对 http/https；scheme 严格相等；hostname 规范化相等（IPv6 去括号；
    不剥离尾点，宁严勿宽）；
  - effective port：显式校验 1..65535；未显式时 https→443、http→80（默认与显式
    等价）；
  - 路径按“原始分段 + 分段边界”：允许整段相等或授权前缀 + '/' 的真实子路径；
    `/v1` 不匹配 `/v10`；根 `/` 覆盖同 origin；尾部斜杠稳定归一；
  - query：allowed 无 query → 目标可在合法路径携带普通 query；allowed 带 query →
    目标 query 必须精确相等（禁止前缀匹配）；
  - 拒绝 userinfo、fragment、反斜杠、NUL/控制字符、点路径（`.`/`..`）、百分号编码
    的 `/`、`\`、点路径、控制字符（%2f、%5c、%2e 系、%00、%0a 等）；
  - malformed/非法端口 → 稳定不匹配（受控错误），原始解析异常不冒泡；
  - 发送前（validate_frozen_send）：frozen_endpoints 非空、当前 provider map
    endpoints 非空、目标必须同时命中冻结集与当前映射；enforce_https 时
    `https_required` 优先于端点匹配抛出。
- 分层语义：resolve/预检（endpoint_declared）仅约束 hostname+path prefix（保留既有
  “https_required 在发送时拒绝 http 端点”的契约与 stage8q 断言）；发送路径统一走
  严格 `endpoint_in`（scheme/端口/路径边界全约束）。四角色共用同一发送校验：
  executor/maintainer/proposer 经 `real_adapters._validate_provider_binding`（委托
  cb.validate_frozen_send），reviewer 经 `ChatSemanticReviewer._send_http` 同一函数；
  无角色旁路；错误码保持 provider_mapping_changed / https_required /
  credential_reference_changed；错误不含凭据值。
- 改动文件（范围外为 0）：`credential_binding.py`（核心）、`real_adapters.py`（仅
  resolve 预检成员判断切 endpoint_declared，由关键链路 8q 契约证明必需）、
  `test_skill_evolution_stage8s_credential_binding.py`、两份文档。
- 反例（stage8s E 节，先红后绿）：接受 33 组参数化 + 拒绝 26 组参数化 + malformed
  布尔稳定性 + executor 发送级拒绝 7 × reviewer 发送级拒绝 7 + frozen 空集 + 当前
  map 空集 + 仅命中冻结 / 仅命中当前映射。全部拒绝类断言：HTTP 出站 0、凭据不解析
  使用、错误稳定受控。修复前关键拒绝用例 26 failed（旧实现放行），修复后全绿。
- 专项测试：stage8s 100 passed（0 failed / 0 errors / 无 skip/xfail）。
- 关键链路（单次 pytest，6 文件）：stage8h_mgmt / 8h_e3 / 8j / 8e_chain / 8f / 8q →
  21 passed（92.52s）：管理 API→独立子进程 worker→本地 stub 仍连通、四角色正确
  provider、used==出站总数、reserved_in_flight 终态空、暂停/恢复冻结配置、无模拟
  兜底、无外部网络；期间 8q https 契约经代码分层语义修复（未改 8q 断言）。
- 全量准入 14 项自检全部满足（反例先红后绿/专项/关键链/四角色静态追踪/双集合校验/
  出站前拒绝/git 范围/无 TODO/无 sleep/无 skip/编译通过/无密钥泄漏/无残留服务/端口
  干净）。
- 最终一次全量（本轮唯一一次）：全部 test_skill_evolution_*.py（33 文件）344 passed
  （459.43s）；Wiki 编译管线（10 文件）273 passed（142.79s）；`alembic heads` =
  `4f83c9e2a1d7` 单 head。0 failed / 0 errors / 无新增 skip/xfail。
- 本轮未运行前端测试/build 与浏览器验收：本任务不涉及前端、API 请求体或 UI（仅后端
  内部端点授权判定）；未修改任何前端文件。
- 本轮无真实模型请求、无生产库/生产绑定变更、无真实凭据、无临时服务与端口残留。
- 外部保留项不变：真实供应商链路与阶段 7B 效果实验、A1–A5 人工标签与真实校准、PG
  环境验收。本修复不构成真实供应商链路验证或论文效果复现。

### D19 端点授权最后补丁（2026-09-08；三处遗漏修复，不重复全量）
- 终审遗漏三处（均已独立复现为 True）：
  1. `endpoint_declared` 预检未比较 hostname：不同 host（含根路径授权）错误通过；
  2. allowed endpoint 带 query 时仍走路径前缀规则 → `/v1?key=ok` 授权可扩张到
     `/v1/sub?key=ok` 等子路径；
  3. 多层百分号编码只单层 unquote：`%252e%252e` 等双重编码点路径可逃逸。
- 修复（仅 `credential_binding.py` + 反例 + 文档）：
  - `endpoint_declared`：hostname 严格相等（含根路径不跨 host；IPv4/IPv6 均不同则拒）；
    scheme/port 仍分层留给发送门禁（stage8q 的 https_required 错误顺序不回归）；
  - `_match_authorized`：allowed 带 query → 目标 path segments 与 query 均须精确相等，
    禁止子路径扩张/query 前缀/追加参数；allowed 无 query → 维持原路径前缀规则并可带
    普通 query；
  - `_path_percent_safe` + `_valid_pct_escapes`：路径按原始分段逐段做有界
    （`_PCT_DECODE_MAX=8` 层，每次有效解码均缩短字符串，确定性终止）逐层
    percent-decode 直到稳定；每层校验 malformed escape（孤立 `%`、`%2`、`%GG` 拒绝，
    不依赖 urllib 静默保留）；收敛后校验解码段不形成 `/`、`\`、NUL/控制字符与 `.`/`..`
    路径段；超过层数上限 fail-closed；query 不参与解码（保持精确字符串契约）。
- 安全顺序不变：解析校验 → frozen/current 非空与双集合 → HTTPS → 双集合命中 →
  凭据解析 → HTTP 出站；错误不含密钥/Authorization/环境变量值。
- 新增反例（stage8s F 节，先红后绿）：修复前 19 failed → 修复后全绿；覆盖
  endpoint_declared 跨 host/根路径跨 host/IPv4/IPv6、query 子路径/异 query/前缀/追加、
  多层编码 %252e%252e/%252E%252E/%252f/%255c/%25252e%25252e/混合/多层 NUL 与控制、
  malformed escape（`%`/`%2`/`%GG`/两侧 malformed）；并保留合法单层 UTF-8 编码、
  无害编码与分层 http→https 预检通过语义。
- 专项：stage8s 136 passed（21.52s，0 failed/0 errors，无 skip/xfail）。
- 关键链路：stage8q + stage8h_mgmt 单次 pytest → 8 passed（62.88s；https_required
  错误顺序不回归；管理 API→独立子进程 worker→本地 stub 正常；mgmt 覆盖严格发送门禁、
  used==出站、reserved_in_flight 空，故按任务决策不重复执行 stage8e_chain）。
- 静态检查：py_compile 通过；git 范围仅 credential_binding.py/stage8s/两份文档；无
  TODO/调试输出/sleep/retry/skip/xfail；模块无密钥/Authorization 字面量；无残留端口
  与进程。
- **本轮按任务决策不重复后端全量与 Wiki 编译全量**：上一轮最终全量（344 passed +
  273 passed + alembic 单 head 4f83c9e2a1d7）保持有效，本补丁为纯 URL 校验局部修改，
  经专项 + HTTPS 契约 + 管理 stub 链路验证充分。未运行前端测试/浏览器验收（不涉及
  前端/API 契约/UI）。alembic head 未变（未新增迁移）。
- 无真实模型/外部网络、无真实凭据、无生产库/生产绑定变更、无临时服务与端口残留。
- 外部保留项不变：真实供应商链路与阶段 7B 效果实验、A1–A5 人工标签与真实校准、PG
  环境验收。
