"""Backward-compatible document processing API."""

from .parser import MarkdownParser
from .splitter import SemanticSplitter

__all__ = ["MarkdownParser", "SemanticSplitter"]
