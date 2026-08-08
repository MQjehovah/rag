# AGENTS.md

## Project Overview

Notes RAG System v2 — enterprise Chinese-language knowledge base with:

- **Notes layer**: user-edited Markdown notes (the source of truth), auto-indexed into vectors + BM25 + entity graph on save.
- **LLM Wiki layer**: Karpathy-style "distill, don't chunk" — an LLM compiles notes into read-only wiki pages that are browsable and human-editable; note saves trigger incremental wiki re-compilation.
- **GraphRAG layer**: community summaries over the entity graph for whole-knowledge-base ("global") Q&A, routed automatically when local retrieval is judged insufficient.
- **Multimodal pre-support** (`MULTIMODAL_ENABLED=false`): image assets are scanned into `image_assets`; OCR/caption/embedding hooks are stubbed and off by default.

Two-package monorepo: `backend/` (Python/FastAPI, PostgreSQL 16 + pgvector) + `frontend/` (Vue 3/TypeScript).

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

### Docker (production)

```bash
# PostgreSQL+pgvector is behind the "pg" compose profile; always include it:
docker compose --profile pg up -d --build backend frontend
```

## Architecture

- **Backend entry**: `app.main:app` — FastAPI app, routers from `app.api.{pages,notebooks,search,upload,graph,auth,dingtalk,chat,organize,wiki}`.
- **Config**: `app/config.py` (`pydantic_settings.BaseSettings`, loads `.env`). Env vars are lowercase snake_case in code. LLM accepts either `LLM_API_URL` (full chat completions endpoint) or `LLM_BASE_URL` (OpenAI-style base; `/chat/completions` appended automatically).
- **Database**: PostgreSQL 16 + pgvector via SQLAlchemy. Tables: `notebooks`, `pages`, `page_chunks` (vector(1024) + HNSW), `page_terms` (BM25), `graph_edges`, `graph_entities`, `graph_entity_edges`, `users`, `user_groups`, `wiki_pages`, `graph_communities`, `image_assets`.
- **Vector store** (`app/core/rag.py`): pgvector HNSW cosine (`<=>` with `CAST(:q AS vector)`); numpy fallback for SQLite dev.
- **Hybrid retrieval** (`app/core/retrieval.py`): query rewrite → vector + BM25 multi-path recall → RRF fusion → rerank → entity/graph expansion → **MMR diversity re-rank** (`MMR_ENABLED`/`MMR_LAMBDA`).
- **Agentic chat** (`app/api/chat.py`): history-aware query rewrite, multi-hop sufficiency judging, and a **GraphRAG global fallback** that searches `graph_communities` when local results are insufficient.
- **LLM Wiki** (`app/core/wiki.py`): per-note incremental ingest (concurrent, merge-aware so human edits survive), `wiki_pages` table, admin rebuild endpoint. Note saves auto-refresh linked wiki pages (60s throttle) via `pages.py`.
- **GraphRAG** (`app/core/graphrag.py`): Louvain community detection over `graph_entity_edges`, LLM community summaries with embeddings, `search_communities()` for global Q&A.
- **Frontend**: routes `/` (Chat), `/notes` (Editor), `/graph` (KnowledgeGraph), `/wiki` (Wiki). Routes defined inline in `main.ts` — `router/index.ts` is dead code.

## Key Gotchas

- **Production runs PostgreSQL**: `docker compose --profile pg ...`. The `db` service maps host port `5433` (5432 on the host is used by another service); containers talk to `db:5432` internally. Old SQLite fallback still works for local dev.
- **LLM endpoints**: embeddings/rerank go through the company gateway (`EMBEDDING_API_URL`/`RERANKER_API_URL`); chat/JSON via `LLM_BASE_URL` (e.g. `https://ai.rosiwit.com/v1`) + `LLM_MODEL` (a reasoning model like `deepseek-v4-flash`). Reasoning models need generous timeouts.
- **Image proxy**: `/api/upload/images/proxy?url=...` fetches external images without a Referer header (bypasses OSS referer checks) and caches to `data/image_cache`. External image URLs in chat sources are normalized to this proxy unless on the safe-host list.
- **Wiki rebuild is incremental and idempotent**: re-running `POST /api/wiki/rebuild` ingests all notes against existing pages; update ops get a merge pass so human edits are preserved. `POST /api/graph/rebuild-communities` first completes entity extraction, then rebuilds community summaries.
- **Vite dev port is 3000**, proxies `/api` → `http://localhost:8000`.
- **`.env` required in `backend/`**. Old `.env` with `CHROMADB_PATH` causes pydantic validation errors — remove it. Production `.env` is baked into the image; changing it requires a rebuild.
- **Frontend `npm run build`** runs `vue-tsc` first — type errors block the build. `noUnusedLocals`/`noUnusedParameters` are on.
- **Reranker is optional** — empty `RERANKER_API_URL` skips reranking.
