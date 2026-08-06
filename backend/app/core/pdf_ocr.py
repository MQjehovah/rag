import io
import logging
import math
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

from app.config import settings


logger = logging.getLogger(__name__)


class PDFOCRService:
    """从 PDF 页面图片中补充文本层未包含的文字。"""

    DOMAIN_LEXICON = (
        "Basement A",
        "Workstation",
        "Mission report",
        "Fixed point",
        "No-go zone",
        "Deceleration zone",
        "Unidirectional",
        "Bidirectional",
        "Draw Area",
        "Task scheduling",
        "Working mode",
        "Put away",
        "Select Map",
        "Task Management",
    )
    UI_NOISE = {
        "connectedsecure",
        "unsafe",
        "printer",
        "paneljira",
        "articlespage",
    }

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).lower()
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)

    @staticmethod
    def has_replacement_chars(value: str) -> bool:
        """检测PDF字体缺失Unicode映射后产生的替换字符。"""
        return "\ufffd" in str(value or "")

    @classmethod
    def _is_novel_line(cls, line: str, source_text: str) -> bool:
        """判断 OCR 行是否为文本层中没有的新内容。"""
        normalized = cls._normalize(line)
        if len(normalized) < settings.pdf_ocr_min_novel_chars:
            return False
        if normalized.isdigit():
            return False

        source_normalized = cls._normalize(source_text)
        if normalized in source_normalized:
            return False

        source_lines = [
            cls._normalize(item)
            for item in source_text.splitlines()
            if cls._normalize(item)
        ]
        source_windows = source_lines + [
            source_lines[index] + source_lines[index + 1]
            for index in range(len(source_lines) - 1)
        ]
        best_ratio = max(
            (
                SequenceMatcher(None, normalized, source_line).ratio()
                for source_line in source_windows
            ),
            default=0.0,
        )
        return best_ratio < settings.pdf_ocr_duplicate_similarity

    @classmethod
    def _closest_lexicon_term(
        cls,
        value: str,
        threshold: float = 0.80,
    ) -> str | None:
        normalized = cls._normalize(value)
        if len(normalized) < 5:
            return None
        best_term = None
        best_ratio = 0.0
        for term in cls.DOMAIN_LEXICON:
            target = cls._normalize(term)
            length_ratio = len(normalized) / max(len(target), 1)
            if not 0.62 <= length_ratio <= 1.35:
                continue
            ratio = SequenceMatcher(None, normalized, target).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_term = term
        return best_term if best_ratio >= threshold else None

    @classmethod
    def _is_noise_line(cls, line: str) -> bool:
        normalized = cls._normalize(line)
        if normalized in cls.UI_NOISE:
            return True
        # 多页重复出现的品牌 Logo 曾被识别成多个近似乱码变体。
        return bool(re.fullmatch(r"rosi[a-z]{2,10}", normalized))

    @classmethod
    def _merge_and_correct_lines(cls, lines: List[str]) -> List[str]:
        """合并被 OCR 拆开的英文词，并修正常见部署领域术语。"""
        result: List[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            corrected = cls._closest_lexicon_term(line)
            if corrected:
                result.append(corrected)
                index += 1
                continue

            if index + 1 < len(lines):
                combined = f"{line}{lines[index + 1]}"
                merged = cls._closest_lexicon_term(combined, threshold=0.84)
                if merged:
                    result.append(merged)
                    index += 2
                    continue

            if not cls._is_noise_line(line):
                result.append(line)
            index += 1

        deduplicated = []
        seen = set()
        for line in result:
            normalized = cls._normalize(line)
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduplicated.append(line)
        return deduplicated

    @classmethod
    def extract_corrupted_pages(
        cls,
        content: bytes,
        pages: List[Dict[str, Any]],
    ) -> Tuple[
        Dict[int, List[str]],
        Dict[int, List[Dict[str, Any]]],
        int,
    ]:
        """对含替换字符的页面执行整页OCR，并保留坐标供表格单元格重建。"""
        corrupted = [
            page
            for page in pages
            if cls.has_replacement_chars(str(page.get("text") or ""))
        ]
        if not settings.pdf_ocr_enabled or not corrupted:
            return {}, {}, 0

        try:
            import pdfplumber
            from rapidocr import RapidOCR
        except ImportError as exc:
            logger.warning(f"PDF乱码恢复依赖未安装，无法执行整页OCR: {exc}")
            return {}, {}, 0

        dpi = min(max(settings.pdf_ocr_dpi, 144), 240)
        recovered_text: Dict[int, List[str]] = {}
        detections_by_page: Dict[int, List[Dict[str, Any]]] = {}
        processed = 0
        try:
            engine = RapidOCR()
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_info in corrupted:
                    page_number = int(page_info.get("page_number") or 0)
                    if not 1 <= page_number <= len(pdf.pages):
                        continue
                    try:
                        pdf_page = pdf.pages[page_number - 1]
                        image = pdf_page.to_image(
                            resolution=dpi,
                            antialias=True,
                        ).original.convert("RGB")
                        result = engine(image)
                        texts = tuple(getattr(result, "txts", None) or ())
                        scores = tuple(getattr(result, "scores", None) or ())
                        boxes = getattr(result, "boxes", None)
                        boxes = tuple(boxes) if boxes is not None else ()
                        scale_x = image.width / max(float(pdf_page.width), 1.0)
                        scale_y = image.height / max(float(pdf_page.height), 1.0)

                        lines: List[str] = []
                        detections: List[Dict[str, Any]] = []
                        for box, text, score in zip(boxes, texts, scores):
                            line = re.sub(r"\s+", " ", str(text)).strip()
                            if (
                                not line
                                or cls.has_replacement_chars(line)
                                or float(score) < settings.pdf_ocr_min_confidence
                            ):
                                continue
                            points = list(box)
                            xs = [float(point[0]) for point in points]
                            ys = [float(point[1]) for point in points]
                            detections.append({
                                "text": line,
                                "score": float(score),
                                "bbox": (
                                    min(xs) / scale_x,
                                    min(ys) / scale_y,
                                    max(xs) / scale_x,
                                    max(ys) / scale_y,
                                ),
                            })
                            lines.append(line)

                        cleaned = cls._merge_and_correct_lines(lines)
                        if cleaned:
                            recovered_text[page_number] = cleaned
                        if detections:
                            detections_by_page[page_number] = detections
                        processed += 1
                    except Exception as exc:
                        logger.warning(
                            f"PDF 第 {page_number} 页乱码OCR恢复失败，继续处理其他页面: {exc}"
                        )
            return recovered_text, detections_by_page, processed
        except Exception as exc:
            logger.warning(f"PDF乱码OCR恢复失败: {exc}")
            return {}, {}, processed

    @classmethod
    def extract_novel_text(
        cls,
        content: bytes,
        pages: List[Dict[str, Any]],
    ) -> Tuple[Dict[int, List[str]], int]:
        """OCR 有图片的页面，返回每页新增文字和实际处理页数。"""
        if not settings.pdf_ocr_enabled:
            return {}, 0

        try:
            import pdfplumber
            from rapidocr import RapidOCR
        except ImportError as exc:
            logger.warning(f"PDF OCR 依赖未安装，跳过图片文字识别: {exc}")
            return {}, 0

        dpi = min(max(settings.pdf_ocr_dpi, 96), 240)
        max_pages = max(settings.pdf_ocr_max_pages, 0)
        candidates = [
            page
            for page in pages
            if int(page.get("image_count") or 0) > 0
            or not str(page.get("text") or "").strip()
        ][:max_pages]
        if not candidates:
            return {}, 0

        try:
            engine = RapidOCR()
            raw_result: Dict[int, List[str]] = {}
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page_info in candidates:
                    page_number = int(page_info.get("page_number") or 0)
                    if not 1 <= page_number <= len(pdf.pages):
                        continue

                    image = pdf.pages[page_number - 1].to_image(
                        resolution=dpi,
                        antialias=True,
                    ).original.convert("RGB")
                    result = engine(image)
                    texts = tuple(getattr(result, "txts", None) or ())
                    scores = tuple(getattr(result, "scores", None) or ())
                    source_text = str(page_info.get("text") or "")

                    page_lines: List[str] = []
                    seen = set()
                    for text, score in zip(texts, scores):
                        line = re.sub(r"\s+", " ", str(text)).strip()
                        normalized = cls._normalize(line)
                        if float(score) < settings.pdf_ocr_min_confidence:
                            continue
                        if normalized in seen:
                            continue
                        if not cls._is_novel_line(line, source_text):
                            continue
                        seen.add(normalized)
                        page_lines.append(line)

                    if page_lines:
                        cleaned_lines = cls._merge_and_correct_lines(page_lines)
                        if cleaned_lines:
                            raw_result[page_number] = cleaned_lines

            frequency = Counter(
                cls._normalize(line)
                for lines in raw_result.values()
                for line in lines
            )
            repeat_threshold = max(3, math.ceil(len(candidates) * 0.18))
            boilerplate = {
                line
                for line, count in frequency.items()
                if count >= repeat_threshold and len(line) <= 100
            }
            filtered = {
                page_number: [
                    line
                    for line in lines
                    if cls._normalize(line) not in boilerplate
                ]
                for page_number, lines in raw_result.items()
            }
            filtered = {
                page_number: lines
                for page_number, lines in filtered.items()
                if lines
            }
            return filtered, len(candidates)
        except Exception as exc:
            logger.warning(f"PDF OCR 识别失败，继续使用文本层内容: {exc}")
            return {}, 0
