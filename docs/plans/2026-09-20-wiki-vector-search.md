# rag Wiki 向量检索 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 Wiki 页面可被语义检索，并作为一路召回源进入聊天回答，同时提供独立搜索接口与回填端点。

**Architecture:** 照 `page_chunks` 的双写先例给 `wiki_pages` 加 JSON `embedding`（始终写）+ Postgres `embedding_vec vector(1024)` + HNSW（裸 DDL 幂等）；嵌入发生在 `_persist` 之后**不持锁**的位置；新增 `core/wiki_search.py` 提供带可见性的向量检索，`RetrievalPipeline` 增加 wiki 召回（id 加 `wiki:` 前缀）。

**Tech Stack:** Python 3.10 / FastAPI / SQLAlchemy / PostgreSQL+pgvector（生产）/ SQLite（测试）/ numpy（回退）；前端 Vue 3 + TS。

**设计依据：** `rag/docs/plans/2026-09-20-wiki-vector-search-design.md`

解释器：`py -3.12`（`python` 是 3.14 无 pytest）。样式：Python 4 空格、中文注释、英文标识符；前端 2 空格、无分号、单引号。

---

## Task 1: 存储与迁移

**Files:** Modify `backend/app/models/database.py`; Test `backend/tests/core/test_wiki_embedding_schema.py`

**Step 1:** `WikiPage` 增加 `embedding = Column(Text, nullable=True)`（放在 `summary` 之后）。

**Step 2:** 新增幂等索引/列助手（仿 `_ensure_wiki_group_index`，`database.py:250-259`），并在 `init_db`（`:262-265`）里于 `_ensure_wiki_group_index(engine)` 之后调用：

```python
def _ensure_wiki_embedding_column(engine):
    """Postgres 上为 wiki_pages 补 embedding_vec 向量列与 HNSW 索引(裸 DDL,失败不阻断)。"""
    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(sqlalchemy_text(
                    "ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS embedding_vec vector(1024)"
                ))
                conn.execute(sqlalchemy_text(
                    "CREATE INDEX IF NOT EXISTS ix_wiki_pages_embedding_hnsw "
                    "ON wiki_pages USING hnsw (embedding_vec vector_cosine_ops)"
                ))
                # JSON 文本 → 向量 的一次性回填(与 page_chunks 同法)
                conn.execute(sqlalchemy_text(
                    "UPDATE wiki_pages SET embedding_vec = embedding::vector "
                    "WHERE embedding IS NOT NULL AND embedding_vec IS NULL"
                ))
    except Exception:
        logger.warning("初始化 wiki_pages 向量列失败", exc_info=True)
```

注意 `init_db` 里现有的 pgvector 块要求 `CREATE EXTENSION vector` 在先（`database.py:277` 附近）——本助手在其后调用即可；SQLite 下整段是 no-op。

**Step 3:** 测试 `test_wiki_embedding_schema.py`：
- 新建 SQLite 库 → `init_db` 后 `wiki_pages` 有 `embedding` 列（`PRAGMA table_info`），且**没有** `embedding_vec`（SQLite 不加）。
- 模拟旧表（先建无 `embedding` 列的 `wiki_pages`，插一行）→ `init_db` 后列被补上且数据保留、`init_db` 可重复执行。

**Step 4:** `py -3.12 -m pytest tests/core/test_wiki_embedding_schema.py -q`；再跑全量 `py -3.12 -m pytest -q`（当前 109 passed）确认无回归。

**Step 5:** 提交 `feat(rag): wiki_pages 增加向量列与幂等迁移`

---

## Task 2: 嵌入工具与写入时机

**Files:** Create `backend/app/core/wiki_embedding.py`; Modify `backend/app/core/wiki.py`, `backend/app/api/wiki.py`; Test `backend/tests/core/test_wiki_embedding.py`

**Step 1（TDD）:** 先写 `test_wiki_embedding.py`，针对新模块的**可注入**接口（把 EmbeddingService 作为参数传入，便于测试时 mock）：

```python
async def embed_wiki_pages(engine, page_ids=None, embedding_svc=None) -> dict: ...
# 返回 {"embedded": int, "errors": int, "total": int}
```
测试用例：
- 给定 2 个 wiki 页（`embedding` 为空），传入一个返回固定 1024 维向量的假 service → 两页的 `embedding` 被写入 JSON，`embedded == 2`。
- 再次调用（`page_ids=None`，全部已有向量）→ 不重复嵌入（`embedded == 0`，可用假 service 的调用计数断言）。
- `page_ids` 传具体 id 时只处理这些。
- 假 service 抛异常 → 记入 `errors`，不中断其他页面。
- 文本为 `title + "\n" + summary`：断言假 service 收到的文本符合该形状。

**Step 2:** 实现 `core/wiki_embedding.py`：

