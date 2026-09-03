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
    vision_mode: str = ""  # disabled/local/remote；空则按 pdf_vision_enabled 推断
    vision_remote_allowed: bool = False

    top_k: int = 5
    vector_recall_k: int = 50

    reranker_api_url: str = ""
    reranker_model: str = ""

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite:///./data/notes.db"

    # P0-BE-03：SQLite 并发优化（WAL/busy_timeout/synchronous）。回滚：SQLITE_PRAGMA_ENABLED=false
    sqlite_pragma_enabled: bool = True
    sqlite_busy_timeout_ms: int = 5000
    sqlite_use_nullpool: bool = False

    # P0-BE-04：SQLite 全表向量扫描放入工作线程。回滚：VECTOR_SEARCH_THREADING_ENABLED=false
    vector_search_threading_enabled: bool = True

    # P0-BE-08：SQLite 外键强制（PRAGMA foreign_keys=ON，模型声明的 CASCADE/SET NULL 才会生效）。
    # 回滚：SQLITE_FOREIGN_KEYS_ENABLED=false
    sqlite_foreign_keys_enabled: bool = True

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
    # wiki_editor 角色对应的用户组（逗号分隔）；admin 天然拥有编辑权。
    ldap_group_map_wiki_editor: str = ""

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

    # W11 生命周期 + 冲突每日扫描
    auto_daily_scan_enabled: bool = True
    daily_scan_hour: int = 3

    # P7：灰度上线 Feature Flag（V4 Phase H 清理后仅保留仍在使用的开关）
    wiki_topic_enabled: bool = False        # P6 Wiki 主题启用

    # 数据源平台 Feature Flag（V3 计划 4.10 / 9.1）
    source_hub_enabled: bool = False        # 数据源平台启用
    dingtalk_connector_enabled: bool = False
    gitlab_connector_enabled: bool = False

    # P19：Community 聚类算法 louvain / connected_components（仅 Page 驱动图谱使用）
    community_algorithm: str = "louvain"
    community_large_split_size: int = 48

    # V4 Phase C：Wiki 增量刷新调度器并发参数
    wiki_refresh_workers: int = 1        # 后台刷新线程数（建议 1，最大 2）
    wiki_refresh_queue_size: int = 500   # 队列上限（配合 Page.wiki_dirty 持久恢复）

    # Phase 5.1：wiki.default 为生产唯一自动链路（kill switch）。
    # 默认 True（单轨启用）；DB RuntimeFeatureFlag 同名行可关闭（kill switch = 暂停编译）。
    wiki_pipeline_default_enabled: bool = True

    # Phase 6：Auto Skill Router 参数（不得写死在 Prompt）。
    wiki_skill_auto_threshold: float = 0.80     # LLM/确定性高置信阈值
    wiki_skill_switch_margin: float = 0.15      # 迁移判定优势阈值（migration_proposed）
    wiki_skill_default_key: str = "default"     # 兜底 Skill key
    wiki_skill_router_max_candidates: int = 5   # 候选上限
    wiki_skill_router_summary_chars: int = 4000 # LLM 请求摘要长度上限

    # Phase 7D：wiki.default 生产 active pipeline version（只允许 "2"/"3"；未知值启动失败）。
    wiki_pipeline_active_version: str = "3"

settings = Settings()
