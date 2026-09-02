"""图片 needs_review 判定（P1-BE-07）。

V3 计划 6.3 P1-BE-07：图片只有 URL、OCR 为空、无功能说明时标记 needs_review=true。

三条件在 Observation 层的映射：
- 「只有 URL」：图片除 URL 外无可读内容 —— 等价于 OCR 提取不到文字
- 「OCR 为空」：ocr Observation 的 content 为空
- 「无功能说明」：不存在 ui_function / operation_flow / manual Observation

三者 AND → needs_review=true。

与 P1-BE-04 的区别：P1-BE-04 只判「OCR 为空 → needs_review」，
本模块补上「无功能说明」维度——空 OCR 但已有功能说明（如人工补充的
manual Observation）的图片不需要复核，needs_review=false。
"""
from __future__ import annotations

# 功能说明类 Observation 类型（ui_function/operation_flow 来自 VLM，manual 来自人工补充）
FUNCTION_OBSERVATION_TYPES = ("ui_function", "operation_flow", "manual")


def image_needs_review(ocr_text: str, has_function_desc: bool) -> bool:
    """判定图片是否需要人工复核。

    Args:
        ocr_text: OCR 提取的文本（ocr Observation.content）。
        has_function_desc: 是否已有功能说明
            （存在 ui_function/operation_flow/manual Observation）。

    Returns:
        True 当且仅当 OCR 为空 且 无功能说明——图片完全无法被自动理解，
        前端据此显示「待人工描述」（P1-FE-03）。
    """
    return (not (ocr_text or "").strip()) and (not has_function_desc)
