import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def call_llm_json(messages: list, context: str = "", timeout: float = 120.0) -> dict:
    """Call the configured LLM API and parse the response as JSON.

    Returns {} when the LLM is not configured or the response cannot be
    parsed, so callers can always fall back gracefully.
    """
    if not settings.llm_api_url:
        return {}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                settings.llm_api_url,
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"LLM call failed [{context}]: {e}")
        return {}

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content or not content.strip():
        logger.warning(f"LLM empty response [{context}]")
        return {}

    import re
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
    if json_match:
        content = json_match.group(1)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    brace_depth = 0
    start = -1
    for i, ch in enumerate(content):
        if ch == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidate = content[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue

    logger.warning(f"LLM response could not be parsed as JSON [{context}]: {content[:300]}")
    return {}


async def call_llm_text(messages: list, context: str = "", timeout: float = 180.0) -> str:
    """Call the configured LLM and return the plain text content (no JSON
    parsing).  Used for tasks like wiki page merging where the output is
    arbitrary Markdown."""
    if not settings.llm_api_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                settings.llm_api_url,
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return (content or "").strip()
    except Exception as e:
        logger.warning(f"LLM text call failed [{context}]: {e}")
        return ""
