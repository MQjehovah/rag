"""阶段 7A：真实模型后端适配（本文件禁止联网；离线 stub 用于契约测试）。

原则：
- 显式选择 simulated / real；real 模式失败/缺配置 → 报错，绝不回退模拟；
- 不假定 OMP 编码模型即运行时模型；不猜测供应商 model ID：
  真实执行/维护/提议复用项目既有配置体系（settings.llm_api_url/key/model，
  OpenAI 兼容 messages→JSON），必要时用显式 config dict 覆盖（不允许猜测默认）；
- 记录角色模型标识、超时/重试参数；usage 不可得 → null/unknown（不写 0）；
- 真实路径不依赖 STRICT-V1 等演示行为标记（角色只按提示词/协议工作）；
- 缺失配置时给可操作报错，不打印密钥，不读取无关凭据；
- 本模块默认全部函数为“构造器/适配器”，不发起任何网络调用。
"""
from __future__ import annotations

import asyncio
import json
import os
import re as _RE
import threading
from typing import Callable
from urllib.parse import urlparse

from app.config import settings
from app.core.skill_evolution.errors import SkillEvolutionError

# 角色模型配置（显式；缺省沿用项目 llm_*；未知 usage 一律 None）。
EXEC_ROLE = "executor"
MAINTAIN_ROLE = "maintainer"
PROPOSE_ROLE = "proposer"
EVAL_ROLE = "eval"
ROLES = (EXEC_ROLE, MAINTAIN_ROLE, PROPOSE_ROLE, EVAL_ROLE)


def _opt_int(d: dict, key: str) -> int | None:
    v = d.get(key)
    if v is None or v == "":
        return None
    return int(v)


class ModelBackendError(SkillEvolutionError):
    """模型后端配置/调用错误（真实模式不得静默回退模拟）。"""


class ModelConfig:
    """某一角色的显式配置快照（不含密钥正文；凭据仅存非秘密引用）。"""

    def __init__(self, *, mode: str, model: str | None, timeout: float = 120.0,
                 retries: int = 1, api_url: str | None = None,
                 api_key_present: bool = False,
                 max_output_tokens: int | None = None,
                 extra: dict | None = None,
                 provider_id: str | None = None,
                 credential_env: str | None = None,
                 allowed_endpoints: tuple = (),
                 enforce_https: bool = False):
        if mode not in ("simulated", "real"):
            raise ModelBackendError(f"model mode 必须是 simulated/real: {mode!r}")
        self.mode = mode
        self.model = model
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.api_url = api_url
        self.api_key_present = bool(api_key_present)
        self.max_output_tokens = (int(max_output_tokens)
                                  if max_output_tokens is not None else None)
        self.extra = dict(extra or {})
        # 服务端受控配置标识（B 项验收）：provider/config ID、凭据引用（env 名）、
        # 允许端点（scheme+host+port+路径前缀）。密钥永不进入本对象/记录。
        self.provider_id = provider_id
        self.credential_env = credential_env
        self.allowed_endpoints = tuple(allowed_endpoints or ())
        self.enforce_https = bool(enforce_https)

    def to_record(self) -> dict:
        """记录用（绝不含密钥值；凭据只保存非秘密 env 引用）。"""
        return {
            "mode": self.mode,
            "model": self.model,
            "timeout": self.timeout,
            "retries": self.retries,
            "api_url": self.api_url,
            "api_key_present": bool(self.api_key_present),
            "provider_id": self.provider_id,
            "credential_env": self.credential_env,
            "allowed_endpoints": list(self.allowed_endpoints),
            "max_output_tokens": self.max_output_tokens,
            "extra": self.extra,
            "usage_tokens": None,  # 供应商用量未知 → null，不写 0
            "usage_measured": False,
        }


def provider_map() -> dict:
    """服务端受控 provider 映射（非秘密）。非法 JSON → 明确失败。

    与 credential_binding 共用同一实现（防止 executor/reviewer 双写漂移）。
    """
    from app.core.skill_evolution.credential_binding import settings_provider_map
    return settings_provider_map()


