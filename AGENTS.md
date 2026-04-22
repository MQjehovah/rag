# AGENTS.md

## Project Overview

Notes RAG System — Chinese-language note-taking app with auto vector indexing (RAG), knowledge graph, and enhanced search. Two-package monorepo: `backend/` (Python/FastAPI) + `frontend/` (Vue 3/TypeScript).

## Dev Commands

### Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest                         # run tests (no pytest.ini / conftest; tests import from app.core.embedding.*)
```

### Frontend

```bash
cd frontend
npm install
npm run dev                    # dev server on port 3000 (NOT 5173 — overridden in vite.config.ts)
npm run build                  # vue-tsc typecheck + vite build (this IS the typecheck step)
```

## Architecture

- **Backend entry**: `app.main:app` — FastAPI app, includes routers from `app.api.{pages,notebooks,search,upload,graph}`
- **Config**: `app/config.py` uses `pydantic_settings.BaseSettings`, loads from `.env`. All env vars are lowercase snake_case in code (e.g. `embedding_api_url`, `minio_endpoint`).
- **Database**: SQLite via SQLAlchemy, stored at `./data/notes.db`. Auto-created on startup. Uses legacy `declarative_base()` style.
- **Vector store**: ChromaDB persistent client at `./data/chromadb`, collection name `"pages"`, cosine similarity.
- **Embeddings**: Calls OpenAI-compatible API (default `EMBEDDING_API_URL`), does NOT use Ollama directly for embeddings despite `OLLAMA_*` env vars existing. `OLLAMA_MODEL` is for LLM generation only.
- **RAG pipeline** (`app/core/rag.py`): `EmbeddingService` chunks via langchain `RecursiveCharacterTextSplitter` + `UnstructuredMarkdownLoader`, then encodes via HTTP. `VectorStore` wraps ChromaDB.
- **Knowledge graph** (`app/core/graph.py`): `GraphBuilder` with three-signal weight model (vector×3.0 + keyword×2.0 + notebook×0.5). Uses jieba for Chinese keyword extraction with regex fallback. Edges stored in `graph_edges` SQL table.
- **Frontend entry**: `src/main.ts` — Vue 3 + Pinia + Element Plus + Vue Router. Two routes: `/` (Editor), `/graph` (KnowledgeGraph). No separate router config file is authoritative — routes are defined inline in `main.ts`.

## Key Gotchas

- **Vite dev port is 3000**, not the Vite default 5173. Vite proxies `/api` → `http://localhost:8000`.
- **`.env` required in `backend/`** before running. Copy from `.env.example`. Config defaults in `config.py` point to a specific LAN IP — override in your `.env`.
- **No pyproject.toml / pytest.ini** — pytest config is implicit. Tests are in `backend/tests/core/` and reference `app.core.embedding.*` module paths that may not match current source layout (current code has `app.core.rag.EmbeddingService`, not `app.core.embedding.encoder`).
- **Docker Compose** uses version `'2.2'`. In containers, Ollama is accessed via `host.docker.internal`. Frontend container is nginx serving built static files + reverse proxy to backend.
- **Frontend `npm run build`** runs `vue-tsc` first — type errors will block the build. There is no separate `typecheck` or `lint` script.
- **`noUnusedLocals` and `noUnusedParameters`** are enabled in `tsconfig.json` — unused imports/params will fail the build.
- **Backend `requirements.txt`** includes `pytest` and `pytest-asyncio` as runtime deps (not a separate dev-requirements file).
