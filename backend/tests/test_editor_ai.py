"""编辑器 AI 接口:鉴权 / 参数校验 / 提示词组装 / LLM 错误映射 / SSE 流与行内补全。"""
import json

import httpx

from app.config import settings


def _login(as_user):
    return as_user([])


def test_editor_ai_requires_auth(api_client):
    """未登录(无 Authorization 头) -> 401。"""
    res = api_client.post("/api/editor/ai", json={"action": "polish", "text": "你好"})
    assert res.status_code == 401


def test_editor_ai_rejects_bad_params(api_client, as_user):
    """action 白名单 / text 空或超限 / context 超限 -> 400。"""
    _login(as_user)
    cases = [
        {"action": "unknown", "text": "你好"},
        {"action": "", "text": "你好"},
        {"action": "polish"},
        {"text": "你好"},
        {"action": "polish", "text": ""},
        {"action": "polish", "text": "   "},
        {"action": "polish", "text": "x" * 8001},
        {"action": "polish", "text": "x", "context": "c" * 4001},
    ]
    for body in cases:
        res = api_client.post("/api/editor/ai", json=body)
        assert res.status_code == 400, f"{body.get('action')!r} -> {res.status_code}"


def _capture_llm(monkeypatch, reply="结果"):
    """把 call_llm_text 替换为记录入参的假实现,返回 captured 容器。"""
    captured = {}

    async def fake_call(messages, context="", timeout=180.0, model=""):
        captured["messages"] = messages
        captured["context"] = context
        return reply

    monkeypatch.setattr("app.api.editor_ai.call_llm_text", fake_call)
    return captured


def test_editor_ai_polish_happy_path(api_client, as_user, monkeypatch):
    """polish:返回 {"result": ...},提示词包含输入文本。"""
    _login(as_user)
    captured = _capture_llm(monkeypatch)
    res = api_client.post("/api/editor/ai", json={"action": "polish", "text": "这段文字有错别字"})
    assert res.status_code == 200
    assert res.json() == {"result": "结果"}
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "这段文字有错别字" in prompt


def test_editor_ai_translate_happy_path(api_client, as_user, monkeypatch):
    """translate:提示词包含输入与目标语言,未传 target_lang 默认英文。"""
    _login(as_user)
    captured = _capture_llm(monkeypatch, reply="translated")
    res = api_client.post(
        "/api/editor/ai",
        json={"action": "translate", "text": "你好世界", "target_lang": "日文"},
    )
    assert res.status_code == 200
    assert res.json() == {"result": "translated"}
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "你好世界" in prompt
    assert "日文" in prompt

    res = api_client.post("/api/editor/ai", json={"action": "translate", "text": "你好世界"})
    assert res.status_code == 200
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "英文" in prompt


def test_editor_ai_continue_uses_context(api_client, as_user, monkeypatch):
    """continue:context(光标前文)进入提示词。"""
    _login(as_user)
    captured = _capture_llm(monkeypatch, reply="续写内容")
    res = api_client.post(
        "/api/editor/ai",
        json={"action": "continue", "text": "接着写", "context": "上一段的结尾是这里"},
    )
    assert res.status_code == 200
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "上一段的结尾是这里" in prompt


def test_editor_ai_llm_not_configured(api_client, as_user, monkeypatch):
    """LLM 未配置 -> 500,文案与 chat.py 一致。"""
    _login(as_user)
    monkeypatch.setattr(settings, "llm_api_url", "")
    res = api_client.post("/api/editor/ai", json={"action": "polish", "text": "你好"})
    assert res.status_code == 500
    assert res.json()["detail"] == "未配置 LLM API"


def test_editor_ai_llm_exception_returns_502(api_client, as_user, monkeypatch):
    """call_llm_text 抛异常 -> 502,带简短原因。"""
    _login(as_user)

    async def boom(messages, context="", timeout=180.0, model=""):
        raise RuntimeError("网关超时")

    monkeypatch.setattr("app.api.editor_ai.call_llm_text", boom)
    res = api_client.post("/api/editor/ai", json={"action": "polish", "text": "你好"})
    assert res.status_code == 502
    assert "网关超时" in res.json()["detail"]