def resolve_real_config(role: str, *, override: dict | None = None) -> ModelConfig:
    """真实模式配置解析：显式 override 用于端点/模型/参数覆盖，否则复用项目 settings。

    凭据来源（凭据终审契约）：
    - provider 模式：provider_id → provider map → credential_env（env 名）；
      绝不回退 settings.llm_api_key；api_key_present 表示引用当前可解析。
    - 旧兼容模式（仅 wikiskill_require_provider_binding=false 场景）：唯一凭据来源为
      settings.llm_api_key。
    - raw `override['llm_api_key']` 一律 fail-closed（明文密钥不可冻结/不可发送，
      错误不输出密钥值）。
    不做任何联网；缺 URL/KEY/MODEL → 明确报错（不猜测、不打印密钥）。
    """
    from app.core.skill_evolution.credential_binding import (
        CredentialBindingError, endpoint_declared, env_present,
        provider_fields, reject_raw_override_key, registered_provider,
    )
    if role not in ROLES:
        raise ModelBackendError(f"未知角色: {role}")
    override = override or {}
    try:
        reject_raw_override_key(override)
    except CredentialBindingError as exc:
        raise ModelBackendError(str(exc)) from exc
    mode = str(override.get("mode") or "real")
    url = str(override.get("llm_api_url") or settings.llm_api_url or "")
    model = str(override.get("model") or settings.llm_model or "")
    if mode == "simulated":
        return ModelConfig(mode="simulated", model=model or None,
                           timeout=float(override.get("timeout") or 120.0),
                           retries=int(override.get("retries") or 1),
                           max_output_tokens=_opt_int(override, "max_output_tokens"))
    pid = override.get("provider") or settings.wikiskill_default_provider or ""
    if pid:
        try:
            registered_provider(pid)   # 映射被换/缺失 → 明确失败
            cred_env, endpoints, enforce_https = provider_fields(pid)
            if not env_present(cred_env):
                raise ModelBackendError(
                    f"credential_unavailable：凭据引用 {cred_env} 未就绪"
                    "（启动/发送前拒绝；不读取/保存密钥值）")
            if not url:
                raise ModelBackendError(
                    f"real 模式缺少 LLM 端点（{role}）：provider {pid} 未指定端点")
            if not endpoint_declared(url, endpoints):
                raise ModelBackendError(
                    f"端点与 provider {pid} 受控允许端点不匹配（provider/config ID 与"
                    "端点未关联）")
        except CredentialBindingError as exc:
            raise ModelBackendError(str(exc)) from exc
        if not model:
            raise ModelBackendError(
                f"real 模式缺少模型标识（{role}）：请配置 llm_model 或 override")
        return ModelConfig(mode="real", model=model,
                           timeout=float(override.get("timeout") or 120.0),
                           retries=int(override.get("retries") or 1),
                           api_url=url,
                           api_key_present=env_present(cred_env),
                           max_output_tokens=_opt_int(override,
                                                       "max_output_tokens"),
                           provider_id=pid, credential_env=cred_env,
                           allowed_endpoints=endpoints,
                           enforce_https=enforce_https)
    # 兼容模式（未启用 provider 映射）：沿用 settings 单端点 + 全局密钥引用。
    # 密钥只读 settings.llm_api_key；override 明文密钥已被上面显式拒绝。
    if not url:
        raise ModelBackendError(
            f"real 模式缺少 LLM 端点（{role}）：请配置 llm_api_url 或显式 override")
    if not settings.llm_api_key:
        raise ModelBackendError(
            f"real 模式缺少 LLM 凭据（{role}）：请配置 llm_api_key（不输出密钥值）；"
            "raw override 密钥不允许，请改用受控 provider + credential_env")
    if not model:
        raise ModelBackendError(
            f"real 模式缺少模型标识（{role}）：不能猜测供应商 model ID，"
            "请配置 llm_model 或显式 override['model']")
    return ModelConfig(mode="real", model=model,
                       timeout=float(override.get("timeout") or 120.0),
                       retries=int(override.get("retries") or 1),
                       api_url=url, api_key_present=True,
                       max_output_tokens=_opt_int(override, "max_output_tokens"))


