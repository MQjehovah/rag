"""Phase 1.3：SourceConverterRegistry（严格 fail closed）。

- register：严格校验 key/version 非空 str、priority int 非 bool、output_kind 合法 ContentKind；
- 任一注册 Converter.match() 抛异常或返回非 ConverterMatch / 伪造 key / 空 reason /
  非法 specificity → ConverterMatchError（fail closed，不继续选其它 Converter）；
- selected_converter 必须与 selected_match.converter_key 对应；
- keys() 稳定排序。
"""
from __future__ import annotations

import logging
import traceback as _tb
from collections.abc import Mapping

from app.core.source_conversion.base import (
    AmbiguousConverterError,
    ConverterMatch,
    ConverterMatchError,
    ConverterSelection,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.schemas import ContentKind, RawSourceItem

logger = logging.getLogger(__name__)


def _validate_converter(converter: object) -> tuple[str, str, int, ContentKind]:
    """校验 converter 满足接口声明，返回 (key, version, priority, output_kind)。

    严格策略（Phase 1.5 封板）：
    - key/version 必须已是 strip 后的规范值（带空白如 " s "/" v1 " → ValueError）；
    - output_kind 必须 ContentKind 实例（拒绝字符串 "text"）；
    - output_kind=UNKNOWN → 拒绝；
    - match/convert 必须 callable。
    """
    key = getattr(converter, "key", "")
    version = getattr(converter, "version", "")
    priority = getattr(converter, "priority", None)
    output_kind = getattr(converter, "output_kind", None)

    if not isinstance(key, str) or not key.strip():
        raise ValueError("converter.key 必须是非空 str")
    if key != key.strip():
        raise ValueError(f"converter.key 必须已是 strip 后的规范值（不能含首尾空白）: {key!r}")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("converter.version 必须是非空 str")
    if version != version.strip():
        raise ValueError(f"converter.version 必须已是 strip 后的规范值（不能含首尾空白）: {version!r}")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("converter.priority 必须是 int（非 bool）")
    # match / convert 必须 callable
    if not callable(getattr(converter, "match", None)):
        raise ValueError("converter.match 必须是 callable")
    if not callable(getattr(converter, "convert", None)):
        raise ValueError("converter.convert 必须是 callable")
    # output_kind：严格要求 ContentKind 实例；字符串 "text" 拒绝
    # 注意：ContentKind 是 (str, Enum)，isinstance(x, str) 对枚举成员为 True，
    # 必须先判枚举。
    if not isinstance(output_kind, ContentKind):
        raise ValueError("converter.output_kind 必须是 ContentKind 实例（不接受字符串枚举值）")
    if output_kind == ContentKind.UNKNOWN:
        raise ValueError("converter.output_kind 禁止为 UNKNOWN（必须是具体 content kind）")
    return key, version, priority, output_kind


class SourceConverterRegistry:
    def __init__(self) -> None:
        self._converters: dict[str, SourceConverter] = {}
        self._sealed = False

    def register(self, converter: SourceConverter, *, replace: bool = False) -> None:
        key, version, priority, output_kind = _validate_converter(converter)
        if self._sealed:
            raise RuntimeError("registry 已 seal，禁止注册")
        if key in self._converters and not replace:
            raise ValueError(f"converter key {key!r} 已存在（如需覆盖请使用 replace=True）")
        # 强制实例属性（对象层声明）
        self._converters[key] = converter

    def replace(self, converter: SourceConverter) -> None:
        self.register(converter, replace=True)

    def unregister(self, key: str) -> None:
        if self._sealed:
            raise RuntimeError("registry 已 seal，禁止注销")
        self._converters.pop(key, None)

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    def get(self, key: str) -> SourceConverter | None:
        return self._converters.get(key)

    def keys(self) -> list[str]:
        return sorted(self._converters.keys())

    def registered_count(self) -> int:
        return len(self._converters)

    def select(self, item: RawSourceItem) -> ConverterSelection | None:
        matches: list[ConverterMatch] = []
        for key, converter in sorted(self._converters.items()):
            try:
                m = converter.match(item)
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("converter %s.match 抛异常（fail closed），traceback:\n%s", key, _tb.format_exc())
                raise ConverterMatchError(key, type(exc).__name__) from exc
            if m is None:
                continue
            # ---- 非法 Match → fail closed（不忽略、不回退） ----
            if not isinstance(m, ConverterMatch):
                logger.error("converter %s.match 返回非 ConverterMatch: %s", key, type(m).__name__)
                raise ConverterMatchError(key, "non_converter_match")
            if m.converter_key != key:
                logger.error("converter %s 返回伪造 converter_key=%s（fail closed）", key, m.converter_key)
                raise ConverterMatchError(key, "forged_converter_key")
            if not isinstance(m.specificity, EvidenceStrength):
                logger.error("converter %s 返回非法 specificity（fail closed）", key)
                raise ConverterMatchError(key, "invalid_specificity")
            if not isinstance(m.reason, str) or not m.reason.strip():
                logger.error("converter %s 返回空 reason（fail closed）", key)
                raise ConverterMatchError(key, "empty_reason")
            # priority 以 Converter.priority 为准（Match 不能伪造）
            try:
                _, _, conv_priority, _ = _validate_converter(converter)
            except ValueError as exc:
                raise ConverterMatchError(key, "invalid_converter_priority", str(exc)) from exc
            normalized = ConverterMatch(
                converter_key=key,
                specificity=m.specificity,
                priority=conv_priority,
                reason=m.reason,
                evidence=dict(m.evidence or {}),
            )
            matches.append(normalized)

        if not matches:
            return None

        if len(matches) == 1:
            return ConverterSelection(
                selected_converter=self._converters[matches[0].converter_key],
                selected_match=matches[0],
                candidate_matches=tuple(matches),
            )

        max_specificity = max(m.specificity for m in matches)
        tier = [m for m in matches if m.specificity == max_specificity]
        if len(tier) == 1:
            return ConverterSelection(
                selected_converter=self._converters[tier[0].converter_key],
                selected_match=tier[0],
                candidate_matches=tuple(self._sort_candidates(matches)),
            )

        max_priority = max(m.priority for m in tier)
        top = [m for m in tier if m.priority == max_priority]
        if len(top) == 1:
            return ConverterSelection(
                selected_converter=self._converters[top[0].converter_key],
                selected_match=top[0],
                candidate_matches=tuple(self._sort_candidates(matches)),
            )

        raise AmbiguousConverterError(
            "多个同证据强度同优先级的转换器同时匹配: " + ", ".join(sorted(m.converter_key for m in top))
            + f" (item external_id={item.external_id})",
            candidates=self._sort_candidates(matches),
        )

    def _sort_candidates(self, matches: list[ConverterMatch]) -> list[ConverterMatch]:
        return sorted(
            matches,
            key=lambda m: (-int(m.specificity.value), -m.priority, m.converter_key),
        )
