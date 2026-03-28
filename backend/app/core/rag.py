import httpx
import chromadb
from chromadb.config import Settings
from typing import List, Dict, Any, Optional
from app.config import settings

class EmbeddingService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self.model = settings.embedding_model
        self.ollama_host = settings.ollama_host
        self.max_length = 400
    
    async def encode(self, text: str) -> List[float]:
        if len(text) > self.max_length:
            text = text[:self.max_length]
        
        url = f"{self.ollama_host}/api/embeddings"
        payload = {"model": self.model, "prompt": text}
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        return response.json().get("embedding", [])
    
    async def encode_chunks(self, text: str, overlap: int = 50) -> List[tuple]:
        """分段编码，返回 [(chunk_text, embedding), ...]"""
        chunks = []
        text_len = len(text)
        
        if text_len <= self.max_length:
            emb = await self.encode(text)
            return [(text, emb)] if emb else []
        
        start = 0
        while start < text_len:
            end = min(start + self.max_length, text_len)
            chunk = text[start:end]
            
            if chunk.strip():
                try:
                    emb = await self.encode(chunk)
                    if emb:
                        chunks.append((chunk, emb))
                except Exception as e:
                    print(f"Encode chunk error: {e}")
            
            start = end - overlap if end < text_len else text_len
        
        return chunks
    
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
    
    async def add_page(self, page_id: str, title: str, content: str, embedding: List[float], chunk_index: int = 0):
        doc_id = f"{page_id}_{chunk_index}" if chunk_index > 0 else page_id
        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[f"{title}\n{content}"],
            metadatas=[{"title": title, "page_id": page_id, "chunk_index": chunk_index}]
        )
    
    async def add_page_chunks(self, page_id: str, title: str, chunks: List[tuple]):
        """添加分块向量"""
        # 先删除旧的分块
        await self.delete_page(page_id)
        
        for i, (chunk_text, embedding) in enumerate(chunks):
            await self.add_page(page_id, title, chunk_text, embedding, i)
    
    async def delete_page(self, page_id: str):
        # 删除所有相关分块
        all_ids = self.collection.get()["ids"]
        ids_to_delete = [id for id in all_ids if id == page_id or id.startswith(f"{page_id}_")]
        if ids_to_delete:
            self.collection.delete(ids=ids_to_delete)
    
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