def test_editor_ai_empty_llm_result_returns_502(api_client, as_user, monkeypatch):
    """call_llm_text 返回空文本(调用失败被内部吞掉) -> 502。"""
    _login(as_user)
    _capture_llm(monkeypatch, reply="")
    res = api_client.post("/api/editor/ai", json={"action": "summarize", "text": "内容"})
    assert res.status_code == 502


# ---------- POST /api/editor/ai/stream ----------


def _capture_stream(monkeypatch, chunks):
    """把 _stream_llm_text 替换为按 chunks 产出的假生成器,返回 captured 容器。"""
    captured = {}

    async def fake_stream(messages):
        captured["messages"] = messages
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr("app.api.editor_ai._stream_llm_text", fake_stream)
    return captured


def _sse_events(body):
    """把 SSE 响应体解析为事件列表;`data: [DONE]` 归一为 "[DONE]"。"""
    events = []
    for part in body.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        assert part.startswith("data: "), part
        payload = part[6:]
        events.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return events


def test_editor_ai_stream_requires_auth(api_client):
    """未登录 -> 401(校验先于流式返回)。"""
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "你好"})
    assert res.status_code == 401


def test_editor_ai_stream_rejects_bad_params(api_client, as_user):
    """与 /ai 相同的校验:action 白名单 / text 空或超限 / context 超限 -> 400。"""
    _login(as_user)
    cases = [
        {"action": "unknown", "text": "你好"},
        {"action": "polish"},
        {"action": "polish", "text": "   "},
        {"action": "polish", "text": "x" * 8001},
        {"action": "polish", "text": "x", "context": "c" * 4001},
    ]
    for body in cases:
        res = api_client.post("/api/editor/ai/stream", json=body)
        assert res.status_code == 400, f"{body.get('action')!r} -> {res.status_code}"


def test_editor_ai_stream_llm_not_configured(api_client, as_user, monkeypatch):
    """LLM 未配置 -> 500,文案与现有接口一致。"""
    _login(as_user)
    monkeypatch.setattr(settings, "llm_api_url", "")
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "你好"})
    assert res.status_code == 500
    assert res.json()["detail"] == "未配置 LLM API"


def test_editor_ai_stream_deltas_then_done(api_client, as_user, monkeypatch):
    """两个 delta + [DONE],SSE 顺序与格式正确,提示词复用 action 构造。"""
    _login(as_user)
    captured = _capture_stream(monkeypatch, ["你好", "，世界"])
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "原始文本"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert _sse_events(res.text) == [
        {"type": "delta", "content": "你好"},
        {"type": "delta", "content": "，世界"},
        "[DONE]",
    ]
    assert "原始文本" in json.dumps(captured["messages"], ensure_ascii=False)


def _patch_httpx_client(monkeypatch, lines):
    """把 editor_ai 使用的 httpx.AsyncClient 换成固定产出 lines 的假实现。"""
    class FakeResp:
        async def aiter_lines(self):
            for line in lines:
                yield line

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("app.api.editor_ai.httpx.AsyncClient", FakeClient)


def test_editor_ai_stream_parses_upstream_sse(api_client, as_user, monkeypatch):
    """真实 _stream_llm_text:只转发 data: 行,[DONE] 后停止,忽略非 data 行。"""
    _login(as_user)
    lines = [
        ": keep-alive",
        "",
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"不应出现"}}]}',
    ]
    _patch_httpx_client(monkeypatch, lines)
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "原文"})
    assert res.status_code == 200
    assert _sse_events(res.text) == [
        {"type": "delta", "content": "你"},
        {"type": "delta", "content": "好"},
        "[DONE]",
    ]


def test_editor_ai_stream_upstream_error_emits_error_event(api_client, as_user, monkeypatch):
    """上游中途异常 -> error 事件 + [DONE],连接不裸断。"""
    _login(as_user)

    async def fake_stream(messages):
        yield "部分"
        raise RuntimeError("上游断开")

    monkeypatch.setattr("app.api.editor_ai._stream_llm_text", fake_stream)
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "原文"})
    assert res.status_code == 200
    events = _sse_events(res.text)
    assert events[0] == {"type": "delta", "content": "部分"}
    assert events[1]["type"] == "error"
    assert "上游断开" in events[1]["content"]
    assert events[-1] == "[DONE]"


