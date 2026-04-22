from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    embedding_api_url: str = "http://192.168.31.34:8000/v1/embeddings"
    embedding_model: str = "bge-large-zh-v1.5"
    embedding_dimensions: int = 1024

    chunk_size: int = 800
    chunk_overlap: int = 100

    top_k: int = 5
    vector_recall_k: int = 50

    reranker_api_url: str = "http://192.168.31.34:8001/v1/rerank"
    reranker_model: str = "bge-reranker-v2-m3"

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/notesrag"

    minio_endpoint: str = "192.168.31.8:9000"
    minio_access_key: str = "admin"
    minio_secret_key: str = "xzyz2022!"
    minio_bucket: str = "xzrobotserver"
    minio_secure: bool = False

    ldap_server_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_group_base_dn: str = ""
    ldap_user_filter: str = "(uid={username})"
    ldap_group_filter: str = "(member={user_dn})"
    ldap_group_map_admin: str = ""

    jwt_secret_key: str = "change-me-in-production"
    jwt_expire_minutes: int = 1440

    local_admin_username: str = "admin"
    local_admin_password: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
