"""受控凭据绑定（executor/maintainer/proposer/reviewer 共用）。

凭据安全契约（凭据终审；本模块不持有任何密钥值）：
- provider 模式：provider_id → 服务端 provider map → credential_env（环境变量名）→
  运行时读取环境变量。绝不回退 settings.llm_api_key；绝不使用 override 明文密钥。
- 旧兼容模式（仅当 wikiskill_require_provider_binding=false）：只允许
  settings.llm_api_key 作为唯一凭据来源。
- raw `override["llm_api_key"]` 一律 fail-closed（不可冻结、不可发送）。
- provider 映射 / credential_env 引用 / allowed endpoints / HTTPS 约束发生漂移时，
  在任何 httpx.post 之前拒绝。
- 密钥值轮换（credential_env 不变）：允许且不改变配置指纹。
- 本模块函数只依赖非秘密引用（provider_id / credential_env 名称），所有异常信息
  不含密钥值。
"""
from __future__ import annotations

import os
import urllib.parse
from urllib.parse import urlparse

from app.core.skill_evolution.errors import SkillEvolutionError

# 与 real_adapters 既有文案保持一致的代码片段（供调用侧断言复用）。
MSG_MAP_INVALID = "wikiskill_credential_providers 配置非法（JSON）"
MSG_MAP_NOT_OBJECT = "wikiskill_credential_providers 必须为对象"
MSG_PROVIDER_UNREGISTERED = ("provider 未在服务端受控映射: {pid!r}（provider/config ID "
                             "必须受控注册）")
MSG_PROVIDER_FIELDS_MISSING = ("provider {pid} 缺少 credential_env/endpoints"
                               "（非秘密映射字段）")
MSG_ENDPOINT_MISMATCH = ("端点与 provider {pid} 受控允许端点不匹配（provider/config ID "
                         "与端点未关联）")


class CredentialBindingError(SkillEvolutionError):
    """凭据绑定安全错误（无密钥值；调用侧转换为各自异常类型）。"""

    def __init__(self, message: str, code: str = "credential_binding_error"):
        super().__init__(message)
        self.code = code

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


def settings_provider_map() -> dict:
    """服务端受控 provider 映射（非秘密）。非法 JSON/类型 → 明确失败。"""
    from app.config import settings
    raw = str(getattr(settings, "wikiskill_credential_providers", "") or "").strip()
    if not raw:
        return {}
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise CredentialBindingError(MSG_MAP_INVALID,
                                     code="provider_map_invalid") from exc
    if not isinstance(data, dict):
        raise CredentialBindingError(MSG_MAP_NOT_OBJECT,
                                     code="provider_map_invalid")
    return data


def provider_binding_required() -> bool:
    from app.config import settings
    return bool(getattr(settings, "wikiskill_require_provider_binding", False))


def registered_provider(provider_id: str) -> dict:
    """provider 必须受控存在；缺失 → CredentialBindingError。"""
    pmap = settings_provider_map()
    prov = pmap.get(provider_id)
    if not prov:
        raise CredentialBindingError(
            MSG_PROVIDER_UNREGISTERED.format(pid=provider_id),
            code="provider_not_registered")
    return prov


def provider_fields(provider_id: str) -> tuple[str, tuple, bool]:
    """(credential_env, endpoints, enforce_https)；缺引用/端点 → 错误。"""
    prov = registered_provider(provider_id)
    credential_env = str(prov.get("credential_env") or "")
    endpoints = tuple(str(e) for e in (prov.get("endpoints") or []))
    if not credential_env or not endpoints:
        raise CredentialBindingError(
            MSG_PROVIDER_FIELDS_MISSING.format(pid=provider_id),
            code="provider_fields_missing")
    enforce_https = not bool(prov.get("allow_insecure"))
    return credential_env, normalize_endpoints(endpoints), enforce_https


