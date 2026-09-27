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

**Collaboration sidecar** (`collab/`): a `y-websocket` Node service (port 1234) providing Yjs WebSocket sync. Deploy/refresh:
```bash
docker build -t rag-collab ./collab
docker rm -f rag-collab && docker run -d --name rag-collab --restart unless-stopped \
  --network rag_default -p 1234:1234 rag-collab
```
The frontend nginx proxies `/api/collab` → `rag-collab:1234` with `Upgrade`; the external gateway (45 nginx, bind mount `/home/xzrobot/docker/nginx/conf.d`) must also upgrade `/rag/api/collab/` (see `ai-services.conf` / `ai.xzrobot.com.conf` + `00-collab-map.conf`).

## Architecture

- **Backend entry**: `app.main:app` — FastAPI app, routers from `app.api.{pages,notebooks,search,upload,graph,auth,dingtalk,chat,organize,wiki,jira,sources,pipelines,embeddings}`.
- **Embedding profiles** (`app/api/embeddings.py`, `app/core/rag.py`): admin-configurable `embedding_profiles` (api_url/model/dimensions/api_key/kind) with one global default; each notebook may set its own `embedding_profile_id` (NULL = default). Chunks store `page_chunks.embedding_profile`; retrieval (`retrieval.py#_visible_profiles`) groups recall per profile so vectors from different models are never compared. `POST /api/embeddings/reindex` rebuilds the whole corpus per-notebook profile.
- **Compile pipelines** (`app/api/pipelines.py`, `app/core/pipeline.py`, `app/core/wiki.py`, `app/core/compile_dispatch.py`): the **only compile channel**. A pipeline = `来源笔记本范围` + `文体(compiler_kind/prompt_template)` + `target_space_id`(空=默认空间) + `auto_trigger`. It ingests notes **one note at a time** into the target space's page tree; the **LLM decides create/update and the page hierarchy** (constrained by the pipeline prompt; the engine imposes no global depth/top-level rule). `update` ops run a **merge pass so human edits survive**. Kinds: `wiki|api_doc|markdown|changelog|custom`. Manual run via `POST /api/pipelines/{id}/run?mode=incremental|full` (incremental = notes updated since last successful run). **Auto trigger** (`compile_dispatch.dispatch_note_compile`, called from `pages.background_index_page`): on note save, notes covered by an enabled+`auto_trigger` pipeline are ingested into that pipeline's space (throttled 60s per (note,pipeline), running→pending rerun); notes **not** covered fall back to the **built-in default pipeline** (`refresh_note_wiki` → 默认空间, wiki 文体).
- **Wiki default space & uniqueness**: `WikiSpace`「默认空间」is materialized (migration `backend/scripts/migrate_pipeline_spaces.py`); `wiki_pages.title` is unique **per space** via index `uq_wiki_pages_space_title (space_id, title)` (no longer globally unique).
- **Config**: `app/config.py` (`pydantic_settings.BaseSettings`, loads `.env`). Env vars are lowercase snake_case in code. LLM accepts either `LLM_API_URL` (full chat completions endpoint) or `LLM_BASE_URL` (OpenAI-style base; `/chat/completions` appended automatically).
- **Database**: PostgreSQL 16 + pgvector via SQLAlchemy. Tables: `notebooks` (has `description` + `embedding_profile_id`), `pages`, `page_chunks` (vector(1024) + HNSW + `embedding_profile`), `page_terms` (BM25), `graph_edges`, `graph_entities`, `graph_entity_edges`, `users`, `user_groups`, `wiki_pages` (has `pipeline_id`/`source_key`/`embedding_profile`), `graph_communities`, `image_assets`, `embedding_profiles`, `pipelines`, `pipeline_runs`.
- **Vector store** (`app/core/rag.py`): pgvector HNSW cosine (`<=>` with `CAST(:q AS vector)`); numpy fallback for SQLite dev.
- **Hybrid retrieval** (`app/core/retrieval.py`): query rewrite → vector + BM25 multi-path recall → RRF fusion → rerank → entity/graph expansion → **MMR diversity re-rank** (`MMR_ENABLED`/`MMR_LAMBDA`).
- **Agentic chat** (`app/api/chat.py`): history-aware query rewrite, multi-hop sufficiency judging, and a **GraphRAG global fallback** that searches `graph_communities` when local results are insufficient.
- **LLM Wiki** (`app/core/wiki.py`): per-note incremental ingest (concurrent, merge-aware so human edits survive), `wiki_pages` table, admin rebuild endpoint. Note saves auto-refresh linked wiki pages (60s throttle) via `pages.py`.
- **GraphRAG** (`app/core/graphrag.py`): Louvain community detection over `graph_entity_edges`, LLM community summaries with embeddings, `search_communities()` for global Q&A.
- **Frontend**: routes `/` (Chat), `/notes` (Editor), `/graph` (KnowledgeGraph), `/wiki` (Wiki), `/pipelines` (Pipelines), `/embeddings` (Embeddings). Routes defined inline in `main.ts` — `router/index.ts` is dead code. Notebook embedding model is chosen in Editor's notebook "设置" dialog; profile list is visible to all users but only admins can edit (api_key is masked for non-admins). There is no "未分类/unassigned" section (removed).
- **Editor** (`components/TipTapEditor.vue`): Notion-style TipTap editor — slash `/` insert menu (`@tiptap/suggestion`, with descriptions), selection bubble toolbar, hover **block handle** with **drag-to-reorder** (⋮⋮ menu: 上移/下移/复制/复制 Markdown/在下方加段落/删除/转换为标题·列表·引用), contextual **table toolbar** (add/delete row/col, merge/split, header), task lists, links, underline, highlight, text-align, typography, character count, Lucide icons, **view prefs** (font size / focus width / typewriter mode, persisted to `localStorage:rag-editor-prefs`), shortcuts help dialog, code-block-lowlight with copy button (`CodeBlockComponent.vue`), mermaid, paste-drop image upload. Content is serialized as Markdown via `tiptap-markdown` (`editor.storage.markdown.getMarkdown()`).
- **Callout & Toggle blocks** (`components/editorExt.ts`): `Callout` = `div[data-callout]` (info/tip/success/warn/danger); `Toggle` = `details[data-toggle]` with `open`/`title` attrs and `div.toggle-content` body. Both inserted via slash/insert menu, serialized to Markdown via the HTML fallback, and give editors `NodeView`s (`ToggleNodeView.vue`).
- **Images** (`components/ImageNodeView.vue`): the `image` node gains a `width` attr + NodeView with drag-to-resize and a caption input (stored in `title`). Serialization: standard `![alt](src "title")` unless a width is set, then HTML `<img ... width>`.
- **Page cover/icon**: `pages.icon` (emoji) + `pages.cover` columns; `cover` holds an image URL or a `grad:<key>` gradient preset (`GRADIENTS` in `Editor.vue`); `PageCreate/Update/Response` carry them. Editor shows a cover + a grouped/searchable-free emoji picker (categories, random) above the title, saved with the normal page PUT.
- **Comments** (`page_comments`): page-level comments — `GET/POST /api/pages/{id}/comments`, `DELETE .../comments/{cid}`, `POST .../comments/{cid}/resolve`. Author from `current_user` (`id`/`display_name`; note `get_current_user` payload keys are `id`/`username`/`display_name`, NOT `sub`). Editor has a 💬 panel with count badge, resolve/delete.
- **Share / publish** (`pages.share_token`): `POST /api/pages/{id}/share` mints a token, `DELETE .../share` revokes; public `GET /api/public/pages/{token}` (no auth, router `app/api/public.py`) returns title/content/icon/cover. Frontend route `/share/:token` (`SharedPage.vue`, public, nav hidden) renders a read-only page.
- **Page history** (`page_revisions`): `update_page` snapshots title/content on change (2-min throttle, max 50/page); `GET /api/pages/{id}/revisions`, `GET .../revisions/{rev}`, `POST .../revisions/{rev}/restore`. Editor "⋯ → 版本历史" dialog previews + restores.
- **Trash / soft-delete** (`pages.deleted_at`): `DELETE /api/pages/{id}` is soft (trash); `GET /api/pages/trash`, `POST /api/pages/{id}/restore`, `DELETE /api/pages/{id}/purge` (hard). All list/tree/tags/search paths filter `deleted_at IS NULL` (`search_common.get_visible_page_ids` too). Editor sidebar has a 回收站 section.
- **Wiki-links & backlinks**: typing `[[` in the editor opens a page-mention autocomplete (`PageMention` suggestion in `TipTapEditor.vue`) that inserts literal `[[Title]]` (plain text, round-trips trivially). `GET /api/pages/{id}/backlinks` returns visible pages whose content contains `[[<title>]]`; the Editor's outline panel shows a 反向链接 section that opens them.
- **Page options / quick switcher / outline / favorites**: Editor topbar "⋯" menu = 收藏, 复制链接, 创建副本, 导出 Markdown, 全宽/小字号 (per-page, `localStorage:rag-page-*`), 移到回收站. `Ctrl+K` opens a command-palette page search; `☰` toggles an outline panel (headings from Markdown, click to scroll); favorites live in `localStorage:rag-fav-pages`.
- **More editor features**: cover vertical reposition (`pages.cover_offset`, drag the cover); export Markdown/HTML/PDF (client-side `markdown-it` + DOMPurify + `window.print`); **templates** (`localStorage:rag-templates`, save-as-template / new-from-template); **`@` user mention** (`GET /api/auth/users`); **notebook drag-reorder + section grouping** (`notebooks.position`/`section`, `POST /api/notebooks/{id}/move {position, section}`); **database views** (`pages.view_type` = doc|table|board|calendar via `PUT /api/pages/{id}/view`, renders direct child pages as rows/cards/calendar, `pages.status` column).
- **Page tree** (`pages.parent_id` + `pages.position`): `GET /api/pages/tree?notebook_id=` returns all pages of a notebook with `parent_id`/`position`; `POST /api/pages/{id}/move {parent_id, position}` reparents + reorders siblings (with cycle guard). Editor sidebar renders the tree (indent by depth) with **HTML5 drag-drop**: drop top/bottom third = reorder before/after, middle = nest inside; menu item "新建子页面". The sidebar is **resizable** (drag its right edge, width persisted to `localStorage:rag-sidebar-width`) and **collapsible** (`Ctrl+\`, persisted `rag-sidebar-collapsed`); tree branches with children get a collapse chevron.
- **Collaborative editing** (`collab/` sidecar + `TipTapEditor.vue`) — **CURRENTLY DISABLED**: Editor forces `collabEnabled = false` (single-user editing) because the old seeding raced with the initial Yjs sync and duplicated note content on every reopen (×2 each open; seen ×8/×32). If re-enabling, seed **only after `provider.on('sync')`** (`collabSynced` guard) and never in `onCreate`. Original design: Editor mounts `TipTapEditor` keyed per page in collab mode — `Y.Doc` + `WebsocketProvider` per room `page-<id>`, `@tiptap/extension-collaboration` (+ cursor), StarterKit history off. Markdown is still the source of truth: an empty Yjs doc is seeded from the note's Markdown, and edits keep emitting Markdown for the normal autosave PUT (RAG unaffected). If the WS service is unreachable the probe fails and editing falls back to single-user (no breakage).
- **Wiki spaces & tree** (`wiki_spaces` + `wiki_pages.space_id/parent_id/position`): knowledge base is split into **spaces** (Docmost/Outline-style top-level groups) and pages form a **multi-level tree**. `GET/POST /api/wiki/spaces`, `PUT/DELETE /api/wiki/spaces/{id}`, `PUT /api/wiki/{id}/space` (move page), `POST /api/wiki` (create page), `PUT /api/wiki/{id}/move {parent_id,position}` (nest/reorder, cycle-guarded), `DELETE /api/wiki/{id}`. `GET /api/wiki?space_id=` (``=all, `default`=no space) returns `items` (flat, with parent_id/position). Frontend sidebar: space list + collapsible tree with drag-to-nest/reorder.
- **Rendered Markdown** (Wiki `renderContent`): `markdown-it` (`html:true`) + `markdown-it-task-lists` + **DOMPurify** whitelist sanitize (note content may contain HTML like `<u>`/`<mark>`/callouts); internal `[[页面]]` links render as `<span class="wiki-link" data-id>` (not `javascript:` hrefs, which DOMPurify strips). Chat (`html:false`) renders assistant text only (task lists supported).

## Key Gotchas

- **Production runs PostgreSQL**: `docker compose --profile pg ...`. The `db` service maps host port `5433` (5432 on the host is used by another service); containers talk to `db:5432` internally. Old SQLite fallback still works for local dev.
- **LLM endpoints**: embeddings/rerank go through the company gateway (`EMBEDDING_API_URL`/`RERANKER_API_URL`); chat/JSON via `LLM_BASE_URL` (e.g. `https://ai.rosiwit.com/v1`) + `LLM_MODEL` (a reasoning model like `deepseek-v4-flash`). Reasoning models need generous timeouts.
- **Image proxy**: `/api/upload/images/proxy?url=...` fetches external images without a Referer header (bypasses OSS referer checks) and caches to `data/image_cache`. External image URLs in chat sources are normalized to this proxy unless on the safe-host list.
- **Wiki rebuild is incremental and idempotent**: re-running `POST /api/wiki/rebuild` ingests all notes against existing pages; update ops get a merge pass so human edits are preserved. `POST /api/graph/rebuild-communities` first completes entity extraction, then rebuilds community summaries.
- **Vite dev port is 3000**, proxies `/api` → `http://localhost:8000`.
- **`.env` required in `backend/`**. Old `.env` with `CHROMADB_PATH` causes pydantic validation errors — remove it. Production `.env` is baked into the image; changing it requires a rebuild.
- **Frontend `npm run build`** runs `vue-tsc` first — type errors block the build. `noUnusedLocals`/`noUnusedParameters` are on.
- **Reranker is optional** — empty `RERANKER_API_URL` skips reranking.
- **Embeddings depend on Xinference** (host `34:9997`) having `bge-large-zh-v1.5` (1024-d) **launched**; the gateway (`34:3100/v1/embeddings`) proxies to it. Xinference **loses launched models on restart** → embeddings return `400 Model not found in the model list`. Relaunch from local files: `docker exec xinference xinference launch --model-name bge-large-zh-v1.5 --model-type embedding --model-uid bge-large-zh-v1.5 --model-path /data/models/bge-large-zh-v1.5` (files at host `~/models/bge-large-zh-v1.5`, mounted `/data/models`). A self-heal cron on 34 (`*/5 * * * * /home/xzrobot/apps/ensure-embed.sh`) relaunches if missing. Note: `backend/.env`'s `LLM_API_KEY` is stale; compose injects the valid gateway key.
- **SSO 双轨**: 资源轨(`SSO_ISSUER`/`SSO_AUDIENCE`/`SSO_JWKS_URI`)校验别的系统(dashboard)传来的 token;
  登录轨(`SSO_CLIENT_ID`/`SSO_CLIENT_SECRET`/`SSO_REDIRECT_URI`/`SSO_REDIRECT_TARGET`)提供浏览器授权码流程
  (`GET /api/auth/sso/start` + `GET /api/auth/oidc/callback`,前端登录页按钮「企业 SSO 登录」)。
  回调拿到的 id_token `aud` 是本系统自己的 client_id,与资源轨受众不同,故 `verify_sso_token(token, audience=...)`
  支持显式覆盖。SSO 只签发 `roles`(无 `groups`),`_normalize_claims_groups` 同时采纳两者,且 `roles` 含 `admin`
  时补内部管理员标记 `__local_admin__`(全库管理端点均按该组名判定)。
- **子路径部署**: 对外 `https://ai.xzrobot.com/rag/...` 由 45 的 nginx **剥掉 `/rag` 前缀**再转发到 34:8092,
  故前端 nginx 只需处理根路径的 `/api`(不需要子路径规则);`PUBLIC_BASE_PATH=/rag` 仅用于生成对外绝对 URL。
  **重建 backend 后必须一并重启/重建 frontend**(其 nginx 会缓存 backend 容器 IP)。
