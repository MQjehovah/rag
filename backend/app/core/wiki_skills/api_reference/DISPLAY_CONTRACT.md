# Phase 8B/8B.1 展示 DTO 契约（已冻结）

作用：在 Workspace Wiki 中将 API Endpoint 章节展示为可展开结构化章节（参数/响应/错误码表格）。
后端与前端以本文档为唯一契约。修改契约必须先评审。

当前展示版本：**`api-section-display/v2`**（8B.1：参数显式 type、请求体/响应逐媒体类型保留、
schema 保守判定）。旧版 v1 或缺少 v2 必需字段的展示数据一律降级 Markdown，不回填猜测值。

## 1. 数据来源与禁止项

- 展示事实只来自**已通过验证的 `ApiCompileResult.ir`**（`ApiDocumentIR` 的事实字段）。
- 投影 = 每个 endpoint role Section 一个确定性字典，**不复制整份 IR**、不复制 Evidence/Prompt/模型输出/内部诊断/ACL/路径/凭证/`skill_decision_json`/Artifact。
- 不读取历史 CompileArtifact 拼装页面；不新增迁移/表/列；不改 Markdown 正文。
- `WikiSection.structure_json` 仍是唯一存储位：保留原 Blueprint spec 字段，另加命名空间子对象 `display`。

## 2. structure_json 存储形态（compile 时写入）

```jsonc
{
  // …原 ApiSectionSpec.to_dict() 全部字段（section_key/section_role/heading/required/
  //   version_label/endpoint_id/field_paths），含义不变…
  "section_key": "api_endpoint|get|/users|v1",
  "section_role": "endpoint",
  "heading": "GET /users（v1）",
  "required": false,
  "version_label": "v1",
  "endpoint_id": "GET /users [v1]",
  "field_paths": ["method","path","version_scope","summary","description","path_parameters","query_parameters","headers","request_body","responses","error_codes","examples"],
  // —— 仅 endpoint role 且 validation_status=="pass" 时写入；否则省略——
  "display": {
    "schema_version": "api-section-display/v2",
    "content_hash": "<sha256(section.content) 小写 hex>",
    "section_role": "endpoint",
    "version_scope": "v1",
    "endpoint": { "method": "GET", "path": "/users", "summary": "…", "description": "…" },
    "parameters": [
      { "location": "query", "name": "page", "required": false,
        "description": "…", "type": "integer" }
    ],
    "request_body": null,
    "responses": [
      { "status_code": "200", "description": "…",
        "media_types": [ { "media_type": "application/json", "schema_status": "present" } ] }
    ],
    "error_codes": [ { "code": "403", "description": "…", "http_status": "403" } ],
    "examples": [ { "title": "…", "description": "示例说明", "media_type": "application/json", "content": {} } ],
    "version_notes": [ { "version_scope": "v1", "note": "…" } ],
    "knowledge_gaps": [ { "gap_type": "missing_responses", "description": "…" } ],
    "conflicts": [ { "field_path": "responses.200" } ]
  }
}
```

要点：
- `content_hash` 必须在 compile 时与写库的 `WikiSection.content_hash` 用同一函数算出（`sha256((content or "").encode()).hexdigest()`）。
- 单目标与 batch 必须复用**同一个投影函数**。
- 投影随 Section 进入既有发布事务，不新增 commit；失败回滚不残留。
- 展示投影与 `display` 不得含：evidence excerpt/对象、Prompt、模型原始输出、raw ACL、源路径、连接配置、凭证、SkillDecision、整份 IR、diagnostics 明细。

## 3. display DTO（只读 API 增量返回；v2 必需字段，缺失即降级）

只读接口在每个 section 上**增量**新增两个字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `section_role` | string \| null | 由存储的 structure_json 中合法 `section_role` 解析，不从 heading 猜 |
| `display` | object \| null | 合法时受限 DTO；否则 `null`（前端回退 Markdown） |

`display` 字段白名单（严格按上表，未知键丢弃；类型不符→null）：

```
schema_version: 必须 == "api-section-display/v2"
content_hash:   64 位小写 hex 字符串
section_role:   必须 == "endpoint"
version_scope:  字符串
endpoint.method/path/summary/description: 字符串
parameters[]:   { location∈{path,query,header}, name:str, required:bool,
                  description:str, type:str }   // type 为空串 = 未提供；禁止猜测
request_body:   null 或 { required:bool, description:str,
                  media_types:[{media_type:str, schema_status:str}] }
responses[]:    { status_code:str, description:str,
                  media_types:[{media_type:str, schema_status:str}] }
error_codes[]:  { code:str, description:str, http_status:str }   // 空串 = 未提供；业务码≠HTTP 状态
examples[]:     { title:str, description:str, media_type:str, content:<json-safe> }
version_notes[]:{ version_scope:str, note:str }
knowledge_gaps[]:{ gap_type:str, description:str }
conflicts[]:    { field_path:str }   // 只提示存在冲突，不定论，不暴露证据 id/候选
```

