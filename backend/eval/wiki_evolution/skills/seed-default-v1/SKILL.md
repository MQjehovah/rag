```yaml
skill_id: default
schema_version: "1"
domain: wiki_compile.default
runtime_ref: wiki.compile.default.runtime/v1
```

# default 技能种子 v1 —— 说明

> 种子来源：`backend/app/core/wiki_skills/builtin/default/instructions.md`（builtin）。
> 差异记录：builtin 原文只声明内容结构（summary/facts/版本块/protected Section/Map-Reduce
> 聚合），不含具体操作步骤；本种子补充「操作步骤」与「适用/不适用条件」的可执行规则，
> 并把既有编译产物契约（JSON 输出结构、来源约束）显式写成步骤。
> 因此本文件是 builtin 说明的**人工整理扩写**，不等价于旧 instructions.md 原文。

# 适用条件

- 编译目标为通用主题 Wiki（`wiki.default` v3 的 default 分支）：面向用户问答的产品、
  接口或操作知识页。
- 来源资料为允许进入本任务的原始资料（来源文档正文）。
- 输出目标是单主题页正文：普通正文（summary + content）或分版本正文
  （summary/common/versions/unversioned）。

# 不适用条件

- 不适用于 API Reference 结构化字段提取（结构化 Endpoint 表、字段级绑定走另一技能）。
- 不适用于对 Workspace、ACL、Revision 生命周期、发布、权限的修改——本技能只负责
  生成正文内容，不修改任何持久化结构。
- 不适用于输入快照之外的资料：不得引用或推断任务未提供的资料内容。
- 不适用于执行 Python、动态加载模块或访问任意文件路径。

# 操作步骤

1. 只依据提示词中「来源文档」与「现有正文」生成正文；不得引入资料外事实。
2. 普通正文返回 JSON 单对象：`{"summary": "≤30 字", "content": "markdown 正文"}`。
   分版本正文返回 JSON：
   `{"summary": "...", "common": "...", "versions": [{"version": "...", "content": "...", "diff_notice": ""}], "unversioned": "..."}`。
3. 正文必须覆盖：适用条件、前置条件、操作步骤、关键参数；内容具体可执行。
4. 多来源合并：保留全部来源的关键知识（命令/代码/参数），不得用后一来源覆盖前一来源。
5. 保留现有正文中仍然有效的内容（含人工润色/修正段落）。
6. 版本归属：版本相关的参数、路径、命令只写入对应产品版本块，不得提升到通用说明；
   versions 只允许包含提示词允许的版本标签，不得新增版本。
7. 冲突处理：同一版本内部来源互相矛盾时不得伪造统一结论，在对应版本的
   `diff_notice` 写「该版本信息存在差异」。
8. 参数与结论必须与来源一致（如扭矩、电压限值）；不确定时宁缺毋造。
9. 页面间引用用 `[[页面标题]]` 语法；保留关键代码块。
10. 禁止引入经验库、历史训练答案、验证答案或其他任务资料；禁止输出无关解释文本。

# 输出约束

- 只输出规则 2 规定的 JSON，不得附加 Markdown 围栏之外的解释。
- 内容中不得出现认证凭据、内部路径、内部错误细节。
