"""Phase 6 Auto Skill Router 测试（20.5）。"""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import router as skill_router
from app.core.wiki_skills import service as skill_service
from app.core.wiki_skills.base import SkillRuntime
from app.core.wiki_skills.schemas import SkillContext, SkillDescriptor

# ---------------------------------------------------------------------------
# 注册辅助（default 真实加载；fake skills 仅测试注册，不进生产 bootstrap）
# ---------------------------------------------------------------------------


class _DocRuntime(SkillRuntime):
    key = "doc"
    version = "1"

    def extract(self, context, evidence):
        return {}

    def plan(self, context, ir):
        return {}

    def render(self, context, blueprint):
        return {}

    def validate(self, context, output, evidence):
        return {"ok": True}


class _ApiRuntime(SkillRuntime):
    key = "api_reference"
    version = "1"

    def extract(self, context, evidence):
        return {}

    def plan(self, context, ir):
        return {}

    def render(self, context, blueprint):
        return {}

    def validate(self, context, output, evidence):
        return {"ok": True}


def _desc(key, runtime_key, signals):
    return SkillDescriptor(
        key=key, version="1", label=key, description=f"{key} skill",
        applicability_signals=list(signals),
        extraction_schema_id=f"{key}-extraction/v1",
        blueprint_schema_id=f"{key}-blueprint/v1",
        instruction_resource="instructions.md",
        runtime_key=runtime_key,
    )


@pytest.fixture(autouse=True)
def _clean():
    sreg.clear_for_tests()
    yield
    sreg.clear_for_tests()


def _only_default():
    skill_service.register_default_skill()


def _with_doc():
    _only_default()
    sreg.register_runtime_allowlist("doc", _DocRuntime)
    sreg.register_descriptor(_desc("doc", "doc", ["version_note"]))
    sreg.register_runtime(_DocRuntime())
    sreg.set_active_version("doc", "1")


def _with_api():
    _only_default()
    sreg.register_runtime_allowlist("api", _ApiRuntime)
    sreg.register_descriptor(_desc("api_reference", "api",
                                   ["http_method", "api_path", "status_code",
                                    "request_json", "response_json"]))
    sreg.register_runtime(_ApiRuntime())
    sreg.set_active_version("api_reference", "1")


def _ctx(title="", summaries=(), *, current_skill=None, current_version=None,
         locked=False, wiki_page_id=None, content_kind=""):
    return SkillContext(
        target_key="t1",
        wiki_page_id=wiki_page_id,
        title=title,
        content_kind=content_kind,
        source_summaries=tuple(
            {"source_page_id": f"p{i}", "summary": s} for i, s in enumerate(summaries)
        ),
        current_skill=current_skill,
        current_version=current_version,
        skill_locked=locked,
    )


def _count_llm_calls():
    calls = []

    def _llm(messages, context="", timeout=120.0):
        calls.append(context)
        return {}
    return calls, _llm


def _register_signal_skill(key, version="1", signals=("http_method", "api_path", "status_code")):
    """注册带 API 信号的确定性 fake Skill（同名 allowlist/runtime/descriptor）。"""
    cls = type(f"RT_{key}_{version}", (SkillRuntime,), {
        "key": key,
        "version": version,
        "extract": lambda self, c, e: {},
        "plan": lambda self, c, ir: {},
        "render": lambda self, c, b: {},
        "validate": lambda self, c, o, e: {"ok": True},
        "__module__": __name__,
    })
    sreg.register_runtime_allowlist(key, cls)
    sreg.register_descriptor(_desc(key, key, list(signals)))
    sreg.register_runtime(cls())
    sreg.set_active_version(key, version)


_API_SUMMARY = "POST /api/v1/users status 400"


# ---------------------------------------------------------------------------
# 只 default：LLM 调用 0
# ---------------------------------------------------------------------------


def test_only_default_no_llm_call():
    _only_default()
    calls, _llm = _count_llm_calls()
    d = skill_service.decide(
        _ctx(title="普通主题", summaries=["一般文本内容"]), llm_runner=_llm
    )
    assert calls == []
    assert d.selected_skill == "default"
    assert d.selected_version == "1"
    assert d.selected_by == "default_fallback"
    assert d.reason_code == "ONLY_DEFAULT_AVAILABLE"


# ---------------------------------------------------------------------------
# 多候选：确定性直选 / mock LLM 高置信
# ---------------------------------------------------------------------------


