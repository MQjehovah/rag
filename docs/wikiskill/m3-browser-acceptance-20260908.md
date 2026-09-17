# M3 浏览器操作验收报告（2026-09-08）

## 结论

浏览器验收已实际执行，结论为 **不通过（存在 3 个阻断缺陷）**。权限、集合展示、正文安全渲染、晋升安全门禁、回退正常路径和并发 CAS 均通过；真实模式控制闭环、回退丢响应幂等重试、v2/real 新建入口未达到 M3 验收要求。

本结论不代表真实供应商链路或 WikiSkill 效果已验证。

## 隔离边界

- 前端：Vite `127.0.0.1:18760`。
- 后端：独立进程 `127.0.0.1:18761`，独立 SQLite。
- 模型替身：本地 HTTP stub `127.0.0.1:18763`，使用虚构凭据引用。
- 未迁移生产库、未改生产绑定、未访问外部模型供应商。
- 本次真实模式启动在参数校验阶段即被 409 拒绝；`stub-counts.json` 未生成，证明没有模型替身请求，更没有真实模型请求。
- HTTP 审计不记录认证请求体、请求头或密钥值；预览中只保留 `api_key_present=true`。

## 实际通过项

1. 匿名访问 `/admin/evolution` 被重定向到 `/login?redirect=/admin/evolution`。
2. 普通用户 `audit-reader` 访问被重定向到 `/403`；管理员可进入。
3. 页面刷新后实验列表与详情可恢复，隔离根 `browser-lab` 正常显示。
4. 双技能集合完整展示：`browser-a:0001`、`browser-b:0001`，各自 `seq` 与完整 `content_hash` 均可见；业务绑定显示 `rev` 和集合哈希。
5. 技能正文完整性校验为 `ok`。正文中的 `<script>window.M3_XSS=1</script>` 作为文本显示，没有执行；页面明确标注正文不可信。
6. 未校准/模拟证据的晋升预览被服务端门禁阻止，页面显示“无可绕过选项”、无有效最优评估、模拟证据不构成真实效果。
7. 回退确认明确说明目标由审计链固定，且只影响后续编译，不恢复已发布 Wiki 内容。
8. 回退正常路径能原子更新完整集合和修订号。
9. 双浏览器并发验证：会话 A 持有 `rev=4` 的旧确认，会话 B 先提交使状态变为 `rev=5`；A 再提交得到 409“请重新预览”，未覆盖新状态。
10. token/费用未知显示为“未知（不显示为 0）”；页面顶部持续显示“真实链路：未验证 / 效果：未验证”。

## 阻断缺陷

### P1-1 真实模式 start 确认请求缺少数据集、迭代与预算字段

浏览器确认框正确展示：数据集 `wiki-default-v2dev`、迭代 2、预算 260/130/1800、角色配置和两个指纹。但确认提交的请求体只有：

- `explicit_confirm`
- `confirm_config_fingerprint`
- `confirm_reviewer_fingerprint`

缺少后端契约要求的 `confirm_dataset_version`、`confirm_max_iterations` 和预算确认字段。服务端实际返回：

`409 确认参数与运行记录不一致（dataset_version: 记录=wiki-default-v2dev 请求=None）`

因此无法从 UI 完成 start，也无法继续浏览器 pause/resume 闭环。

代码落点：`frontend/src/views/admin/EvolutionConsole.vue` 的 `runAction`（约 747–793 行）；后端字段契约在 `backend/app/api/evolution_admin.py`（约 100–113 行）。

### P1-2 回退成功响应丢失后，UI 重试执行了第二次不同操作

故障注入把第一次成功回退的 HTTP 200 响应替换为 502。第一次请求：

- `expected_rev=2`
- 幂等键 `bb8cade4-...`
- 服务端实际提交成功，绑定变为 `rev=3`

用户按页面再次点击回退后，UI 重新读取当前绑定并生成新键：

- `expected_rev=3`
- 新幂等键 `8cc442f9-...`
- 服务端再次成功提交，绑定变为 `rev=4`

