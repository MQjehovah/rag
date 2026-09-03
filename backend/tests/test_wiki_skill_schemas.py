"""Phase 6 DTO 契约测试（20.1：往返 / 不可变 / JSON 安全 / 非法拒绝）。"""
from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from app.core.wiki_skills.schemas import (
    DECISION_STATUS_VALUES,
    SELECTED_BY_VALUES,
    SKILL_DECISION_SCHEMA,
    ApplicabilitySignal,
    DecisionStatus,
    SelectedBy,
    SkillCandidate,
    SkillContext,
    SkillDecision,
    SkillDescriptor,
)

VALID_DESCRIPTOR = {
    "key": "default",
    "version": "1",
    "label": "默认知识结构",
    "description": "通用主题 Wiki 的默认内容结构",
    "applicability_signals": ["generic_text"],
    "extraction_schema_id": "default-extraction/v1",
    "blueprint_schema_id": "default-blueprint/v1",
    "instruction_resource": "instructions.md",
    "runtime_key": "default",
}


def _valid_decision() -> SkillDecision:
    return SkillDecision(
        target_key="k1",
        wiki_page_id="w1",
        selected_skill="default",
        selected_version="1",
        selected_by="default_fallback",
        confidence=1.0,
        status="selected",
        reason_code="ONLY_DEFAULT_AVAILABLE",
    )


def test_descriptor_json_roundtrip():
    d = SkillDescriptor(**VALID_DESCRIPTOR)
    raw = json.dumps(d.to_dict(), ensure_ascii=False)
    restored = SkillDescriptor.from_dict(json.loads(raw))
    assert restored.to_dict() == d.to_dict()


def test_decision_json_roundtrip():
    d = _valid_decision()
    raw = json.dumps(d.to_dict(), ensure_ascii=False)
    restored = SkillDecision.from_dict(json.loads(raw))
    assert restored.to_dict() == d.to_dict()
    assert restored.schema_version == SKILL_DECISION_SCHEMA


def test_context_json_roundtrip():
    ctx = SkillContext(
        workspace_id="ws1",
        wiki_page_id="w1",
        target_key="k1",
        title="标题",
        source_page_ids=("p1", "p2"),
        source_summaries=({"source_page_id": "p1", "summary": "摘要"},),
        current_skill="default",
        current_version="1",
        skill_locked=True,
    )
    raw = json.dumps(ctx.to_dict(), ensure_ascii=False)
    restored = SkillContext.from_dict(json.loads(raw))
    assert restored.to_dict() == ctx.to_dict()


def test_enum_restored_after_roundtrip():
    assert SelectedBy("default_fallback").value == "default_fallback"
    assert DecisionStatus("migration_proposed").value == "migration_proposed"
    d = _valid_decision().to_dict()
    assert d["selected_by"] in SELECTED_BY_VALUES
    assert d["status"] in DECISION_STATUS_VALUES
    # Enum 成员可序列化且值保持。
    raw = json.dumps({"s": SelectedBy.LOCKED.value, "st": DecisionStatus.STICKY.value})
    assert json.loads(raw) == {"s": "locked", "st": "sticky"}


def test_deep_immutable_decision():
    d = _valid_decision()
    with pytest.raises(AttributeError):
        d.target_key = "other"
    # candidates 存 tuple of 深层不可变 MappingProxy（list→tuple、dict→MappingProxy）。
    d2 = SkillDecision(
        target_key="t", selected_skill="default", selected_version="1",
        selected_by="auto", confidence=1.0, status="selected",
        reason_code="X", candidates=({"a": {"b": [1, 2]}},),
    )
    inner = d2.candidates[0]
    with pytest.raises(TypeError):
        inner["a"] = "x"
    assert isinstance(inner["a"], MappingProxyType)
    # list 已被冻结为 tuple（无 append）。
    assert not hasattr(inner["a"]["b"], "append")


def test_descriptor_deep_immutable():
    d = SkillDescriptor(**VALID_DESCRIPTOR)
    assert isinstance(d.applicability_signals, tuple)
    with pytest.raises(AttributeError):
        d.applicability_signals = ["x"]


def test_nan_infinity_rejected():
    with pytest.raises(ValueError):
        SkillCandidate(skill_key="a", skill_version="1", deterministic_score=float("nan"))
    with pytest.raises(ValueError):
        SkillDecision(target_key="t", selected_skill="default", selected_version="1",
                      confidence=float("inf"), selected_by="auto", status="selected")
    with pytest.raises(ValueError):
        ApplicabilitySignal(signal_type="http_method", strength=1.5)


def test_callable_orm_object_rejected():
    with pytest.raises(ValueError):
        SkillContext(source_summaries=({"summary": lambda x: x},))
    with pytest.raises(ValueError):
        SkillDecision(target_key="t", candidates=({"x": object()},))

    class FakeORM:
        _sa_instance_state = object()
    with pytest.raises(ValueError):
        SkillContext(source_summaries=({"summary": FakeORM()},))


def test_bytes_datetime_rejected():
    import datetime

    with pytest.raises(ValueError):
        SkillContext(source_summaries=({"summary": b"bytes"},))
    with pytest.raises(ValueError):
        SkillContext(source_summaries=({"summary": datetime.datetime.now()},))


def test_invalid_key_version_rejected():
    with pytest.raises(ValueError):
        SkillDescriptor(key="Bad Key", version="1", label="x")
    with pytest.raises(ValueError):
        SkillDescriptor(key="", version="1", label="x")
    with pytest.raises(ValueError):
        SkillDescriptor(key="default", version="", label="x")
    with pytest.raises(ValueError):
        SkillDescriptor(key="default", version="a\x00b", label="x")


def test_invalid_confidence_rejected():
    with pytest.raises(ValueError):
        SkillCandidate(skill_key="a", skill_version="1", combined_score=2.0)
    with pytest.raises(ValueError):
        SkillDecision(target_key="t", selected_skill="default", selected_version="1",
                      confidence=-0.1, selected_by="auto", status="selected")


def test_illegal_enum_values_rejected():
    with pytest.raises(ValueError):
        SkillDecision(target_key="t", selected_skill="default", selected_version="1",
                      selected_by="hacker", status="selected")
    with pytest.raises(ValueError):
        SkillDecision(target_key="t", selected_skill="default", selected_version="1",
                      selected_by="auto", status="whatever")
    with pytest.raises(ValueError):
        ApplicabilitySignal(signal_type="x", source="remote")


def test_label_description_length_limited():
    SkillDescriptor(key="a", version="1", label="x" * 128, description="y" * 512)
    with pytest.raises(ValueError):
        SkillDescriptor(key="a", version="1", label="x" * 129)
    with pytest.raises(ValueError):
        SkillDescriptor(key="a", version="1", description="y" * 513)


def test_context_summary_bounded():
    ctx = SkillContext(
        target_key="t",
        source_summaries=tuple(
            {"source_page_id": f"p{i}", "summary": "长" * 5000} for i in range(50)
        ),
    )
    assert len(ctx.source_summaries) <= SkillContext.MAX_SUMMARIES
    for s in ctx.source_summaries:
        assert len(s["summary"]) <= SkillContext.MAX_SUMMARY_CHARS


def test_context_mappingproxy_deep_frozen():
    ctx = SkillContext(target_key="t", source_summaries=({"source_page_id": "p1", "summary": "a"},))
    s = ctx.source_summaries[0]
    assert isinstance(s, MappingProxyType)
    with pytest.raises(TypeError):
        s["summary"] = "b"
