"""Backward-compatible embedding API."""

from .encoder import EmbeddingEncoder
from .store import VectorStore

__all__ = ["EmbeddingEncoder", "VectorStore"]
