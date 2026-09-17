# 阶段 8A：演化实验只读管理入口（08a-readonly-console.md）

> 只读实验管理入口。不代表阶段 7/效果验收通过；不得把“只读页面完成”描述为
> WikiSkill 效果达标。状态保持：阶段 7、Grader v2、生产晋升均为**待验收**；
> 真实链路未验证 / 效果未验证。

## 1. 接入核查与设计结论

- 演化记录位于**隔离实验 SQLite**（每实验根 `skill_store.db` + `runs/*/meta.json`），
  不在业务库；业务 Session 没有演化表 → 只读入口**独立直连实验库文件**，不触碰业务库。
- 鉴权：复用现有 JWT（`app.core.jwt_utils.get_current_user`）+ 统一权限
  （`access_control.is_admin`）。前端 `/admin/*` 已有 `system_admin` 路由守卫
  （由 `/api/auth/me` 的 groups/is_admin 推导）。**默认仅管理员可读**：
  缺少可靠 workspace→业务授权映射时不做用户级授权，只允许管理员。
- 实验根只来自服务端配置：`settings.wikiskill_console_roots`（JSON `{root_id: 绝对路径}`），
  仅由 id 访问；客户端永不提交文件路径；id 字符白名单 + 根映射校验。
- 读库方式：`sqlite3 mode=ro`（`file:...?mode=ro`）；库文件缺失/空 → 503 unavailable；
  **不调用 skill_store.session_for、不建库/建表/迁移**。开关：
  `wikiskill_console_enabled`（默认 false）。
- 前端：`/admin/evolution`（EvolutionConsole.vue，Element Plus，与现有风格一致）。

## 2. 后端只读接口（app/api/evolution_console.py）

| 端点 | 内容 |
| --- | --- |
| GET /api/evolution-console/status | 功能状态/根列表；real_link_verified=false、effect_verified=false（如实展示未验证） |
| GET /experiments?root&q&page&size | 实验列表（分页） |
| GET /experiments/{id}?root | 详情 + 运行摘要 + 迭代/评估计数 + 当前技能集合 |
| GET /runs/{id}?root | 运行详情：状态/停止原因/预算上限与使用/迭代步骤/模式(simulated/real)/token 与费用 unknown（null 而非 0） |
| GET /experiments/{id}/skills | 不可变技能版本元数据（md 长度/哈希/seq）+ 候选 diff 摘要（仅元数据） |
| GET /experiments/{id}/patterns | 经验 Pattern 摘要（修订数/日志数） |
| GET /experiments/{id}/gate-history | 门控历史（decision/score） |
| GET /experiments/{id}/evaluations | 评估（pass 计数、valid、invalid_reason） |
| GET /runs/{id}/trajectories | 该 run 作用域内执行摘要（meta.json，无正文） |

全部端点：登录 + 管理员校验；每次访问校验作用域与根映射；不提供任何写/晋升/恢复/
取消/模型调用；不返回密钥/认证头/敏感配置/原始资料/参考答案；不渲染正文。

## 3. 前端只读页（views/admin/EvolutionConsole.vue）

- 状态分流：功能未启用 / 控制台不可用(未配置根) / 无数据 / 查询失败 / 有数据；
- 区分：simulated/real 徽标；运行 status/stop_reason；未验证链路与效果横幅；
- token/费用未知 → 显示“未知”（后端 null），**不显示 0**；
- 列表分页；轨迹摘要按需加载（点击查看弹窗，不预载大内容）；
- 运行表展示 模式(simulated/real) / 已用模型调用与上限 / token 与费用“未知（不显示为 0）”；
- 展示仅用插值（`{{ }}`），**不使用 v-html**——HTML/Markdown 视为不可信内容，
  不执行其中脚本（框架默认转义）。

## 4. 本轮明确不做

无 启动/恢复/取消/模型调用按钮；无 业务晋升/绑定修改/回退按钮；无 原始资料/参考
答案/任意文件下载；无 模型密钥/认证头/敏感配置下发。v2 判定状态（pass/fail/
needs_review/invalid）仅当记录存在时如实展示（当前未接入正式门控，评估行以
valid/invalid 与分数呈现，v2 未接线）。

## 4a. 技能差异交付范围

当前只交付 **diff 元数据**（parent→candidate 版本/哈希、patch 字符数、action）；
页面显示版本与哈希，并明确标注“正文差异对比尚未实现”。`/skills` 响应含
`content_diff_supported: false` 与说明；**不宣称已支持技能正文对比**，本轮不扩大实现。

## 5. 验收测试（实际执行）

- 后端：`tests/test_skill_evolution_stage8.py` —— **5 passed**（含补审新增项）：
  ① 未登录/非管理员拒绝；② 功能关闭与实验库缺失 → 明确不可用且**无建库副作用**；
  ③ 列表/详情/技能/门控/评估/轨迹返回**真实持久记录**（值断言、real 模式、
  token/cost=null 未知用法、作用域过滤）；④ 未知根/路径越界 →404，读取前后
  `skill_store.db` 哈希不变（无写入/迁移），无模型请求；⑤ 补审边界：注入“密钥/正文/
  脚本”后响应只透出白名单字段（current_skill_set 成员键白名单、轨迹显式字段白名单、
  不出现 sk-*/正文/`<script>`），符号链接与根外目录被拒绝，`content_diff_supported`
  =false 且注明正文差异未实现。
- 浏览器验收（headless Chromium + 隔离临时环境 data/dev8a：合成实验根 + 独立业务库
  + vite dev；已删除，未触碰生产配置）：
  - 未登录 → 重定向 /login；非管理员 → /403（无权访问页）；
  - 功能未启用 → “功能未启用”提示；未配置根 → “控制台不可用 + 未配置服务端实验根映射”；
  - 正常数据 → 实验行（wiki-default-v3）；详情 drawer：run completed / max_iterations /
    real 徽标 / 12/90 已用上限 / token 与费用“未知（不显示为 0）”/ 未验证横幅；
  - 轨迹弹窗：exec-ui-001 + “仅摘要”提示；页面刷新后数据正常重载；
    浏览器控制台无未处理错误。
- **未执行（如实注明，不宣称通过）**：浏览器“空数据”与“请求失败”两个展示分支
  （种子库恒有数据、后端正常返回，未在浏览器触发）；“恶意 HTML 按文字显示不执行”以
  代码审计（全页面无 v-html）与后端白名单/脱敏测试为证据，未在浏览器注入 XSS 样例
  验证（内容不向页面返回，无可执行路径）。
- 前端：`npm run build`（vue-tsc + vite）**已执行并通过**。

## 6. 配置样例（服务端，示例值；不包含任何密钥）

```json
// 环境变量 WIKISKILL_CONSOLE_ENABLED=true
// WIKISKILL_CONSOLE_ROOTS={"demo-d":"D:/data/wiki-experiments/d-demo"}
```
