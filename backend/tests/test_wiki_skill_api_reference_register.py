"""Phase 7C.2：api_reference 生产 Skill 注册与选择测试。

- builtin/api_reference 资源可被 Loader 加载（default 必须仍在）；
- service allowlist 显式映射 api_reference→ApiReferenceRuntime，active=1；
- 新 Wiki 的高置信 API 信号 → 确定性直选 api_reference；generic → default；
- locked Wiki 不切换；migration_proposed 时候选含 api_reference（仍沿用当前）；
- 注册顺序不影响结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.wiki_skills import loader, registry, service
from app.core.wiki_skills.api_reference.runtime import ApiReferenceRuntime
from app.core.wiki_skills.builtin.default.runtime import DefaultSkillRuntime
from app.core.wiki_skills.schemas import SkillContext, SkillDescriptor

_BACKEND = Path(__file__).resolve().parent.parent

_API_SIGNALS = [
    "http_method", "api_path", "status_code", "error_code", "request_json",
    "response_json", "parameter_table",
]


def _descriptor(key, signals):
    return SkillDescriptor(
        key=key, version="1", label=key,
        description=f"{key} test descriptor",
        applicability_signals=signals,
        extraction_schema_id=f"{key}-extraction/v1",
        blueprint_schema_id=f"{key}-blueprint/v1",
        instruction_resource="instructions.md",
        runtime_key=key,
    )


@pytest.fixture()
def skills_loaded(monkeypatch):
    registry.clear_for_tests()
    registry.register_runtime_allowlist("default", DefaultSkillRuntime)
    registry.register_runtime_allowlist("api_reference", ApiReferenceRuntime)
    registry.register_descriptor(_descriptor("default", ["generic_text"]))
    registry.register_descriptor(_descriptor("api_reference", _API_SIGNALS))
    registry.set_active_version("default", "1")
    registry.set_active_version("api_reference", "1")
    yield registry.snapshot()
    registry.clear_for_tests()
    service.register_builtin_skills()  # 恢复真实 builtin（default+api_reference）


def _ctx(**kw):
    base = dict(target_key="t1", title="用户 API 接口文档",
                content_kind="generic_text")
    base.update(kw)
    return SkillContext(**base)


def test_builtin_api_reference_resource_loads():
    result = loader.load_all()
    assert "api_reference" in result and "default" in result
    desc = result["api_reference"]["1"]
    assert desc.runtime_key == "api_reference"
    assert set(_API_SIGNALS) <= set(desc.applicability_signals)


def test_service_registers_builtin_skills():
    registry.clear_for_tests()
    keys = service.register_builtin_skills()
    assert "api_reference" in keys and "default" in keys
    assert registry.get_active("api_reference").version == "1"
    assert registry.get_active("default").version == "1"
    runtime = registry.get_runtime("api_reference", "1")
    assert isinstance(runtime, ApiReferenceRuntime)


def test_new_api_wiki_deterministically_selects_api_reference(skills_loaded):
    d = service.decide(_ctx(
        title="用户管理 API",
        source_summaries=({"source_page_id": "p", "summary":
                           "GET /api/users 返回用户列表。状态码：200。"
                           "请求参数：{id} 参数说明见下表。"},),
    ))
    assert d.selected_skill == "api_reference"
    assert d.status == "selected"
    assert d.reason_code == "DETERMINISTIC_HIGH_CONFIDENCE"


def test_generic_text_still_selects_default(skills_loaded):
    d = service.decide(_ctx(
        title="日常运营手册",
        source_summaries=({"source_page_id": "p",
                           "summary": "普通主题的说明文字。"},),
    ))
    assert d.selected_skill == "default"
    assert d.status in ("selected", "fallback")


def test_locked_wiki_never_switches(skills_loaded):
    d = service.decide(_ctx(
        title="用户管理 API",
        current_skill="default", current_version="1", skill_locked=True,
        source_summaries=({"source_page_id": "p", "summary":
                           "GET /api/users 状态码：200"},),
    ))
    assert d.selected_skill == "default"
    assert d.status == "locked"


def test_migration_proposed_keeps_current_and_lists_api(skills_loaded):
    d = service.decide(_ctx(
        title="旧接口手册",
        current_skill="default", current_version="1",
        source_summaries=({"source_page_id": "p", "summary":
                           "GET /api/users 状态码：200 请求体 响应体 参数表"},),
    ))
    assert d.status == "migration_proposed"
    assert d.selected_skill == "default"  # v3 验证后迁移前仍沿用当前
    keys = [c["skill_key"] for c in d.candidates]
    assert "api_reference" in keys


def _plain(value):
    if hasattr(value, "items"):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def test_registration_order_does_not_change_snapshot():
    def _build(rev=False):
        regs = [
            _descriptor("default", ["generic_text"]),
            _descriptor("api_reference", _API_SIGNALS),
        ]
        if rev:
            regs.reverse()
        return regs

    registry.clear_for_tests()
    for desc in _build(False):
        registry.register_descriptor(desc)
    registry.set_active_version("default", "1")
    registry.set_active_version("api_reference", "1")
    snap_a = _plain(registry.snapshot())

    registry.clear_for_tests()
    for desc in _build(True):
        registry.register_descriptor(desc)
    registry.set_active_version("api_reference", "1")
    registry.set_active_version("default", "1")
    snap_b = _plain(registry.snapshot())

    assert snap_a == snap_b
    assert sorted(snap_a.keys()) == ["api_reference", "default"]
    registry.clear_for_tests()
    service.register_builtin_skills()