这不是“同请求同键重放”，而是执行了下一次回退，和页面文案“网络超时重试复用同一幂等键”冲突，可能导致连续切换历史集合。

代码根因：`doRollback` 每次进入都调用 `refreshPromoBinding()`，并局部生成 `const key = this.genKey()`；失败后未持久保留原请求。位置：`frontend/src/views/admin/EvolutionConsole.vue` 约 901–947 行。

### P1-3 新建实验表单无法创建可运行的 real/v2 实验

- 即使后端隔离配置已启用 real，前端“真实（未授权启动）”仍被硬编码为 disabled。
- 选择 `wiki-default-v2dev（语义评审（原型 v2））` 后能创建 queued 实验，但请求体没有 `review: "v2"`。
- 数据库实际记录的 runner 配置没有 `review`，因此它不是与 v2 独立语义评审契约一致的实验。
- 页面提示仍为“v2 未接入自动评分”，与当前工程状态不一致。

代码落点：`frontend/src/views/admin/EvolutionConsole.vue` 约 488–493 行和 `submitCreate` 约 807–826 行；后端已支持 `review` 字段（`backend/app/api/evolution_admin.py` 约 92 行）。

## 非阻断界面问题

- 页面标题仍为“演化实验只读控制台 / 演化实验(只读)”，但页面已有创建、运行控制、晋升和回退写操作，容易误导管理员。
- 回退失败统一显示“晋升操作失败”，错误类别文案不准确。

## 可复核证据

- 隔离浏览器夹具：`backend/tools/audit_m3_browser.py`
- 脱敏 HTTP 请求与响应：`backend/reports/m3-browser-20260908/http-audit.jsonl`
- 隔离业务库：`backend/reports/m3-browser-20260908/business.db`
- 隔离实验库：`backend/reports/m3-browser-20260908/lab/skill_store.db`
- 初始运行标识：`backend/reports/m3-browser-20260908/seed.json`

## 修复后必须复跑

1. real 新建（或既有 real run）→ 无请求预览 → 完整确认参数提交 → 独立 worker 启动。
2. running 状态下暂停 → paused 稳定落盘 → 使用同一冻结配置指纹恢复 → completed。
3. 核对浏览器显示的预算与请求日志、最终 `used_model_calls`、本地 stub 角色计数一致且无预留残留。
4. 回退成功响应丢失 → 页面保留原 body 与原幂等键 → 同键重放只返回第一次结果，`rev` 只增加一次。
5. 409 后必须丢弃旧请求，重新预览并生成新操作键。
6. v2dev 新建请求必须显式携带 `review=v2`；不允许创建 grader=v2 但 runner 无 reviewer 的不可运行记录。


## 整改后修复与自动化证据（2026-09-08 续；浏览器复验待执行）

- P1-1 修复：frontend/src/utils/evolutionConfirm.ts `buildRealStartConfirm(preview)`
  从 start-preview 响应构造完整确认体（dataset/iterations/预算×3/双指纹，
  fail-closed：缺失/null/非法抛错不提交）；EvolutionConsole.runAction real
  start/resume 共用该构造并整体提交；后端 `_require_real_confirm` 校验未弱化。
- P1-2 修复：rollback 请求级幂等 pending（组件状态保存
  workspace_id/expected_rev/expected_set_hash/idempotency_key；不确定结果
  （网络/超时/5xx）保留原请求，页面显示“结果未知，可重试原请求”并给出
  “重试上次回退（同键同请求）”；2xx 清空 pending；409/422 作废并提示重新预览/
  重新发起；新操作生成新键；独立“回退操作”错误文案；`classifyRollbackError`/
  `createPendingRollback` 为无 DOM 纯函数。
- P1-3 修复：admin_meta 返回 grader_version/grader_role/grader_calibration/
  required_review/allow_business_promotion/real_start_enabled（无密钥/凭据）；
  create_experiment_and_run fail-closed：v2 数据集必须 review=v2（422
  review_required_for_v2），v1+review=v2 仍 422；前端去 real disabled、v2dev 自动
  带 review=v2（提交前校验元数据/grader_ready/评审契约），标题去“只读”，标注
  real_start_enabled 与 engineering_only 未校准。
