"""Phase 6：Skill 数据契约（frozen DTO + Enum + JSON-safe）。

全部公开 DTO 满足：
- frozen dataclass；构造期深度冻结（list→tuple、dict→MappingProxyType）；
- JSON-safe（拒绝 NaN/Infinity/bytes/datetime/callable/ORM/自定义对象）；
- 有 to_dict/from_dict；Enum 往返后恢复；
- 不保存 SQLAlchemy 对象 / callable / Secret / Prompt / 完整正文 / ACL 原始数据。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

# skill-decision Artifact schema 版本（Phase 6 契约）。
SKILL_DECISION_SCHEMA = "skill-decision/v1"

# skill key 规范：^[a-z][a-z0-9_.-]*$
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")

# 受限枚举取值（DB CHECK 与 DTO 校验共用同一集合）。
SELECTED_BY_VALUES = (
    "auto", "manual", "migration", "default_fallback", "locked", "sticky",
)
DECISION_STATUS_VALUES = (
    "selected", "fallback", "locked", "sticky", "migration_proposed", "not_applicable",
)
SIGNAL_SOURCE_VALUES = ("content", "title", "metadata", "deterministic_parser")

# 文本长度上限（label/description）。
LABEL_MAX = 128
DESCRIPTION_MAX = 512


class SelectedBy(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    MIGRATION = "migration"
    DEFAULT_FALLBACK = "default_fallback"
    LOCKED = "locked"
    STICKY = "sticky"


class DecisionStatus(str, Enum):
    SELECTED = "selected"
    FALLBACK = "fallback"
    LOCKED = "locked"
    STICKY = "sticky"
    MIGRATION_PROPOSED = "migration_proposed"
    NOT_APPLICABLE = "not_applicable"


class SignalSource(str, Enum):
    CONTENT = "content"
    TITLE = "title"
    METADATA = "metadata"
    DETERMINISTIC_PARSER = "deterministic_parser"


def _freeze(value: Any) -> Any:
    """深度冻结：list→tuple、dict→MappingProxyType（递归）。"""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    return value


def _thaw(value: Any) -> Any:
    """深度解冻：tuple→list、MappingProxyType→dict（递归），恢复 JSON-safe 普通结构。"""
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _is_json_safe(value: Any) -> bool:
    """严格 JSON-safe 校验（allow_nan=False：拒绝 NaN/Infinity）。"""
    import json

    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def _assert_json_safe(value: Any, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError(f"{field_name}: NaN/Infinity not allowed")
        return
    if isinstance(value, (bytes, datetime, date)):
        raise ValueError(f"{field_name}: bytes/datetime not allowed")
    if callable(value):
        raise ValueError(f"{field_name}: callable not allowed")
    # 粗略识别 ORM/自定义对象（有 _sa_instance_state 或 __table__ 属性）。
    if hasattr(value, "_sa_instance_state") or hasattr(value, "__table__"):
        raise ValueError(f"{field_name}: ORM object not allowed")
    if isinstance(value, (list, tuple, dict, Mapping)):
        if not _is_json_safe(_thaw(value)):
            raise ValueError(f"{field_name}: not JSON-safe")
        return
    raise ValueError(f"{field_name}: unsupported type {type(value).__name__}")


def _validate_key(key: str, field_name: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"{field_name} required")
    key = key.strip()
    if not _KEY_RE.match(key):
        raise ValueError(f"{field_name} must match ^[a-z][a-z0-9_.-]*$")
    return key


def _validate_version(version: str, field_name: str) -> str:
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{field_name} required")
    version = version.strip()
    if "\x00" in version or len(version) > 64:
        raise ValueError(f"{field_name} invalid")
    return version


def _validate_confidence(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be float in [0,1]")
    if not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be float in [0,1]")
    value = float(value)
    if math.isnan(value) or math.isinf(value) or not (0.0 <= value <= 1.0):
        raise ValueError(f"{field_name} must be in [0,1]")
    return value


@dataclass(frozen=True)
class SkillDescriptor:
    """Skill 声明式描述（来自受控 YAML，不含任何可执行 Python 路径）。"""

    key: str
    version: str
    label: str = ""
    description: str = ""
    applicability_signals: tuple[str, ...] = ()
    extraction_schema_id: str = ""
    blueprint_schema_id: str = ""
    instruction_resource: str = ""
    runtime_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _validate_key(self.key, "key"))
        object.__setattr__(self, "version", _validate_version(self.version, "version"))
        label = self.label.strip()
        if len(label) > LABEL_MAX:
            raise ValueError("label too long")
        object.__setattr__(self, "label", label)
        description = self.description.strip()
        if len(description) > DESCRIPTION_MAX:
            raise ValueError("description too long")
        object.__setattr__(self, "description", description)
        signals = tuple(s.strip() for s in self.applicability_signals)
        for s in signals:
            if not s:
                raise ValueError("applicability_signals must be non-empty strings")
        object.__setattr__(self, "applicability_signals", signals)
        object.__setattr__(
            self, "extraction_schema_id", self.extraction_schema_id.strip()
        )
        object.__setattr__(self, "blueprint_schema_id", self.blueprint_schema_id.strip())
        object.__setattr__(
            self, "instruction_resource", self.instruction_resource.strip()
        )
        object.__setattr__(self, "runtime_key", self.runtime_key.strip())

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "applicability_signals": list(self.applicability_signals),
            "extraction_schema_id": self.extraction_schema_id,
            "blueprint_schema_id": self.blueprint_schema_id,
            "instruction_resource": self.instruction_resource,
            "runtime_key": self.runtime_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillDescriptor":
        allowed = {
            "key", "version", "label", "description", "applicability_signals",
            "extraction_schema_id", "blueprint_schema_id", "instruction_resource",
            "runtime_key",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            key=data.get("key", ""),
            version=data.get("version", ""),
            label=data.get("label", ""),
            description=data.get("description", ""),
            applicability_signals=tuple(data.get("applicability_signals") or ()),
            extraction_schema_id=data.get("extraction_schema_id", ""),
            blueprint_schema_id=data.get("blueprint_schema_id", ""),
            instruction_resource=data.get("instruction_resource", ""),
            runtime_key=data.get("runtime_key", ""),
        )


@dataclass(frozen=True)
class ApplicabilitySignal:
    """确定性信号（只保留最小必要摘要，不含完整正文）。"""

    signal_type: str
    value: str = ""
    strength: float = 1.0
    source: str = "content"

    def __post_init__(self) -> None:
        if not isinstance(self.signal_type, str) or not self.signal_type.strip():
            raise ValueError("signal_type required")
        object.__setattr__(self, "signal_type", self.signal_type.strip())
        object.__setattr__(self, "value", self.value[:512])
        object.__setattr__(self, "strength", _validate_confidence(self.strength, "strength"))
        source = self.source.strip()
        if source not in SIGNAL_SOURCE_VALUES:
            raise ValueError(f"source must be one of {SIGNAL_SOURCE_VALUES}")
        object.__setattr__(self, "source", source)

    def to_dict(self) -> dict:
        return {
            "signal_type": self.signal_type,
            "value": self.value,
            "strength": self.strength,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ApplicabilitySignal":
        return cls(
            signal_type=data.get("signal_type", ""),
            value=data.get("value", ""),
            strength=data.get("strength", 1.0),
            source=data.get("source", "content"),
        )


@dataclass(frozen=True)
class SkillCandidate:
    """候选 Skill 评分（确定性 + LLM + 组合）。"""

    skill_key: str
    skill_version: str
    deterministic_score: float = 0.0
    llm_score: float = 0.0
    combined_score: float = 0.0
    matched_signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_key", _validate_key(self.skill_key, "skill_key"))
        object.__setattr__(
            self, "skill_version", _validate_version(self.skill_version, "skill_version")
        )
        object.__setattr__(
            self, "deterministic_score",
            _validate_confidence(self.deterministic_score, "deterministic_score"),
        )
        object.__setattr__(
            self, "llm_score", _validate_confidence(self.llm_score, "llm_score"),
        )
        object.__setattr__(
            self, "combined_score", _validate_confidence(self.combined_score, "combined_score"),
        )
        object.__setattr__(self, "matched_signals", tuple(self.matched_signals))

    def to_dict(self) -> dict:
        return {
            "skill_key": self.skill_key,
            "skill_version": self.skill_version,
            "deterministic_score": self.deterministic_score,
            "llm_score": self.llm_score,
            "combined_score": self.combined_score,
            "matched_signals": list(self.matched_signals),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillCandidate":
        return cls(
            skill_key=data.get("skill_key", ""),
            skill_version=data.get("skill_version", ""),
            deterministic_score=data.get("deterministic_score", 0.0),
            llm_score=data.get("llm_score", 0.0),
            combined_score=data.get("combined_score", 0.0),
            matched_signals=tuple(data.get("matched_signals") or ()),
        )


@dataclass(frozen=True)
class SkillDecision:
    """skill_route 输出（skill-decision/v1）。

    proposed_skill/proposed_version（Phase 7C.3-B）：status=migration_proposed 时的
    显式迁移目标（由 Router 给出，Pipeline 不得再从 candidates[0] 猜测）。两字段必须
    同空或同有；非 migration_proposed 状态不得携带非空 proposed。
    注意：DTO 只做"成对/状态互斥"格式校验。"migration_proposed 必须有显式目标 /
    目标 ∈ candidates / 与当前不同 / 方向受支持"属语义校验，由 Router 生产侧与
    v3 Pipeline 边界强制（旧 Artifact 缺 proposed 保持可读兼容，由 v3 fail closed）。
    """

    schema_version: str = SKILL_DECISION_SCHEMA
    target_key: str = ""
    wiki_page_id: str | None = None
    selected_skill: str | None = None
    selected_version: str | None = None
    selected_by: str = "default_fallback"
    confidence: float = 1.0
    status: str = "selected"
    reason_code: str = ""
    matched_signals: tuple[str, ...] = ()
    candidates: tuple[dict, ...] = ()
    previous_skill: str | None = None
    previous_version: str | None = None
    locked: bool = False
    proposed_skill: str | None = None
    proposed_version: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SKILL_DECISION_SCHEMA:
            raise ValueError("schema_version must be skill-decision/v1")
        object.__setattr__(self, "target_key", self.target_key.strip())
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence, "confidence"))
        if self.selected_by not in SELECTED_BY_VALUES:
            raise ValueError(f"selected_by must be one of {SELECTED_BY_VALUES}")
        if self.status not in DECISION_STATUS_VALUES:
            raise ValueError(f"status must be one of {DECISION_STATUS_VALUES}")
        object.__setattr__(self, "reason_code", self.reason_code.strip())
        object.__setattr__(self, "matched_signals", tuple(self.matched_signals))
        candidates = _freeze(list(self.candidates))
        object.__setattr__(self, "candidates", tuple(candidates))
        _assert_json_safe(self.candidates, "candidates")
        # not_applicable 可无 selected skill；其余状态 selected_skill/version 必须
        # 能在 Registry 精确解析（解析由 service 层校验，这里只保证格式）。
        if self.selected_skill is not None:
            _validate_key(self.selected_skill, "selected_skill")
        if self.selected_version is not None:
            _validate_version(self.selected_version, "selected_version")
        # proposed 字段：格式 + 成对 + 与状态互斥（格式层；语义校验在 Router/v3）。
        if self.proposed_skill is not None:
            object.__setattr__(self, "proposed_skill", self.proposed_skill.strip())
            _validate_key(self.proposed_skill, "proposed_skill")
        if self.proposed_version is not None:
            object.__setattr__(self, "proposed_version", self.proposed_version.strip())
            _validate_version(self.proposed_version, "proposed_version")
        if (self.proposed_skill is None) != (self.proposed_version is None):
            raise ValueError(
                "proposed_skill and proposed_version must be both set or both empty")
        if self.status != "migration_proposed":
            if self.proposed_skill is not None or self.proposed_version is not None:
                raise ValueError(
                    "proposed_skill/proposed_version only allowed for migration_proposed")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "target_key": self.target_key,
            "wiki_page_id": self.wiki_page_id,
            "selected_skill": self.selected_skill,
            "selected_version": self.selected_version,
            "selected_by": self.selected_by,
            "confidence": self.confidence,
            "status": self.status,
            "reason_code": self.reason_code,
            "matched_signals": list(self.matched_signals),
            "candidates": _thaw(self.candidates),
            "previous_skill": self.previous_skill,
            "previous_version": self.previous_version,
            "locked": self.locked,
            "proposed_skill": self.proposed_skill,
            "proposed_version": self.proposed_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillDecision":
        allowed = {
            "schema_version", "target_key", "wiki_page_id", "selected_skill",
            "selected_version", "selected_by", "confidence", "status", "reason_code",
            "matched_signals", "candidates", "previous_skill", "previous_version",
            "locked", "proposed_skill", "proposed_version",
        }
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        return cls(
            schema_version=data.get("schema_version", SKILL_DECISION_SCHEMA),
            target_key=data.get("target_key", ""),
            wiki_page_id=data.get("wiki_page_id"),
            selected_skill=data.get("selected_skill"),
            selected_version=data.get("selected_version"),
            selected_by=data.get("selected_by", "default_fallback"),
            confidence=data.get("confidence", 1.0),
            status=data.get("status", "selected"),
            reason_code=data.get("reason_code", ""),
            matched_signals=tuple(data.get("matched_signals") or ()),
            candidates=tuple(data.get("candidates") or ()),
            previous_skill=data.get("previous_skill"),
            previous_version=data.get("previous_version"),
            locked=bool(data.get("locked", False)),
            proposed_skill=data.get("proposed_skill"),
            proposed_version=data.get("proposed_version"),
        )


@dataclass(frozen=True)
class SkillContext:
    """Skill 选择所需的最小上下文（不含 Session/Publisher/ACL 修改器/完整 DB 对象）。"""

    workspace_id: str | None = None
    wiki_page_id: str | None = None
    target_key: str = ""
    title: str = ""
    content_kind: str = ""
    source_page_ids: tuple[str, ...] = ()
    source_summaries: tuple[dict, ...] = ()
    current_skill: str | None = None
    current_version: str | None = None
    skill_locked: bool = False

    # source_summaries 数量与单条长度上限（防超长上下文）。
    MAX_SUMMARIES = 20
    MAX_SUMMARY_CHARS = 4000

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", self.target_key.strip())
        object.__setattr__(self, "title", self.title[:512])
        object.__setattr__(self, "content_kind", self.content_kind.strip()[:64])
        page_ids = tuple(str(p) for p in self.source_page_ids if p)
        object.__setattr__(self, "source_page_ids", page_ids[: self.MAX_SUMMARIES])
        summaries = []
        for s in self.source_summaries:
            if not isinstance(s, dict):
                raise ValueError("source_summaries must be dicts")
            _assert_json_safe(s, "source_summaries")
            text = str(s.get("summary", ""))[: self.MAX_SUMMARY_CHARS]
            summaries.append({"source_page_id": str(s.get("source_page_id", "")), "summary": text})
        object.__setattr__(self, "source_summaries", _freeze(summaries[: self.MAX_SUMMARIES]))
        if self.current_skill is not None:
            _validate_key(self.current_skill, "current_skill")
        if self.current_version is not None:
            _validate_version(self.current_version, "current_version")
        object.__setattr__(self, "skill_locked", bool(self.skill_locked))

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "wiki_page_id": self.wiki_page_id,
            "target_key": self.target_key,
            "title": self.title,
            "content_kind": self.content_kind,
            "source_page_ids": list(self.source_page_ids),
            "source_summaries": _thaw(self.source_summaries),
            "current_skill": self.current_skill,
            "current_version": self.current_version,
            "skill_locked": self.skill_locked,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SkillContext":
        return cls(
            workspace_id=data.get("workspace_id"),
            wiki_page_id=data.get("wiki_page_id"),
            target_key=data.get("target_key", ""),
            title=data.get("title", ""),
            content_kind=data.get("content_kind", ""),
            source_page_ids=tuple(data.get("source_page_ids") or ()),
            source_summaries=tuple(data.get("source_summaries") or ()),
            current_skill=data.get("current_skill"),
            current_version=data.get("current_version"),
            skill_locked=bool(data.get("skill_locked", False)),
        )