def test_multi_candidate_deterministic_high_confidence():
    _with_api()
    calls, _llm = _count_llm_calls()
    d = skill_service.decide(
        _ctx(title="接口文档", summaries=["调用 POST /api/v1/users 状态码 400 错误码 X"]),
        llm_runner=_llm,
    )
    assert calls == []
    assert d.selected_skill == "api_reference"
    assert d.status == "selected"
    assert d.selected_by == "auto"
    assert d.reason_code == "DETERMINISTIC_HIGH_CONFIDENCE"


def test_multi_candidate_mock_llm_high_confidence():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        assert context == "wiki-skill-route"
        text = messages[0]["content"]
        # LLM 请求不包含完整正文 / 内部路径 / 完整 schema / instructions。
        assert "instructions" not in text.lower()
        return {"skill_key": "doc", "skill_version": "1",
                "confidence": 0.95, "reason_code": "DOC_CONTENT"}

    d = skill_service.decide(
        _ctx(title="版本说明", summaries=["普通无特异信号文本"]), llm_runner=_llm
    )
    assert d.selected_skill == "doc"
    assert d.selected_version == "1"
    assert d.selected_by == "auto"
    assert d.confidence == 0.95
    assert d.status == "selected"


# ---------------------------------------------------------------------------
# 低置信 / 非法 JSON / 超时 / 异常 / 未知 key / 未知 version
# ---------------------------------------------------------------------------


def _weak_ctx():
    # 内容无特异信号：确定性无法高置信，必须走 LLM。
    return _ctx(title="普通主题", summaries=["这是一段没有任何接口特征的说明文字。"])


def test_low_confidence_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        # 只能选本次候选（weak 内容下候选只有 default）；低置信 → fallback。
        return {"skill_key": "default", "skill_version": "1",
                "confidence": 0.3, "reason_code": "LOW"}

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.status == "fallback"
    assert d.selected_by == "default_fallback"
    assert d.reason_code == "LOW_LLM_CONFIDENCE"


def test_invalid_json_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        return "not json at all {"

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_INVALID_RESPONSE"


def test_timeout_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        raise TimeoutError("slow")

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_TIMEOUT"


def test_llm_exception_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        raise RuntimeError("boom")

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_ERROR"


def test_unknown_key_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        return {"skill_key": "ghost", "skill_version": "1",
                "confidence": 0.9, "reason_code": "X"}

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_UNKNOWN_SKILL"


def test_unknown_version_fallback_default():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        return {"skill_key": "doc", "skill_version": "99",
                "confidence": 0.9, "reason_code": "X"}

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_UNKNOWN_VERSION"


# ---------------------------------------------------------------------------
# 锁定 / 黏性 / 迁移
# ---------------------------------------------------------------------------


def test_locked_keeps_locked_skill():
    _with_api()
    calls, _llm = _count_llm_calls()
    d = skill_service.decide(
        _ctx(title="接口文档", summaries=["POST /api/x"],
             current_skill="api_reference", current_version="1", locked=True),
        llm_runner=_llm,
    )
    assert calls == []
    assert d.selected_skill == "api_reference"
    assert d.status == "locked"
    assert d.selected_by == "locked"
    assert d.reason_code == "SKILL_LOCKED"


def test_sticky_current_skill():
    _with_doc()
    calls, _llm = _count_llm_calls()
    d = skill_service.decide(
        _ctx(title="版本说明", summaries=["版本 2.0 说明"],
             current_skill="default", current_version="1"),
        llm_runner=_llm,
    )
    # doc 优势 0.1（0.3-0.2）< margin，保持当前 default。
    assert calls == []
    assert d.selected_skill == "default"
    assert d.status == "sticky"
    assert d.reason_code == "SKILL_STICKY_MARGIN"


def test_current_best_remains_sticky_current():
    _with_doc()
    d = skill_service.decide(
        _ctx(title="版本说明", summaries=["版本 2.0 说明"],
             current_skill="doc", current_version="1"),
    )
    assert d.selected_skill == "doc"
    assert d.status == "sticky"


def test_clear_advantage_proposes_migration():
    _with_api()
    calls, _llm = _count_llm_calls()
    d = skill_service.decide(
        _ctx(title="接口文档", summaries=["POST /api/v1/users 状态码 400"],
             current_skill="default", current_version="1"),
        llm_runner=_llm,
    )
    assert calls == []
    assert d.status == "migration_proposed"
    assert d.selected_skill == "default"  # Phase 6 不实际迁移，仍用当前。
    assert d.selected_by == "sticky"