def normalize_endpoints(endpoints) -> tuple:
    """规范化允许端点：去空白/去重/确定性排序（避免同集顺序变化造成无意义漂移）。"""
    seen = []
    for e in endpoints:
        s = str(e).strip()
        if s and s not in seen:
            seen.append(s)
    return tuple(sorted(seen))


def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "?").lower()
    except ValueError:
        return (url or "").lower()


_PCT_DECODE_MAX = 8  # 有界解码层数；未在界内稳定 → fail-closed


def _valid_pct_escapes(text: str) -> bool:
    """malformed percent escape（孤立 %、%2、%GG 等）→ False。

    urllib.unquote 对非法转义会静默保留原样；此处必须显式拒绝，不得继续授权。
    """
    i = text.find("%")
    while i != -1:
        if i + 2 >= len(text):
            return False
        if text[i + 1] not in "0123456789abcdefABCDEF" or \
                text[i + 2] not in "0123456789abcdefABCDEF":
            return False
        i = text.find("%", i + 3)
    return True


def _path_percent_safe(path: str) -> bool:
    """路径多层百分号编码安全检查（fail-closed）。

    安全边界（端点授权最后补丁）：
    - 路径按原始 '/' 分段逐段处理；每段做有界、确定性的逐层 percent-decode 直到内容
      稳定；每层都校验 malformed escape；
    - 解码收敛后该分段若形成 NUL/控制字符、'\\'、跨层解码引入 '/'（分段伪造）或形成
      '.'/'..' 路径段 → 拒绝；
    - 超过 _PCT_DECODE_MAX 层仍未稳定 → 拒绝（防编码炸弹/无限循环；每层解码严格缩短，
      8 层足以覆盖 %2525… 深链）；
    - 只处理路径安全语义；query 不参与解码（保持精确字符串契约）。
    """
    for seg in path.split("/"):
        if seg == "":
            continue
        if seg in (".", ".."):
            return False
        if "\\" in seg:
            return False
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in seg):
            return False
        if not _valid_pct_escapes(seg):
            return False
        if "%" not in seg:
            continue
        cur = seg
        stable = False
        for _ in range(_PCT_DECODE_MAX):
            dec = urllib.parse.unquote(cur)
            if dec == cur:
                stable = True
                break
            cur = dec
            if not _valid_pct_escapes(cur):
                return False
        if not stable:
            return False
        # 收敛后的最终语义检查
        if cur in ("", ".", ".."):
            return False
        if "/" in cur or "\\" in cur:
            return False
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in cur):
            return False
    return True


def _url_parse_strict(raw):
    """结构化解析绝对 HTTP(S) URL；非法/逃逸 → ValueError。

    安全边界（端点授权 P1）：
    - 仅接受 scheme∈{http,https} 的绝对 URL；缺 scheme/hostname 拒绝；
    - hostname 取 urlparse.hostname（IPv6 去括号、小写化）；不剥离尾点（尾点视为
      不同 host，宁严勿宽）；
    - effective port：显式端口严格校验范围 1..65535；未显式时 https→443、http→80，
      默认端口与显式等价；
    - userinfo（netloc 含 '@'）拒绝；fragment 拒绝；
    - 路径多层百分号编码安全检查（_path_percent_safe）：逐段有界逐层 decode 直到稳定，
      拒绝 malformed escape、解码后形成 '/'、'\\'、NUL/控制字符与 '.'/'..' 路径段；
    - 路径分段按原始（未 decode）路径切分，比较时只允许“逐段相等或授权前缀 + '/'”
      边界，杜绝 '/v1' 匹配 '/v10' 与点路径越界。
    调用方（endpoint_in / validate_frozen_send）负责把 ValueError 转成稳定的受控
    不匹配/错误，绝不让原始解析异常冒泡。
    """
    if not isinstance(raw, str):
        raise ValueError("endpoint 必须为字符串")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
        raise ValueError("控制字符")
    if "\\" in raw:
        raise ValueError("反斜杠")
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ValueError("malformed url") from exc
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("仅 http/https")
    if "@" in u.netloc:
        raise ValueError("userinfo 禁止")
    if u.fragment:
        raise ValueError("fragment 禁止")
    try:
        host = u.hostname
    except ValueError as exc:
        raise ValueError("malformed host") from exc
    if not host:
        raise ValueError("缺少 hostname")
    try:
        port = u.port
    except ValueError as exc:
        raise ValueError("非法端口") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise ValueError("非法端口")
    path = u.path or ""
    if not _path_percent_safe(path):
        raise ValueError("路径多层编码/逃逸不安全（fail-closed）")
    return {"scheme": scheme, "host": host.lower(), "port": port,
            "segs": _path_segments(path), "query": u.query}


