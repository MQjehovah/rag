# Phase 9B 第二批（闭合）：OpenAPI 版本接线 + Pipeline 时间配置校验

> 主 Agent 冻结（基线 35eb3e3）。Agent A 负责版本接线与契约，Agent B 负责配置读取/校验。
> 同文件单一写入者。延续隔离要求：临时/内存 SQLite + Fake 模型 + 真实 Alembic；
> 不读/不迁移真实库、不调远程 Connector/真实模型、不改正式 .env、不清理用户运行产物。

## 0. 授权范围（不越界）
- A. OpenAPI 显式版本范围接线；B. Pipeline 时间配置校验。
- 不进入数据库副本迁移；不新增前端功能；不改 lease/fencing/recovery 状态机、不重构 executor、
  不新增数据库列、不改已发布迁移、不改旧资料。

## 1. 版本契约（按此实施，不再猜测）
项目专用 OpenAPI 顶层扩展字段：`x-wiki-version-scope`（示例见 §5）。
- 该字段是**本项目扩展**，不宣称是标准 API 版本字段。
- 作用范围：该份 OpenAPI 文档中的**全部 Endpoint**。
- 缺失 → 维持既有 unversioned（缺省语义，不宣称是资料明示版本）。
- 存在但 null / 非字符串 / 空白 → **来源级受控失败，禁止发布**（即使其它来源合法也不得静默
  丢弃坏来源后发布）。
- 合法值按既有 `identity` 的 `normalize_version_scope` 规范化（小写/去空白/字符集/长度）；**不新增**
  另一套规范化算法。
- 不读取 URL / 文件名 / 标题 / `openapi` 版本 / `info.version` 来推断版本。
- Markdown 保持既有 unversioned，不新增自然语言版本提取。

实现边界：
1. 在**现有 OpenAPI 解析路径**读取扩展（顶层解析后一次读取），**不让 adapter 再独立解析一遍全文**。
2. 保留既有内部显式 `version_scope` 参数兼容：
   - 内部参数为空/缺省 → 用文档声明（再缺省 → unversioned）；
   - 内部参数与文档声明都**明确指定**时：规范化后相同 → 接受；不同 → 受控失败（明确错误码/issue，
     零发布）。
3. 文档声明版本与 Endpoint.version_scope 的 **Evidence Binding 必须通过既有覆盖门禁**；
   不能借无关字段/其它来源的证据伪造支持（同 method/path 的 v1、v2 各自用自己的资料 Evidence）。
4. 只改版本声明本身 = 来源内容变化：继续参与 Page/source hash、编译输入、发布前重验
   （改声明不同步 Evidence `source_doc_hash/content_hash` 必须过不了 `_reverify_api_publish`）。
   不绕过既有缓存/重试一致性保护。
5. 非法声明 = 来源级阻断，publishable=False / compile 受控失败；不改旧资料。
6. 兼容 JSON 与 YAML（既有安全解析路径），两种输入都要有测试。

## 2. 版本验收测试（Agent A 新建 `backend/tests/test_phase9b_version_scope.py`）
复用既有 fixtures 与真实 Pipeline 测试方式（alembic head 临时库 + v3 executor + 真实发布），至少：
1. **同 method/path 双版本隔离**：两份资料（各自 OpenAPI 文档）显式声明 `v1`/`v2`，同 method/path
   → 两套 Section key（`api_endpoint|…|v1` / `|v2`）、内容与 Evidence Binding 各自正确隔离；
   v1 页 Evidence 不支撑 v2 section，反之亦然。
2. **修改 v1 参数后重编**：v2 语义与字段/证据不串改；不要求跨 Revision 的行主键相同。
3. **无声明 → unversioned**（真实链断言 `…|unversioned`）。
4. **非法声明**（null/非字符串/空白，含 YAML）：validate/compile 受控失败；旧 current_revision
   不变、无新发布、dirty 保持；即使另一来源合法也不发布。
5. **内部参数 × 文档声明边界**：相同接受、不同受控失败；内部空用声明。
6. **大小写等规范化**沿用既有 `normalize_version_scope` 规则（如 ` V1 ` → `v1`）。
7. **只改声明不同步证据 hash** → 过不了发布前重验（EVIDENCE_STALE/PAGE_STALE 或等价，零发布）。
8. 说明：`/v1`、`/v2` **不同路径**不视为版本隔离证据（本测试单独有声明驱动的同路径双版本）。

