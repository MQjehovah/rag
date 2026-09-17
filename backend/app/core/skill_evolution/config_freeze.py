"""B4：完整运行配置冻结（非秘密快照 + 指纹；worker/resume 从冻结配置构造角色）。

原则：
- 创建/首次授权启动时把 real 模式的角色（executor/maintainer/proposer）与语义评审
  配置快照（provider endpoint host/model/prompt 版本与模板哈希/参数/timeout/retries/
  max_output_tokens）写入 run 配置（runner.frozen*），并保存组合指纹；
- 只存 受控凭据引用（api_key_present 布尔），不存密钥；endpoint host 入指纹；
- worker 与 resume 一律从冻结配置构造角色 runner/reviewer，**不读当前 settings**
  替换原模型/地址/参数；缺少冻结字段的旧记录 → 明确“需迁移/不可恢复”错误；
- start/resume 显式确认可绑定 config_fingerprint（记录存在时必配）；
- 评审请求实际透传输出上限（max_output_tokens → max_tokens 载荷）。
"""
from __future__ import annotations

import hashlib
import json as _JSON
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.skill_evolution.errors import SkillEvolutionError

FROZEN_KEY = "frozen"
FROZEN_REVIEWER_KEY = "frozen_reviewer"
FROZEN_SCHEMA = "runtime-config-freeze/v1"


