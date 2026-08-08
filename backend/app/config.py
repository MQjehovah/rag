from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    embedding_api_url: str = "http://localhost:11434/api/embed"
    embedding_model: str = "modelscope.cn/Embedding-GGUF/bge-large-zh-v1.5:latest"
    embedding_dimensions: int = 1024

    chunk_size: int = 300
    chunk_overlap: int = 50

    top_k: int = 5
    vector_recall_k: int = 50

    hybrid_bm25_enabled: bool = True
    query_rewrite_enabled: bool = True
    query_rewrite_min_len: int = 12
    contextual_retrieval_enabled: bool = True
    entity_graph_enabled: bool = True
    agentic_max_hops: int = 2
    mmr_enabled: bool = True
    mmr_lambda: float = 0.7
    community_qa_enabled: bool = True
    multimodal_enabled: bool = False

    reranker_api_url: str = ""
    reranker_model: str = ""

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite:///./data/notes.db"

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
    local_admin_password: str = "123456"

    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_agent_id: str = ""
    dingtalk_knowledge_base_id: str = ""
    dingtalk_operator_id: str = ""

    llm_api_url: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    auto_organize_enabled: bool = False
    auto_organize_interval_hours: int = 24

    class Config:
        env_file = ".env"

    def model_post_init(self, __context):
        # Accept either LLM_API_URL (full chat/completions endpoint) or
        # LLM_BASE_URL (OpenAI-style base URL; /chat/completions is appended).
        if not self.llm_api_url and self.llm_base_url:
            self.llm_api_url = self.llm_base_url.rstrip("/") + "/chat/completions"


settings = Settings()
