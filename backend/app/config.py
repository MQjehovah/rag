from pydantic_settings import BaseSettings
from typing import Optional
class Settings(BaseSettings):
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    # Embedding配置 (OpenAI兼容API)
    embedding_api_url: str = "http://192.168.31.34:8000/v1/embeddings"
    embedding_model: str = "Qwen3-VL-Embedding-2B"
    
    chromadb_path: str = "./data/chromadb"
    
    chunk_size: int = 800
    chunk_overlap: int = 100
    
    top_k: int = 5
    
    host: str = "0.0.0.0"
    port: int = 8000
    
    database_url: str = "sqlite:///./data/notes.db"
    
    minio_endpoint: str = "192.168.31.8:9000"
    minio_access_key: str = "admin"
    minio_secret_key: str = "xzyz2022!"
    minio_bucket: str = "xzrobotserver"
    minio_secure: bool = False
    
    class Config:
        env_file = ".env"

settings = Settings()