def _path_segments(path: str) -> tuple:
    """原始路径按 '/' 分段，稳定去除尾部空段；根 '/' 归一为空元组。"""
    parts = path.split("/")
    while parts and parts[-1] == "":
        parts.pop()
    return tuple(parts)


def endpoint_in(url: str, endpoints) -> bool:
    """结构化端点授权：scheme + hostname + effective port + path prefix 分段边界。

    - 任一侧解析非法/逃逸 → 该配对不匹配（稳定返回 False，绝不抛原始解析异常）；
    - 不允许“同 host 即通过”；/v1 不匹配 /v10；跨 scheme/端口/host 一律拒绝；
    - allowed 无 query：目标可在授权路径携带普通 query；allowed 含 query：目标
      query 必须精确相等（不做字符串前缀匹配）。
    仅作匹配判定；provider 发送前的非空冻结/当前集合与 HTTPS 强制在
    validate_frozen_send 完成。
    """
    try:
        t = _url_parse_strict(url)
    except ValueError:
        return False
    for raw in endpoints or ():
        try:
            a = _url_parse_strict(str(raw))
        except ValueError:
            continue
        if _match_authorized(t, a):
            return True
    return False


def _match_authorized(target: dict, allowed: dict) -> bool:
    if target["scheme"] != allowed["scheme"]:
        return False
    if target["host"] != allowed["host"]:
        return False
    if target["port"] != allowed["port"]:
        return False
    a_segs = allowed["segs"]
    t_segs = target["segs"]
    if allowed["query"]:
        # allowed 带 query：路径必须精确相等（禁止子路径扩张），query 必须精确相等
        # （禁止字符串前缀匹配与追加参数）。
        return t_segs == a_segs and target["query"] == allowed["query"]
    # allowed 无 query：目标可在授权路径（含真实子路径）上携带普通 query。
    if not a_segs:
        return True                       # 根路径覆盖同一 origin
    if len(t_segs) < len(a_segs):
        return False
    return t_segs[: len(a_segs)] == a_segs


def endpoint_declared(url: str, allowed_endpoints) -> bool:
    """resolve/预检阶段的“声明端点”判定：hostname + path prefix 分段边界。

    只用于 real_adapters.resolve_real_config 的 provider 端点预检（非发送判定），
    兼容既有“https_required 在发送时拒绝 http 端点”的语义分层：
    - scheme 与 effective port 不在本层约束（由发送前的 validate_frozen_send 与
      enforce_https 强制拒绝，见 https_required）；
    - **hostname 严格比较**：不同 hostname（含根路径授权）一律 False，绝不跨 host；
    - 路径边界仍按分段（/v1 不匹配 /v10、拒绝点路径/编码逃逸/斜杠/控制字符）；
    - 非法/逃逸 → 该配对 False；发送路径绝不经过本函数。
    """
    try:
        t = _url_parse_strict(url)
    except ValueError:
        return False
    for raw in allowed_endpoints or ():
        try:
            a = _url_parse_strict(str(raw))
        except ValueError:
            continue
        if t["host"] != a["host"]:
            continue
        a_segs = a["segs"]
        t_segs = t["segs"]
        if not a_segs:
            return True
        if len(t_segs) < len(a_segs):
            continue
        if t_segs[: len(a_segs)] == a_segs:
            return True
    return False


