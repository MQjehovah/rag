# Notes RAG System v2

企业级中文知识库系统：**笔记管理 + 混合检索 RAG + LLM Wiki 知识蒸馏 + GraphRAG 全局问答**。

用户把碎片知识写进笔记，系统自动完成向量化、BM25 关键词索引和实体图谱抽取；LLM 再把笔记**蒸馏**成一份持续演进的 Wiki 供全团队浏览；当普通检索答不了全局性问题时，基于实体图谱的**社区摘要**自动兜底。多模态（图片检索）已预留架构，默认关闭。

---

## 一、设计理念：分层知识架构

系统按"**事实源 → 派生层**"的单向数据流组织，避免重复建索引、避免派生数据污染原始数据：

```
┌─────────────────────────────────────────────────────────────┐
│  笔记层（唯一可编辑的事实源）                                  │
│  用户写入碎片/临时/正式知识 → 向量(page_chunks) + BM25(page_terms)│
│  + 实体图谱(graph_entities/edges) + 页面相似图(graph_edges)     │
└───────────────┬─────────────────────────────────────────────┘
                │ 笔记保存自动触发
                ▼
┌─────────────────────────────────────────────────────────────┐
│  Wiki 层（LLM 蒸馏产物，只读为主，人工可微调）                   │
│  wiki_pages：按主题组织的 Markdown 页面 + 互链 + 来源笔记引用      │
│  更新页面时"合并式编译"：LLM 看到现有全文（含人工修订）再增量修改    │
└───────────────┬─────────────────────────────────────────────┘
                │ 复用实体图
                ▼
┌─────────────────────────────────────────────────────────────┐
│  GraphRAG 层（全局问答索引）                                   │
│  Louvain 社区发现 → 每社区 LLM 摘要（graph_communities）        │
│  局部检索不足时，自动转全局摘要回答                              │
└─────────────────────────────────────────────────────────────┘
```

**核心原则：**

- 笔记是**唯一可编辑**的层；Wiki、社区摘要都是**派生产物**，随时可整体重建，删了也不影响原始数据
- **实体图谱只从笔记抽取一次**，Wiki / 社区摘要全部复用，不重复抽取
- **向量分层、粒度不同、不重复**：笔记向量是"细粒度证据层"（找原文、引用、兜底）；Wiki 向量是"粗粒度答案层"（高质量、少而精）；社区摘要是"全局综述层"
- **检索分层**：提问 → 先搜 Wiki → 不足搜笔记 → 仍不足转全局社区摘要
- **人工微调优先**：Wiki 页面支持人工编辑，LLM 重新编译时会读取现有全文做合并，**保留人工修订**，不做"人工标记"式的覆盖保护

---

## 二、技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 / FastAPI / SQLAlchemy 2 / httpx |
| 数据库 | PostgreSQL 16 + pgvector（HNSW 索引）；本地开发可回退 SQLite |
| 向量 | bge-large-zh-v1.5（1024 维），公司网关 OpenAI 兼容接口 |
| 重排 | bge-reranker-large |
| LLM | OpenAI 兼容接口（`LLM_BASE_URL`），如 deepseek-v4-flash（推理模型） |
| 检索 | 自研混合管线：向量 + BM25(RRF) + 重排 + 实体/图扩展 + MMR |
| 图谱 | networkx + python-louvain（社区发现） |
| 文档解析 | pymupdf4llm / python-docx / openpyxl / python-pptx + 二进制校验 |
| 对象存储 | MinIO（图片自托管） |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + TipTap + D3 + markdown-it |

---

## 三、数据模型

| 表 | 说明 |
|---|---|
| `notebooks` / `pages` | 笔记本与笔记（事实源） |
| `page_chunks` | 分块 + `embedding`(JSON) + `embedding_vec`(pgvector, HNSW) + `context`(结构上下文) |
| `page_terms` | BM25 词频表（jieba 分词） |
| `graph_edges` | 页面相似图（三信号权重：向量×3 + 关键词×2 + 笔记本×0.5） |
| `graph_entities` / `graph_entity_edges` | 实体级图谱（LightRAG 思路，LLM 抽取） |
| `wiki_pages` | LLM 蒸馏的 Wiki 页面（标题/分类/正文/摘要/来源笔记 ID） |
| `graph_communities` | GraphRAG 社区摘要（成员实体 + 标题 + 摘要 + 摘要向量） |
| `image_assets` | 图片资产（多模态预支持，OCR/描述/向量字段预留） |
| `users` / `user_groups` | 用户与权限组（LDAP / 本地管理员） |

