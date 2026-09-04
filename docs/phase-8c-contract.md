# Phase 8C 契约（已冻结）

面向：章节 Evidence 追溯、编辑者只读诊断、管理员当前工作区 CompileRun 面板。
后端与前端以本文档为唯一契约；修改先评审。

## 0. 全局原则
- 全部新增只读接口不触发 flush/commit、不触发编译/修复/绑定回填。
- 不返回 raw ACL、连接配置、磁盘路径、完整 ORM 对象、Artifact payload、Prompt、metrics 原始 JSON、原始 reason/诊断文本。
- 隐藏资源不泄露名称、数量、内部决策。授权与计数一律基于“授权后结果”。
- 读者接口返回的“原文”是用户有权查看的 Evidence 正文（EvidenceItem.content），不是后台 Prompt。
- 复用既有 Wiki ACL / Revision 可见性 / 来源 Page 可见性（get_visible_page_ids），不扩大权限。

## 1. 章节 Evidence 查询（只读）
GET `/api/wiki/{wiki_id}/revisions/{revision_id}/sections/{section_id}/evidence?limit=50&offset=0`

鉴权与存在性：
1. Wiki 存在且当前用户可访问（对齐既有 _visible_wiki 语义：普通读者仅 published+ACL 可见；admin 需 ACL 可见）。
2. Revision 必须属于该 Wiki；普通读者仅允许 page.current_revision_id 且 revision.status==published；admin 可看属于该 Wiki 的任意 revision（沿用 detail 的 revision/preview 只读规则，不扩大）。
3. Section 必须属于该 Revision。
4. Evidence 必须由该 Section 的 `WikiSectionEvidenceBinding` 引用。
5. Evidence 的来源 Page（EvidenceItem.source_page_id）必须对当前用户可见（get_visible_page_ids）；不可见即不返回该条，也不计入 total。
任一无权/不存在 → 统一 404（不区分不存在与无权）；无授权绑定时 `items=[]`、`total=0`。

响应：
```jsonc
{
  "wiki_id": "…", "revision_id": "…", "section_id": "…",
  "total": 1,               // 仅授权结果计数
  "limit": 50, "offset": 0,
  "items": [
    {
      "evidence_id": "e1",
      "evidence_type": "text",
      "status": "active",            // EvidenceItem.status: active/stale/rejected
      "hash_matches": true,          // 绑定快照 evidence_content_hash == EvidenceItem.content_hash
      "state": "active_current",     // 见下
      "content": "…",                // 已授权原文；>2000 字符截断
      "content_truncated": false,
      "locator": { "page_number": 3, "heading": "安装", "image_id": null, "content_type": null },
      "source_display_name": "水箱安装指导(原始)",
      "bindings": [ { "field_path": "responses.200", "usage_type": "support" } ]
    }
  ]
}
```
- 同 Evidence 多条 Binding 按 evidence_id 去重为一条，`bindings` 聚合其 field_path/usage_type（排序稳定）。
- `state`：
  - status==active 且 hash_matches → `active_current`
  - status==active 且 !hash_matches → `changed`
  - status==stale → `stale`
  - status==rejected → `rejected`
- EvidenceItem 物理不存在 → 该 Binding 不伪造记录（跳过；也不计入隐藏计数）。
- `content` 上限 2000 字符（截断时置 content_truncated=true）；limit 默认 50、最大 100，列表有界。
- locator 仅白名单字段：page_number(int)、heading(str)、image_id(str|null)、content_type(str|null)，绝不返回 locator_json/chunk_id/bbox。
- 历史 Revision 的 Binding hash 不是“历史正文”的证明：hash_matches 仅表示“绑定快照与当前 Evidence 内容一致”；前端须提示“正文/内容为当前 Evidence，非发布时原文”（统一在抽屉提示）。
- 历史无 Binding 的 Section → items=[]（绝不回退成整 Page 的全部 Evidence）。

## 2. 编辑者只读诊断
GET `/api/wiki/{wiki_id}/diagnostics`

权限：admin 或 access_control.can_edit_wiki(current_user, page) 的用户；否则 403。前端仅在可编辑页面请求。Wiki 不可见 → 404。

响应：
```jsonc
{
  "wiki_id": "…",
  "editable": true,
  "is_current_wiki_config": true,   // Skill 取自已展示的当前 Wiki 配置，不是历史 revision 当时配置
  "skill": {
    "key": "api_reference", "display_name": "API Reference", "version": "1",
    "selection": "auto",            // auto/manual/none
    "selected_by": "u-1",           // 可能 null
    "locked": false,
    "reason_code": "auto_top_score" // 仅允许受控集合；未知/内部 → "unknown"
  },
  "validation": {
    "summary": "pass",              // 任一 fail→fail；否则任一 NULL/unknown→unknown；全 pass→pass
    "sections": [ { "heading": "GET /users（v1）", "validation_status": "pass" } ]
  }
}
```
- 不返回 skill_decision_json、Prompt、候选、原始 reason 文本。
- validation.sections 取自当前查看 Revision 的 sections：validation_status 为 NULL → "unknown"（“历史全 NULL 不得算作通过”）。
- 隐藏 wiki 不泄露 skill/来源数量/决策。
- reason_code 后端只回放一个很小的受控 code 集合（由实现方据 SkillDecision 字段确定），其余一律 "unknown"；前端按受控映射显示文案，未知显示通用提示。

## 3. 管理员当前工作区 CompileRun 面板
- 复用现有 wiki_compile API：`GET /api/wiki-compile/runs`（新增可选 query `workspace_id`，先过滤后 count/分页）、`GET /runs/{id}`（含 stages/artifacts，既有安全序列化）、`POST /runs/{id}/retry`、`POST /runs/{id}/cancel`。
- list 保持 require_admin；普通编辑者不得调用。
- 前端面板标注“当前工作区编译任务”（不是“当前 Wiki 生成历史”）。
- UI 展示：pipeline/version、trigger_type、status、attempt、created/started/finished、safe_error（safe_error_code/message）、stage 时间线（get_run stages）。
- 不显示 Artifact payload/metrics 原始 JSON。
- 无精确 Wiki 关联的 batch Run 不猜测关联。
- retry：仍调 retry endpoint；不新建 run 绕过 max_attempts；409 → 受控提示并刷新状态。
- cancel：queued/running 才显示入口；`cancel_requested` 与最终 `cancelled` 分开显示（status==cancelled 显示已取消；status!=cancelled 且 cancel_requested==true 显示“取消请求中”）；提交后不得直接假装已取消。
- 按钮防重复提交（busy 锁）；服务端权限/状态是最终依据。
- 轮询：仅管理员面板打开且存在非终态任务时自动轮询；请求不重叠（上一次返回后才发下一次）；关闭面板/切工作区/卸载/全部终态后停止；轮询网络错误停止自动轮询并显示手动“重试刷新”。
- 不修改 executor/worker/state_machine/lease/retry 语义。

## 4. 无授权/边界行为（统一提示语义，不区分“不存在”与“无权”）
- 章节证据空/无授权 → “暂无可查看的章节证据”。
- 请求失败与空列表在 UI 中分开。
