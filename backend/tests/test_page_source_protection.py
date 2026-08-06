import hashlib

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.pages import create_page, import_source_page, update_page
from app.models.database import Base, Notebook, Page
from app.models.schema import PageCreate, PageUpdate, SourcePageImport


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Notebook(id="notebook-1", name="同步文档测试"))
    content = "# 原始同步正文"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    session.add(Page(
        id="page-1",
        notebook_id="notebook-1",
        title="钉钉文档",
        content=content,
        source_type="dingtalk",
        source_id="node-1",
        source_content=content,
        source_content_hash=digest,
        content_hash=digest,
        indexed_content_hash=digest,
        index_dirty=False,
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.mark.asyncio
async def test_dingtalk_page_rejects_implicit_content_update(db):
    with pytest.raises(HTTPException) as exc:
        await update_page(
            "page-1",
            PageUpdate(content="被隐式改写的正文"),
            BackgroundTasks(),
            db,
            {"groups": ["__local_admin__"]},
        )

    assert exc.value.status_code == 409
    assert db.get(Page, "page-1").content == "# 原始同步正文"


@pytest.mark.asyncio
async def test_explicit_source_edit_marks_index_stale(db):
    updated = await update_page(
        "page-1",
        PageUpdate(content="# 人工修改正文", allow_source_edit=True),
        BackgroundTasks(),
        db,
        {"groups": ["__local_admin__"]},
    )

    assert updated.content == "# 人工修改正文"
    assert updated.content_hash == hashlib.sha256(
        "# 人工修改正文".encode("utf-8")
    ).hexdigest()
    assert updated.index_status == "stale"
    assert updated.source_content == "# 原始同步正文"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "%PDF-1.7\n3 0 obj\nstream\n二进制内容",
        "PK\x03\x04\x14\x00Office压缩包内容",
        "普通标题\x00二进制空字节",
    ],
)
async def test_create_page_rejects_binary_content(db, content):
    with pytest.raises(HTTPException) as exc:
        await create_page(
            PageCreate(
                title="错误文件.pdf",
                content=content,
                notebook_id="notebook-1",
            ),
            BackgroundTasks(),
            db,
            {"groups": ["__local_admin__"]},
        )

    assert exc.value.status_code == 400
    assert "请先转换为Markdown" in exc.value.detail


@pytest.mark.asyncio
async def test_update_page_rejects_binary_content_even_when_source_edit_allowed(db):
    with pytest.raises(HTTPException) as exc:
        await update_page(
            "page-1",
            PageUpdate(
                content="%PDF-1.7\n3 0 obj\nstream\n错误正文",
                allow_source_edit=True,
            ),
            BackgroundTasks(),
            db,
            {"groups": ["__local_admin__"]},
        )

    assert exc.value.status_code == 400
    assert db.get(Page, "page-1").content == "# 原始同步正文"


@pytest.mark.asyncio
async def test_create_page_allows_normal_markdown(db):
    page = await create_page(
        PageCreate(
            title="正常文档.pdf",
            content="# 正常文档\n\n这是转换后的Markdown正文。",
            notebook_id="notebook-1",
        ),
        BackgroundTasks(),
        db,
        {"groups": ["__local_admin__"]},
    )

    assert page.content.startswith("# 正常文档")


@pytest.mark.asyncio
async def test_source_import_adopts_page_and_persists_protection_metadata(db):
    ordinary = Page(
        id="ordinary-page",
        notebook_id="notebook-1",
        title="旧页面",
        content="旧正文",
    )
    db.add(ordinary)
    db.commit()
    content = "# 收费标准\n\n这是最新版Markdown正文。"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    imported = await import_source_page(
        SourcePageImport(
            page_id="ordinary-page",
            title="收费标准.pdf",
            content=content,
            notebook_id="notebook-1",
            source_id="node-收费标准",
            source_path="服务政策/收费标准.pdf",
            source_space_id="space-1",
            source_url="https://alidocs.dingtalk.com/i/nodes/node-收费标准",
            source_file_hash="a" * 64,
            source_file_size=1234,
            source_mime_type="application/pdf",
            source_markdown_hash="b" * 64,
            source_pipeline_version="dingtalk-markdown-pipeline-v24",
            published_content_hash=digest,
        ),
        db,
        {"groups": ["__local_admin__"]},
    )

    assert imported.id == "ordinary-page"
    assert imported.source_type == "dingtalk"
    assert imported.source_id == "node-收费标准"
    assert imported.source_pipeline_version == "dingtalk-markdown-pipeline-v24"
    assert imported.source_markdown_hash == "b" * 64
    assert imported.index_status == "missing"


@pytest.mark.asyncio
async def test_source_import_rejects_wrong_published_hash(db):
    with pytest.raises(HTTPException) as exc:
        await import_source_page(
            SourcePageImport(
                title="收费标准.pdf",
                content="# 正常Markdown",
                notebook_id="notebook-1",
                source_id="node-2",
                source_markdown_hash="b" * 64,
                source_pipeline_version="dingtalk-markdown-pipeline-v24",
                published_content_hash="0" * 64,
            ),
            db,
            {"groups": ["__local_admin__"]},
        )

    assert exc.value.status_code == 400
    assert "哈希" in exc.value.detail
