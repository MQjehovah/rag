# rag Wiki 向量检索 设计

日期：2026-09-20
状态：已确认，待实施
范围：`rag` 单仓库

## 背景

Wiki 由 LLM 把笔记蒸馏成页面（`core/wiki.py`），可浏览但**完全检索不到**：

- `WikiPage` 没有向量列（`backend/app/models/database.py:110-122`）。
- `RetrievalPipeline.retrieve`（`core/retrieval.py:164-401`）只召回 `page_chunks`(pgvector)/`page_terms`(BM25)/`pages`/图扩展，**从不碰 wiki_pages**。
- 聊天的全局回退走 `graph_communities`（`api/chat.py:195-218`），也不是 wiki。
- 前端 `Wiki.vue:14-20` 只有**标题**客户端过滤，不查 content/summary，无后端搜索。
- `api/search.py` 也无 wiki 来源。

结果：用户把知识蒸馏成 wiki 后，问答仍然只基于原始笔记分块，wiki 的价值无法复用。

## 目标

1. Wiki 页面可被语义检索，并**作为一路召回源进入聊天回答**（可引用）。
2. 提供独立的 wiki 搜索接口，供 Wiki 页做语义搜索。

## 非目标（YAGNI）

- 不做 wiki 分块（一页一个向量，不做 `wiki_chunks` 表）。
- 不重做 wiki 编译逻辑（`build_wiki` 的合并/去重策略不变）。
- 不做跨语言/多向量（仍用现有 bge-large-zh 1024 维）。
- 不改 `graph_communities` 的检索方式。

## 设计

### 1. 存储：照 `page_chunks` 的双写先例

- `WikiPage` 增加 `embedding = Column(Text, nullable=True)`（JSON 数组）。`_migrate_schema`（`database.py:230-247`）会自动为已存在的表补该列（Text 在 SQLite/Postgres 都能编译）。
- Postgres 上另加 `embedding_vec vector(1024)` + HNSW 索引，走**裸 DDL 幂等**先例（新增 `_ensure_wiki_embedding_index(engine)`，仿 `_ensure_wiki_group_index`，`database.py:250-259`），并在 `init_db` 里追加"JSON→vector 回填"（仿 `database.py:299-304`）。
- 理由：生产用 pgvector ANN，SQLite 用 numpy 回退 → **可测**（`page_chunks` 就是这个模式）。

### 2. 嵌入对象与时机

- 嵌入文本 = `title + "\n" + summary`（照 `graph_communities` 嵌 `summary[:800]` 的先例；content 是 Markdown、含图片链接，噪声大且长）。
- 时机：
  - **编译**：`_persist`（`wiki.py:141-163`）返回本次发生变更的 page id；`_ingest_one`（`wiki.py:244-250`）在**释放锁之后**调用新增的 `async def embed_wiki_pages(engine, page_ids)`。放在锁内会跨网络调用持锁，禁止。
  - **批量收尾**：`build_wiki`/`refresh_stale_wiki` 末尾做一次"补齐缺失向量"的扫尾（应对并发去重、失败重试）。
  - **人工编辑**：`update_wiki_page`（`api/wiki.py:109-128`）提交后刷新该页向量。
  - **回填**：新增管理员端点 `POST /api/wiki/reindex-embeddings`（仿 `api/pages.py:310-344` 的 `reindex-all`），返回 `{embedded, errors, total}`。
- 去重：按 page id 去重后再嵌入，避免同一页在一轮内被多个笔记重复嵌入。

### 3. 检索：新增 wiki 召回

- 新增 `core/wiki_search.py`：
  - `search_wiki(db, query_embedding, limit, current_user) -> list[dict]`，返回 `{id, title, summary, content, distance}`。
  - 可见性：非管理员 `group_id IS NULL OR group_id IN (groups)`；管理员不过滤。（与刚加的 wiki 可见性边界一致。）
  - Postgres 用 `embedding_vec <=> CAST(:q AS vector)`（照 `rag.py:351-378` 的写法）；SQLite 回退为读 `embedding` JSON 做 numpy 余弦。
- `RetrievalPipeline.retrieve`（`core/retrieval.py`）新增一路 wiki 召回，与 vector/BM25 并列参与 RRF：
  - wiki 结果 id 统一加前缀 `wiki:`，避免与 `pages.id` 撞车；
  - `sources_map[wiki_id].add("wiki")`；
  - 结果构造（`:366-401`）需要能取 wiki 的 title/content/chunks——用一张**独立的 `wiki_map`**，不要把 wiki id 塞进 pages 的 `page_map`（否则 `:239-257` 查不到会得到空 title）。
- 聊天（`api/chat.py`）：`_agentic_search_notes` 已支持追加额外来源（community 先例，`:195-218`），wiki 走同一路径并标 `source_kind: "wiki"`；`sources` 载荷（`:251-271`）沿用现有 `id/title/chunks/images` 形状。

### 4. 前端

- `Chat.vue:310-325` 的 `openSource`：加 `wiki:` 前缀分支 → 跳 `/wiki/:id`（`community:` 已有"跳过"的先例）。其余保持跳 `/notes`。
- `Wiki.vue`：保留现有即时标题过滤；新增**语义搜索**（输入回车或点按钮）调用 `POST /api/wiki/search`，结果列表可点击打开页面。

### 5. 接口

```
POST /api/wiki/search          body { query, top_k=5 } → { results: [{id,title,summary,category,score}], total }
POST /api/wiki/reindex-embeddings   (管理员) → { embedded, errors, total }
```

## 验收标准

1. 有 wiki 且库中有语义相关页面时，`POST /api/wiki/search` 返回按相关度排序的可见页面；不可见（他组）页面**不出现**。
2. 聊天在命中 wiki 时，回答的 `sources` 包含 `wiki:` 前缀的条目，且前端点击跳转到对应 wiki 页（不是 `/notes`）。
3. 管理员可见全部 wiki；普通用户只见公共 + 本组（与 `GET /api/wiki` 一致）。
4. 编译一条新笔记后，其影响的 wiki 页在**无需重启**的情况下可被搜索到（嵌入随写发生）。
5. `POST /api/wiki/reindex-embeddings` 可为存量页面补齐向量，重复调用幂等。
6. 既有测试全绿（当前 109 passed / 0 errors）；新增测试覆盖：可见性、SQLite 回退路径、结果合并与 id 前缀、回填幂等。

## 风险

- **向量维度/模型变更**：`embedding_vec` 固定 `vector(1024)`，换嵌入模型需重建列与全部向量；本期不做模型切换保护。
- **一页一向量**：长 wiki 页只用 summary 表征，细节可能召回不到；这是刻意的成本/复杂度取舍（与 community 一致）。
- **`wiki:` 前缀是新的 id 命名约定**：现有 `community:` 是唯一先例；需同时改 `Chat.vue`，否则点击会错误跳到 `/notes`。
- **嵌入失败静默**：`encode_batch` 会吞异常并返回空向量（`rag.py:80-84`），因此嵌入失败可能只表现为"搜不到"；回填端点与分析日志需能暴露这种情况。
- **SQLite 回退是 O(n) 全表余弦**：wiki 页数量级小（百级）可接受；若未来上千需改回 pgvector 专用路径。
