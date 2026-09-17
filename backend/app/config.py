import math

from pydantic import field_validator, model_validator
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

    # 阶段 8A：演化实验只读控制台（服务端受控根映射，JSON 对象 {root_id: 绝对目录}）。
    wikiskill_console_enabled: bool = False
    wikiskill_console_roots: str = ""

    # 阶段 8C：演化运行控制（创建/启动/暂停/恢复/取消，仅管理员；写实验根）。
    # 独立于只读开关：即使 console 只读开启，控制写端点仍默认关闭。
    wikiskill_evolution_admin_enabled: bool = False
    # 真实模式启动白名单（本轮默认 False：真实执行未授权；模拟不受影响）。
    wikiskill_evolution_admin_real_enabled: bool = False
    # 模拟证据晋升旁路（仅隔离测试环境可开启；生产默认 False，请求参数不能开启）。
    wikiskill_evolution_allow_simulated_promotion: bool = False
    # 业务晋升环境："production"（默认）| "isolated-test"。
    # production 下本包不开放业务晋升资格（真实供应商/校准/批准独立待办），即使
    # 误设模拟旁路与请求 allow_simulated=true 也一律拒绝；"isolated-test" 仅供
    # 隔离测试夹具建立受控资格（记录永不标为真实效果）。
    wikiskill_promotion_env: str = "production"

    # 允许发送请求的供应商端点（空格/逗号分隔 host[:port] 或完整 URL；空 → 仅
    # 允许 settings.llm_api_url 与 wikiskill_reviewer_api_url 的 host）。
    # 凭据通过“服务端受控端点 + 全局密钥”绑定：任意地址不得搭配全局密钥发送；
    # 冻结/恢复路径在发送前仍校验该名单（防 settings/override 漂移外联）。
    wikiskill_allowed_llm_endpoints: str = ""

    # 受控凭据—端点映射（JSON）：{ "<provider_id>": {
    #   "credential_env": "<密钥所在环境变量名（非秘密引用）>",
    #   "endpoints": ["scheme://host[:port]/allowed/path…"],  # 含路径前缀
    #   "allow_insecure": false } }。
    # provider/config ID、凭据引用、允许端点显式关联；冻结记录只存非秘密标识。
    # 为空 → 兼容模式：沿用 llm_api_key/llm_api_url（仍受 allowed 名单约束）。
    wikiskill_credential_providers: str = ""
    # real 角色默认 provider_id（credential_providers 非空时生效；可按角色 override）
    wikiskill_default_provider: str = ""
    # 正式 real 路径门禁：必须使用受控 provider 映射（executor/maintainer/proposer，
    # review=v2 时评审角色也须绑定），不允许因 providers 未配置自动落入旧全局
    # key 模式。旧记录可读/展示；不满足契约的真实启动在首个请求前拒绝。
    wikiskill_require_provider_binding: bool = True
    # 评审角色使用的 provider_id（review=v2；须在 credential_providers 内且其
    # endpoints 覆盖评审端点；credential_env 为评审密钥的非秘密引用）
    wikiskill_reviewer_provider: str = ""

    # 阶段 8D：业务编译读取作用域内的 WikiSkill 业务绑定（默认 False → 旧行为不变）。
    wikiskill_business_compile_enabled: bool = False

    # Grader v2 语义评审模型（独立显式配置：绝不自动沿用执行模型）。
    # 缺 model_id / api_url / prompt_version → real 评审 fail closed。
    wikiskill_reviewer_model_id: str = ""
    wikiskill_reviewer_api_url: str = ""
    wikiskill_reviewer_prompt_version: str = ""
    wikiskill_reviewer_timeout: float = 60.0
    wikiskill_reviewer_retries: int = 1
    wikiskill_reviewer_max_output_tokens: int | None = None

    # Phase 9B：KnowledgeCompileRun worker lease/heartbeat/poll/renew 时长接线
    # （纯配置，默认值=现值 300/300/2.0/30.0，生产行为不变）。跨进程故障恢复测试
    # 经这些 env 注入短 lease 以加速 stale 判定。executor.claim_by_id 与
    # worker.heartbeat 必须同源读 wiki_pipeline_lease_seconds（不得不同步）。
    wiki_pipeline_lease_seconds: int = 300
    wiki_pipeline_heartbeat_timeout_seconds: int = 300
    wiki_pipeline_poll_interval_seconds: float = 2.0
    wiki_pipeline_lease_renew_interval_seconds: float = 30.0

    # Phase 9B 配置校验：非法时长在启动配置阶段即失败（Settings 构造抛 ValidationError
    # → 应用起不来、worker 不启动），绝不静默回退默认值。
    @field_validator("wiki_pipeline_lease_seconds", "wiki_pipeline_heartbeat_timeout_seconds")
    @classmethod
    def _validate_wiki_pipeline_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer (seconds > 0)")
        return value

    @field_validator(
        "wiki_pipeline_poll_interval_seconds", "wiki_pipeline_lease_renew_interval_seconds"
    )
    @classmethod
    def _validate_wiki_pipeline_positive_finite_float(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("must be a finite positive number (reject NaN/±Infinity/≤0)")
        return value

    @model_validator(mode="after")
    def _validate_wiki_pipeline_renew_budget(self) -> "Settings":
        budget = min(
            self.wiki_pipeline_lease_seconds, self.wiki_pipeline_heartbeat_timeout_seconds
        ) / 3
        if self.wiki_pipeline_lease_renew_interval_seconds > budget:
            raise ValueError(
                "wiki_pipeline_lease_renew_interval_seconds must be <= "
                f"min(lease, heartbeat_timeout) / 3 = {budget}"
            )
        return self

settings = Settings()
