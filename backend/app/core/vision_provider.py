"""视觉 Provider 接口（P1-BE-06）。

V3 计划 6.3 P1-BE-06：定义视觉 Provider 接口；可选 VLM 不可用时自动退化到 OCR。
硬约束 6：GLM-5.1 只处理文本，图片必须先转换为 OCR/Observation 文本。

两个实现 + 一个工厂：
- VLMVisionProvider：调用多模态 VLM（OpenAI 兼容），返回结构化功能说明 JSON，
  带 .vision.json 磁盘缓存。
- OCRVisionProvider：VLM 不可用时的降级——调用 app.core.ocr.run_ocr 提取图片文字，
  只产出文字说明，不补造视觉关系（硬约束 7 精神：不编造证据）。
- get_vision_provider()：工厂。VLM 配置齐全 → VLM 优先、失败自动降级 OCR；
  否则直接 OCR。

analyze() 约定与 PDFVisionService 原 _analyze_image 一致：
返回 dict（已解析 JSON）或 None（完全无法产出说明，调用方据此标记待补充）。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_VISION_PROMPT = """
你正在分析设备部署手册第 {page_number} 页。请重点理解图片的功能，不要只做 OCR。

请识别：
1. 图片属于设备部件图、安装操作图、软件界面截图、地图区域示意图还是普通装饰图；
2. 数字标号、箭头、框选区域分别指向什么；
3. 多张截图之间的操作顺序；
4. 图片对完成部署操作有什么实际作用；
5. 哪些结论无法从图片中可靠确认。

PDF 文本层内容如下，仅用于辅助，不得补造图片中看不到的信息：
{source_text}

只返回 JSON 对象，格式为：
{{
  "important": true,
  "image_type": "图片类型",
  "summary": "图片功能摘要",
  "steps": ["操作步骤"],
  "callouts": [{{"label": "标号或箭头", "target": "指向对象", "function": "作用"}}],
  "spatial_relations": ["区域或位置关系"],
  "warnings": ["注意事项或不确定内容"],
  "confidence": "high|medium|low"
}}
""".strip()


class VisionProvider(Protocol):
    """视觉分析接口：图片 → 结构化说明 dict 或 None（无可产出说明）。"""

    def analyze(
        self,
        image_path: Path,
        page_number: int,
        source_text: str,
    ) -> dict[str, Any] | None: ...


class VLMVisionProvider:
    """多模态 VLM 视觉分析，带磁盘缓存与限流重试。"""

    def _api_config(self) -> tuple[str, str, str]:
        return (
            settings.pdf_vision_api_url or settings.llm_api_url,
            settings.pdf_vision_api_key or settings.llm_api_key,
            settings.pdf_vision_model,
        )

    def analyze(
        self,
        image_path: Path,
        page_number: int,
        source_text: str,
    ) -> dict[str, Any] | None:
        api_url, api_key, model = self._api_config()
        if not settings.pdf_vision_enabled or not api_url or not api_key or not model:
            return None

        cache_path = image_path.with_suffix(".vision.json")
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    return cached
            except Exception:
                logger.warning(f"PDF 第 {page_number} 页视觉缓存损坏，将重新分析")

        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        prompt = _VISION_PROMPT.format(
            page_number=page_number,
            source_text=source_text[:6000],
        )
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
            "temperature": 0.1,
            "max_tokens": 1000,
            "response_format": {"type": "json_object"},
        }
        try:
            response = None
            last_request_error: Exception | None = None
            for attempt, delay in enumerate((0, 3, 8, 15)):
                if delay:
                    time.sleep(delay)
                try:
                    response = httpx.post(
                        api_url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                        timeout=settings.pdf_vision_timeout_seconds,
                    )
                except httpx.HTTPError as exc:
                    last_request_error = exc
                    logger.info(
                        f"PDF 第 {page_number} 页视觉接口连接异常，"
                        f"第 {attempt + 1} 次请求失败: {exc}"
                    )
                    continue
                if response.status_code != 429 and response.status_code < 500:
                    break
                logger.info(
                    f"PDF 第 {page_number} 页视觉接口暂不可用，"
                    f"第 {attempt + 1} 次请求状态码 {response.status_code}"
                )
            if response is None:
                if last_request_error is not None:
                    raise last_request_error
                return None
            response.raise_for_status()
            value = response.json()["choices"][0]["message"]["content"]
            if isinstance(value, list):
                value = "".join(
                    str(item.get("text") or "") if isinstance(item, dict) else str(item)
                    for item in value
                )
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(value).strip())
            json_start = raw.find("{")
            json_end = raw.rfind("}")
            if json_start >= 0 and json_end > json_start:
                raw = raw[json_start:json_end + 1]
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cache_path.write_text(
                    json.dumps(parsed, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return parsed
            return None
        except Exception as exc:
            logger.warning(f"PDF 第 {page_number} 页视觉理解失败: {exc}")
            return None


class OCRVisionProvider:
    """VLM 不可用时的降级：OCR 提取图片文字，不补造视觉关系。"""

    def analyze(
        self,
        image_path: Path,
        page_number: int,
        source_text: str,
    ) -> dict[str, Any] | None:
        from app.core.ocr import run_ocr  # 延迟导入：避免初始化 RapidOCR 引擎

        try:
            text, confidence = run_ocr(image_path)
        except Exception as exc:
            logger.warning(f"PDF 第 {page_number} 页 OCR 降级失败: {exc}")
            return None
        if not text.strip():
            return None  # OCR 无文字 → 无可产出说明，交给 needs_review（P1-BE-07）
        return {
            "important": False,
            "image_type": "OCR 文字识别（视觉模型不可用）",
            "summary": text,
            "steps": [],
            "callouts": [],
            "spatial_relations": [],
            "warnings": ["视觉模型不可用，以下为 OCR 提取的文字，不含空间关系。"],
            "confidence": "low",
            "ocr_confidence": round(float(confidence or 0.0), 4),
        }


class FallbackVisionProvider:
    """主 Provider 失败/无产出时，自动降级到备用 Provider。"""

    def __init__(self, primary: VisionProvider, fallback: VisionProvider):
        self.primary = primary
        self.fallback = fallback

    def analyze(
        self,
        image_path: Path,
        page_number: int,
        source_text: str,
    ) -> dict[str, Any] | None:
        try:
            result = self.primary.analyze(image_path, page_number, source_text)
        except Exception as exc:
            logger.info(f"PDF 第 {page_number} 页主视觉 Provider 异常，降级 OCR: {exc}")
            result = None
        if result is not None:
            return result
        return self.fallback.analyze(image_path, page_number, source_text)


def get_vision_mode() -> str:
    mode = (settings.vision_mode or "").strip().lower()
    if mode in {"disabled", "local", "remote"}:
        if mode == "remote" and not settings.vision_remote_allowed:
            return "local"
        return mode
    if settings.pdf_vision_enabled:
        return "remote" if settings.vision_remote_allowed else "local"
    return "disabled"


def get_vision_provider() -> VisionProvider:
    """工厂：disabled 只走 OCR；remote 需显式允许；否则 VLM+OCR 降级。"""
    mode = get_vision_mode()
    if mode == "disabled":
        return OCRVisionProvider()
    vlm = VLMVisionProvider()
    if mode == "remote" and not settings.vision_remote_allowed:
        return OCRVisionProvider()
    if all(vlm._api_config()):
        return FallbackVisionProvider(vlm, OCRVisionProvider())
    return OCRVisionProvider()