```python
async def embed_wiki_pages(engine, page_ids=None, embedding_svc=None, batch_size: int = 32) -> dict:
    """为 wiki_pages 计算/刷新向量。page_ids 为空表示"补齐所有缺失向量的页面"。

    嵌入失败不抛异常(记入 errors),因为 wiki 编译是后台流程,不应因嵌入失败整体失败。
    """
```
- 内部用 `get_session(engine)` 查 `WikiPage`（`embedding is None` 或指定 id），构造 `title + "\n" + summary`，调用 `embedding_svc.encode_batch(texts)`，逐页 `json.dumps(emb)` 写回并 commit。
- `embedding_svc` 缺省时 `EmbeddingService()`；用完 `await svc.close()` 仅在自建时关闭（避免关掉调用方的）。
- 去重传入的 id。

**Step 3:** 接进 wiki 编译：
- `_persist`（`wiki.py:141-163`）改为返回本次写入的 page id 列表（`List[str]`），不改变其写入行为。
- `_ingest_one`（`wiki.py:244-250`）：在 **`async with lock` 块之外**，拿到 `changed_ids` 后 `await embed_wiki_pages(engine, changed_ids)`。注意 `refresh_note_wiki` 路径没有锁，同样处理。
- `build_wiki` / `refresh_stale_wiki` 末尾各加一次 `await embed_wiki_pages(engine)`（补齐本轮因并发/失败遗漏的）。

**Step 4:** `api/wiki.py` 的 `update_wiki_page`（`:109-128`）在 `db.commit()` 后 `await embed_wiki_pages(engine, [page.id])`（用 `db.get_bind()` 取 engine）。注意该端点原来不是 `async`——如需要改为 `async def`（FastAPI 支持），并确认没有同步调用方依赖其同步性。

**Step 5:** 验证 `py -3.12 -m pytest tests/core/test_wiki_embedding.py -q` 与全量（109 → 应有新增通过，无失败）。

**Step 6:** 提交 `feat(rag): wiki 页面嵌入与随写刷新`

---

## Task 3: wiki 向量检索模块

**Files:** Create `backend/app/core/wiki_search.py`; Test `backend/tests/core/test_wiki_search.py`

**Step 1（TDD）:** 测试要点：
- 造 3 个 wiki 页（公共 / 研发部 / 财务部），各自写入固定 `embedding` JSON，向量按构造使查询向量最接近目标页。
- 普通用户（`groups=['研发部']`）搜索只返回公共 + 研发部，不返回财务部；管理员返回全部。
- 结果按距离升序（得分降序）；`top_k` 生效。
- 无向量页面不参与（`embedding IS NULL` 跳过）。
- 查询向量为零/空时返回空列表而非报错。

**Step 2:** 实现 `core/wiki_search.py`：

```python
def search_wiki(db, query_embedding, limit=5, current_user=None) -> list[dict]:
    """语义检索可见 wiki 页面。Postgres 走 pgvector,其他方言回退 numpy 余弦。

    返回 [{id, title, summary, content, category, score, distance}]
    """
```
- 可见性：`__local_admin__` 不过滤；否则 `group_id IS NULL OR group_id IN (groups)`（用 `visible_wiki_filter`，`search_common.py:10-19`；它是 ORM 条件，ORM 路径直接用；裸 SQL 路径用参数化 `IN` 重建同一条件——**两种写法必须由等价性测试锁定**，仿 `test_wiki_visibility.py:57-66`）。
- Postgres 分支：`SELECT ... FROM wiki_pages WHERE (<visibility>) ORDER BY embedding_vec <=> CAST(:q AS vector) LIMIT :n`；查询向量按 `rag.py:347` 的 `"[" + ",".join(...) + "]"` 形式构造。异常时 `logger.warning` 并回退 numpy（与 `rag.py:351-378` 同风格）。
- 回退分支：查 `embedding is not None` 的可见页面，numpy 余弦（`score = 1 - distance`）。

**Step 3:** 验证并提交 `feat(rag): 新增 wiki 向量检索模块`

---

## Task 4: 合并进检索管线与聊天

**Files:** Modify `backend/app/core/retrieval.py`, `backend/app/api/chat.py`; Test `backend/tests/core/test_retrieval_wiki.py`

**Step 1（TDD）:** 测试要点（用假 pipeline/假 embedding 与内存 SQLite，避开 pgvector）：
- `wiki:` 前缀：wiki 命中项的 id 形如 `wiki:<uuid>`，`sources` 含 `"wiki"`。
- wiki 结果与页面结果**不互相覆盖**（同名/同 id 不冲突）；`title`/`content` 非空（证明有独立的 `wiki_map`，没有误用 `page_map`）。
- 可见性：他组 wiki 不出现在结果里。

