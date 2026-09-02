from __future__ import annotations

import inspect

import httpx

from app.config import settings


class EmbeddingEncoder:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)

    async def encode_query(self, text: str) -> list[float]:
        response = self.client.post(
            settings.embedding_api_url,
            json={"model": settings.embedding_model, "input": text},
        )
        if inspect.isawaitable(response):
            response = await response
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload.get("embedding"), list):
            return payload["embedding"]
        return payload.get("data", [{}])[0].get("embedding", [])

    async def close(self) -> None:
        await self.client.aclose()
