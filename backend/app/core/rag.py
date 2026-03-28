import httpx
import chromadb
from chromadb.config import Settings
from typing import List, Dict, Any, Optional
from app.config import settings

class EmbeddingService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.model = settings.embedding_model
        self.ollama_host = settings.ollama_host
    
    async def encode(self, text: str) -> List[float]:
        url = f"{self.ollama_host}/api/embeddings"
        payload = {"model": self.model, "prompt": text}
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        return response.json().get("embedding", [])
    
    async def close(self):
        await self.client.aclose()

class VectorStore:
    def __init__(self):
        self.client = chromadb.PersistentClient(
            path=settings.chromadb_path,
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name="pages",
            metadata={"hnsw:space": "cosine"}
        )
    
    async def add_page(self, page_id: str, title: str, content: str, embedding: List[float]):
        self.collection.upsert(
            ids=[page_id],
            embeddings=[embedding],
            documents=[f"{title}\n{content}"],
            metadatas=[{"title": title}]
        )
    
    async def delete_page(self, page_id: str):
        self.collection.delete(ids=[page_id])
    
    async def search(self, query_embedding: List[float], top_k: int = 5) -> Dict[str, Any]:
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k
        )
        return {
            "ids": results.get("ids", [[]])[0],
            "documents": results.get("documents", [[]])[0],
            "metadatas": results.get("metadatas", [[]])[0],
            "distances": results.get("distances", [[]])[0]
        }
    
    async def get_count(self) -> int:
        return self.collection.count()