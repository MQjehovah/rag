from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    embedding_api_url: str = "http://localhost:11434/api/embed"
    embedding_model: str = "modelscope.cn/Embedding-GGUF/bge-large-zh-v1.5:latest"
    embedding_dimensions: int = 1024

    chunk_size: int = 300
    chunk_overlap: int = 50

    pdf_ocr_enabled: bool = False
    pdf_ocr_dpi: int = 200
    pdf_ocr_min_confidence: float = 0.86
    pdf_ocr_duplicate_similarity: float = 0.86
    pdf_ocr_min_novel_chars: int = 4
    pdf_ocr_max_pages: int = 80

    pdf_image_assets_enabled: bool = False
    pdf_image_storage_dir: str = "./data/pdf-pages"
    pdf_image_dpi: int = 144
    pdf_image_max_width: int = 1800
    pdf_image_max_pages: int = 60
    pdf_image_importance_threshold: float = 4.0

    pdf_vision_enabled: bool = False
    pdf_vision_api_url: str = ""
    pdf_vision_api_key: str = ""
    pdf_vision_model: str = "glm-4v-flash"
    pdf_vision_page_ratio: float = 1.0
    pdf_vision_min_pages: int = 10
    pdf_vision_hard_max_pages: int = 60
    pdf_vision_concurrency: int = 2
    pdf_vision_timeout_seconds: int = 120

    top_k: int = 5
    vector_recall_k: int = 50

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
    dingtalk_sync_scope: str = "configured"
    dingtalk_supported_extensions: str = "pdf,docx,pptx,xlsx,txt,csv,md"
    dingtalk_include_wiki: bool = True
    dingtalk_include_subfolders: bool = True
    dingtalk_local_storage_dir: str = "./data/dingtalk"
    dingtalk_download_concurrency: int = 3
    dingtalk_markdown_pdf_enhanced: bool = True
    pdf_hybrid_enabled: bool = True
    markitdown_enabled: bool = True
    markitdown_pdf_fallback_enabled: bool = True

    remote_rag_base_url: str = ""
    remote_rag_username: str = ""
    remote_rag_password: str = ""
    remote_rag_notebook_name: str = "钉钉知识库"
    remote_rag_notebook_id: str = ""
    remote_rag_timeout_seconds: int = 60
    remote_rag_max_retries: int = 3
    remote_rag_state_file: str = "./data/dingtalk/remote-sync-state.json"
    remote_rag_backup_dir: str = "./data/dingtalk/remote-backups"

    llm_api_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    auto_organize_enabled: bool = False
    auto_organize_interval_hours: int = 24

settings = Settings()
