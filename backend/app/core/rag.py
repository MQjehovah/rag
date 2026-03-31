import httpx
import chromadb
from chromadb.config import Settings as ChromaSettings
from typing import List, Dict, Any, Optional
from app.config import settings
from langchain_community.document_loaders import UnstructuredMarkdownLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
import tempfile
import os
import logging

logger = logging.getLogger(__name__)


class EmbeddingService:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)
        self.model = settings.embedding_model
        self.api_url = settings.embedding_api_url
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            separators=["##", "#", "\n\n", "\n", " ", ""]
        )

    async def encode(self, text: str) -> List[float]:
        # OpenAI兼容API格式
        payload = {"input": text, "model": self.model}
        response = await self.client.post(self.api_url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("data", [{}])[0].get("embedding", [])

    def load_markdown(self, content: str, title: str = "") -> List[str]:
        """使用 UnstructuredMarkdownLoader 加载并切片 markdown 内容"""
        # 写入临时文件，指定 UTF-8 编码
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write(content)
            temp_path = f.name

        try:
            loader = UnstructuredMarkdownLoader(temp_path)
            docs = loader.load()

            # 合并文档内容
            full_text = "\n\n".join([doc.page_content for doc in docs])
            if title:
                full_text = f"# {title}\n\n{full_text}"

            # 切片
            chunks = self.splitter.split_text(full_text)
            return [chunk for chunk in chunks if chunk.strip()]
        finally:
            os.unlink(temp_path)

    async def encode_chunks(self, content: str, title: str = "") -> List[tuple]:
        """加载、切片并编码 markdown 内容"""
        chunks = self.load_markdown(content, title)
        result = []

        for chunk_text in chunks:
            try:
                emb = await self.encode(chunk_text)
                if emb:
                    result.append((chunk_text, emb))
            except Exception as e:
                logger.error(f"Encode chunk error: {str(e).encode('utf-8', errors='replace').decode('utf-8')}")

        return result
    
    async def close(self):
        await self.client.aclose()

class VectorStore:
    def __init__(self):
        self.client = chromadb.PersistentClient(
            path=settings.chromadb_path,
            settings=ChromaSettings(anonymized_telemetry=False)
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