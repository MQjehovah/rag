"""P1-BE-06：视觉 Provider 接口 + VLM→OCR 退化测试。

覆盖：
- 工厂：VLM 配置齐全 → Fallback(VLM, OCR)；不齐全 → 直接 OCR
- 退化：VLM 返回 None/抛异常 → 降级 OCR；VLM 有产出 → 不降级
- OCRProvider：有文字 → 产出来自 OCR 的说明；无文字 → None
- PDFVisionService._analyze_image 委托 Provider（可注入 Fake，验证 P0-BE-10 可注入性延续）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.core import vision_provider
from app.core.vision_provider import (
    FallbackVisionProvider,
    OCRVisionProvider,
    VLMVisionProvider,
    get_vision_provider,
)


class _FakePrimary:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def analyze(self, image_path, page_number, source_text):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeFallback:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def analyze(self, image_path, page_number, source_text):
        self.calls += 1
        return self.result


def _patch_settings(monkeypatch, **kwargs):
    for key, value in kwargs.items():
        monkeypatch.setattr(settings, key, value)


# ---------- 工厂 ----------

def test_factory_vlm_configured(monkeypatch):
    _patch_settings(
        monkeypatch,
        pdf_vision_enabled=True,
        pdf_vision_api_url="http://vlm.local/v1",
        pdf_vision_api_key="k",
        pdf_vision_model="glm-4v",
    )
    provider = get_vision_provider()
    assert isinstance(provider, FallbackVisionProvider)
    assert isinstance(provider.primary, VLMVisionProvider)
    assert isinstance(provider.fallback, OCRVisionProvider)


def test_factory_no_vlm_degrades_to_ocr(monkeypatch):
    _patch_settings(
        monkeypatch,
        pdf_vision_enabled=False,
        pdf_vision_api_url="",
        pdf_vision_api_key="",
        pdf_vision_model="",
    )
    provider = get_vision_provider()
    assert isinstance(provider, OCRVisionProvider)


# ---------- 退化逻辑 ----------

def test_fallback_uses_primary_when_result_present():
    primary = _FakePrimary(result={"summary": "ok"})
    fallback = _FakeFallback(result={"summary": "should-not-run"})
    provider = FallbackVisionProvider(primary, fallback)
    result = provider.analyze(Path("x.jpg"), 1, "")
    assert result == {"summary": "ok"}
    assert fallback.calls == 0


def test_fallback_degrades_when_primary_none():
    primary = _FakePrimary(result=None)
    fallback = _FakeFallback(result={"summary": "ocr"})
    provider = FallbackVisionProvider(primary, fallback)
    result = provider.analyze(Path("x.jpg"), 1, "")
    assert result == {"summary": "ocr"}
    assert primary.calls == 1 and fallback.calls == 1


def test_fallback_degrades_when_primary_raises():
    primary = _FakePrimary(exc=RuntimeError("vlm down"))
    fallback = _FakeFallback(result={"summary": "ocr"})
    provider = FallbackVisionProvider(primary, fallback)
    result = provider.analyze(Path("x.jpg"), 1, "")
    assert result == {"summary": "ocr"}


# ---------- OCRProvider ----------

def test_ocr_provider_returns_text(monkeypatch):
    monkeypatch.setattr(
        "app.core.ocr.run_ocr",
        lambda path: ("启动 按钮", 0.93),
    )
    provider = OCRVisionProvider()
    result = provider.analyze(Path("x.jpg"), 3, "文本层")
    assert result is not None
    assert result["summary"] == "启动 按钮"
    assert result["image_type"].startswith("OCR")
    assert result["ocr_confidence"] == 0.93
    assert "视觉模型不可用" in result["warnings"][0]


def test_ocr_provider_empty_text_returns_none(monkeypatch):
    monkeypatch.setattr("app.core.ocr.run_ocr", lambda path: ("", 0.0))
    provider = OCRVisionProvider()
    assert provider.analyze(Path("x.jpg"), 3, "") is None


# ---------- PDFVisionService 委托 ----------

def test_pdf_vision_service_delegates_to_provider(monkeypatch):
    from app.core import pdf_vision

    class _FakeProvider:
        def __init__(self):
            self.calls = []

        def analyze(self, image_path, page_number, source_text):
            self.calls.append((image_path, page_number, source_text))
            return {"summary": "fake-vision"}

    fake = _FakeProvider()
    monkeypatch.setattr(
        "app.core.vision_provider.get_vision_provider",
        lambda: fake,
    )
    result = pdf_vision.PDFVisionService._analyze_image(Path("x.jpg"), 5, "src")
    assert result == {"summary": "fake-vision"}
    assert fake.calls == [(Path("x.jpg"), 5, "src")]
