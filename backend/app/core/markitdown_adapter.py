import io
from dataclasses import dataclass, field
from typing import Any, Dict, List


class MarkItDownUnavailableError(RuntimeError):
    """当前环境未安装或无法初始化MarkItDown。"""


class MarkItDownConversionError(RuntimeError):
    """MarkItDown未能把输入文件转换为有效Markdown。"""


@dataclass(frozen=True)
class MarkdownConversionResult:
    """统一描述一次文件转Markdown的结果和降级信息。"""

    text: str
    converter: str
    fallback_used: bool = False
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PDFPageConversionResult:
    """MarkItDown逐页转换结果，失败页面由上层使用原解析器降级。"""

    total_pages: int
    pages: Dict[int, str] = field(default_factory=dict)
    failed_pages: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class MarkItDownAdapter:
    """对Microsoft MarkItDown的轻量封装，便于替换和单元测试。"""

    SUPPORTED_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx"}

    def __init__(self, engine: Any | None = None):
        self._engine = engine

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise MarkItDownUnavailableError(
                "MarkItDown尚未安装，请先安装backend/requirements.txt中的依赖"
            ) from exc
        try:
            self._engine = MarkItDown(enable_plugins=False)
        except Exception as exc:
            raise MarkItDownUnavailableError(
                f"MarkItDown初始化失败: {exc}"
            ) from exc
        return self._engine

    @staticmethod
    def _result_text(result: Any) -> str:
        return str(
            getattr(result, "markdown", None)
            or getattr(result, "text_content", None)
            or ""
        ).strip()

    def convert_bytes(self, content: bytes, extension: str) -> str:
        normalized_extension = str(extension or "").lower().lstrip(".")
        if normalized_extension not in self.SUPPORTED_EXTENSIONS:
            raise MarkItDownConversionError(
                f"MarkItDown不处理该文件格式: {normalized_extension or '未知'}"
            )
        if not content:
            raise MarkItDownConversionError("待转换文件内容为空")

        try:
            result = self._get_engine().convert_stream(
                io.BytesIO(content),
                file_extension=f".{normalized_extension}",
            )
        except (MarkItDownUnavailableError, MarkItDownConversionError):
            raise
        except Exception as exc:
            raise MarkItDownConversionError(
                f"MarkItDown转换{normalized_extension}失败: {exc}"
            ) from exc

        text = self._result_text(result)
        if not text:
            raise MarkItDownConversionError(
                f"MarkItDown未从{normalized_extension}文件中提取到正文"
            )
        return text

    def convert_pdf_pages(self, content: bytes) -> PDFPageConversionResult:
        """拆分PDF并逐页调用MarkItDown，确保增强内容能够按原页码合并。"""
        if not content.startswith(b"%PDF"):
            raise MarkItDownConversionError("输入内容不是有效的PDF文件")

        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError as exc:
            raise MarkItDownUnavailableError(
                "缺少pypdf，无法执行MarkItDown逐页PDF转换"
            ) from exc

        try:
            reader = PdfReader(io.BytesIO(content))
            total_pages = len(reader.pages)
        except Exception as exc:
            raise MarkItDownConversionError(f"无法拆分PDF页面: {exc}") from exc
        if total_pages == 0:
            raise MarkItDownConversionError("PDF中没有可转换页面")

        pages: Dict[int, str] = {}
        failed_pages: List[int] = []
        warnings: List[str] = []
        for page_number, page in enumerate(reader.pages, 1):
            try:
                writer = PdfWriter()
                writer.add_page(page)
                buffer = io.BytesIO()
                writer.write(buffer)
                pages[page_number] = self.convert_bytes(buffer.getvalue(), "pdf")
            except MarkItDownUnavailableError as exc:
                failed_pages.extend(range(page_number, total_pages + 1))
                warnings.append(f"MarkItDown不可用，已停止逐页转换: {exc}")
                break
            except Exception as exc:
                failed_pages.append(page_number)
                warnings.append(f"PDF第{page_number}页MarkItDown转换失败: {exc}")

        return PDFPageConversionResult(
            total_pages=total_pages,
            pages=pages,
            failed_pages=failed_pages,
            warnings=warnings,
        )
