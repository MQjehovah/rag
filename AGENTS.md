# AGENTS.md

## Project Overview

Notes RAG System v2 — Enterprise-grade Chinese-language knowledge base with PostgreSQL+pgvector vector search, reranker, knowledge graph, and enhanced multi-signal search. Two-package monorepo: `backend/` (Python/FastAPI) + `frontend/` (Vue 3/TypeScript).

## Dev Commands

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest                         # run tests
```

### Frontend

```bash
cd frontend
npm install
npm run dev                    # dev server on port 3000 (NOT 5173 — overridden in vite.config.ts)
npm run build                  # vue-tsc typecheck + vite build (this IS the typecheck step)
```

### Docker

```bash
docker-compose up -d           # starts PostgreSQL (pgvector), backend, frontend
```

## Architecture

- **Backend entry**: `app.main:app` — FastAPI app, includes routers from `app.api.{pages,notebooks,search,upload,graph,auth}`
- **Config**: `app/config.py` uses `pydantic_settings.BaseSettings`, loads from `.env`. All env vars are lowercase snake_case in code (e.g. `embedding_api_url`, `reranker_api_url`).
- **Database**: PostgreSQL 16 + pgvector extension via SQLAlchemy. Tables: `notebooks`, `pages`, `page_chunks` (with vector(1024) column + HNSW index), `graph_edges`, `users`, `user_groups`. Connection pool size 20.
- **Vector store**: pgvector HNSW index with cosine distance (`<=>` operator) on `page_chunks.embedding_vec`. Falls back to numpy cosine similarity for SQLite compatibility.
- **Embeddings**: bge-large-zh-v1.5 (1024 dims) via OpenAI-compatible API. Batch encoding supported. Chunking via langchain `RecursiveCharacterTextSplitter`.
- **Reranker**: bge-reranker-v2-m3 via independent API endpoint. Called after vector+keyword recall for result refinement.
- **Search pipeline** (`app/api/search.py`): Vector recall (pgvector top 50) → Keyword match (pre-computed keywords column) → Reranker → Graph expansion → Return top_k.
- **RAG pipeline** (`app/core/rag.py`): `EmbeddingService` (chunk + encode), `VectorStore` (pgvector operations), `RerankerService`. Keywords pre-computed on save and stored in `pages.keywords`.
- **Knowledge graph** (`app/core/graph.py`): `GraphBuilder` with three-signal weight model (vector×3.0 + keyword×2.0 + notebook×0.5). Uses jieba for Chinese keyword extraction. Embeddings read from DB (no re-encoding on rebuild).
- **Frontend entry**: `src/main.ts` — Vue 3 + Pinia + Element Plus + Vue Router. Two routes: `/` (Editor), `/graph` (KnowledgeGraph). Routes defined inline in `main.ts` — `router/index.ts` is dead code.
- **Pagination**: `GET /api/pages` returns paginated list (no content). `GET /api/pages/{id}` returns full page content on demand.

## Key Gotchas

- **Vite dev port is 3000**, not the Vite default 5173. Vite proxies `/api` → `http://localhost:8000`.
- **`.env` required in `backend/`** before running. Copy from `.env.example`. Old `.env` files with `CHROMADB_PATH` will cause pydantic validation errors — remove it.
- **Local dev can use SQLite** (default `DATABASE_URL=sqlite:///./data/notes.db`) — pgvector features will fall back to numpy similarity. For production, use PostgreSQL+pgvector.
- **Docker Compose** uses `pgvector/pgvector:pg16` image for PostgreSQL. Backend waits for DB healthcheck before starting.
- **Frontend `npm run build`** runs `vue-tsc` first — type errors will block the build. There is no separate `typecheck` or `lint` script.
- **`noUnusedLocals` and `noUnusedParameters`** are enabled in `tsconfig.json` — unused imports/params will fail the build.
- **Reranker is optional** — if `RERANKER_API_URL` is empty, search skips reranking and uses raw scores.
- **`frontend/src/router/index.ts`** is dead code (never imported) — do not use it.
