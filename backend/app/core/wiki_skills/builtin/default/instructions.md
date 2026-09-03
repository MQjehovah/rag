# 默认知识结构（default Skill）说明

本 Skill 是通用主题 Wiki 的默认内容结构，作为所有 Wiki 编译的可靠 fallback。

## 内容结构

- summary：短摘要；
- 普通 facts/body 正文；
- 版本化内容支持 common / version / unversioned 分块；
- protected Section 保护（人工锁定的 Section 不因自动刷新被覆盖）；
- 多来源 Map-Reduce 聚合。

## 使用边界

- 本 Skill 不判断 Wiki 主题、不修改 Workspace/ACL；
- 本 Skill 不创建/发布 Revision，不直接写数据库；
- 本 Skill 只声明「默认内容结构」供 Skill Router 作为 fallback 选择。