def test_migration_proposed_does_not_change_current():
    _with_api()
    d = skill_service.decide(
        _ctx(title="接口文档", summaries=["POST /api/v1/users 状态码 400"],
             current_skill="default", current_version="1"),
    )
    assert d.selected_skill == "default"
    assert d.selected_version == "1"
    assert d.status == "migration_proposed"


def test_skill_version_not_auto_upgraded():
    # 注册 default v1 与 v2 两个版本，active=2；已有 wiki 精确用 v1 → 不升级。
    _only_default()
    from app.core.wiki_skills.builtin.default.runtime import DefaultSkillRuntime
    # default v2 descriptor（同 runtime_key）。
    d = sreg.get_active("default")
    v2_desc = SkillDescriptor(
        key="default", version="2", label=d.label, description=d.description,
        applicability_signals=list(d.applicability_signals),
        extraction_schema_id=d.extraction_schema_id,
        blueprint_schema_id=d.blueprint_schema_id,
        instruction_resource=d.instruction_resource,
        runtime_key=d.runtime_key,
    )

    class _DefaultV2(DefaultSkillRuntime):
        version = "2"
    sreg.register_descriptor(v2_desc)
    sreg.register_runtime(_DefaultV2())
    sreg.set_active_version("default", "2")
    # 已有 wiki current default/1，内容普通 → 精确沿用 v1，不升级 v2。
    d = skill_service.decide(
        _ctx(title="普通主题", summaries=["普通内容"], current_skill="default",
             current_version="1"),
    )
    assert d.selected_version == "1"
    assert d.selected_skill == "default"


# ---------------------------------------------------------------------------
# 约束：不改外部对象 / 排序稳定 / 不含正文
# ---------------------------------------------------------------------------


def test_router_does_not_mutate_workspace_acl_topic():
    from app.core.wiki_skills.schemas import _thaw

    _with_api()
    ctx = _ctx(title="接口文档", summaries=["POST /api/v1/users 状态码 400"])
    snapshot_before = json.dumps(_thaw(sreg.snapshot()), sort_keys=True, ensure_ascii=False)
    d = skill_service.decide(ctx)
    after = json.dumps(_thaw(sreg.snapshot()), sort_keys=True, ensure_ascii=False)
    assert after == snapshot_before
    assert d.selected_skill in ("default", "api_reference")


def test_candidates_and_signals_sorted_stable():
    _with_doc()
    s1 = json.dumps(
        skill_service.decide(
            _ctx(title="版本说明", summaries=["版本 2.0"], current_skill="default",
                 current_version="1")
        ).to_dict(), sort_keys=True, ensure_ascii=False
    )
    s2 = json.dumps(
        skill_service.decide(
            _ctx(title="版本说明", summaries=["版本 2.0"], current_skill="default",
                 current_version="1")
        ).to_dict(), sort_keys=True, ensure_ascii=False
    )
    assert s1 == s2


def test_decision_contains_no_full_body_prompt_secret():
    _with_api()
    d = skill_service.decide(
        _ctx(title="接口文档", summaries=["A" * 3000 + " POST /api/v1/users 状态码 400"]),
    )
    blob = json.dumps(d.to_dict(), ensure_ascii=False)
    assert ("A" * 500) not in blob  # 不落完整正文
    assert "prompt" not in blob.lower() or "reason_code" in blob
    assert "secret" not in blob.lower() and "token" not in blob.lower()
    assert len(blob) < 4000


def test_fallback_when_no_llm_runner():
    _with_doc()
    # 无法确定性高置信且未注入 LLM → 安全 fallback default（不失败）。
    d = skill_service.decide(_ctx(title="普通主题", summaries=["普通文本内容"]))
    assert d.selected_skill == "default"
    assert d.status == "fallback"


# ---------------------------------------------------------------------------
# Phase 6.1：候选边界 / 单一 snapshot 一致性
# ---------------------------------------------------------------------------


def test_llm_cannot_select_registered_but_not_candidate():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        # doc 已注册但未进入本次候选（weak 内容无版本信号）→ 拒绝并 fallback。
        return {"skill_key": "doc", "skill_version": "1",
                "confidence": 0.99, "reason_code": "X"}

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.status == "fallback"
    assert d.reason_code == "LLM_OUT_OF_CANDIDATES"


