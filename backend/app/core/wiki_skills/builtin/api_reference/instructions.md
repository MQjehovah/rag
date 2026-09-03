# api_reference Skill 说明（受控文本；Loader 只做大小/编码校验，不执行）

- 提取：OpenAPI JSON/YAML 走 Phase 7A 确定性 parser；Markdown 先确定性 hint，
  仅在提供 Fake LLM runner 且存在 active excerpt 时才做受限候选提取。
- 事实纪律：每个事实字段必须绑定 active Evidence；无 Evidence 内容只进入
  knowledge_gaps。
- 蓝图：由确定性 plan_document 生成，不允许 LLM 自由决定 section_key。
- 发布：publish_by_skill 唯一写产品表；同一事务写 Revision/Section/Binding/
  Page/Skill 字段与 Manifest；任一步失败整体回滚，旧 current_revision 不变。
- 安全：Artifact/Section 不保存 Page 原始正文、excerpt、Prompt、ACL、Secret；
  不调用真实模型（本轮仅注入 Fake）。
