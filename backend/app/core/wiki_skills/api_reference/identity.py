"""Phase 7A：Endpoint 稳定身份（纯确定性函数，无 IO / 无 LLM）。

规则摘要：
- method 大写并限定标准 HTTP method（GET/POST/PUT/PATCH/DELETE/HEAD/
  OPTIONS/TRACE/CONNECT）；
- path 只接受 API path：拒绝完整 URL / query string / fragment；补开头 `/`、
  合并重复 `/`、根路径外去除末尾 `/`；不统一转小写；不修改 `{userId}` 等
  参数名；非法或空 path fail closed；
- version_scope 空值规范为 `unversioned`；去空白 + 大小写规范化（V1→v1），
  v1/v2 保持隔离；不凭路径猜测版本；
- endpoint_id 唯一依据 = normalized_method + normalized_path +
  normalized_version_scope；同一输入恒得相同 endpoint_id 与 section_key。
"""
from __future__ import annotations

import re

# 标准 HTTP method（不允许扩展）。
HTTP_METHODS = (
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT",
)

DEFAULT_VERSION_SCOPE = "unversioned"

# version_scope 规范化后允许的字符集（小写字母/数字/_ . -）。
_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# path 中的非法字符（空格与控制字符直接拒绝；其余按 URL path 字符处理）。
_PATH_BAD = re.compile(r"[\x00-\x20\x7f]")
# 参数占位符（{...}）整体保护，校验时只检查形式，不读取/改写内容。
_PATH_PARAM_RE = re.compile(r"\{[^{}]+\}")


def normalize_http_method(method: str) -> str:
    """转大写并校验标准 HTTP method；非法 method fail closed。"""
    if not isinstance(method, str):
        raise ValueError("method must be a string")
    value = method.strip().upper()
    if value not in HTTP_METHODS:
        raise ValueError(
            f"unsupported HTTP method {method!r}; allowed: {', '.join(HTTP_METHODS)}")
    return value


def normalize_api_path(path: str) -> str:
    """规范化 API path。

    - 必须是 API path：拒绝完整 URL（含 scheme://）、query string（?）与
      fragment（#）；
    - 补齐开头 `/`；合并重复 `/`；根路径外去除末尾 `/`；
    - 不统一转小写；不修改参数名与大小写；非法或空 path fail closed。
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    value = path.strip()
    if not value:
        raise ValueError("path must not be empty")
    if "://" in value:
        raise ValueError(f"path must be an API path, got full URL-like input: {path!r}")
    if "?" in value:
        raise ValueError(f"path must not contain a query string: {path!r}")
    if "#" in value:
        raise ValueError(f"path must not contain a fragment: {path!r}")
    if _PATH_BAD.search(value):
        raise ValueError(f"path contains spaces or control characters: {path!r}")
    if not value.startswith("/"):
        value = "/" + value
    # 合并重复 '/'；根路径外去除末尾 '/'。
    collapsed = re.sub(r"/{2,}", "/", value)
    if collapsed != "/":
        collapsed = collapsed.rstrip("/")
    if collapsed == "" or _PATH_BAD.search(collapsed):
        raise ValueError(f"invalid path: {path!r}")
    return collapsed


def normalize_version_scope(version_scope: str | None) -> str:
    """版本作用域规范化：空值→unversioned；去空白；大小写规范化。"""
    if version_scope is None:
        return DEFAULT_VERSION_SCOPE
    if not isinstance(version_scope, str):
        raise ValueError("version_scope must be a string or None")
    value = version_scope.strip().lower()
    if not value:
        return DEFAULT_VERSION_SCOPE
    if len(value) > 64:
        raise ValueError("version_scope too long")
    if not _SCOPE_RE.match(value):
        raise ValueError(
            f"invalid version_scope {version_scope!r}: only [a-z0-9._-] allowed")
    return value


def build_endpoint_id(method: str, path: str, version_scope: str | None = None) -> str:
    """endpoint_id：normalized_method + normalized_path + normalized_version_scope。

    形如 `GET /users/{userId} [v1]`；同一输入恒得相同结果。参数名与大小写保留。
    """
    m = normalize_http_method(method)
    p = normalize_api_path(path)
    s = normalize_version_scope(version_scope)
    return f"{m} {p} [{s}]"


def build_endpoint_section_key(
    method: str, path: str, version_scope: str | None = None,
) -> str:
    """endpoint 的稳定 section_key（用于知识库分组/锚定）。

    仅 method 转小写（method 大小写无语义），path 与 version_scope 与
    endpoint_id 保持同一规范化结果，因此同输入恒得同 key。
    """
    m = normalize_http_method(method)
    p = normalize_api_path(path)
    s = normalize_version_scope(version_scope)
    return f"api_endpoint|{m.lower()}|{p}|{s}"
