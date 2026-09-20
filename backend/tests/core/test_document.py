"""文档解析与分块行为测试。

原先测的 app.core.document.parser / splitter 模块已不存在,当前职责合并到
app.core.rag.EmbeddingService(split_text / split_text_structured),故改测实际实现。
"""

from app.core.rag import EmbeddingService


def test_split_text_structured_records_heading_chain():
    """按标题切块时,每个块携带标题链作为检索上下文。"""
    svc = EmbeddingService()
    content = "## 第一节\n\n内容甲\n\n### 小节\n\n内容乙"

    chunks = svc.split_text_structured(content, title="文档")

    assert chunks
    contexts = [c["context"] for c in chunks]
    assert "文档 > 第一节" in contexts
    assert "文档 > 第一节 > 小节" in contexts
    assert all(c["text"] for c in chunks)


def test_split_text_drops_blank_chunks():
    """纯空白内容不产生分块,避免写入空向量。"""
    svc = EmbeddingService()

    assert svc.split_text("   ") == []
    assert svc.split_text("第一段内容") == ["第一段内容"]
