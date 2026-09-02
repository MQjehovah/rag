# Phase H-1 删除/保留对象矩阵

> 生成时间：Phase H-1。当前迁移 head：`c5e6f7a8b9d0`（P32 V4 债务字段）。
> 新迁移 down_revision 指向 `c5e6f7a8b9d0`。

## 一、删除对象（ORM 表 → 当前引用 → 替代链路 → 可否删除）

| 删除对象（表） | 当前引用（清理前） | 替代链路 | 可否删除 |
|---|---|---|---|
| `knowledge_cards` / KnowledgeCard | cards.py、p5_graph.py、wiki.py、conflicts.py、qa.py、persistence.py、wiki_builder.py、wiki_links.py、jwt_utils.py | Wiki（wiki_pages）成为正式知识层 | ✅ |
| `knowledge_card_revisions` | cards.py、persistence.py、wiki_builder.py | WikiRevision 承接版本 | ✅ |
| `knowledge_card_blocks` | cards.py、qa.py、persistence.py、wiki_builder.py | WikiSection 承接正文 | ✅ |
| `knowledge_claims` | cards.py、conflicts.py、persistence.py、wiki_builder.py | 无替代（事实断言随 Card 退出） | ✅ |
| `knowledge_card_sources` | cards.py、conflicts.py、jwt_utils.py、persistence.py | WikiPage.source_page_ids 承接来源 | ✅ |
| `card_compile_reports` | persistence.py | 无替代（编译报告随编译退出） | ✅ |
| `card_entity_links` | cards.py、p5_graph.py、card_graph.py、qa.py | page_graph.py 纯内存实体（不落库） | ✅ |
| `card_graph_relations` | cards.py、p5_graph.py、card_graph.py、graph_retrieval.py | page_graph.py 纯内存关系 | ✅ |
| `canonical_entities` | card_graph.py、graph_retrieval.py、entity_resolution.py、p5_graph.py | page_graph.py 用确定性 entity key（不落库） | ✅ |
| `entity_aliases` | entity_resolution.py、p5_graph.py | 同义词表已并入 normalizer（纯函数，无表） | ✅ |
| `knowledge_communities` | cards.py、p5_graph.py、wiki.py、access_control.py、community.py、wiki_builder.py | Page/Chunk/Evidence 驱动 page_graph 纯内存社区 | ✅ |
| `community_members` | p5_graph.py、community.py、wiki_builder.py | 纯内存社区成员 | ✅ |
| `community_rebuild_jobs` | community.py | 纯内存社区无需重建任务 | ✅ |
| `wiki_citations` | wiki.py、wiki_lifecycle.py、wiki_builder.py、wiki_links.py | Wiki 不再引用 Card/Evidence | ✅ |
| `conflict_tasks` | conflicts.py、conflict_center.py | 冲突中心随 Card 退出 | ✅ |
| `query_logs` | debts.py、knowledge/debt.py | 旧 debt 扫描退出，V4 债务不依赖查询日志 | ✅ |
| `debt_candidates` | knowledge/debt_candidate.py | V4 债务用 KnowledgeDebt 直接记录 | ✅ |
| `knowledge_debt_queries` | debts.py | V4 债务不再关联查询日志 | ✅ |
| `knowledge_debt_cards` | debts.py、wiki_builder.py | V4 债务不再关联 Card | ✅ |
| `evidence_links` | cards.py、conflicts.py、debts.py、wiki.py、qa.py、persistence.py | Evidence 不再桥接 Card/Claim | ✅ |
| `graph_edges` | graph.py、dingtalk_rag_importer.py | 旧 Page 相似度图谱（GraphBuilder）已删 | ✅ |

**旧字段（在保留表内）**：
- `pages.compile_hash`（Card 编译哈希）→ 已删。
- `knowledge_debts.resolution_card_id` / `resolution_evidence_id`（Card/Evidence 解决引用）→ 已删。

## 二、保留对象（必须保留）

| 保留表 | 用途 |
|---|---|
| `notebooks` | 权限域（group_id） |
| `pages` | 原始文档 |
| `page_chunks` | Chunk 检索 + embedding |
| `users` / `user_groups` | RBAC |
| `vision_analysis_jobs` | 图片理解后台任务 |
| `wiki_pages` / `wiki_revisions` / `wiki_sections` / `wiki_links` | 正式知识层 |
| `knowledge_debts`（V4 字段 original_query/normalized_query/cluster_key/affected_user_count/scope_id/retrieval_reason） | 知识债务 |
| `knowledge_debt_users` | 债务受影响用户去重 |
| `evidence_items` / `asset_observations` | 证据与图片观察 |
| `runtime_feature_flags` | Feature Flag |
| `source_connections` / `source_items` / `source_sync_runs` / `source_sync_errors` | 数据源 |

## 三、删除代码文件

API：cards.py、conflicts.py、governance.py、p5_graph.py、debts.py、legacy.py（Phase G 临时 readonly router）。

核心：knowledge_compiler/conflict_center.py、knowledge_compiler_v3/{card_builder,card_graph,claims,cluster,community,consolidation,derivatives,diff,graph_retrieval,matcher,migration,persistence,pipeline,quality,wiki_builder,wiki_links,worthiness}.py、knowledge/{debt,debt_candidate,quality_metrics,root_cause,knowledge_status}.py、legacy_ko_inventory.py、retrieval/{qa,pipeline,dense}.py、graph.py、evidence_validator.py、knowledge_compiler_v3/entity_resolution.py、knowledge_compiler 包、knowledge 包。

## 四、删除 flags

card_v3_enabled、unified_retrieval_enabled、debt_chat_enabled、card_graph_enabled、layered_retrieval_enabled、source_card_compile_enabled、legacy_debt_card_enabled、legacy_readonly。

保留 flags：wiki_topic_enabled、source_hub_enabled、dingtalk_connector_enabled、gitlab_connector_enabled。
