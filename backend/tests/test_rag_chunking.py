import pytest

from app.core.rag import EmbeddingService, VectorStore


@pytest.mark.asyncio
async def test_pdf_chunks_keep_page_context_and_merge_short_tail():
    service = EmbeddingService()
    content = (
        "# 部署手册\n\n"
        "> 来源：钉钉知识库\n\n"
        "## 第 1 页 - 安装准备\n\n"
        + "安装说明。" * 100
        + "\n\n## 第 2 页 - 连接电池\n\n"
        + "电池连接步骤。" * 100
        + "\n\nThank you"
    )

    try:
        chunks = service.split_text(content)
    finally:
        await service.close()

    page_two_chunks = [chunk for chunk in chunks if "电池连接步骤" in chunk]
    assert page_two_chunks
    assert all("## 第 2 页 - 连接电池" in chunk for chunk in page_two_chunks)
    assert not any(chunk.strip() == "Thank you" for chunk in chunks)
    assert not any(chunk.strip() in {"#", "##"} for chunk in chunks)


@pytest.mark.asyncio
async def test_encode_chunks_uses_batch_embedding(monkeypatch):
    service = EmbeddingService()
    calls = []
    monkeypatch.setattr(
        service,
        "split_text",
        lambda _content, _title="": ["第一块", "第二块", "第三块"],
    )

    async def fake_encode_batch(texts, batch_size=32):
        calls.append((texts, batch_size))
        return [[1.0], [], [3.0]]

    monkeypatch.setattr(service, "encode_batch", fake_encode_batch)
    try:
        chunks = await service.encode_chunks("正文")
    finally:
        await service.close()

    assert calls == [(["第一块", "第二块", "第三块"], 32)]
    assert chunks == [("第一块", [1.0]), ("第三块", [3.0])]


def test_chunk_metadata_distinguishes_image_table_and_text():
    image = VectorStore._chunk_metadata(
        "## 第 18 页 - Battery\n\n### 本页重要图片\n\n"
        "![图](/api/upload/pdf-pages/" + "a" * 64 + "/page-18.jpg)"
    )
    table = VectorStore._chunk_metadata(
        "## 第 36 页 - Rules\n\n### 本页识别表格\n\n| Name | Value |"
    )
    text = VectorStore._chunk_metadata(
        "## 第 2 页 - Description\n\n普通说明正文"
    )

    assert image == {
        "content_type": "image_caption",
        "page_number": 18,
        "image_id": "page-18.jpg",
    }
    assert table["content_type"] == "table"
    assert table["page_number"] == 36
    assert text["content_type"] == "text"
    assert text["page_number"] == 2
