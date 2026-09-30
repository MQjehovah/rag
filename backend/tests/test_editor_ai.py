"""编辑器 AI 助手接口(/api/editor/ai):鉴权 / 参数校验 / 提示词组装 / LLM 错误映射。"""
import json

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