**Step 2:** 改 `core/retrieval.py`：
- 在现有 vector/BM25 召回之后（`:188-218` 附近）加入 wiki 召回：用 `queries` 的第一个查询向量调用 `search_wiki(self.db, emb, recall_k, current_user)`，命中的 id 记为 `f"wiki:{w['id']}"`，与页面候选一起参与 `_rrf`。
- 新增 `wiki_map = {f"wiki:{w['id']}": w for w in wiki_hits}`；结果构造（`:366-401`）中对 `pid.startswith("wiki:")` 的条目改从 `wiki_map` 取 `title`/`content`/`chunks`（chunks 用 `[{"content": summary 或 content[:300]}]`），并确保 `page_map` 查询**不会**因为 wiki id 报错（现有是 `.get(pid) or {}`，安全）。
- `sources_map` 对 wiki id 记 `"wiki"`。

**Step 3:** 改 `api/chat.py`：`_agentic_search_notes`（`:151-220`）里对 `note["id"].startswith("wiki:")` 的条目设置 `source_kind = "wiki"`（与 community 的分支并列）；确认 `sources` 载荷（`:251-271`）无需改动即可输出（`id/title/chunks/images` 形状已容纳）。

**Step 4:** 验证并提交 `feat(rag): 检索管线与聊天接入 wiki 来源`

---

## Task 5: 接口与前端

**Files:** Modify `backend/app/api/wiki.py`, `frontend/src/views/Chat.vue`, `frontend/src/views/Wiki.vue`

**Step 1:** `api/wiki.py` 新增：

```python
class WikiSearchRequest(BaseModel):
    query: str
    top_k: int = 5

@router.post("/search")
async def search_wiki_endpoint(data: WikiSearchRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    """语义检索可见 wiki 页。"""
```
- 用 `EmbeddingService().encode(query)`（用完 close）取查询向量，调 `search_wiki`，返回 `{results: [{id,title,summary,category,score}], total}`；空查询返回 400。
- 注意路由顺序：`/search` 必须在 `/{page_id}` **之前**定义，否则会被后者吞掉（`/{page_id}` 是单段，`/search` 也是单段 → 必须先定义）。

**Step 2:** `api/wiki.py` 新增管理员回填端点 `POST /reindex-embeddings` → 调 `embed_wiki_pages(db.get_bind())` 返回统计；非管理员 403。

**Step 3:** `Chat.vue` 的 `openSource`（`:310-325`）：在既有 `community:` 跳过分支旁，新增 `wiki:` 分支 → `router.push({ path: '/wiki/' + src.id.slice(5) })`（先确认是否有 `/wiki/:id` 路由；若 Wiki 页是单页 + 内部选中，则改为跳 `/wiki?page=<id>` 并让 `Wiki.vue` 读取 query 选中）。**实现前先读 `frontend/src/main.ts` 的路由定义与 `Wiki.vue` 的选中机制，按真实结构实现并在报告中说明。**

**Step 4:** `Wiki.vue`：保留即时标题过滤；新增语义搜索（输入框回车或「语义搜索」按钮）调用 `POST /api/wiki/search`，结果以列表展示，点击打开对应页面（用 Step 3 同一套选中逻辑）。

**Step 5:** 验证 `cd frontend; npm run build`（含 `vue-tsc`）。

**Step 6:** 提交 `feat(rag): wiki 搜索接口与前端接入`

---

## Task 6: 验收

**Step 1:** 全量验证：
- `py -3.12 -m pytest -q`（当前 109 passed / 0 errors，应只增不减）
- `cd frontend; npm run build`
- `py -3.12 -m ruff check` 改动文件（既有 34 个问题在未改动文件，不得新增）

**Step 2:** 逐条核对设计文档《验收标准》1–6，结果写入 `docs/plans/2026-09-20-wiki-vector-search-design.md` 的「实施结果」小节。无法在本机验证的（如真实 pgvector 路径）如实标注为「仅推理/未执行」。

**Step 3:** 提交 `docs(rag): wiki 向量检索实施结果`

---

## 已知限制（本期不做）

- 一页一向量（嵌 `title + summary`），长页细节可能召回不到。
- 不做嵌入模型切换保护；`vector(1024)` 与现有 bge-large-zh 绑定。
- 不做 wiki 分块；wiki 不参与 rerank 与 MMR：它没有 `page_chunks` 向量，MMR 拿不到相似度。wiki 只按 RRF 融合分参与最终排序——MMR 开启时先从笔记候选里为可见 wiki 预留名额（按融合分降序），剩余名额交给 MMR，最终顺序为 MMR 选出的笔记在前、wiki 在后；MMR 关闭时与笔记一起按融合分整体排序。无可见笔记时仍照常召回 wiki（不依赖笔记可见集）。`_fetch_embeddings` 只覆盖 `page_chunks`，故 `_mmr_rank` 的候选需显式剔除 `wiki:` 前缀。
- SQLite 回退为 O(n) 全表余弦，页数上千需改回 pgvector 专用路径。