- 自动化反例：backend/tests/test_skill_evolution_stage8r_browser_remediation.py
  —— 5 passed（v2 缺 review 422；v2+review 实验/run 两处记录；v1+review 422；
  rollback 同 body 同 key 重放 rev/审计各一次、同键不同 body 422、陈旧 rev 新键
  409；meta 字段脱敏与 required_review/calibration）。
- 受影响回归：stage8r+8h_mgmt(A/B)+8n+8p+8c+8d+8j+8k —— 55 passed（2:18）。
- 前端构建：npm run build（vue-tsc+vite）通过。
- 浏览器真实操作复验（A–F 清单）：尚未执行（执行窗口耗尽；非外部阻塞）。

## 整改复验与最终完成（2026-09-08 收尾轮；真实浏览器 A–F 已执行）

本报告保留上一轮“未执行”结论于其上；本节记录本轮收尾的执行与结果，不改写历史。

### 隔离环境（全新，未复用任何前次数据库）
- 报告根：`backend/reports/m3-browser-remediation-20260908-143805`（前端 127.0.0.1:28760、
  后端 127.0.0.1:28761、本地模型 stub 127.0.0.1:28763；全部仅监听 127.0.0.1）。
- `backend/tools/audit_m3_browser.py` 最小夹具改造：新增环境变量 M3_REPORT_ROOT /
  M3_BUSINESS_DB / M3_EXPERIMENT_ROOT / M3_BACKEND_PORT / M3_STUB_PORT / M3_STUB_URL
  （默认值保持 2026-09-08 原布局不变），使新根/新库/新端口可重复驱动。
- 证据目录含：business.db、lab/skill_store.db、http-audit.jsonl（脱敏，无登录体/
  Authorization/Cookie/密钥值）、stub-counts.json、pause-state.json、final-reconcile.json、
  acceptance-summary.json、environment-summary.json、seed.json、ui-evidence/ 文本转储。

### 三个 P1 最终代码状态（复核通过）
- P1-1：`frontend/src/utils/evolutionConfirm.ts` `buildRealStartConfirm(preview)` 无 DOM
  纯函数，由 `EvolutionConsole.runAction` real start/resume 共用；只使用 start-preview
  返回值构造 8 个确认字段（dataset/迭代/预算×3/双指纹），任一字段缺失、null、类型错、
  数值非法（本轮补加 max_iterations 必须为正整数）即抛错且不发送；不补默认值；后端
  `_require_real_confirm` 未弱化，漂移 409。浏览器实测 start 与 resume 的 POST 均携带
  全部 8 字段、无 409 缺参。
- P1-2：rollback 请求级幂等 pending（组件状态存 workspace_id/expected_rev/
  expected_set_hash/idempotency_key）；本轮补全了上一轮代码缺漏的页面 UI：回退专用
  “上次回退结果未知，可重试原请求”提示、“重试上次回退（同键同请求）”按钮与
  “回退操作失败”独立错误提示（不再把回退失败显示为晋升失败）。2xx 清 pending 并刷新
  绑定；409/422 作废并提示重新预览/重新发起；网络/5xx 保留 pending 可同键重试；新操作
  生成新键；后端同键同体返回首结果、rev/审计事件仅一次、同键不同体 422、陈旧 rev 新键
  409。
- P1-3：real 不再硬编码 disabled；页面在数据集选择处显示服务端元数据
  （grader 版本/角色、required review、calibration=engineering_only、人工标签与真实
  校准未完成、不允许业务晋升），选择 real 显示“仅创建 queued 记录、创建不调用模型、
  启动仍需服务端授权开关与逐次完整确认”。本轮修复了上一轮遗漏：数据集下拉未绑定
  @change 导致 review 不会自动置为 v2（后端 422 review_required_for_v2 真实拦截成功），
  绑定 @change=onDatasetChange 后创建成功。后端门禁未动：v2 缺 review 422、v1+review=v2
  422、未知 review 422、grader=v2 无 reviewer 不可创建。

