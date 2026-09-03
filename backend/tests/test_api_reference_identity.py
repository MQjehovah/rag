"""Phase 7A：Endpoint 确定性身份测试（纯函数，无 IO / 无 LLM）。"""
from __future__ import annotations

import pytest

from app.core.wiki_skills.api_reference.identity import (
    build_endpoint_id,
    build_endpoint_section_key,
    normalize_api_path,
    normalize_http_method,
    normalize_version_scope,
)


# ---------------------------------------------------------------------------
# HTTP method
# ---------------------------------------------------------------------------


def test_method_uppercased_and_get_get_identical():
    assert normalize_http_method("get") == "GET"
    assert normalize_http_method(" GET ") == "GET"
    assert normalize_http_method("GET") == normalize_http_method("get")


@pytest.mark.parametrize("bad", ["FETCH", "PURGE", "", "g e t", "get1", None, 1])
def test_unsupported_method_rejected(bad):
    with pytest.raises(ValueError):
        normalize_http_method(bad)


def test_all_standard_methods_accepted():
    from app.core.wiki_skills.api_reference.identity import HTTP_METHODS

    for m in HTTP_METHODS:
        assert normalize_http_method(m) == m


# ---------------------------------------------------------------------------
# path 规范化
# ---------------------------------------------------------------------------


def test_path_prepends_slash_and_collapses_and_trims():
    assert normalize_api_path("users") == "/users"
    assert normalize_api_path("//users//x//") == "/users/x"
    assert normalize_api_path("/users/{userId}/") == "/users/{userId}"


def test_path_root_allowed():
    assert normalize_api_path("/") == "/"
    assert normalize_api_path("//") == "/"


def test_path_preserves_case_and_param_names():
    # 不统一转小写；不把 {userId} 改成 {id}。
    assert normalize_api_path("/USERS/{UserId}") == "/USERS/{UserId}"


@pytest.mark.parametrize("bad", [
    "http://example.com/users",
    "https://example.com/users?q=1",
    "/users?page=1",
    "/users#frag",
    "/users/#/x",
    "/users with space",
    "",
    "   ",
    None,
    42,
])
def test_full_url_query_fragment_and_invalid_rejected(bad):
    with pytest.raises(ValueError):
        normalize_api_path(bad)


# ---------------------------------------------------------------------------
# version_scope
# ---------------------------------------------------------------------------


def test_version_scope_defaults():
    assert normalize_version_scope(None) == "unversioned"
    assert normalize_version_scope("") == "unversioned"
    assert normalize_version_scope("   ") == "unversioned"


def test_version_scope_case_and_whitespace_normalized():
    assert normalize_version_scope("v1") == "v1"
    assert normalize_version_scope("V1") == "v1"
    assert normalize_version_scope(" v1 ") == "v1"


def test_v1_v2_scopes_isolated():
    id_v1 = build_endpoint_id("GET", "/users", "v1")
    id_v2 = build_endpoint_id("GET", "/users", "v2")
    assert id_v1 != id_v2
    assert id_v1 == build_endpoint_id("GET", "/users", "V1")


@pytest.mark.parametrize("bad", ["v1 beta", "v1!", "-v1", "版本1", "a" * 65])
def test_version_scope_invalid_chars_rejected(bad):
    with pytest.raises(ValueError):
        normalize_version_scope(bad)


# ---------------------------------------------------------------------------
# endpoint_id / section_key 确定性
# ---------------------------------------------------------------------------


def test_endpoint_id_deterministic_and_unique_per_method():
    a = build_endpoint_id("get", "/users", None)
    b = build_endpoint_id("GET", "/users", "unversioned")
    assert a == b
    assert a != build_endpoint_id("POST", "/users", None)
    assert a != build_endpoint_id("GET", "/groups", None)
    # method/path/scope 中任一不同 → id 不同。
    assert a != build_endpoint_id("GET", "/users", "v2")


def test_endpoint_id_format_stable():
    assert build_endpoint_id("GET", "/users/{userId}", "v1") == \
        "GET /users/{userId} [v1]"
    assert build_endpoint_id("DELETE", "/users", None) == \
        "DELETE /users [unversioned]"


def test_section_key_deterministic_and_case_insensitive_method():
    k1 = build_endpoint_section_key("get", "/users/{UserId}", "v1")
    k2 = build_endpoint_section_key("GET", "/users/{UserId}", "V1")
    k3 = build_endpoint_section_key("GET", "/users/{UserId}", "v1")
    assert k1 == k2 == k3
    assert k1 != build_endpoint_section_key("POST", "/users/{UserId}", "v1")
    assert k1 != build_endpoint_section_key("GET", "/users/{userId}", "v1")


def test_repeated_generation_is_stable():
    for _ in range(5):
        assert build_endpoint_id("GET", "/a/{b}", "v1") == \
            build_endpoint_id("GET", "/a/{b}", "v1")
        assert build_endpoint_section_key("GET", "/a/{b}", "v1") == \
            build_endpoint_section_key("GET", "/a/{b}", "v1")


def test_endpoint_id_and_section_key_agree_on_version_separation():
    assert build_endpoint_section_key("GET", "/users", "v1") != \
        build_endpoint_section_key("GET", "/users", "v2")
    assert build_endpoint_id("GET", "/users", "v1") != \
        build_endpoint_id("GET", "/users", "v2")
