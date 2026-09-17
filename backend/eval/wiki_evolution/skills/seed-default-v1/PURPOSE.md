# default 技能种子 v1 —— PURPOSE.md

> 来源：`builtin default`（instructions.md + skill.yaml）；整理方式见「演化历史」。
> 本种子为 builtin 来源的人工种子，**未经自动进化验证**（无验证分数、无 accept 事件）。

# 改进目的

- 把 builtin 仅声明的“内容结构”变成可注入并可校验的**可执行编译指令**：
  覆盖普通正文与分版本正文的生成契约、版本归属、冲突标注与禁止事项。
- 为阶段 2 的“指令版本真正进入模型请求”提供 v1 基线；后续进化在 v1 之上产生新版本。
- 让「适用条件/不适用条件/操作步骤」成为可被评分器核对的确定文本（而非仅结构描述）。

# 关联经验或来源说明

- 来源：`app/core/wiki_skills/builtin/default/instructions.md`；
  `builtin/default/skill.yaml`（结构字段：summary/facts/版本块/protected Section/Map-Reduce）。
- 结构事实来源（代码，非推测）：
  - 普通正文 JSON：`{"summary","content"}`（wiki_page_builder.WIKI_SYNTHESIS_PROMPT）；
  - 版本化 JSON：`{"summary","common","versions","unversioned"}`
    （WIKI_SYNTHESIS_VERSIONED_PROMPT；versions 标签由提示词指定，LLM 不得新增）；
  - 同版本来源矛盾 → diff_notice（提示词规则 5）。
- 本阶段无 Pattern 引用（经验 Wiki 未建立）：种子如实标记 builtin/manual 来源，
  不引用任何经验 execution。

# 演化历史

- v1（本版）：builtin 说明 → 人工整理扩写种子。
  - 保留：内容结构、合并规则、人工保护 Section 保留语义、Map-Reduce 说明。
  - 新增（人工整理，非 builtin 原文）：适用/不适用条件、十步操作流程、
    JSON 输出契约显式化、来源约束与禁止事项。
  - 状态：builtin_seed 来源，未经自动进化验证；不宣称与旧 instructions.md 等价。
- 后续版本须记录：父版本、变化动机、关联经验 execution、验证结论。
