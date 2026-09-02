"""共享 LLM 客户端。

抽取自 app/api/chat.py 的 _call_llm_json，供知识编译器（W3）和后续模块复用。

调用约定：
- 入参 messages（OpenAI 兼容格式），返回 dict（已解析 JSON）或空 dict。
- 失败时记录日志并返回 {}，不抛异常 — 调用方自行决定兜底策略。
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_KNOWLEDGE_EXTRACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_knowledge",
        "description": "Extract knowledge objects and their entity graph in one pass.",
        "parameters": {
            "type": "object",
            "properties": {
                "objects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "body": {"type": "string"},
                            "structured": {"type": ["string", "object"]},
                            "success_condition": {"type": ["string", "null"]},
                            "confidence": {"type": "number"},
                            "entities": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string"},
                                        "name": {"type": "string"},
                                    },
                                    "required": ["type", "name"],
                                },
                            },
                            "relations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source_type": {"type": "string"},
                                        "source_name": {"type": "string"},
                                        "target_type": {"type": "string"},
                                        "target_name": {"type": "string"},
                                        "relation_type": {"type": "string"},
                                        "weight": {"type": "number"},
                                    },
                                    "required": [
                                        "source_type", "source_name",
                                        "target_type", "target_name",
                                        "relation_type",
                                    ],
                                },
                            },
                        },
                        "required": ["type", "title", "body"],
                    },
                }
            },
            "required": ["objects"],
        },
    },
}


async def _post_json(messages: list, use_tools: bool, timeout: float) -> dict:
    """发送一次 LLM 请求，返回响应 JSON。失败抛异常，由调用方决定是否回退。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = {
            "model": settings.llm_model,
            "messages": messages,
            "stream": False,
        }
        if use_tools:
            payload["tools"] = [_KNOWLEDGE_EXTRACTION_TOOL]
            payload["tool_choice"] = {"type": "function", "function": {"name": "extract_knowledge"}}
        resp = await client.post(
            settings.llm_api_url,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def call_llm_json(messages: list, context: str = "", timeout: float = 120.0) -> dict:
    """调用 LLM 并解析为 JSON dict。

    Args:
        messages: OpenAI 兼容消息列表。
        context: 用于日志标识的上下文字符串。
        timeout: 超时秒数。

    Returns:
        解析后的 dict；若响应为空或解析失败，返回 {}。
    """
    if not settings.llm_api_url or not settings.llm_api_key:
        logger.warning(f"LLM not configured [{context}]")
        return {}

    use_tools = context.startswith("gen:")
    try:
        data = await _post_json(messages, use_tools, timeout)
    except Exception as exc:
        # Function Calling 失败（服务不支持 tools / 网络错误）→ 回退普通 JSON 模式
        if use_tools:
            logger.warning(f"LLM tools request failed [{context}]: {exc}; retrying without tools")
            try:
                data = await _post_json(messages, False, timeout)
            except Exception as exc2:
                logger.warning(f"LLM fallback request failed [{context}]: {exc2}")
                return {}
        else:
            logger.warning(f"LLM request failed [{context}]: {exc}")
            return {}

    message = data.get("choices", [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        arguments = tool_calls[0].get("function", {}).get("arguments", "")
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, json.JSONDecodeError):
            logger.warning(f"LLM tool arguments could not be parsed [{context}]")
    content = message.get("content", "")
    logger.info(f"LLM raw response ({len(content)} chars) [{context}]: {content[:2000]}")
    if not content or not content.strip():
        logger.warning(f"LLM empty response [{context}]")
        return {}

    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
    if json_match:
        content = json_match.group(1)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    candidate = _extract_first_json_block(content)
    if candidate:
        return candidate

    logger.warning(f"LLM response could not be parsed as JSON [{context}]: {content[:500]}")
    return {}


def _extract_first_json_block(content: str) -> dict:
    """在大段文本中查找第一个能解析为 JSON 的花括号块。"""
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
                    start = -1
                    continue
    return {}
