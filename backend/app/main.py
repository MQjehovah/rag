from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.api import pages, search, search_v2, upload, notebooks, auth, dingtalk, chat, organize, evidence, wiki, wiki_workspaces, wiki_compile, p7_flags, sources, model_health, rag_chat, debts_v4, v4_graph, dingtalk_folder_mappings, source_path_mappings, feedback_v4

app = FastAPI(
    title="Notes RAG System",
    description="笔记系统 + 自动RAG索引 + 企业级搜索",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pages.router)
app.include_router(notebooks.router)
app.include_router(search.router)
app.include_router(search_v2.router)
app.include_router(upload.router)
app.include_router(auth.router)
app.include_router(dingtalk.router)
app.include_router(chat.router)
app.include_router(organize.router)
app.include_router(evidence.router)
app.include_router(wiki.router)
app.include_router(wiki_workspaces.router)
app.include_router(wiki_compile.router)
app.include_router(p7_flags.router)
app.include_router(sources.router)
app.include_router(model_health.router)
app.include_router(rag_chat.router)
app.include_router(debts_v4.router)
app.include_router(v4_graph.router)
app.include_router(dingtalk_folder_mappings.router)
app.include_router(source_path_mappings.router)
app.include_router(feedback_v4.router)


@app.on_event("startup")
def _start_scheduler():
    from app.api.organize import start_scheduler
    from app.api.scheduler import start_daily_scheduler
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.models.database import get_engine, get_session, init_db, SchemaNotReadyError
    from app.sources.bootstrap import ensure_builtin_connections

    DingTalkLocalStorage().ensure_directories()
    try:
        engine = get_engine(settings.database_url)
        init_db(engine)
        db = get_session(engine)
        try:
            ensure_builtin_connections(db)
        finally:
            db.close()
        engine.dispose()

        # V4 Phase C：数据库迁移检查成功后恢复 dirty Page。
        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import recover_dirty_pages
        recover_dirty_pages()
    except SchemaNotReadyError:
        # schema 未就绪必须阻止应用以错误 schema 启动，不能静默继续。
        import logging
        logging.getLogger(__name__).exception("schema not ready, aborting startup")
        raise
    except Exception:
        import logging
        logging.getLogger(__name__).exception("failed to initialize database on startup")
    start_scheduler()
    start_daily_scheduler()

    # Phase 4：KnowledgeCompileRun 重启恢复 + 进程内 worker（与 recover_dirty_pages
    # 并存：DB 中 queued/running 任务恢复入队，由本 worker 消费；不抢 recover_dirty_pages 资源）。
    try:
        from app.core.wiki_pipeline.worker import run_startup_recovery, start_worker
        _compile_recovery = run_startup_recovery()
        logging.getLogger(__name__).info("wiki compile recovery: %s", _compile_recovery)
        start_worker()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("failed to start wiki compile worker")


@app.on_event("shutdown")
def _shutdown_workers():
    try:
        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import shutdown
        shutdown()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("failed to shutdown wiki refresh workers")
    try:
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import shutdown as graph_shutdown
        graph_shutdown()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("failed to shutdown graph build workers")
    try:
        from app.core.wiki_pipeline.worker import stop_worker as pipeline_stop
        pipeline_stop()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("failed to shutdown wiki compile workers")

@app.get("/")
async def root():
    return {"message": "Notes RAG System", "version": "2.0.0"}

@app.get("/health")
async def health():
    from app.models.database import get_engine, check_managed_migrations, SchemaNotReadyError
    try:
        engine = get_engine(settings.database_url)
        missing = check_managed_migrations(engine)
        engine.dispose()
        if missing:
            return {"status": "schema_not_ready", "missing": missing}
        return {"status": "healthy"}
    except SchemaNotReadyError:
        return {"status": "schema_not_ready"}
    except Exception:
        import logging
        logging.getLogger(__name__).exception("health check failed")
        return {"status": "unhealthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
