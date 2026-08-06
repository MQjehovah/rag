# Notes RAG System

智能笔记系统，支持富文本编辑、自动向量检索（RAG）、知识图谱和增强搜索。

## 功能特性

- **富文本编辑器**：基于 TipTap，支持：

  - 标题、粗体、斜体、删除线
  - 有序/无序列表、引用
  - 代码块（支持语法高亮，可切换语言）
  - 表格、图片上传
  - Mermaid 流程图
  - 撤销/重做
- **自动索引**：笔记创建/更新时自动后台构建向量索引
- **增强搜索**：四阶段检索管线

  - 向量语义搜索（ChromaDB）
  - 关键词匹配（jieba 中文分词）
  - 图谱扩展（基于知识图谱的关联发现）
  - 多信号融合排序
- **知识图谱**：D3.js 力导向图可视化

  - 三信号关联模型（向量相似度 × 3.0 + 关键词重叠 × 2.0 + 笔记本邻近 × 0.5）
  - 节点按笔记本着色，大小按连接数缩放
  - 悬停高亮邻居节点和关联边
  - 缩放、拖拽、过滤、一键重建
  - 图谱统计（节点数、边数、平均连接、聚类数）
- **笔记本管理**：支持多笔记本分类

## 技术栈

### 后端

- **FastAPI** - Python Web 框架
- **SQLAlchemy** - ORM
- **ChromaDB** - 向量数据库
- **BGE** - 嵌入模型（通过 OpenAI 兼容 API）
- **jieba** - 中文关键词提取
- **MinIO** - 对象存储（图片）

### 前端

- **Vue 3** + **TypeScript**
- **Vite** - 构建工具
- **TipTap** - 富文本编辑器
- **Element Plus** - UI 组件库
- **D3.js** - 知识图谱可视化
- **Pinia** - 状态管理
- **Mermaid** - 流程图渲染

## 快速开始

### 前置要求

- Python 3.10+
- Node.js 18+
- Ollama（用于嵌入模型）
- MinIO（可选，用于图片存储）

### 1. 安装 Ollama 并下载模型

```bash
# 安装 Ollama
# macOS/Linux: curl -fsSL https://ollama.ai/install.sh | sh
# Windows: 访问 https://ollama.ai 下载

# 下载嵌入模型
ollama pull modelscope.cn/Embedding-GGUF/bge-large-zh-v1.5:latest
```

### 2. 配置环境变量

```bash
cd backend
cp .env.example .env
# Windows PowerShell: Copy-Item .env.example .env
# 只在 .env 中填写真实密钥；.env.example 不保存敏感信息
```

### 3. 启动后端

```bash
cd backend

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 本地启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

后端启动后访问 http://localhost:8000/docs 查看 API 文档。

### 4. 启动前端

```bash
cd frontend

# 安装依赖
npm install

# 开发模式
npm run dev

# 生产构建
npm run build
```

前端开发服务器：http://localhost:3000

## Docker 部署

```bash
# 使用backend/.env中的现有数据库配置启动前后端
docker compose up -d --build

# 需要同时启动项目内置PostgreSQL + pgvector时
docker compose --profile pg up -d --build

# 查看日志
docker compose logs -f

