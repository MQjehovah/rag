"""Phase 6 Skill Registry 测试（20.2）。"""
from __future__ import annotations

import pytest

from app.core.wiki_skills import registry as reg
from app.core.wiki_skills.base import SkillRuntime
from app.core.wiki_skills.registry import (
    SkillActiveVersionError,
    SkillDefinitionConflict,
    SkillError,
    SkillNotFound,
    SkillRuntimeError,
)
from app.core.wiki_skills.schemas import SkillDescriptor


class FakeRuntime(SkillRuntime):
    key = "fake"
    version = "1"

    def extract(self, context, evidence):  # noqa: D102
        return {}

    def plan(self, context, ir):  # noqa: D102
        return {}

    def render(self, context, blueprint):  # noqa: D102
        return {}

    def validate(self, context, output, evidence):  # noqa: D102
        return {"ok": True}


class FakeV2Runtime(FakeRuntime):
    version = "2"


def _desc(key="fake", version="1", runtime_key="fake", signals=("generic_text",),
          label="fake"):
    return SkillDescriptor(
        key=key, version=version, label=label, description="fake skill",
        applicability_signals=list(signals),
        extraction_schema_id=f"{key}-extraction/v1",
        blueprint_schema_id=f"{key}-blueprint/v1",
        instruction_resource="instructions.md",
        runtime_key=runtime_key,
    )


@pytest.fixture(autouse=True)
def _clean():
    reg.clear_for_tests()
    yield
    reg.clear_for_tests()


def _register_default_style():
    reg.register_runtime_allowlist("fake", FakeRuntime)
    reg.register_descriptor(_desc())
    reg.register_runtime(FakeRuntime())
    reg.set_active_version("fake", "1")


def test_default_registration_success():
    _register_default_style()
    d = reg.get("fake", "1")
    assert d.key == "fake" and d.version == "1"


def test_duplicate_same_definition_idempotent():
    _register_default_style()
    reg.register_descriptor(_desc())  # 相同定义 → no-op
    assert reg.get("fake", "1").key == "fake"


def test_same_key_version_different_definition_conflict():
    reg.register_descriptor(_desc())
    with pytest.raises(SkillDefinitionConflict):
        reg.register_descriptor(_desc(label="changed"))


def test_multi_version_coexist():
    reg.register_descriptor(_desc())
    reg.register_descriptor(_desc(version="2"))
    assert set(reg.registered_versions("fake")) == {"1", "2"}


def test_active_version_explicit():
    reg.register_runtime_allowlist("fake", FakeRuntime)
    reg.register_descriptor(_desc(version="1"))
    reg.register_descriptor(_desc(version="2"))
    # 未设置 active → fail closed（get 无 active）。
    with pytest.raises(SkillNotFound):
        reg.get("fake")
    reg.set_active_version("fake", "2")
    assert reg.get_active("fake").version == "2"
    assert reg.get("fake").version == "2"


def test_registration_order_does_not_change_active():
    # 先注册 v2 后注册 v1，active 仍指向显式设置的 v2。
    reg.register_descriptor(_desc(version="2"))
    reg.set_active_version("fake", "2")
    reg.register_descriptor(_desc(version="1"))
    assert reg.get_active("fake").version == "2"


def test_active_points_unknown_version_rejected():
    with pytest.raises(SkillActiveVersionError):
        reg.set_active_version("fake", "99")


def test_runtime_key_not_allowlisted_rejected():
    d = SkillDescriptor(
        key="evil", version="1", label="x", runtime_key="not_allowed",
        applicability_signals=["generic_text"],
    )
    reg.register_descriptor(d)
    with pytest.raises(SkillRuntimeError):
        reg.register_runtime(FakeRuntime())


class WrongRuntime(FakeRuntime):
    key = "other"


def test_runtime_key_version_mismatch_rejected():
    reg.register_runtime_allowlist("fake", FakeRuntime)
    reg.register_descriptor(_desc())
    # runtime key 与 descriptor 不一致。
    with pytest.raises(SkillRuntimeError):
        reg.register_runtime(WrongRuntime())
    # version 不一致。
    class WrongVersion(FakeRuntime):
        version = "9"
    with pytest.raises(SkillRuntimeError):
        reg.register_runtime(WrongVersion())


def test_mutate_after_register_does_not_affect_registry():
    d = _desc()
    reg.register_descriptor(d)
    # descriptor 是 frozen；applicability_signals 已转 tuple。
    assert isinstance(reg.get("fake", "1").applicability_signals, tuple)


def test_snapshot_immutable_and_sorted():
    from types import MappingProxyType

    _register_default_style()
    snap = reg.snapshot()
    assert sorted(snap.keys()) == list(snap.keys())
    assert isinstance(snap, MappingProxyType)
    # 顶层不可修改。
    with pytest.raises(TypeError):
        snap["fake"] = "x"
    info = snap["fake"]
    assert isinstance(info, MappingProxyType)
    # Skill 信息层不可修改。
    with pytest.raises(TypeError):
        info["active_version"] = "2"
    versions = info["versions"]
    assert isinstance(versions, MappingProxyType)
    # versions 层不可修改。
    with pytest.raises(TypeError):
        versions["1"] = {}
    desc = versions["1"]
    assert isinstance(desc, MappingProxyType)
    # descriptor 字段层不可修改。
    with pytest.raises(TypeError):
        desc["label"] = "tampered"
    # descriptor 内 applicability_signals（已冻结为 tuple，不可改）。
    assert not hasattr(desc["applicability_signals"], "append")
    with pytest.raises(TypeError):
        desc["applicability_signals"][0] = "x"


def test_list_stable_sorted():
    reg.register_descriptor(_desc(key="b-fake"))
    reg.register_descriptor(_desc(key="a-fake"))
    keys = [s["key"] for s in reg.list_skills()]
    assert keys == ["a-fake", "b-fake"]


def test_snapshot_reverse_registration_order_same_serialization():
    import json

    from app.core.wiki_skills.schemas import _thaw

    serials = []
    for order in (("v1", "v2"), ("v2", "v1")):
        reg.clear_for_tests()
        reg.register_runtime_allowlist("fake", FakeRuntime)
        for tag in order:
            cls = FakeRuntime if tag == "v1" else FakeV2Runtime
            reg.register_descriptor(_desc(version=cls.version))
            reg.register_runtime(cls())
        reg.set_active_version("fake", "2")
        serials.append(json.dumps(_thaw(reg.snapshot()), sort_keys=True, ensure_ascii=False))
    assert serials[0] == serials[1]


def test_snapshot_versions_order_stable_not_registration_order():
    from types import MappingProxyType

    versions = []
    for order in (("v1", "v2"), ("v2", "v1")):
        reg.clear_for_tests()
        reg.register_runtime_allowlist("fake", FakeRuntime)
        for tag in order:
            cls = FakeRuntime if tag == "v1" else FakeV2Runtime
            reg.register_descriptor(_desc(version=cls.version))
            reg.register_runtime(cls())
        reg.set_active_version("fake", "1")
        snap = reg.snapshot()
        assert isinstance(snap, MappingProxyType)
        versions.append(list(snap["fake"]["versions"].keys()))
    assert versions[0] == versions[1] == ["1", "2"]


def test_controlled_errors_no_keyerror():
    with pytest.raises(SkillNotFound):
        reg.get("nonexistent")
    with pytest.raises(SkillNotFound):
        reg.get("fake", "9")
    assert reg.get_active("nonexistent") is None


def test_clear_does_not_pollute():
    _register_default_style()
    reg.clear_for_tests()
    with pytest.raises(SkillNotFound):
        reg.get("fake", "1")
