from __future__ import annotations

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter


@dataclass(frozen=True)
class DocumentChunk:
    document_id: str
    content: str
    metadata: dict
    chunk_index: int


class SemanticSplitter:
    def __init__(self, chunk_size: int = 800, overlap: int = 120):
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=min(overlap, max(0, chunk_size - 1)),
            separators=["\n## ", "\n### ", "\n\n", "\n", "。", "；", " "],
        )

    def split(self, content: str, document_id: str, metadata: dict | None = None) -> list[DocumentChunk]:
        chunks = self._splitter.split_text(content or "")
        return [
            DocumentChunk(document_id, value, dict(metadata or {}), index)
            for index, value in enumerate(chunks)
        ]
