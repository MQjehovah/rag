"""Converter 公共辅助：最强证据收集。

每个 Converter 收集自己所有候选证据，再选择最高 EvidenceStrength（不依赖
if 书写顺序）。
"""
from __future__ import annotations

from app.core.source_conversion.base import ConverterMatch, EvidenceStrength


def pick_strongest(
    candidates: list[ConverterMatch],
    *,
    converter_key: str,
    priority: int,
    fallback_reason: str = "",
) -> ConverterMatch | None:
    """从候选 match 中选择最高 EvidenceStrength 者；同强度取最先收集者。"""
    if not candidates:
        return None
    best = max(candidates, key=lambda m: int(m.specificity.value))
    return ConverterMatch(
        converter_key=converter_key,
        specificity=best.specificity,
        priority=priority,
        reason=best.reason,
        evidence=dict(best.evidence or {}),
    )