---

## 四、核心流程

### 4.1 保存即索引

笔记创建/更新 → 后台任务（15s 冷却）：

1. 结构化分块：按 Markdown 标题切分，保留"文档 > 章节"上下文链（contextual retrieval），可选 LLM 增强前 10 块
2. 批量向量化（32 条/批）→ 写入 `page_chunks`（先删后插，独立提交，锁窗毫秒级）
3. 关键词 + BM25 词表（`page_terms`）→ 独立提交
4. LLM 实体抽取（`graph_entities`/`graph_entity_edges`）→ 独立提交
5. **Wiki 增量编译**（60s 冷却）：该笔记关联的 Wiki 页面重新蒸馏合并

> SQLite 时代的教训：早期把"Embedding 调用 + 全部写库 + LLM 抽取"放在一个事务里，几分钟的写锁导致全库 `database is locked`。现在每阶段独立提交 + WAL + busy_timeout，生产已迁移 PostgreSQL，从根上解决。

### 4.2 混合检索管线（`app/core/retrieval.py`）

```
查询 → 历史感知改写（多轮对话时）
     → 查询改写（LLM 生成最多 2 个子问题）
     → 多路召回：向量(pgvector HNSW top50) + BM25(page_terms)
     → RRF 融合
     → Reranker 重排（可选）
     → 实体展开（实体图谱命中加分）
     → 相似图展开（邻居加权）
     → MMR 多样性重排（λ=0.7，去重）
     → 结果带 chunk 级引用（chunk_index）
```

可见性过滤全程下沉 SQL（`WHERE page_id IN (可见集合)`），不拉全表。

### 4.3 Agentic 多跳 + 全局回退（`app/api/chat.py`）

1. 有对话历史时先把问题改写成独立检索查询
2. 检索 → LLM 判断 `sufficient` → 不足则生成 followup 查询再搜（默认最多 2 跳）
3. 仍不足 → **全局模式**：问题向量化 → 检索 `graph_communities` 摘要 → 作为"知识库综述"并入上下文
4. 流式回答，`[1][2]` 编号引用来源；来源带引用原文 chunk 与图片

### 4.4 LLM Wiki 编译（`app/core/wiki.py`）

**Karpathy "Distill, don't chunk and vector" 模式**：

- 逐篇深度 ingest（3 并发）：每篇笔记带着"现有页面索引"交给 LLM，决定 create/update 页面
- **合并式更新**：update 操作单独一次 LLM 调用，把"现有页面全文 + 新笔记"合并——人工修订、有效旧内容被保留，只做增量修正
- 页面间用 `[[页面标题]]` 互链；每页记录来源笔记 ID（可溯源）
- 笔记保存自动触发关联页面刷新；管理员可一键全量重编译（增量幂等，可反复跑）
- 前端 `/wiki`：分类侧栏 + Markdown 渲染 + 互链跳转 + 来源笔记回链 + **人工编辑**入口

### 4.5 GraphRAG 社区摘要（`app/core/graphrag.py`）

- Louvain 社区发现（networkx，weight=关系数）
- 每社区 LLM 生成标题 + ≤200 字摘要 → 摘要向量化存入 `graph_communities`
- 全局检索：查询向量对社区摘要做余弦，取 top5 进回答
- 重建接口先补全缺失页面的实体抽取，再建社区（后台任务，进度可查）

### 4.6 图片链路

- 上传图片 → MinIO（`192.168.31.8:9000`）或本地 `/api/upload/images/...`
- 外部图片（如阿里云 OSS）有 Referer 校验：`/api/upload/images/proxy?url=...` 服务端无 Referer 抓取 + 磁盘缓存兜底
- 历史外部图片已批量迁移至 MinIO（笔记正文与分块 URL 同步重写）
- 聊天回答可嵌入文章图片（LLM 上下文附图片 URL + 前端缩略图展示）
- 多模态预支持：`image_assets` 记录全部图片（1100+ 张），`MULTIMODAL_ENABLED=false` 时不做 OCR/向量，开启后填充

