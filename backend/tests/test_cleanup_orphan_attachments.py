"""孤儿附件清理脚本的纯函数测试:URL 提取 / 相对路径归一化 / 差集。

只测纯函数, 不连接数据库/磁盘/MinIO。
"""
from scripts.cleanup_orphan_attachments import (
    collect_referenced_keys,
    compute_orphans,
    extract_attachment_keys,
    keys_from_relative_paths,
    normalize_attachment_key,
)

KEY = "attachments/20240101/abc123-def.pdf"


class TestNormalizeAttachmentKey:
    def test_local_api_url(self):
        assert normalize_attachment_key("/api/upload/attachments/20240101/abc123-def.pdf") == KEY

    def test_local_url_with_public_base_path(self):
        assert normalize_attachment_key("/rag/api/upload/attachments/20240101/abc123-def.pdf") == KEY

    def test_strips_signature_query(self):
        raw = "/api/upload/attachments/20240101/abc123-def.pdf?sig=deadbeef&exp=1893456000"
        assert normalize_attachment_key(raw) == KEY

    def test_strips_fragment(self):
        assert normalize_attachment_key("/api/upload/attachments/20240101/abc123-def.pdf#page=2") == KEY

    def test_percent_encoding_restored(self):
        raw = "/api/upload/attachments/20240101/%E6%8A%A5%E5%91%8A%20v2.pdf"
        assert normalize_attachment_key(raw) == "attachments/20240101/报告 v2.pdf"

    def test_minio_object_url(self):
        raw = "http://192.168.31.8:9000/xzrobotserver/attachments/20240101/abc123-def.pdf"
        assert normalize_attachment_key(raw) == KEY

    def test_relative_paths(self):
        assert normalize_attachment_key("attachments/20240101/abc123-def.pdf") == KEY
        assert normalize_attachment_key("attachments\\20240101\\abc123-def.pdf") == KEY
        assert normalize_attachment_key("20240101/abc123-def.pdf") == KEY

    def test_rejects_traversal(self):
        assert normalize_attachment_key("/api/upload/attachments/../../etc/passwd") is None
        assert normalize_attachment_key("attachments/20240101/../evil.pdf") is None
        assert normalize_attachment_key("..\\..\\evil.pdf") is None

    def test_rejects_non_attachment_shapes(self):
        assert normalize_attachment_key("") is None
        assert normalize_attachment_key(None) is None
        assert normalize_attachment_key("/api/upload/images/20240101/a.png") is None
        assert normalize_attachment_key("attachments/notadate/a.pdf") is None
        assert normalize_attachment_key("attachments/20240101/sub/dir.pdf") is None


class TestExtractAttachmentKeys:
    def test_markdown_with_title_and_punctuation(self):
        text = "见图 ![报告](/api/upload/attachments/20240101/abc123-def.pdf \"标题\")。"
        assert extract_attachment_keys(text) == {KEY}

    def test_html_attributes(self):
        text = (
            '<div data-attachment url="/api/upload/attachments/20240101/abc123-def.pdf" '
            'name="报告.pdf"></div>'
        )
        assert extract_attachment_keys(text) == {KEY}

    def test_signed_url_query_is_stripped(self):
        text = "下载: /api/upload/attachments/20240101/abc123-def.pdf?sig=abc&exp=1234567890"
        assert extract_attachment_keys(text) == {KEY}

    def test_url_encoded_minio(self):
        text = (
            "http://minio.local:9000/bucket/attachments/20240101/"
            "%E6%8A%A5%E5%91%8A%20v2.pdf"
        )
        assert extract_attachment_keys(text) == {"attachments/20240101/报告 v2.pdf"}

    def test_multiple_refs_dedupe(self):
        text = (
            "![](/api/upload/attachments/20240101/a.pdf) "
            "![x](/api/upload/attachments/20240101/a.pdf?sig=1) "
            "![y](/api/upload/attachments/20240102/b.png)"
        )
        assert extract_attachment_keys(text) == {
            "attachments/20240101/a.pdf",
            "attachments/20240102/b.png",
        }

    def test_ignores_traversal_and_other_urls(self):
        text = (
            "/api/upload/attachments/../../etc/passwd "
            "/api/upload/images/20240101/a.png "
            "https://example.com/attachments/20240101/ok.pdf"
        )
        assert extract_attachment_keys(text) == {"attachments/20240101/ok.pdf"}

    def test_empty_input(self):
        assert extract_attachment_keys(None) == set()
        assert extract_attachment_keys("") == set()

    def test_collect_referenced_keys_union(self):
        keys = collect_referenced_keys(
            [None, "![a](/api/upload/attachments/20240101/a.pdf)", "![b](/api/upload/attachments/20240102/b.pdf)"]
        )
        assert keys == {"attachments/20240101/a.pdf", "attachments/20240102/b.pdf"}


class TestKeysFromRelativePaths:
    def test_normalizes_valid_paths(self):
        keys = keys_from_relative_paths([
            "attachments/20240101/a.pdf",
            "attachments\\20240101\\b c.txt",
            "20240101/c.txt",
            "/attachments/20240101/d.png",
        ])
        assert keys == {
            "attachments/20240101/a.pdf",
            "attachments/20240101/b c.txt",
            "attachments/20240101/c.txt",
            "attachments/20240101/d.png",
        }

    def test_skips_illegal_paths(self):
        keys = keys_from_relative_paths([
            "../escape.pdf",
            "attachments/20240101/../evil.pdf",
            "attachments/notadate/x.pdf",
            "attachments/20240101/sub/dir.pdf",
            "",
        ])
        assert keys == set()


class TestComputeOrphans:
    def test_difference(self):
        files = {"attachments/20240101/a.pdf", "attachments/20240101/b.pdf", "attachments/20240102/c.png"}
        referenced = {"attachments/20240101/b.pdf"}
        assert compute_orphans(files, referenced) == {"attachments/20240101/a.pdf", "attachments/20240102/c.png"}

    def test_no_orphans(self):
        assert compute_orphans({"a"}, {"a", "b"}) == set()

    def test_all_orphans(self):
        assert compute_orphans({"a", "b"}, set()) == {"a", "b"}
