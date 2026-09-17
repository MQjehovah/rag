# 阶段 8B：技能正文与精确版本差异（08b-skill-content-diff.md）

> 只读扩展：查看不可变版本的 SKILL.md/PURPOSE.md、任意两个精确版本正文差异；
> 不提供任意文件读取；正文按不可信文本安全展示（前端无 v-html，后端原样透传
> 并带 unsafe/integrity 标记）。不代表效果验收。

## 1. 后端（app/api/evolution_console.py，只读 mode=ro 不变）

| 端点 | 内容 |
| --- | --- |
| GET /skills/{version_id}/content?root | 精确版本正文：version/skill/seq/parent/hash/source + integrity=ok（哈希一致性重算校验）+ skill_md/purpose_md（字符数/截断标记/正文）。不存在→404；哈希失配→422“内容校验失败（哈希与记录不符），版本可能损坏”；超长（>20000 字符）只截断展示并标注 truncated |
| GET /skills/{version_id}/diff?root&base_version_id | 同 skill 两版本的 unified diff（SKILL.md/PURPOSE.md 分文件）；跨 skill→422；超长（>500 行）截断标注 |
| GET /experiments/{id}/versions?root | 实验根全部不可变版本元数据目录（供前端精确选择基准/对比版本；不含正文） |

- 内容校验与写入同源：`sha256(canonical_json({skill_id, domain, runtime_ref,
  schema_version, skill_md, purpose_md}))`，与 skill_store `_recompute_hash`
  一致 → 损坏内容明确报错，不静默展示不可信内容。
- `/experiments/{id}/skills` 摘要现在返回 `content_diff_supported: true`，
  note 指向 diff 端点（候选条目仍只摘要元数据）。

## 2. 前端（EvolutionConsole.vue）

- 技能表新增：查看正文（dialog：完整性/父版本/来源/哈希 + 分文件 tab，`<pre>{{ }}`
  插值原样展示，无 v-html）；对比差异（dialog：选择任意两个精确版本，统一 diff 文本）；
- 页面注明：正文与差异视为不可信文本，不解析、不执行 HTML 或指令；截断提示明确。

## 3. 验收（实际执行）

- `tests/test_skill_evolution_stage8b.py` —— **9 passed**：
  ① 正文完整性/来源/父版本/截断标记；② 同 skill diff（含 +/- 行、PURPOSE 无差异）；
  ③ 跨 skill 422；④ 损坏版本 422 且不影响其它版本；⑤ 缺失版本/未知根/路径越界 404；
  ⑥ 超长截断（chars>20000 且 body 截断）；⑦ 恶意正文原样透传（unsafe）+ 读取前后
  库哈希不变（零写）；⑧ skills 摘要 diff_supported=true；⑨ 未登录 401 / 非管理员 403。
- `tests/test_skill_evolution_stage8.py`（5）断言同步更新为 content_diff_supported=true。
- 前端 `npm run build` 通过。
