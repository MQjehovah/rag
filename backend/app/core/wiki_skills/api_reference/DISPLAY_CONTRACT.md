# Phase 8B 展示 DTO 契约（已冻结）

作用：在 Workspace Wiki 中将 API Endpoint 章节展示为可展开结构化章节（参数/响应/错误码表格）。
后端与前端以本文档为唯一契约。修改契约必须先评审。

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
  // —— Phase 8B 新增：仅 endpoint role 且 validation_status=="pass" 时写入；否则省略——
  "display": {
    "schema_version": "api-section-display/v1",
    "content_hash": "<sha256(section.content) 小写 hex>",
    "section_role": "endpoint",
    "version_scope": "v1",
    "endpoint": { "method": "GET", "path": "/users", "summary": "…", "description": "…" },
    "parameters": [
      { "location": "query", "name": "page", "required": false, "description": "…" }
    ],
    "request_body": null,
    "responses": [
      { "status_code": "200", "description": "…", "schema_present": true }
    ],
    "error_codes": [ { "code": "403", "description": "…", "http_status": "403" } ],
    "examples": [ { "title": "…", "description": "", "media_type": "application/json", "content": {} } ],
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

## 3. display DTO（只读 API 增量返回）

只读接口在每个 section 上**增量**新增两个字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `section_role` | string \| null | 由存储的 structure_json 中合法 `section_role` 解析，不从 heading 猜 |
| `display` | object \| null | 合法时受限 DTO；否则 `null`（前端回退 Markdown） |

`display` 字段白名单（严格按上表，未知键丢弃；类型不符→null）：

```
schema_version: 必须 == "api-section-display/v1"
content_hash:   64 位小写 hex 字符串
section_role:   必须 == "endpoint"
version_scope:  字符串
endpoint.method/path/summary/description: 字符串
parameters[]:   { location∈{path,query,header}, name:str, required:bool, description:str }
request_body:   null 或 { required:bool, description:str, media_types:[{media_type:str, schema_present:bool}] }
responses[]:    { status_code:str, description:str, schema_present:bool }
error_codes[]:  { code:str, description:str, http_status:str }   // http_status 为空串 = 未提供，业务码≠HTTP 状态
examples[]:     { title:str, description:str, media_type:str, content:<json-safe> }
version_notes[]:{ version_scope:str, note:str }
knowledge_gaps[]:{ gap_type:str, description:str }
conflicts[]:    { field_path:str }   // 只提示存在冲突，不定论，不暴露证据 id/候选
```

大小/嵌套边界：整个 display 序列化后 > 100_000 字节，或嵌套深度 > 12 → 视为超限，`display=null`（降级 Markdown，绝不静默截断冒充完整）。

## 4. 降级规则（只读 API 计算 display=null 的条件）

任一命中即 `display=null`（`section_role` 仍可返回合法 role）：

1. structure_json 缺失 / 非 JSON / 顶层不是对象 / `section_role` 不在合法 role 集。
2. role 不是 `endpoint`（overview/authentication/… 不结构化）。
3. 该 section 的 `validation_status` 非 `pass`。
4. `content_origin=='manual'` 或 `merge_policy=='protected'` 或 `locked` 为真（人工/保护优先 Markdown）。
5. structure_json 无 `display` 子对象（历史数据/未知版本）或 display 未通过白名单/版本/类型/边界校验。
6. 现网正文 hash 与 `display.content_hash` 不一致（sha256(section.content)）。

普通读者不返回内部字段：不返回 section_key/hash/skill_key/validation JSON/结构诊断/Evidence。

## 5. 正文与编辑语义（不可变约束）

- Markdown `content` 仍是历史兼容、编辑与降级的依据；不做展示则整节按 Markdown 渲染。
- 编辑始终改原始 Markdown（既有 lifecycle 会复制出无 structure_json 的新行 → role/display 自然为 null）。
- protected/manual 复制逻辑不改写（v3 `_write_protected_section` 原样随行复制结构；lifecycle 仍不复制 P44 字段）。
- 结构化展示有效时，前端不再重复渲染该节整段 Markdown。

## 6. 前端展示行为

- Section 阅读分支分派：`section_role=='endpoint' && display` → ApiEndpointSection；否则 MarkdownPreview。
- endpoint 标题清晰显示 method/path/version；参数表（位置/名称/必填/说明）、响应分区、错误码分区（业务码不并入 HTTP 状态）。
- request body/schema/examples 用文本或安全代码块（不执行、不 v-html 原始字段、不自动下载外部 schema）。
- version_notes 与 knowledge_gaps 保持独立分区并有清晰提示；conflicts 只显示“存在冲突，未判定对错”。
- 空 description/http_status 显示“未提供”；必填等布尔原样显示；不虚构类型/默认值/状态列。
- 折叠/展开按钮支持键盘与 `aria-expanded`；展开状态不跨 Workspace/Wiki/Revision/Section 串用。
- 编辑后必须重新读取服务端 display DTO（不能保留编辑前表格）。
- 320px：表格局部横向滚动/折行，不造成整页横向溢出。