# 停止服务
docker compose down
```

Docker只从`backend/.env`读取运行配置，Compose不会再用默认值覆盖数据库、Embedding、LDAP、
JWT或管理员配置。使用内置PostgreSQL前，必须在`backend/.env`填写`POSTGRES_DB`、
`POSTGRES_USER`和强密码`POSTGRES_PASSWORD`，并保证`DATABASE_URL`与其一致。

`backend/data`挂载到容器`/app/data`，数据库文件、钉钉原文件、Markdown、manifest和PDF图片
会在容器重建后继续保留。`.env`和`backend/data`均被排除在Docker构建上下文及Git提交之外。

服务地址：

- 前端：http://localhost:8092
- 后端 API：http://localhost:8000

## 配置说明

### 环境变量

| 配置组 | 主要变量 | 说明 |
| ------ | -------- | ---- |
| LLM | `OLLAMA_HOST`、`OLLAMA_MODEL`、`LLM_API_URL`、`LLM_API_KEY`、`LLM_MODEL` | 本地 Ollama 或兼容 API |
| Embedding | `EMBEDDING_API_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSIONS` | 默认使用本地 Ollama `bge-m3` |
| 检索 | `CHUNK_SIZE`、`CHUNK_OVERLAP`、`TOP_K`、`VECTOR_RECALL_K` | 分块和召回参数 |
| PDF OCR | `PDF_OCR_ENABLED`、`PDF_OCR_DPI`、`PDF_OCR_MIN_CONFIDENCE`、`PDF_OCR_MAX_PAGES` | 补充截图和扫描页文字 |
| 数据库 | `DATABASE_URL` | 本地默认 SQLite，生产建议 PostgreSQL + pgvector |
| 钉钉 | `DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET`、`DINGTALK_KNOWLEDGE_BASE_ID`、`DINGTALK_OPERATOR_ID` | 钉钉知识库同步 |
| Markdown转换 | `MARKITDOWN_ENABLED`、`PDF_HYBRID_ENABLED`、`MARKITDOWN_PDF_FALLBACK_ENABLED` | Office优先使用MarkItDown，PDF融合MarkItDown与增强解析 |
| 鉴权 | `JWT_SECRET_KEY`、`LOCAL_ADMIN_PASSWORD`、`LDAP_*` | 本地或 LDAP 登录 |
| 对象存储 | `MINIO_*` | 可选的 MinIO 文件存储 |
| 自动整理 | `AUTO_ORGANIZE_ENABLED`、`AUTO_ORGANIZE_INTERVAL_HOURS` | 可选的后台图谱整理 |

完整变量以 `backend/.env.example` 为准。真实值只写入 `backend/.env`，该文件已被 Git 忽略。

### 钉钉知识库同步

1. 在 `backend/.env` 中配置钉钉应用、操作人和知识库 ID。
2. 启动后端和前端，在编辑器中打开“钉钉同步”。
3. 选择目标钉钉知识库，可以获取文档列表后同步选中文档，也可以直接执行整个知识库全量同步。
4. 前端会依次显示清单扫描、原文件下载、Markdown转换和RAG入库进度，并在完成后列出写入、未变化、失败和分块数量。
5. 系统会将内容统一整理为 Markdown，并按钉钉节点 ID 增量写入 RAG；重复点击或多人同时操作时，后端只允许一个同步任务运行。

Word、Excel和PowerPoint默认优先通过MarkItDown转换，失败时自动回退到项目原有解析器；
TXT、CSV和Markdown继续使用轻量内置转换。每份结果会在Markdown前置元数据和manifest中记录
实际使用的转换器、是否发生回退以及回退原因，便于排查格式差异。

PDF默认逐页使用MarkItDown提取正文，再按原页码融合现有表格、OCR、重要图片保存和视觉说明。
单页MarkItDown失败时只回退该页，不影响其他页面；整体增强解析失败时才使用整份MarkItDown
结果兜底。OCR结果会按置信度过滤，并与MarkItDown正文做相似度去重。系统还会记录PDF页数、
图片数、MarkItDown成功页数、OCR页数、视觉分析页数、原始文件SHA-256、文件大小、MIME类型
和钉钉原文链接。打开已同步页面后，可以使用“查看钉钉原文”按钮返回源文件。

如果下载内容的文件头包含`E-SafeNet`和`LOCK`，系统会将其标记为“源文件已加密”，并保留
原文件和明确错误原因。这类文件不是MarkItDown转换问题，必须先由有权限的企业安全终端
导出解密后的PDF或Office原文件，再重新执行同步。

生产环境采用服务器内部同步：将本分支部署到现有Notes RAG服务器后，由服务器完成钉钉下载、
Markdown转换、Embedding和数据库写入，不需要在个人电脑和服务器之间增加远程上传客户端。

本地全量流程会把下载、转换和RAG入库状态完整记录到
`backend/data/dingtalk/manifest.json`。可以运行以下命令查看汇总或执行文件完整性审计：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\report_dingtalk_manifest.py
.\.venv\Scripts\python.exe scripts\report_dingtalk_manifest.py --audit

# 核对manifest、本地文件、RAG页面、哈希、索引和分块是否一致
.\.venv\Scripts\python.exe scripts\verify_dingtalk_acceptance.py

# 选择一份体积较小的真实文档，验收下载、转换和RAG幂等链路
.\.venv\Scripts\python.exe scripts\verify_dingtalk_live.py

# 全量扫描并核对新增、移动、恢复和源端删除（不下载）
.\.venv\Scripts\python.exe scripts\sync_dingtalk_raw.py --list-only

# 先预览待清理数量；确认后显式清理已软删除文档的RAG页面
.\.venv\Scripts\python.exe scripts\import_dingtalk_rag.py --dry-run --prune-deleted
.\.venv\Scripts\python.exe scripts\import_dingtalk_rag.py --prune-deleted

# 只读审计老师服务器中受跟踪页面的内容哈希、二进制污染和来源元数据
.\.venv\Scripts\python.exe scripts\import_dingtalk_remote_rag.py --audit-integrity

# 正式恢复：先审计并生成快照，只修复审计异常项，完成后再次回读校验
# 必须先在老师服务器部署本分支的来源导入接口
.\.venv\Scripts\python.exe scripts\import_dingtalk_remote_rag.py --repair-integrity
```

远程同步文档通过管理员专用的`POST /api/pages/source-import`写入。该接口会验证发布内容哈希，
拒绝PDF、ZIP/Office等原始二进制文本，并保存钉钉文档ID、原文路径、原文链接、文件哈希、
Markdown哈希和转换管线版本。远端服务器缺少该接口时，客户端不会降级使用普通笔记接口，
避免页面失去来源标记和只读保护。

