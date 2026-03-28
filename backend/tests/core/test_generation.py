import pytest
from app.core.generation.prompt import PromptTemplate
from app.core.generation.context import ContextAssembler

class TestPromptTemplate:
    def test_format_context(self):
        docs = [
            {"content": "内容1", "metadata": {"title": "文档1", "source": "file1.md"}},
            {"content": "内容2", "metadata": {"title": "文档2", "source": "file2.md"}}
        ]
        result = PromptTemplate.format_context(docs)
        assert "文档1" in result
        assert "内容1" in result

class TestContextAssembler:
    def test_estimate_tokens(self):
        assembler = ContextAssembler()
        tokens = assembler.estimate_tokens("这是一个测试文本")
        assert tokens > 0