### 本轮新发现并修复的问题
1. EvolutionConsole.vue 数据集下拉未触发 onDatasetChange → 创建 v2 不带 review → 后端
   422（浏览器真实复现后修复并复验）。
2. P1-2 状态逻辑存在但模板未渲染“结果未知”提示/重试按钮/回退专用错误 → 本轮补齐 UI。
3. buildRealStartConfirm 未拒绝非整数迭代 → 补 Number.isInteger 校验（fail-closed）。
4. 全量回归暴露两个存量缺陷（此前子集回归未覆盖）：
   - real_adapters 兼容分支忽略 override.llm_api_key（provider 重构漂移）→ 密钥存在性
     校验改读 override 优先；stage7a::test_real_config_validation_no_secrets 通过。
   - orchestrator.execute 对已由内部分支（eval_invalid/失败降级）自行落盘 paused 的
     结果再次 _pause → fence 失败 LeaseConflict；改为仅当 run 仍 running 时才落盘一次；
     stage7c::test_cli_create_run_resume_guard_accounting 通过。

### 自动化测试（实际命令/文件/数量/耗时；均复跑通过）
- stage8r（5 passed，11.71s）；前端纯函数行为测试（node:test + esbuild 本地打包，
  frontend/tests/unit/evolutionConfirm.test.ts，12 passed）。
- 全部 test_skill_evolution_*.py（32 文件 244 项）最终复跑：244 passed，无 skip/xfail，
  477.67s（首轮 242 passed/2 failed → 修复后全绿）。
- Wiki 编译管线回归 10 文件（pipeline_7d/pipeline_core/pipeline_api/skill_pipeline/
  skill_batch_v3/skill_batch_c1/skill_default_v3/skill_default_equivalence/skill_api/
  8c_section_evidence）：273 passed，无 skip/xfail，177.98s。
- Python：compileall app/tools/tests 通过；import app.main + skill_evolution 模块通过。
- alembic heads = 4f83c9e2a1d7（单 head，未新增迁移）。
- 前端：vue-tsc --noEmit 通过；vite build 通过（18.57s）；test:unit 12 passed。

### 浏览器 A–F 逐项结果（真实页面操作，均通过）
- A 权限：未登录 /admin/evolution → /login?redirect=/admin/evolution；audit-reader →
  /403（无权访问页，无实验/配置泄露）；audit-admin 可进入控制台（h2=WikiSkill 演化
  管理控制台，顶部保留 真实链路：未验证 / 效果：未验证）。
- B v2 real 新建：选择 wiki-default-v2dev 显示 grader v2/required review=v2/
  engineering_only（人工标签/真实校准未完成）/不允许业务晋升；选 real 显示仅 queued、
  创建不调用模型、启动需服务端开关与逐次确认；创建后页面 queued；stub-counts.json 不
  存在（创建阶段 0 请求）；lab 库断言 grader_version=…/v2-prototype-2、runner 配置
  review=v2、run runner {mode:real,real:true,review:v2}、val 完整 4 项。注：修复前一次
  创建被服务端 422 拒绝（缺失 review），作为 v2 门禁的浏览器侧真实证据保留在
  http-audit.jsonl。
- C real start→pause→resume→completed：无网络 start-preview 弹窗显示冻结配置
  （角色=executor-stub/reviewer-stub @127.0.0.1:28763、预算 90/40/3600、迭代 3、
  双指纹 5fa363d9…/c8545b85…）；确认后 start POST 200 含全部 8 个确认字段并返回独立
  子进程 PID（日志 lab/runs/_console/run_01342ff8ea684d9cacee.log）；浏览器暂停 →
  paused(user_pause) 稳定跨两次刷新；resume 弹窗复显相同冻结指纹与预算 → POST 200 含
  全部确认字段；最终 completed(max_iterations)，UI 显示 65/90、费用“未知（不显示为
  0）”。精确对账：used_model_calls 65 == stub 四角色总数（executor31/reviewer16/
  maintainer3/proposer15）；暂停时 27==27；恢复增量 used 38 == stub 增量 38；
  reserved_in_flight==[]；预算未重置/未扩大；无 Simulated*；全部请求仅达本地 stub。
