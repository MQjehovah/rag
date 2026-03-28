import pytest
from app.core.document.parser import MarkdownParser
from app.core.document.splitter import SemanticSplitter

class TestMarkdownParser:
    def test_extract_headers(self):
        parser = MarkdownParser()
        content = "# Title\n\n## Section 1\n\nContent\n\n## Section 2"
        headers = parser.extract_headers(content)
        assert len(headers) == 3
        assert headers[0].level == 1
        assert headers[1].level == 2
    
    def test_extract_plain_text(self):
        parser = MarkdownParser()
        content = "# Title\n\nThis is [a link](http://example.com) and `code`"
        text = parser.extract_plain_text(content)
        assert "a link" in text
        assert "code" in text

class TestSemanticSplitter:
    def test_split_by_headers(self):
        splitter = SemanticSplitter(chunk_size=100, overlap=20)
        content = "# Title\n\n## Section 1\n\nLong content here " * 20
        chunks = splitter.split(content, "doc1", {"source": "test"})
        assert len(chunks) > 0
        assert all(c.document_id == "doc1" for c in chunks)