---

## 五、配置（`backend/.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | PG 生产 / sqlite 本地 | 生产 `postgresql+psycopg2://postgres:postgres@db:5432/notesrag` |
| `EMBEDDING_API_URL` / `EMBEDDING_MODEL` | — | 向量服务（公司网关） |
| `RERANKER_API_URL` | 空 | 重排服务，空则跳过重排 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | — | 聊天/JSON 调用；也接受 `LLM_API_URL` 全路径 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 300 / 50 | 分块参数 |
| `TOP_K` / `VECTOR_RECALL_K` | 5 / 50 | 检索参数 |
| `HYBRID_BM25_ENABLED` | true | BM25 召回开关 |
| `QUERY_REWRITE_ENABLED` | true | LLM 查询改写 |
| `CONTEXTUAL_RETRIEVAL_ENABLED` | true | 上下文增强分块 |
| `ENTITY_GRAPH_ENABLED` | true | 实体抽取 |
| `AGENTIC_MAX_HOPS` | 2 | 多跳检索上限 |
| `MMR_ENABLED` / `MMR_LAMBDA` | true / 0.7 | MMR 多样性重排 |
| `COMMUNITY_QA_ENABLED` | true | GraphRAG 全局回退 |
| `MULTIMODAL_ENABLED` | false | 多模态（图片 OCR/向量） |
| `JWT_SECRET_KEY` / `LOCAL_ADMIN_PASSWORD` | — | 认证 |

---

## 六、API 概览

- `/api/pages`、`/api/notebooks`：笔记 CRUD + 分页列表（不含正文，详情按需加载）
- `/api/search`：混合检索（结果带 chunk 引用）
- `/api/chat`：流式问答（多轮历史、来源引用、图片）；`/api/chat/import/*` 导入
- `/api/graph/data|stats`：图谱数据（按 view/max_nodes 裁剪）；`rebuild` / `rebuild-entities` / `rebuild-communities` / `rebuild-images` 后台重建
- `/api/wiki`：Wiki 列表/详情/编辑；`/api/wiki/rebuild` 全量编译；`/rebuild-status` 进度
- `/api/upload/images/...`：图片；`/api/upload/images/proxy`：外链图片代理
- `/api/organize`：LLM 自动整理；`/api/dingtalk/*`：钉钉知识库同步

---

## 七、部署（生产）

```bash
# PostgreSQL+pgvector 在 compose "pg" profile 下，必须带 profile：
docker compose --profile pg up -d --build backend frontend
```

- `db` 服务镜像走公司 Docker 代理（`public-docker-virtual.xzrobot.com/pgvector/pgvector:pg16`），宿主端口 **5433**（5432 被占用），容器间 `db:5432`
- `backend/.env` 打进镜像，改配置需重建
- 数据卷：`./backend/data`（SQLite 备份、上传、图片缓存）

## 八、前端页面

| 路由 | 功能 |
|---|---|
| `/` | AI 问答（流式、多轮、引用、图片） |
| `/notes` | 笔记编辑器（异步加载不卡切换、搜索弹窗） |
| `/graph` | 知识图谱 / 实体图谱（后端按重要度裁剪 400 节点，大图隐藏标签） |
| `/wiki` | LLM Wiki（分类浏览、互链、人工编辑、重新编译） |

---

## 九、已知限制与演进方向

- **多模态**：`image_assets` 已就绪，OCR（RapidOCR）与 VLM 描述、CLIP 向量为下一步
- **Wiki 检索接入**：Wiki 页面向量化 + 检索优先命中（当前 Wiki 用于浏览）
- **评估体系**：建议引入 RAGAS 测试集量化检索/回答质量
- **幻觉护栏**：回答逐句校验引用依据
- **语义缓存**：相似问题缓存省 token
- **备份**：PG 建议加每日 `pg_dump` 定时任务
- **前端拆包**：3MB bundle 按路由拆分为宜
