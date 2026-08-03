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
# 构建并启动所有服务
docker-compose up -d --build

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

服务地址：

- 前端：http://localhost
- 后端 API：http://localhost:8000

## 配置说明

### 环境变量

| 配置组 | 主要变量 | 说明 |
| ------ | -------- | ---- |
| LLM | `OLLAMA_HOST`、`OLLAMA_MODEL`、`LLM_API_URL`、`LLM_API_KEY`、`LLM_MODEL` | 本地 Ollama 或兼容 API |
| Embedding | `EMBEDDING_API_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSIONS` | 默认使用本地 Ollama `bge-m3` |
| 检索 | `CHUNK_SIZE`、`CHUNK_OVERLAP`、`TOP_K`、`VECTOR_RECALL_K` | 分块和召回参数 |
| 数据库 | `DATABASE_URL` | 本地默认 SQLite，生产建议 PostgreSQL + pgvector |
| 钉钉 | `DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET`、`DINGTALK_KNOWLEDGE_BASE_ID`、`DINGTALK_OPERATOR_ID` | 钉钉知识库同步 |
| 鉴权 | `JWT_SECRET_KEY`、`LOCAL_ADMIN_PASSWORD`、`LDAP_*` | 本地或 LDAP 登录 |
| 对象存储 | `MINIO_*` | 可选的 MinIO 文件存储 |
| 自动整理 | `AUTO_ORGANIZE_ENABLED`、`AUTO_ORGANIZE_INTERVAL_HOURS` | 可选的后台图谱整理 |

完整变量以 `backend/.env.example` 为准。真实值只写入 `backend/.env`，该文件已被 Git 忽略。

### 钉钉知识库同步

1. 在 `backend/.env` 中配置钉钉应用、操作人和知识库 ID。
2. 启动后端和前端，在编辑器中打开“钉钉同步”。
3. 获取文档列表并选择需要同步的文件。
4. 系统会将内容统一整理为 Markdown，并按钉钉节点 ID 增量写入 RAG。

相关接口：

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/dingtalk/spaces` | 获取知识库空间 |
| GET | `/api/dingtalk/docs` | 获取文档列表 |
| POST | `/api/dingtalk/sync-selected` | 同步选中文档 |
| POST | `/api/dingtalk/sync` | 同步整个知识库 |
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
