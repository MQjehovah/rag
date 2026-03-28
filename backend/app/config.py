from pydantic_settings import BaseSettings
from typing import Optional
class Settings(BaseSettings):
    ollama_host: str = "http://192.168.31.34:11434"
    ollama_model: str = "qwen2.5:7b"
    
    embedding_model: str = "bge-large-zh-v1.5"
    embedding_device: str = "cpu"
    
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