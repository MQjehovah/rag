import httpx

from app.config import settings
from app.core.pdf_vision import PDFVisionService
from app.core.vision_provider import VLMVisionProvider


def test_vision_page_limit_uses_pdf_total_pages(monkeypatch):
    monkeypatch.setattr(settings, "pdf_vision_page_ratio", 1.0)
    monkeypatch.setattr(settings, "pdf_vision_min_pages", 10)
    monkeypatch.setattr(settings, "pdf_vision_hard_max_pages", 60)

    assert PDFVisionService._vision_page_limit(8, 6) == 6
    assert PDFVisionService._vision_page_limit(55, 53) == 53
    assert PDFVisionService._vision_page_limit(100, 80) == 60


def test_vision_page_limit_supports_configurable_ratio(monkeypatch):
    monkeypatch.setattr(settings, "pdf_vision_page_ratio", 0.5)
    monkeypatch.setattr(settings, "pdf_vision_min_pages", 10)
    monkeypatch.setattr(settings, "pdf_vision_hard_max_pages", 60)

    assert PDFVisionService._vision_page_limit(12, 12) == 10
    assert PDFVisionService._vision_page_limit(40, 40) == 20


def test_important_page_selection_prioritizes_functional_images(monkeypatch):
    monkeypatch.setattr(settings, "pdf_image_importance_threshold", 4.0)
    monkeypatch.setattr(settings, "pdf_image_max_pages", 10)
    pages = [
        {"page_number": 1, "text": "Cover", "image_count": 1},
        {
            "page_number": 2,
            "text": "1. Connect battery\n2. Install fixing screws\nPower button",
            "image_count": 3,
        },
        {
            "page_number": 3,
            "text": "Select Map and click Task Management",
            "image_count": 4,
        },
    ]

    selected = PDFVisionService.select_important_pages(pages)

    assert [page["page_number"] for page in selected] == [2, 3]


def test_large_business_image_is_selected_without_action_keywords(monkeypatch):
    monkeypatch.setattr(settings, "pdf_image_importance_threshold", 4.0)
    monkeypatch.setattr(settings, "pdf_image_max_pages", 10)
    pages = [{
        "page_number": 1,
        "text": "收费标准",
        "image_count": 2,
        "substantive_images": [{"area_ratio": 0.18}],
    }]

    selected = PDFVisionService.select_important_pages(pages)

    assert [page["page_number"] for page in selected] == [1]


def test_all_large_business_images_are_kept_beyond_screenshot_limit(monkeypatch):
    monkeypatch.setattr(settings, "pdf_image_importance_threshold", 4.0)
    monkeypatch.setattr(settings, "pdf_image_max_pages", 1)
    pages = [
        {
            "page_number": page_number,
            "text": "业务图",
            "image_count": 1,
            "substantive_images": [{"area_ratio": 0.12}],
        }
        for page_number in range(1, 4)
    ]

    selected = PDFVisionService.select_important_pages(pages)

    assert [page["page_number"] for page in selected] == [1, 2, 3]


def test_visual_analysis_markdown_keeps_image_relationships():
    markdown = PDFVisionService.to_markdown(18, {
        "url": "/api/upload/pdf-pages/hash/page-18.jpg",
        "analysis": {
            "image_type": "设备部件说明图",
            "summary": "说明电池接口与固定螺钉的位置关系",
            "steps": ["连接电池接口", "安装固定螺钉"],
            "callouts": [
                {"label": "标号 1", "target": "电池接口", "function": "连接供电"},
            ],
            "spatial_relations": ["固定螺钉位于接口两侧"],
            "warnings": ["接口方向需要结合箭头确认"],
            "confidence": "high",
        },
    })

    assert "![第 18 页重要操作图]" in markdown
    assert "1. 连接电池接口" in markdown
    assert "标号 1：电池接口；连接供电" in markdown
    assert "固定螺钉位于接口两侧" in markdown


def test_visual_analysis_retries_connection_errors(tmp_path, monkeypatch):
    image_path = tmp_path / "page-1.jpg"
    image_path.write_bytes(b"jpeg")
    monkeypatch.setattr(settings, "pdf_vision_enabled", True)
    monkeypatch.setattr(settings, "pdf_vision_api_url", "https://example.test/v1")
    monkeypatch.setattr(settings, "pdf_vision_api_key", "test-key")
    monkeypatch.setattr(settings, "pdf_vision_model", "test-model")
    monkeypatch.setattr("app.core.vision_provider.time.sleep", lambda _delay: None)

    attempts = []

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "choices": [{
                    "message": {
                        "content": '{"important": true, "summary": "已恢复"}',
                    },
                }],
            }

    def fake_post(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("连接中断")
        return Response()

    monkeypatch.setattr("app.core.vision_provider.httpx.post", fake_post)

    result = VLMVisionProvider().analyze(image_path, 1, "页面正文")

    assert len(attempts) == 2
    assert result["summary"] == "已恢复"