### 3.1 语义约束（不得推测事实）

- **参数类型**：`parameters[].type` 只读 `schema.type` 显式声明——字符串原样；类型数组（冻结后为
  tuple/list）按 `a | b` 展示；未声明或无法支持时为空串（UI 显示“未提供”）。
  绝不根据 name/example/format/default 猜类型；不新增完整 Schema 浏览器。
- **媒体类型保留**：`responses[].media_types` / `request_body.media_types` 保留原 Markdown 已展示的
  每个媒体类型名称；无 content 的响应 `media_types=[]`（UI 仍保留该状态码行，媒体类型显示“未提供”）。
- **schema 保守判定**：`schema_status ∈ {present, unspecified}`。只有该媒体类型对应的 schema 为非空
  容器时才是 `present`；`content={"media": {}}` 或 schema 缺失（IR 无法区分“缺失”与“显式空”）一律
  `unspecified`，UI 文案为“未提供具体结构”，**绝不宣称“无 Schema”**。不再使用布尔 schema_present。
- **example.description** 属于白名单并须展示（空串 = 未提供）。

大小/嵌套边界：整个 display 序列化后 > 100_000 字节，或嵌套深度 > 12 → 视为超限，`display=null`
（降级 Markdown，绝不静默截断冒充完整）。

## 4. 降级规则（只读 API 计算 display=null 的条件）

任一命中即 `display=null`（`section_role` 仍可返回合法 role）：

1. structure_json 缺失 / 非 JSON / 顶层不是对象 / `section_role` 不在合法 role 集。
2. structure_json 长度超限（先字符数后 UTF-8 字节数，超出直接安全降级，不完整解析）。
3. role 不是 `endpoint`（overview/authentication/… 不结构化）。
4. 该 section 的 `validation_status` 非 `pass`。
5. `content_origin=='manual'` 或 `merge_policy=='protected'` 或 `locked` 为真（人工/保护优先 Markdown）。
6. structure_json 无 `display` 子对象（历史数据）或 schema_version≠v2 / display 未通过白名单/类型/版本/边界校验（含旧版 v1、缺 type/schema_status/media_types 等新必需字段）。
7. 现网正文 hash 与 `display.content_hash` 不一致（sha256(section.content)）。

普通读者不返回内部字段：不返回 section_key/hash/skill_key/validation JSON/结构诊断/Evidence。

## 5. 正文与编辑语义（不可变约束）

- Markdown `content` 仍是历史兼容、编辑与降级的依据；不做展示则整节按 Markdown 渲染。
- 编辑始终改原始 Markdown（既有 lifecycle 会复制出无 structure_json 的新行 → role/display 自然为 null）。
- protected/manual 复制逻辑不改写（v3 `_write_protected_section` 原样随行复制结构；lifecycle 仍不复制 P44 字段）。
- 结构化展示有效时，前端不再重复渲染该节整段 Markdown。

## 6. 前端展示行为

- Section 阅读分支分派：`section_role=='endpoint' && display`（且通过前端运行时契约校验）→
  ApiEndpointSection；否则 MarkdownPreview。
- 运行时契约校验：schema_version/section_role/endpoint 必需字段/各数组与元素类型/request_body
  nullable 结构/`required` 必须为真 bool。不合法整节回退 Markdown，不部分渲染、不抛组件异常、
  不调模型或解析 Markdown 补结构。
- endpoint 标题显示 method/path/version；参数表列：位置/名称/类型/必填/说明；
  响应与错误码独立分区；请求/响应逐媒体类型保留并显示保守结构文案。
- request body/schema/examples 用文本或安全代码块（不执行、不 v-html 原始字段、不自动下载外部 schema）。
- version_notes 与 knowledge_gaps 保持独立分区并有清晰提示；conflicts 只显示“存在冲突，未判定对错”。
- 空 description/http_status/type 显示“未提供”；required 等布尔原样显示；不虚构类型/默认值/状态列。
- 折叠/展开按钮支持键盘与 `aria-expanded`；展开状态不跨 Workspace/Wiki/Revision/Section 串用。
- 编辑后必须重新读取服务端 display DTO（不能保留编辑前表格）。
- 320px：表格容器 `overflow-x:auto`、长示例代码在组件内纵向滚动，不造成整页横向溢出。