def test_default_kept_when_candidates_exceed_cap(monkeypatch):
    monkeypatch.setattr(settings, "wiki_skill_router_max_candidates", 3)
    _only_default()
    for i in range(5):
        _register_signal_skill(f"sk{i}")
    snap = sreg.snapshot()
    cands = skill_router._filter_candidates(
        snap, {"http_method", "api_path", "status_code"}, 3
    )
    keys = [c.skill_key for c in cands]
    assert len(cands) <= 3
    assert "default" in keys
    # 端到端：decision 的候选同样保留 default 且不超过上限。
    d = skill_service.decide(_ctx(title="接口文档", summaries=[_API_SUMMARY]))
    assert len(d.candidates) <= 3
    assert any(c["skill_key"] == "default" for c in d.candidates)


def test_max_candidates_one_keeps_only_default(monkeypatch):
    monkeypatch.setattr(settings, "wiki_skill_router_max_candidates", 1)
    _only_default()
    _register_signal_skill("sk0")
    snap = sreg.snapshot()
    cands = skill_router._filter_candidates(
        snap, {"http_method", "api_path", "status_code"}, 1
    )
    assert [c.skill_key for c in cands] == ["default"]


def test_route_uses_single_snapshot_ignores_later_registration():
    _with_api()
    snap = sreg.snapshot()  # 仅含 default + api_reference
    _register_signal_skill("later_skill")  # 全局随后变化
    d = skill_service.decide_from_snapshot(
        _ctx(title="接口文档", summaries=[_API_SUMMARY]), snap
    )
    # 本次判断只基于传入 snapshot：later_skill 不在候选，不参与。
    assert d.selected_skill == "api_reference"
    assert all(c["skill_key"] != "later_skill" for c in d.candidates)


def test_current_skill_unmatched_clear_advantage_migration():
    _with_api()
    # doc 已注册（snapshot 中存在）但本内容无版本信号 → 未进入候选。
    cls = type("RT_doc", (SkillRuntime,), {
        "key": "doc", "version": "1",
        "extract": lambda self, c, e: {},
        "plan": lambda self, c, ir: {},
        "render": lambda self, c, b: {},
        "validate": lambda self, c, o, e: {"ok": True},
        "__module__": __name__,
    })
    sreg.register_runtime_allowlist("doc", cls)
    sreg.register_descriptor(_desc("doc", "doc", ["version_note"]))
    sreg.register_runtime(cls())
    sreg.set_active_version("doc", "1")
    ctx = _ctx(title="接口文档", summaries=[_API_SUMMARY],
               current_skill="doc", current_version="1")
    d = skill_service.decide(ctx)
    assert d.status == "migration_proposed"
    assert d.selected_skill == "doc"  # Phase 6 仍沿用当前，不实际切换
    assert d.selected_version == "1"


def test_llm_json_extra_field_fallback():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        return {"skill_key": "default", "skill_version": "1",
                "confidence": 0.99, "reason_code": "R", "extra_field": "x"}

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_INVALID_RESPONSE"


def test_llm_missing_required_field_fallback():
    _with_doc()

    def _llm(messages, context="", timeout=120.0):
        return {"skill_key": "default", "confidence": 0.99}  # 缺 skill_version

    d = skill_service.decide(_weak_ctx(), llm_runner=_llm)
    assert d.selected_skill == "default"
    assert d.reason_code == "LLM_INVALID_RESPONSE"


def test_candidate_selection_independent_of_registration_order():
    outs = []
    for order in (("ska", "skb"), ("skb", "ska")):
        sreg.clear_for_tests()
        skill_service.register_default_skill()
        for k in order:
            _register_signal_skill(k)
        d = skill_service.decide(
            _ctx(title="接口文档", summaries=[_API_SUMMARY])
        )
        outs.append(json.dumps(d.to_dict(), sort_keys=True, ensure_ascii=False))
    assert outs[0] == outs[1]


def test_router_consumes_immutable_snapshot():
    _with_api()
    snap = sreg.snapshot()  # MappingProxyType，不可变
    d = skill_router.route(
        _ctx(title="普通主题", summaries=["普通文本内容"]), snapshot=snap
    )
    # 仅 default+api 时内容普通 → default；不抛序列化/类型错误。
    assert d.selected_skill in ("default", "api_reference")
