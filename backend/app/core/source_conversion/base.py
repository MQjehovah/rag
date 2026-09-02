"""Phase 1.3：SourceConverter 接口 + ConverterMatch + ConverterSelection。

- ConverterMatch 严格校验（converter_key/specificity/priority/reason/evidence）；
- ConverterMatchError 保存结构化 converter_key/error_type（用户消息不暴露异常文本）；
- SourceConverter 声明 output_kind（Converter 输出类型固定）；
- ConverterSelection 类型严格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from app.core.source_conversion.schemas import (
    ContentKind,
    ConverterOutput,
    RawSourceItem,
    freeze_mapping,
    require_mapping,
    thaw_value,
)


class ConverterCategory(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"
    SOURCE_CODE = "source_code"
    CSV = "csv"
    OFFICE = "office"
    PDF = "pdf"


class EvidenceStrength(int, Enum):
    FILE_HEADER = 5
    CONTENT_KIND = 4
    MIME = 3
    EXTENSION = 2
    TEXT_PAYLOAD = 1


class AmbiguousConverterError(RuntimeError):
    def __init__(self, message: str, candidates: list["ConverterMatch"] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


class ConverterMatchError(RuntimeError):
    """已注册 Converter.match() 返回非法结果或抛异常（fail closed）。

    保存结构化 converter_key/error_type；用户消息不暴露原始异常文本。
    """

    def __init__(self, converter_key: str, error_type: str, message: str = ""):
        super().__init__(message or f"converter {converter_key} match 非法（{error_type}）")
        self.converter_key = converter_key
        self.error_type = error_type


@dataclass(frozen=True)
class ConverterMatch:
    """匹配结果。严格校验。"""

    converter_key: str
    specificity: EvidenceStrength
    priority: int = 0
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        key = self.converter_key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("ConverterMatch.converter_key 必须是 strip 后非空字符串")
        object.__setattr__(self, "converter_key", key.strip())
        # specificity：EvidenceStrength 或严格 int；"5" 字符串拒绝
        spec = self.specificity
        if isinstance(spec, str):
            raise ValueError("ConverterMatch.specificity 不允许字符串，必须是 EvidenceStrength 或 int")
        if isinstance(spec, bool):
            raise ValueError("ConverterMatch.specificity 不允许 bool")
        if isinstance(spec, int) and not isinstance(spec, EvidenceStrength):
            try:
                object.__setattr__(self, "specificity", EvidenceStrength(spec))
            except ValueError:
                raise ValueError(f"非法 specificity: {spec!r}")
        elif not isinstance(spec, EvidenceStrength):
            raise ValueError(f"非法 specificity: {type(spec).__name__}")
        # priority：int 且非 bool
        prio = self.priority
        if isinstance(prio, bool) or not isinstance(prio, int):
            raise ValueError("ConverterMatch.priority 必须是 int（非 bool）")
        object.__setattr__(self, "priority", prio)
        # reason：str 且 strip 后非空（对象层强制）
        if not isinstance(self.reason, str):
            raise ValueError("ConverterMatch.reason 必须是 str")
        if not self.reason.strip():
            raise ValueError("ConverterMatch.reason 不能为空")
        object.__setattr__(self, "reason", self.reason.strip())
        require_mapping(self.evidence, field_name="ConverterMatch.evidence")
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence, field_name="ConverterMatch.evidence"))

    def to_dict(self) -> dict:
        return {
            "converter_key": self.converter_key,
            "specificity": int(self.specificity.value),
            "priority": self.priority,
            "reason": self.reason,
            "evidence": thaw_value(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConverterMatch":
        from collections.abc import Mapping
        if not isinstance(data, Mapping):
            raise ValueError("ConverterMatch.from_dict 输入必须是 Mapping")
        spec_raw = data.get("specificity")
        if not isinstance(spec_raw, int) or isinstance(spec_raw, bool):
            raise ValueError("ConverterMatch.specificity 必须是 int")
        evidence_raw = data.get("evidence")
        if not isinstance(evidence_raw, Mapping):
            raise ValueError("ConverterMatch.evidence 必须是 Mapping")
        priority_raw = data.get("priority", 0)
        if not isinstance(priority_raw, int) or isinstance(priority_raw, bool):
            raise ValueError("ConverterMatch.priority 必须是 int（非 bool）")
        return cls(
            converter_key=data.get("converter_key"),
            specificity=EvidenceStrength(spec_raw),
            priority=priority_raw,
            reason=data.get("reason", ""),
            evidence=dict(evidence_raw),
        )


@dataclass(frozen=True)
class ConverterSelection:
    """Registry 结构化选择结果。"""

    selected_converter: object
    selected_match: ConverterMatch
    candidate_matches: tuple[ConverterMatch, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.selected_match, ConverterMatch):
            raise ValueError("selected_match 必须是 ConverterMatch")
        if not isinstance(self.candidate_matches, tuple) or not self.candidate_matches:
            raise ValueError("candidate_matches 必须是非空 ConverterMatch 元组")
        if not all(isinstance(m, ConverterMatch) for m in self.candidate_matches):
            raise ValueError("candidate_matches 必须全为 ConverterMatch")
        # selected_converter.key 必须精确等于 selected_match.converter_key
        conv_key = getattr(self.selected_converter, "key", "")
        if conv_key != self.selected_match.converter_key:
            raise ValueError("selected_converter.key 必须精确等于 selected_match.converter_key")
        # selected_match 必须与 candidate_matches 中某个对象完整相等（非仅 key）
        if not any(m == self.selected_match for m in self.candidate_matches):
            raise ValueError("selected_match 必须与 candidate_matches 中某个对象完整相等")
        # candidate_matches 不得重复 key
        keys = [m.converter_key for m in self.candidate_matches]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate_matches 不得包含重复 converter_key")

    def to_dict(self) -> dict:
        return {
            "selected_match": self.selected_match.to_dict(),
            "candidate_matches": [m.to_dict() for m in self.candidate_matches],
        }


@runtime_checkable
class SourceConverter(Protocol):
    """转换器统一接口。"""

    key: str
    version: str
    priority: int
    output_kind: ContentKind  # Converter 固定输出类型声明

    def match(self, item: RawSourceItem) -> ConverterMatch | None: ...
    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput: ...