def _run_coro(coro):
    """同步边界执行协程：已运行事件循环内禁止嵌套 asyncio.run → 起线程执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def _worker():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "?").lower()
    except ValueError:
        return (url or "").lower()


def allowed_llm_endpoint(api_url: str) -> bool:
    """服务端受控端点校验：目标 host 必须命中允许名单。

    名单 = settings.wikiskill_allowed_llm_endpoints（host[:port] 或完整 URL）；
    名单为空 → 仅允许 settings.llm_api_url / wikiskill_reviewer_api_url 的
    host（即“只允许当前受控默认端点”）。任意地址不能搭配全局密钥外联。
    """
    entries = [e.strip() for e in _RE.split(r"[\s,]+",
               str(settings.wikiskill_allowed_llm_endpoints or "")) if e.strip()]
    target = _host_of(api_url)
    if entries:
        allowed = {_host_of(e) if "://" in e else e.lower() for e in entries}
        return target in allowed or api_url in entries
    defaults = {_host_of(str(settings.llm_api_url or "")),
                _host_of(str(settings.wikiskill_reviewer_api_url or ""))}
    defaults.discard("?")
    return target in defaults


def ensure_endpoint_allowed(api_url: str) -> None:
    """发送前校验；不匹配 → 抛错且不发任何 HTTP 请求。"""
    if not api_url:
        raise ModelBackendError("real 配置缺少 api_url（fail closed）")
    if not allowed_llm_endpoint(api_url):
        raise ModelBackendError(
            f"endpoint_not_allowed：端点不在受控允许名单（任意地址不得搭配全局"
            f"凭据发送）: {_host_of(api_url)}")


def _validate_provider_binding(cfg) -> None:
    """发送前校验 provider/config ID + 凭据引用 + 允许端点 三者的冻结关联。

    与评审器共用 credential_binding.validate_frozen_send（同一安全判定）：
    - provider 仍受控存在（映射被换 → 拒绝，要求新 run）；
    - 凭据引用未变（settings 变化不能换成另一供应商的凭据）；
    - 端点仍在冻结允许列表与当前映射允许列表内；
    - enforce_https 规则。
    """
    from app.core.skill_evolution.credential_binding import (
        CredentialBindingError, validate_frozen_send)
    try:
        validate_frozen_send(
            provider_id=cfg.provider_id,
            credential_env=cfg.credential_env,
            api_url=cfg.api_url,
            frozen_endpoints=cfg.allowed_endpoints or (),
            enforce_https=bool(cfg.enforce_https))
    except CredentialBindingError as exc:
        raise ModelBackendError(str(exc)) from exc


def _resolve_credential(cfg) -> str:
    """按冻结凭据引用解析密钥（provider 模式只读其 credential_env；兼容模式只读
    settings.llm_api_key；不做跨模式回退）。密钥不落日志/记录/响应。"""
    from app.core.skill_evolution.credential_binding import (
        CredentialBindingError, resolve_env_credential)
    if cfg.provider_id:
        try:
            return resolve_env_credential(cfg.credential_env or "")
        except CredentialBindingError as exc:
            raise ModelBackendError(str(exc)) from exc
    return str(settings.llm_api_key or "")


def _cfg_http_chat(cfg, messages, *, timeout=None) -> dict:
    """按冻结配置发送 chat（model/api_url/max_tokens 全部来自 cfg，不读当前
    settings 替换；凭据仅受控引用，不落日志）。"""
    import json as _j
    import re as _re
    import httpx
    from app.config import settings
    if not cfg.api_url:
        raise ModelBackendError("real 配置缺少 api_url（fail closed）")
    if cfg.provider_id:
        _validate_provider_binding(cfg)
    else:
        ensure_endpoint_allowed(cfg.api_url)
    payload = {"model": cfg.model, "messages": messages, "stream": False}
    if cfg.max_output_tokens is not None:
        payload["max_tokens"] = int(cfg.max_output_tokens)
    headers = {"Content-Type": "application/json"}
    secret = _resolve_credential(cfg)
    if not secret:
        raise ModelBackendError(
            "credential_unavailable：凭据为空（不发送无 Authorization 的请求；"
            "raw override 密钥不允许）")
    headers["Authorization"] = "Bearer " + secret
    try:
        resp = httpx.post(cfg.api_url, json=payload, headers=headers,
                          timeout=(timeout if timeout is not None
                                   else cfg.timeout),
                          follow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ModelBackendError(
            f"模型服务不可用: {type(exc).__name__}") from exc
    content = (data.get("choices") or [{}])[0].get("message", {}).get(
        "content", "") or ""
    text = content.strip()
    try:
        out = _j.loads(text)
        if isinstance(out, dict):
            return out
    except _j.JSONDecodeError:
        pass
    m = _re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, _re.DOTALL)
    if m:
        try:
            out = _j.loads(m.group(1))
            if isinstance(out, dict):
                return out
        except _j.JSONDecodeError as exc:
            raise ModelBackendError(
                f"模型输出 JSON 解析失败: {exc}") from exc
    raise ModelBackendError("模型输出非 JSON 对象（明确失败，不回退模拟）")


def build_executor_runner(cfg: ModelConfig) -> Callable:
    """执行者 runner（同步 messages→dict；严格按冻结配置发送）。"""
    if cfg.mode == "simulated":
        raise ModelBackendError("执行者 simulated runner 由 adapter 内建提供；"
                                "请勿把 simulated 当 real 覆盖")

    def _runner(messages, context: str = "", timeout: float = cfg.timeout):
        try:
            return _cfg_http_chat(cfg, messages, timeout=timeout)
        except ModelBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ModelBackendError(f"执行者调用失败: {type(exc).__name__}") from exc
    return _runner


def build_maintainer_runner(cfg: ModelConfig) -> Callable:
    """Maintainer runner：messages→dict（JSON 契约；有限格式修复由调用方完成）。"""
    if cfg.mode == "simulated":
        raise ModelBackendError("maintainer simulated runner 由维护流程内建提供")
    return build_executor_runner(cfg)  # 同一 JSON chat 契约


def build_proposer_runner(cfg: ModelConfig) -> Callable:
    """Skill Proposer runner（多轮工具协议逐轮 JSON 调用）。"""
    if cfg.mode == "simulated":
        raise ModelBackendError("proposer simulated runner 由 proposer 流程内建提供")
    return build_executor_runner(cfg)