class FrozenConfigError(SkillEvolutionError):
    def __init__(self, message: str, code: str = "frozen_config_error",
                 *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or "?"
    except ValueError:
        return "?"


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        _JSON.dumps(payload, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoleFrozen:
    role: str
    model: str
    timeout: float
    retries: int
    endpoint_host: str
    api_url: str
    api_key_present: bool
    max_output_tokens: int | None
    provider_id: str | None = None
    credential_env: str | None = None
    allowed_endpoints: tuple = ()
    enforce_https: bool = False

    def to_dict(self) -> dict:
        return {"role": self.role, "model": self.model, "timeout": self.timeout,
                "retries": self.retries, "endpoint_host": self.endpoint_host,
                "api_url": self.api_url, "api_key_present": self.api_key_present,
                "max_output_tokens": self.max_output_tokens,
                "provider_id": self.provider_id,
                "credential_env": self.credential_env,
                "allowed_endpoints": list(self.allowed_endpoints),
                "enforce_https": bool(self.enforce_https)}

    def fingerprint(self) -> str:
        return _digest({k: v for k, v in self.to_dict().items()
                        if k not in ("api_url",)})


def role_frozen_from_live(role: str, override: dict | None = None) -> RoleFrozen:
    """从当前受控配置解析角色冻结快照（仍不落密钥值）。"""
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, resolve_real_config)
    try:
        cfg = resolve_real_config(role, override=override)
    except ModelBackendError as exc:
        raise FrozenConfigError(f"{role} 冻结失败: {exc}",
                                code="real_preflight_failed",
                                status_code=409) from exc
    return RoleFrozen(role=role, model=cfg.model, timeout=cfg.timeout,
                      retries=cfg.retries, endpoint_host=_host(cfg.api_url),
                      api_url=cfg.api_url,
                      api_key_present=bool(cfg.api_key_present),
                      max_output_tokens=cfg.max_output_tokens,
                      provider_id=cfg.provider_id,
                      credential_env=cfg.credential_env,
                      allowed_endpoints=tuple(cfg.allowed_endpoints or ()),
                      enforce_https=bool(getattr(cfg, "enforce_https", False)))


def reviewer_frozen_from_live() -> dict:
    """语义评审冻结快照（独立 provider 绑定 + 模板哈希）。

    凭据终审契约：
    - 读取 settings.wikiskill_reviewer_provider → 受控 provider map；记录
      provider_id / credential_env（env 名）/ allowed_endpoints / enforce_https；
      绝不改用默认执行 provider，也绝不回退 settings.llm_api_key 或 override 明文。
    - 校验 reviewer API URL 属于该 provider；校验 credential_env **存在**（不读取/
      不保存其值）；enforce_https 规则在冻结时即生效。
    - 缺 provider / 缺引用 / 缺端点 / 端点不匹配 / 凭据引用不可用 → fail-closed。
    - 旧兼容模式（wikiskill_require_provider_binding=false 且未配 reviewer provider）：
      仅允许 settings.llm_api_key 作为唯一凭据来源（与执行侧旧兼容一致）。
    """
    from app.config import settings
    from app.core.skill_evolution.credential_binding import (
        CredentialBindingError, endpoint_in, env_present, normalize_endpoints,
        provider_binding_required, provider_fields,
    )
    from app.core.skill_evolution.model_review import (
        REVIEW_PROMPT_VERSION, ReviewerConfig, template_hash)
    cfg = ReviewerConfig(
        mode="real", model_id=settings.wikiskill_reviewer_model_id or None,
        prompt_version=settings.wikiskill_reviewer_prompt_version or None,
        api_url=settings.wikiskill_reviewer_api_url or None,
        api_key_present=bool(settings.llm_api_key),
        timeout=float(getattr(settings, "wikiskill_reviewer_timeout", 60)),
        retries=int(getattr(settings, "wikiskill_reviewer_retries", 1)),
        max_output_tokens=_opt_int(getattr(settings,
                                           "wikiskill_reviewer_max_output_tokens",
                                           None)))
    if not cfg.model_id or not cfg.prompt_version or not cfg.api_url:
        raise FrozenConfigError(
            "review=v2 缺少独立评审配置（model/prompt/api 不得沿用执行模型）",
            code="reviewer_not_configured")
    rp = str(settings.wikiskill_reviewer_provider or "").strip()
    provider_id: str | None = None
    credential_env: str | None = None
    allowed_endpoints: tuple = ()
    enforce_https: bool = False
    if rp:
        try:
            credential_env, allowed_endpoints, enforce_https = provider_fields(rp)
            if not endpoint_in(cfg.api_url, allowed_endpoints):
                raise FrozenConfigError(
                    f"reviewer 端点不属于 provider {rp} 受控允许端点"
                    "（评审不得外联/不得沿用执行 provider）",
                    code="reviewer_endpoint_not_bound")
            if enforce_https and not str(cfg.api_url).startswith("https://"):
                raise FrozenConfigError(
                    "https_required：reviewer provider 要求 HTTPS（拒绝冻结）",
                    code="https_required")
            if not env_present(credential_env):
                raise FrozenConfigError(
                    f"评审凭据引用 {credential_env} 未就绪（preview/start 前 "
                    "fail-closed；不读取/不保存密钥值）",
                    code="reviewer_credential_unavailable")
        except CredentialBindingError as exc:
            raise FrozenConfigError(
                f"reviewer provider 绑定失败：{exc}",
                code="reviewer_provider_required") from exc
        provider_id = rp
        cfg = ReviewerConfig(
            mode="real", model_id=cfg.model_id,
            prompt_version=cfg.prompt_version, api_url=cfg.api_url,
            api_key_present=True, timeout=cfg.timeout, retries=cfg.retries,
            max_output_tokens=cfg.max_output_tokens,
            provider_id=provider_id, credential_env=credential_env,
            allowed_endpoints=allowed_endpoints,
            enforce_https=enforce_https)
    elif provider_binding_required():
        raise FrozenConfigError(
            "review=v2 需要显式 wikiskill_reviewer_provider（评审不得沿用默认执行 "
            "provider 或全局 key）",
            code="reviewer_provider_required")
    # 旧兼容（require_provider_binding=false）：仅 settings.llm_api_key 可用，
    # 冻结仍须有该全局凭据（否则 real 评审无法发送）。
    elif not settings.llm_api_key:
        raise FrozenConfigError(
            "旧兼容模式评审缺少全局凭据 settings.llm_api_key（不得发送无认证请求）",
            code="reviewer_not_configured")
    d = {"model_id": cfg.model_id, "prompt_version": cfg.prompt_version,
         "endpoint_host": _host(cfg.api_url), "api_url": cfg.api_url,
         "api_key_present": cfg.api_key_present, "timeout": cfg.timeout,
         "retries": cfg.retries, "max_output_tokens": cfg.max_output_tokens,
         "template_version": REVIEW_PROMPT_VERSION,
         "template_hash": template_hash(),
         "provider_id": cfg.provider_id,
         "credential_env": cfg.credential_env,
         "allowed_endpoints": list(normalize_endpoints(
             cfg.allowed_endpoints or ())),
         "enforce_https": bool(cfg.enforce_https)}
    d["fingerprint"] = _digest(
        {k: v for k, v in d.items() if k not in ("api_url",)})
    return d


def _opt_int(v) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def build_frozen_block(*, override: dict | None = None,
                       review: str | None = None) -> dict:
    roles = {}
    for role in ("executor", "maintainer", "proposer"):
        rf = role_frozen_from_live(role, override=override)
        roles[role] = rf.to_dict()
    d = {"schema": FROZEN_SCHEMA, "roles": roles}
    if review == "v2":
        d[FROZEN_REVIEWER_KEY] = reviewer_frozen_from_live()
    d["fingerprint"] = _digest(
        {"roles": roles, FROZEN_REVIEWER_KEY: d.get(FROZEN_REVIEWER_KEY),
         "schema": FROZEN_SCHEMA})
    return d


def fingerprint_of(frozen: dict) -> str:
    return str(frozen.get("fingerprint") or "")


def reviewer_fingerprint_of(frozen: dict) -> str | None:
    rv = (frozen or {}).get(FROZEN_REVIEWER_KEY)
    return str(rv.get("fingerprint")) if rv else None


def build_actor_runner_frozen(role: str, frozen: dict):
    """从冻结配置构造真实角色 runner（不读当前 settings）。"""
    roles = (frozen or {}).get("roles") or {}
    fd = roles.get(role)
    if not fd:
        raise FrozenConfigError(
            f"run 冻结配置缺少角色 {role}：旧记录需迁移/不可恢复（禁止以当前默认值补齐）",
            code="frozen_missing", status_code=409)
    from app.core.skill_evolution.real_adapters import ModelConfig
    cfg = ModelConfig(mode="real", model=str(fd["model"]),
                      timeout=float(fd["timeout"]), retries=int(fd["retries"]),
                      api_url=str(fd["api_url"]),
                      api_key_present=bool(fd.get("api_key_present")),
                      max_output_tokens=(int(fd["max_output_tokens"])
                                         if fd.get("max_output_tokens") is not None
                                         else None),
                      provider_id=fd.get("provider_id"),
                      credential_env=fd.get("credential_env"),
                      allowed_endpoints=tuple(fd.get("allowed_endpoints") or ()),
                      enforce_https=bool(fd.get("enforce_https")))
    from app.core.skill_evolution.real_adapters import (
        build_executor_runner, build_maintainer_runner, build_proposer_runner)
    if role == "executor":
        return build_executor_runner(cfg)
    if role == "maintainer":
        return build_maintainer_runner(cfg)
    return build_proposer_runner(cfg)


def build_reviewer_frozen(frozen: dict):
    """从冻结配置构造真实评审器（含输出上限；不读当前 settings）。

    凭据终审：
    - 恢复冻结的 model/url/prompt/timeout/retries/max_tokens 与 provider_id/
      credential_env/allowed_endpoints/enforce_https，一律不读当前 settings 替换；
    - 发送前仍由 ChatSemanticReviewer 与当前 provider map 复核（漂移拒绝）；
    - 旧冻结记录缺 reviewer provider 绑定字段：provider-required 环境明确
      frozen_missing（不得以当前 settings 补齐、不得回退全局 key）；
      仅 wikiskill_require_provider_binding=false 的旧兼容场景允许恢复为 legacy。
    """
    from app.config import settings
    from app.core.skill_evolution.credential_binding import (
        normalize_endpoints, provider_binding_required)
    from app.core.skill_evolution.model_review import (
        ReviewerConfig, create_reviewer)
    rv = (frozen or {}).get(FROZEN_REVIEWER_KEY)
    if not rv:
        raise FrozenConfigError("run 冻结配置缺少评审配置", code="frozen_missing")
    provider_id = rv.get("provider_id") or None
    credential_env = rv.get("credential_env") or None
    allowed_endpoints = tuple(rv.get("allowed_endpoints") or ())
    enforce_https = bool(rv.get("enforce_https"))
    if provider_id is None:
        # 旧冻结记录（provider 重构前）：正式 provider-required 环境必须 fail-closed。
        if provider_binding_required():
            raise FrozenConfigError(
                "run 冻结配置缺少评审 provider 绑定字段（旧记录不可恢复真实发送；"
                "不得以当前 settings 补齐、不得回退全局 key）",
                code="frozen_missing")
    cfg = ReviewerConfig(
        mode="real", model_id=str(rv["model_id"]),
        prompt_version=str(rv["prompt_version"]),
        api_url=str(rv["api_url"]),
        api_key_present=bool(rv.get("api_key_present")),
        timeout=float(rv["timeout"]), retries=int(rv["retries"]),
        max_output_tokens=(int(rv["max_output_tokens"])
                           if rv.get("max_output_tokens") is not None else None),
        provider_id=provider_id,
        credential_env=credential_env,
        allowed_endpoints=normalize_endpoints(allowed_endpoints),
        enforce_https=enforce_https)
    return create_reviewer(cfg)
