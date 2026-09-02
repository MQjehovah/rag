"""RapidOCR 封装：图片 → 文本 + 平均置信度（P1-BE-04）。

V3 硬约束 6：GLM-5.1 只处理文本，图片必须先转换为 OCR/Observation 文本。
此模块是该链路的第一环，也是 P1-BE-06 视觉 Provider 的 OCR 后端。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_engine = None


def _get_engine():
    """惰性初始化 OCR 引擎（首次调用加载 ONNX 模型，约 1s）。"""
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        _engine = RapidOCR()
    return _engine


def run_ocr(image_path: Path) -> tuple[str, float]:
    """对单张图片执行 OCR，返回 (按行拼接的文本, 平均置信度)。

    无文本时返回 ("", 0.0)——调用方据此标记 needs_review（P1-BE-07）。
    """
    result = _get_engine()(str(image_path))
    txts = [str(t) for t in (getattr(result, "txts", None) or [])]
    scores = [float(s) for s in (getattr(result, "scores", None) or [])]
    if not txts:
        return "", 0.0
    avg = sum(scores) / len(scores) if scores else 0.0
    return "\n".join(txts), round(avg, 4)