- D rollback 502 同键重试：预状态 rev=2（双成员）、事件 2；故障注入首次请求服务端提交
  成功而浏览器收到 502 → 页面显示“上次回退结果未知，可重试原请求”与“重试上次回退
  （同键同请求）”；DB rev→3/事件→3/成员回退一层；点击重试 → 审计两次 POST 逐字段一致
  （同一 workspace_id/expected_rev=2/expected_set_hash/idempotency_key
  d56cf4f2-…），返回首次结果，DB 不变（rev 仍 3、事件仍 3、无二次切换）；随后发起全新
  回退操作 → 新键 595c1c51-…、新 rev/hash（rev→4）。
- E 双浏览器 CAS：A 持 rev4 旧令牌打开确认；B 先回退完成（rev→5、事件→5）；A 提交 →
  409（detail 含“请重新预览”），A 的 pending 作废、无自动覆盖（DB 保持 rev5）；A 重新
  预览后以 rev5+新键 59f38a76-… 执行成功（rev→6、事件→6，成员恢复双成员集）。
- F 安全与展示：多技能集合完整显示（browser-a/browser-b 行含 version/seq/full
  content_hash，绑定区含 rev 与集合哈希）；`<script>window.M3_XSS=1</script>` 按文本
  展示且 window.M3_XSS 未定义；EvolutionConsole.vue 无 v-html；模拟证据晋升被阻止
  （“阻止晋升（无可绕过选项）…需显式 allow_simulated（仅隔离测试）”，无绕过按钮）；
  未校准 v2 real 晋升执行被服务端 409 grader_not_promotable（人工标签=False/真实校准
  =False，DB 不变）；回退确认文案明确“只影响后续编译，不恢复已发布 Wiki”；
  token/费用未知显示“未知（不显示为 0）”；页面标题=演化实验控制台（无“只读”）；
  回退失败标题“回退操作失败”（与“晋升操作失败”分离）；刷新后列表与详情恢复；
  复验期间页面无未处理异常/console error。

### 服务与端口清理
本轮启动的前端(28760)、后端(28761，含 stub 线程 28763)均已按收尾清单关闭；无新进程
残留；未触碰用户原有进程（3000/8000 及既有 PID）。隔离数据库与脱敏报告保留在
`backend/reports/m3-browser-remediation-20260908-143805`。

### 允许保留的外部事项（本轮未执行，不属于当前环境可修复范围）
- 真实供应商链路验证与阶段 7B 效果实验（需真实模型与授权）。
- Grader v2 A1–A5 人工标签与真实校准批准（人工；本轮未代填）。
- PostgreSQL 环境验收（本机无 PG 环境；不以 SQLite 结果代替）。

## 附注：凭据绑定终审 P2（2026-09-08，独立终审；M3 结论保持通过）

本节不改写上文任何 M3 结论。独立终审发现真实模型凭据链存在凭据隔离 P2（非浏览器验收
缺陷），已在本轮修复并验证：
- executor 兼容路径不再接受 `override['llm_api_key']` 预检通过（raw key 一律拒绝）；
- reviewer 冻结/恢复/发送与 executor/maintainer/proposer 共用受控 provider 绑定
  （`credential_binding.py`），reviewer 不再读取全局执行 key；
- 密钥值轮换不改配置指纹；provider/引用/端点漂移与 HTTPS 违约发送前拒绝；
- 证据：stage8s 23 passed、五角色两 provider 本地 stub 浏览器链
  （backend/reports/m3-credential-browser-20260908-160156）used==stub 46==46、
  逐角色 0 mismatch、哨兵 0 泄露；全量 skill_evolution 267 + Wiki 编译 273 通过。
- M3 三个 P1、浏览器 A–F、real start/pause/resume/completed、rollback 同键重试、
  CAS 409 等结论不受影响，仍通过。真实供应商链路/7B 效果、A1–A5 人工标签与真实校准、
  PG 环境验收仍为允许保留的外部事项。