def endpoint_https(url: str) -> bool:
    return str(url or "").startswith("https://")


def resolve_env_credential(credential_env: str) -> str:
    """按 credential_env 运行时取值；缺失 → 错误（不输出密钥值）。"""
    value = os.environ.get(credential_env or "", "")
    if not value:
        raise CredentialBindingError(
            f"credential_unavailable：凭据引用 {credential_env} 未就绪",
            code="credential_unavailable")
    return value


def env_present(credential_env: str) -> bool:
    return bool(os.environ.get(credential_env or "", ""))


def validate_frozen_send(*, provider_id: str, credential_env: str | None,
                         api_url: str, frozen_endpoints,
                         enforce_https: bool) -> None:
    """发送前复核：provider 仍受控 + 引用未换 + 端点命中（冻结/当前映射）+ HTTPS。

    冻结映射与当前服务端 provider map 必须同时满足；任一漂移 → 拒绝发送。
    （角色与评审器共用同一判定。）
    """
    pmap = settings_provider_map()
    prov = pmap.get(provider_id)
    if not prov:
        raise CredentialBindingError(
            f"provider_mapping_changed：{provider_id} 已不在服务端受控映射"
            "（拒绝发送；如需换供应商请新建 run）",
            code="provider_mapping_changed")
    if str(prov.get("credential_env") or "") != str(credential_env or ""):
        raise CredentialBindingError(
            f"credential_reference_changed：provider {provider_id} 的凭据引用"
            "已改变（拒绝以另一供应商凭据发送）",
            code="credential_reference_changed")
    fe = normalize_endpoints(frozen_endpoints or ())
    cur = normalize_endpoints(prov.get("endpoints") or [])
    if not fe:
        # 端点授权 P1：provider-bound 发送时冻结端点集缺失/为空 → fail-closed，
        # 不得以当前 settings / 当前映射自动补齐。
        raise CredentialBindingError(
            "provider_mapping_changed：冻结端点集为空（拒绝发送；frozen 记录缺失"
            "不得以当前配置自动补齐）",
            code="provider_mapping_changed")
    if not cur:
        raise CredentialBindingError(
            "provider_mapping_changed：当前 provider 映射 endpoints 为空"
            "（拒绝发送）",
            code="provider_mapping_changed")
    if enforce_https and not endpoint_https(api_url):
        # https 强制优先：http 目标在端点匹配之前即被拒（稳定 https_required）。
        raise CredentialBindingError(
            "https_required：正式端点默认要求 HTTPS（拒绝发送）",
            code="https_required")
    frozen_ok = endpoint_in(api_url, fe)
    cur_ok = endpoint_in(api_url, cur)
    if not frozen_ok or not cur_ok:
        raise CredentialBindingError(
            "provider_mapping_changed：端点不在冻结/当前受控允许端点内（拒绝发送）",
            code="provider_mapping_changed")


def reject_raw_override_key(override: dict | None) -> None:
    """raw `override['llm_api_key']` 明文密钥一律拒绝（不可冻结/不可发送）。"""
    key = (override or {}).get("llm_api_key")
    if key is not None and str(key) != "":
        raise CredentialBindingError(
            "raw_credential_override_forbidden：不允许 override['llm_api_key'] "
            "用明文密钥绕过凭据绑定；请配置受控 provider + credential_env"
            "（错误信息不输出密钥值）",
            code="raw_credential_override_forbidden")


def settings_global_secret() -> str:
    """旧兼容模式唯一凭据来源（settings.llm_api_key；不含值泄露断言职责在调用侧）。"""
    from app.config import settings
    return str(getattr(settings, "llm_api_key", "") or "")
