# V3 基线统计（P0-BE-01）

- 数据库：`C:\Users\20474\Documents\学习Agent\gitlab-rag-feature\backend\data\notes.db`
- 生成时间：2026-08-18T15:27:06
- 孤儿记录总数：**0**

## 表级数量（与审核时基线对比）

| 表 | 当前数量 | 审核时基线（V3 文档 1.2 节） |
| --- | --- | --- |
| notebooks | 1 | - |
| pages | 144 | 144 |
| page_chunks | 5403 | 5403 |
| knowledge_objects | 403 | 403 |
| ko_chunks | 368 | - |
| graph_entities | 15 | 15 |
| graph_relations | 8 | 8 |
| graph_edges | 0 | - |
| conflict_tasks | 0 | - |
| query_logs | 76 | 76 |
| knowledge_debts | 6 | 6 |
| users | 1 | - |
| user_groups | 1 | - |
| evidence_items | not_created | - |
| evidence_links | not_created | - |
| asset_observations | not_created | - |

## KnowledgeObject 分布

| 状态 | 数量 |
| --- | --- |
| pending | 358 |
| extraction_failed | 35 |
| published | 9 |
| rejected | 1 |

| 类型 | 数量 |
| --- | --- |
| procedure | 338 |
| fact | 65 |

| 指标 | 当前 | 审核时基线 |
| --- | --- | --- |
| Published 总数 | 9 | 9 |
| Pending | 358 | 358 |
| Extraction Failed | 35 | 35 |
| Published 缺少 evidence_ids | 8 | - |
| Published Procedure 缺少 evidence_ids | 6 | 6 |
| 无 source_page_id | 3 | - |
| 重复 content_hash 组数 | 0 | - |

## Evidence

P1 落地前以 `knowledge_objects.evidence_ids` 覆盖情况为代理指标。

| 指标 | 数量 |
| --- | --- |
| 带 evidence_ids 的 KO | 1 |
| 表 evidence_items | 未创建（P1 待新增） |
| 表 evidence_links | 未创建（P1 待新增） |
| 表 asset_observations | 未创建（P1 待新增） |

## Graph

| 实体类型 | 数量 |
| --- | --- |
| component | 7 |
| version | 6 |
| product | 2 |

| 关系类型 | 数量 |
| --- | --- |
| belongs_to | 6 |
| supersedes | 2 |

实体 ko_ids 悬空引用（指向不存在的 KO）：0

## KnowledgeDebt 与 QueryLog

| debt_type | 数量 |
| --- | --- |
| no_answer | 3 |
| low_score | 1 |
| no_evidence | 1 |
| no_owner | 1 |

| debt status | 数量 |
| --- | --- |
| open | 6 |

| QueryLog 指标 | 数量 |
| --- | --- |
| 总数 | 76 |
| Judge 通过 | 7 |
| Judge 未通过 | 69 |
| 未记录 judgment | 0 |
| 反馈 good | 0 |
| 反馈 bad | 1 |

## 孤儿记录

| 检查项 | 数量 |
| --- | --- |
| page_chunks_without_page | 0 |
| ko_chunks_without_ko | 0 |
| ko_without_source_page | 0 |
| graph_relations_without_entity | 0 |
| graph_relations_with_dangling_ko | 0 |
| graph_edges_without_page | 0 |
| pages_with_dangling_notebook | 0 |
| total | 0 |

## 索引健康度

| 指标 | 数量 |
| --- | --- |
| page_chunks_without_embedding | 0 |
| ko_chunks_without_embedding | 0 |
| pages_without_chunks | 0 |
| pages_index_dirty | 0 |
