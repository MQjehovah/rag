from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.api import pages, search, upload, notebooks, graph, auth

app = FastAPI(
    title="Notes RAG System",
    description="笔记系统 + 自动RAG索引",
    version="1.0.0"
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
app.include_router(upload.router)
app.include_router(graph.router)
app.include_router(auth.router)

@app.get("/")
async def root():
    return {"message": "Notes RAG System", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)