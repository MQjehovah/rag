"""模型调用结果契约 + 错误分类（P13-MODEL-01/05，V3 计划 8.5）。

统一返回契约，让上层明确知道模型是否真正参与：
- EmbeddingBatchResult：used=true 才允许标记 dense 来源、写 Trace、算命中率
- RerankResult：used=true 才允许标记 reranker 来源、生成 relevance_score

错误分类（V3 8.5 表格）统一 error_code，决定是否重试。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 错误分类：error_code → 是否重试
ERROR_CODES = {
    "NETWORK_UNREACHABLE": True,
    "TIMEOUT": True,
    "MODEL_NOT_LOADED": False,      # 服务在线但模型列表无 UID
    "MODEL_UID_INVALID": False,     # 配置 UID 不存在
    "AUTH_FAILED": False,
    "BAD_REQUEST": False,
    "RATE_LIMITED": True,
    "SERVER_ERROR": True,
    "INVALID_RESPONSE": False,
    "DIMENSION_MISMATCH": False,    # 向量维度不是配置值
}


def is_retryable(error_code: str | None) -> bool:
    """错误码是否可重试。未知错误码默认不可重试（保守）。"""
    if not error_code:
        return False
    return ERROR_CODES.get(error_code, False)


@dataclass
class EmbeddingBatchResult:
    """Embedding 批次结果（V3 8.5）。"""
    embeddings: list[list[float]] = field(default_factory=list)
    used: bool = False               # 是否真正调用模型成功
    degraded: bool = False           # 是否降级（空向量/跳过）
    model_uid: str = ""
    dimensions: int | None = None
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.used and not self.degraded and self.error_code is None


@dataclass
class RerankResult:
    """Reranker 结果（V3 8.5）。"""
    results: list[dict] = field(default_factory=list)  # [{index, relevance_score}]
    used: bool = False
    degraded: bool = False
    model_uid: str = ""
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.used and not self.degraded and self.error_code is None


def classify_http_error(status_code: int, body: str = "") -> str:
    """把 HTTP 状态码 + 脱敏正文分类为统一 error_code。

    - 400 保留脱敏正文用于区分 MODEL_NOT_LOADED 与普通 BAD_REQUEST（V3 8.5）
    - 401/403 → AUTH_FAILED；429 → RATE_LIMITED；5xx → SERVER_ERROR
    - 连接/DNS/Socket → NETWORK_UNREACHABLE（由调用方 catch 后传入）
    """
    if status_code == 401 or status_code == 403:
        return "AUTH_FAILED"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code >= 500:
        return "SERVER_ERROR"
    if status_code == 400:
        # 服务返回 400 时，脱敏正文区分 MODEL_NOT_LOADED 与 BAD_REQUEST
        lowered = (body or "").lower()
        if "model not found" in lowered or "model_not_loaded" in lowered or "not in the model list" in lowered:
            return "MODEL_NOT_LOADED"
        return "BAD_REQUEST"
    return "BAD_REQUEST"