def test_editor_ai_stream_upstream_http_error(api_client, as_user, monkeypatch):
    """上游 HTTP 非 2xx -> error 事件带状态码 + [DONE]。"""
    _login(as_user)

    class FakeResp:
        def raise_for_status(self):
            request = httpx.Request("POST", "http://llm.local/v1/chat/completions")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("503", request=request, response=response)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("app.api.editor_ai.httpx.AsyncClient", FakeClient)
    res = api_client.post("/api/editor/ai/stream", json={"action": "polish", "text": "原文"})
    assert res.status_code == 200
    events = _sse_events(res.text)
    assert events == [
        {"type": "error", "content": "LLM API 调用失败: 503"},
        "[DONE]",
    ]


# ---------- POST /api/editor/ai/complete ----------


def test_editor_ai_complete_requires_auth(api_client):
    """未登录 -> 401。"""
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前文"})
    assert res.status_code == 401


def test_editor_ai_complete_rejects_bad_params(api_client, as_user):
    """text_before 空/超限、text_after 超限、context 超限 -> 400。"""
    _login(as_user)
    cases = [
        {},
        {"text_before": ""},
        {"text_before": "   "},
        {"text_before": "x" * 2001},
        {"text_before": "x", "text_after": "y" * 1001},
        {"text_before": "x", "context": "c" * 2001},
    ]
    for body in cases:
        res = api_client.post("/api/editor/ai/complete", json=body)
        assert res.status_code == 400, f"{body} -> {res.status_code}"


def test_editor_ai_complete_cleans_suggestion(api_client, as_user, monkeypatch):
    """取首行,去掉开头引号与「续写:」前缀,丢弃多余行。"""
    _login(as_user)
    _capture_llm(monkeypatch, reply="  “续写：这是续写的自然接续。”\n多余的第二行")
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前面的话没写完"})
    assert res.status_code == 200
    assert res.json() == {"suggestion": "这是续写的自然接续。"}


def test_editor_ai_complete_truncates_to_120(api_client, as_user, monkeypatch):
    """超过 120 字符的续写被截断。"""
    _login(as_user)
    _capture_llm(monkeypatch, reply="好" * 200)
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前文"})
    assert res.status_code == 200
    assert res.json() == {"suggestion": "好" * 120}


def test_editor_ai_complete_prompt_contains_before_after_context(api_client, as_user, monkeypatch):
    """光标前文/后文/上下文都进入补全提示词。"""
    _login(as_user)
    captured = _capture_llm(monkeypatch, reply="自然接续")
    res = api_client.post(
        "/api/editor/ai/complete",
        json={"text_before": "光标前面的内容", "text_after": "光标后面的内容", "context": "整页背景"},
    )
    assert res.status_code == 200
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "光标前面的内容" in prompt
    assert "光标后面的内容" in prompt
    assert "整页背景" in prompt


def test_editor_ai_complete_empty_llm_result_returns_200_empty(api_client, as_user, monkeypatch):
    """上游返回空/纯空白 -> 200 + 空建议(不是 502)。"""
    _login(as_user)
    _capture_llm(monkeypatch, reply="   \n")
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前文"})
    assert res.status_code == 200
    assert res.json() == {"suggestion": ""}


def test_editor_ai_complete_llm_not_configured(api_client, as_user, monkeypatch):
    """LLM 未配置 -> 500,文案同现有接口。"""
    _login(as_user)
    monkeypatch.setattr(settings, "llm_api_url", "")
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前文"})
    assert res.status_code == 500
    assert res.json()["detail"] == "未配置 LLM API"


def test_editor_ai_complete_llm_exception_returns_502(api_client, as_user, monkeypatch):
    """上游调用抛异常 -> 502,带简短原因。"""
    _login(as_user)

    async def boom(messages, context="", timeout=180.0, model=""):
        raise RuntimeError("网关超时")

    monkeypatch.setattr("app.api.editor_ai.call_llm_text", boom)
    res = api_client.post("/api/editor/ai/complete", json={"text_before": "前文"})
    assert res.status_code == 502
    assert "网关超时" in res.json()["detail"]