增量同步以钉钉文档ID作为稳定身份：重命名或移动不会产生重复RAG页面，原文件内容哈希变化
时才重新转换和入库。只有一次无递归错误且知识库范围明确的全量扫描，才能把缺失文档标为
`source_status=deleted`；此时本地原文件仍保留，RAG页面也必须通过上述显式命令清理。

同步任务会把检查点原子保存到`backend/data/dingtalk/sync-task.json`。如果后端在同步过程中
退出，重启后任务会显示为“已中断”，前端可点击“重试失败/未完成项”。重试会按manifest阶段
状态断点执行：下载失败重新访问钉钉，转换失败复用本地原文件，RAG失败复用本地Markdown。

相关接口：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/dingtalk/spaces` | 获取知识库空间 |
| GET | `/api/dingtalk/docs` | 获取文档列表 |
| POST | `/api/dingtalk/sync-selected` | 同步选中文档 |
| POST | `/api/dingtalk/sync` | 同步整个知识库 |
| POST | `/api/dingtalk/retry` | 按最近任务检查点重试失败或未完成阶段 |
| GET | `/api/dingtalk/status` | 查询同步状态 |

## API 接口

### 笔记本

| 方法   | 路径                    | 说明           |
| ------ | ----------------------- | -------------- |
| GET    | `/api/notebooks`      | 获取笔记本列表 |
| POST   | `/api/notebooks`      | 创建笔记本     |
| PUT    | `/api/notebooks/{id}` | 更新笔记本     |
| DELETE | `/api/notebooks/{id}` | 删除笔记本     |

### 笔记

| 方法   | 路径                      | 说明                           |
| ------ | ------------------------- | ------------------------------ |
| GET    | `/api/pages`            | 获取笔记列表（可按笔记本筛选） |
| POST   | `/api/pages`            | 创建笔记                       |
| POST   | `/api/pages/source-import` | 管理员幂等导入钉钉来源文档   |
| GET    | `/api/pages/{id}`       | 获取笔记详情                   |
| PUT    | `/api/pages/{id}`       | 更新笔记                       |
| DELETE | `/api/pages/{id}`       | 删除笔记                       |
| POST   | `/api/pages/{id}/index` | 手动触发 RAG 索引              |

### 搜索

| 方法 | 路径            | 说明                         |
| ---- | --------------- | ---------------------------- |
| POST | `/api/search` | 增强搜索（向量+关键词+图谱） |

### 知识图谱

| 方法 | 路径                   | 说明             |
| ---- | ---------------------- | ---------------- |
| GET  | `/api/graph/data`    | 获取图谱节点和边 |
| GET  | `/api/graph/stats`   | 获取图谱统计信息 |
| POST | `/api/graph/rebuild` | 重建知识图谱     |

请求示例：

```json
{
  "query": "如何使用 Python",
  "top_k": 5
}
```

响应示例：

```json
{
  "results": [
    {
      "id": "xxx",
      "title": "Python 基础",
      "content": "...",
      "score": 2.8456,
      "source": "keyword+vector"
    }
  ],
  "total": 5,
  "graph_expanded": 2
}
```

### 上传

| 方法 | 路径                  | 说明     |
| ---- | --------------------- | -------- |
| POST | `/api/upload/image` | 上传图片 |

## 项目结构

```
.
├── backend/
│   ├── app/
│   │   ├── api/           # API 路由
│   │   │   ├── notebooks.py
│   │   │   ├── pages.py
│   │   │   ├── search.py
│   │   │   └── upload.py
│   │   ├── core/          # 核心功能
│   │   │   └── rag.py     # RAG 服务
│   │   ├── models/        # 数据模型
│   │   ├── config.py      # 配置
│   │   └── main.py        # 入口
│   ├── tests/             # 测试
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/           # API 调用
│   │   ├── components/    # Vue 组件
│   │   │   ├── TipTapEditor.vue
│   │   │   └── CodeBlockComponent.vue
│   │   ├── views/         # 页面视图
│   │   ├── router/        # 路由配置
│   │   ├── stores/        # Pinia 状态
│   │   └── main.ts
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

## 开发指南

### 后端开发

```bash
cd backend

# 运行测试
pytest

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 前端开发

```bash
cd frontend

# 类型检查
npm run build

# 开发服务器
npm run dev
```

## 常见问题

### Q: Ollama 连接失败

确保 Ollama 服务正在运行：

```bash
ollama serve
```

Docker 环境中，使用 `host.docker.internal` 访问宿主机的 Ollama。

### Q: 向量搜索无结果

1. 确保已创建笔记
2. 检查笔记是否已索引（创建/更新时自动索引）
3. 确认 Ollama 模型可用

### Q: 图片上传失败

1. 确保 MinIO 服务运行中
2. 检查 MinIO 配置是否正确
3. 确认存储桶已创建（首次会自动创建）

## License

MIT
