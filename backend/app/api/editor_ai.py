"""编辑器 AI 助手接口: POST /api/editor/ai。

按 action 选用专用提示词,经由 app.core.llm.call_llm_text 调用已配置的 LLM,
统一返回 Markdown 文本;不写审计表、不落库,输入仅截断 200 字记录日志。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core.jwt_utils import get_current_user
from app.core.llm import call_llm_text

router = APIRouter(prefix="/api/editor", tags=["编辑器AI"])

logger = logging.getLogger(__name__)

TEXT_MAX = 8000
CONTEXT_MAX = 4000
DEFAULT_TARGET_LANG = "英文"
LOG_SNIPPET = 200

# 每个 action 独立 system/user 提示词;统一要求输出 Markdown、不解释、不寒暄。
# system/user 中除 {text}/{target_lang} 外不得出现花括号,组装时直接 format。
ACTION_PROMPTS = {
    "polish": {
        "system": (
            "你是资深中文编辑,负责润色文本:修正错别字、病句与标点,理顺语序,使表达更通顺专业。"
            "保持原意、语气与 Markdown 结构不变。只输出润色后的 Markdown,不要解释,不要寒暄。"
        ),
        "user": "请润色以下内容:\n\n{text}",
    },
    "fix": {
        "system": (
            "你是中文校对助手,负责修正文本中的错别字、语法错误、标点误用与明显的 Markdown 格式问题。"
            "不改变原意,不做风格改写。只输出修正后的 Markdown,不要解释,不要寒暄。"
        ),
        "user": "请校对并修正以下内容:\n\n{text}",
    },
    "summarize": {
        "system": (
            "你是知识整理助手,负责把给定内容总结为要点列表:覆盖关键信息,不添加原文之外的内容。"
            "只输出 Markdown 要点列表,不要解释,不要寒暄。"
        ),
        "user": "请把以下内容总结为要点列表:\n\n{text}",
    },
    "expand": {
        "system": (
            "你是中文写作助手,负责扩写内容:在不偏离原意的前提下补充细节、例子与过渡,"
            "保持原有 Markdown 结构与标题层级。只输出扩写后的 Markdown,不要解释,不要寒暄。"
        ),
        "user": "请扩写以下内容:\n\n{text}",
    },
    "translate": {
        "system": (
            "你是专业翻译,负责把 Markdown 文本翻译为{target_lang}:忠实原意、术语准确、行文自然,"
            "完整保留标题、列表、表格、引用与代码块等 Markdown 结构;代码、URL、专有名词不翻译。"
            "只输出译文,不要解释,不要寒暄。"
        ),
        "user": "请把以下内容翻译为{target_lang}:\n\n{text}",
    },
    "continue": {
        "system": (
            "你是中文写作助手,负责顺着前文自然续写:保持人称、语气、术语与 Markdown 结构一致。"
            "只输出续写部分,不重复已有文字,不要解释,不要寒暄。"
        ),
        "user": "请续写以下内容,只输出续写部分:\n\n{text}",
    },
    "to_table": {
        "system": (
            "你是信息整理助手,负责把给定内容整理为 Markdown 表格:以字段作表头,逐条对应填入;"
            "原文没有的信息留空,不要臆造。只输出 Markdown 表格,不要解释,不要寒暄。"
        ),
        "user": "请把以下内容整理为 Markdown 表格:\n\n{text}",
    },
    "outline": {
        "system": (
            "你是文档结构助手,负责为给定内容生成多级标题大纲:层级清晰、覆盖完整,不展开正文。"
            "只输出 Markdown 多级标题大纲,不要解释,不要寒暄。"
        ),
        "user": "请为以下内容生成多级标题大纲:\n\n{text}",
    },
}


class EditorAIRequest(BaseModel):
    action: str = ""
    text: str = ""
    context: str = ""
    target_lang: str = ""


def _build_messages(action: str, text: str, context: str, target_lang: str) -> list:
    """组装对应 action 的系统/用户提示词;continue 的 context 为光标前文,其余为参考上下文。"""
    spec = ACTION_PROMPTS[action]
    user = spec["user"].format(text=text, target_lang=target_lang)
    if context:
        if action == "continue":
            user += f"\n\n光标前文(续写需与其衔接):\n\n{context}"
        else:
            user += f"\n\n参考上下文(光标前后文或整页摘要):\n\n{context}"
    return [
        {"role": "system", "content": spec["system"].format(target_lang=target_lang)},
        {"role": "user", "content": user},
    ]


@router.post("/ai")
async def editor_ai(request: EditorAIRequest, current_user=Depends(get_current_user)):
    """编辑器 AI 助手:按 action 调用 LLM,返回 {"result": "<Markdown 文本>"}。"""
    action = request.action.strip()
    if action not in ACTION_PROMPTS:
        raise HTTPException(status_code=400, detail="不支持的 action")
    text = request.text
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    if len(text) > TEXT_MAX:
        raise HTTPException(status_code=400, detail=f"text 超过 {TEXT_MAX} 字符限制")
    if len(request.context) > CONTEXT_MAX:
        raise HTTPException(status_code=400, detail=f"context 超过 {CONTEXT_MAX} 字符限制")

    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    target_lang = request.target_lang.strip() or DEFAULT_TARGET_LANG
    messages = _build_messages(action, text, request.context, target_lang)
    try:
        result = await call_llm_text(messages, context=f"editor-ai:{action}")
    except Exception as e:
        logger.warning(f"editor-ai[{action}] LLM call failed: {e}; input(截断): {text[:LOG_SNIPPET]}")
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {str(e)[:100]}")
    if not result:
        logger.warning(f"editor-ai[{action}] LLM 返回为空; input(截断): {text[:LOG_SNIPPET]}")
        raise HTTPException(status_code=502, detail="LLM 返回为空")
    return {"result": result}