文件写入者：Agent A 可改 `backend/app/core/wiki_skills/api_reference/`（parser/compiler 为主；
adapter 仅在确实需要时做极小改动并说明），新建上述测试文件；**禁止改** config/executor/worker、
迁移、phase9b 既有文档（主 Agent 维护）。默认（扩展缺失）行为必须与现状一致，既有
API Reference parser/identity/compiler/adapter/merge/runtime 测试应不改即绿；确需调整既有测试时
必须最小并说明。

## 3. 配置校验（Agent B）
新增四配置默认值必须保持：lease=300、heartbeat_timeout=300、poll=2.0、renew=30.0。
校验（复用 Settings/pydantic 校验，不造新框架）：
- lease / heartbeat_timeout：合法正整数；
- poll / renew：有限正数（拒绝 NaN / ±Infinity）；
- `renew <= min(lease, heartbeat_timeout) / 3`（模型级交叉校验）；
- 正常环境变量字符串（如 `"300"`、`"0.5"`）由 Settings 正常解析；
- 非法配置在启动配置阶段明确失败（Settings 构造抛错 → 应用起不来、worker 不启动）；
- 默认配置与既有短 lease 测试配置（lease=6, heartbeat=6, poll=0.5, renew=1.0）都通过。
- executor claim 与 worker heartbeat 继续读**同一** lease 配置；**删除/收紧上一轮新增的宽泛
  `except Exception` 回退**：非法配置不得被吞掉后静默恢复默认值（helpers 直接读已验证的
  settings，不再吞异常回退默认）。

改动（Agent B 单一写入者）：`backend/app/config.py`（字段校验器 + 模型校验器）、
`backend/app/core/wiki_pipeline/executor.py` 与 `worker.py`（helpers 不再吞异常回退；读同一
settings 字段）。默认/合法/短 lease 行为不变。新建测试 `backend/tests/test_phase9b_config.py`：
- 默认值保持；
- 合法短配置通过；
- 反例：非正数、非有限数（NaN/Infinity）、renew>min(lease,heartbeat)/3、非法字符串，
  断言 Settings 构造失败（异常）；
- helpers 非法值不被吞（如 monkeypatch settings 字段为非法后 `_lease_seconds()`/对应 helper 抛错
  或不回退默认）——取决于你最终的 helper 实现，断言“不回退静默默认”语义即可。

禁止改 executor claim/heartbeat/fencing/recovery 状态机本体语义；仅配置读取与校验。不改 9B
既有测试的显式参数用法；不得为迁就非法 fixture 放宽校验。

## 4. 门禁（文件去重、一次组合）
- Agent A：`pytest tests/test_phase9b_version_scope.py` + 受影响 API Reference 测试
  （parser/identity/compiler/adapter/merge/runtime/schemas 等，一次列出组合跑）。
- Agent B：`pytest tests/test_phase9b_config.py` + `tests/test_wiki_pipeline_core.py`（定向回归，
  可 `-k` 收敛到 lease/claim/heartbeat/requeue）+ 若改动影响短 lease 恢复路径则补跑
  `tests/test_phase9b_recovery.py` 中那一个跨进程崩溃用例。
- 主 Agent 集成复核时复跑上述组合（文件去重）。
- 不跑后端全量；不跑前端 build；不重跑 9A 全部浏览器联调。

## 5. 交付文档要点（主 Agent 更新 phase9b/README 与契约）
扩展字段示例：
```yaml
openapi: 3.1.0
x-wiki-version-scope: v2
info: { title: "…", version: "…" }
paths: { … }
```
文档需写明：缺省=unversioned；null/非字符串/空白=来源级阻断；规范化沿用 identity；与内部参数
冲突规则；声明变更走来源 hash/重验；不做 URL/文件名/标题/info.version 推断；Markdown 不变。

## 6. 交付与 checkpoint
- 仅提交：api_reference 相关源码（Agent A）、config/executor/worker（Agent B）、两个新测试文件、
  phase9b 文档更新。不提交日志/DB/截图/缓存；不 amend/push。
- 报告：两项实际改动与分工、版本→Section/Evidence 真实链路、同 method/path 双版本及改一版结果、
  非法声明与非法配置失败结果、测试数量/命令/退出码、checkpoint/git status、剩余阻断。
