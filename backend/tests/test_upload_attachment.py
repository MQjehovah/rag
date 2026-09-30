"""非图片附件接口测试:上传/下载/签名与安全边界。

附件复用图片的存储与签名访问机制,只是前缀换成 /api/upload/attachments/。
"""
from urllib.parse import parse_qs, quote, urlparse

import pytest

from app.config import settings
from app.core.image_sign import sign_image_url, verify_image_signature

ATTACHMENT_PATH = "/api/upload/attachments/20240101/a.pdf"


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _local_url_path(url: str) -> str:
    """去掉部署子路径前缀,拿到签名对象用的裸路径。"""
    prefix = settings.public_base_path
    if prefix and url.startswith(prefix + "/"):
        return url[len(prefix):]
    return url


@pytest.fixture
def local_attachments(tmp_path, monkeypatch):
    """强制本地存储并把附件目录指向临时目录(避免测试触碰真实 MinIO/磁盘)。"""
    from app.api import upload

    monkeypatch.setattr(upload, "MINIO_AVAILABLE", False)
    monkeypatch.setattr(upload, "ATTACHMENT_DIR", tmp_path / "attachments")
    return tmp_path / "attachments"


def _upload(api_client, filename="报告 v2.pdf", content=b"%PDF-1.4 hello", content_type="application/pdf"):
    return api_client.post(
        "/api/upload/attachment",
        files={"file": (filename, content, content_type)},
    )


def test_upload_attachment_requires_auth(api_client):
    """未登录不可上传(缺 token 与坏 token 都被拒绝)。"""
    assert _upload(api_client).status_code in (401, 403)
    bad = {"Authorization": "Bearer not-a-real-token"}
    res = api_client.post(
        "/api/upload/attachment",
        files={"file": ("a.pdf", b"x", "application/pdf")},
        headers=bad,
    )
    assert res.status_code == 401


def test_upload_attachment_happy_path(api_client, as_user, local_attachments):
    as_user([])
    content = b"%PDF-1.4 hello"
    res = _upload(api_client, content=content)
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "报告 v2.pdf"
    assert data["size"] == len(content)
    assert data["mime"] == "application/pdf"

    path = _local_url_path(data["url"])
    assert path.startswith("/api/upload/attachments/")
    date_dir, stored = path[len("/api/upload/attachments/"):].split("/")
    # 随机前缀 + 原名,且原文件的目录部分已被剥离
    assert stored.endswith("-报告 v2.pdf")
    assert (local_attachments / date_dir / stored).read_bytes() == content

    # 同一套签名机制:签名后能取回,未签名被拒绝
    signed = sign_image_url(path)
    base, _, query = signed.partition("?")
    got = api_client.get(quote(base) + "?" + query)  # 模拟浏览器对非 ASCII 路径的编码
    assert got.status_code == 200
    assert got.content == content
    assert api_client.get(quote(base)).status_code == 403


def test_sign_endpoint_signs_attachment_paths(api_client, as_user):
    """签名端点同时接受图片与附件路径;穿越路径不签名。"""
    as_user([])
    traversal = "/api/upload/attachments/../etc/passwd"
    res = api_client.post("/api/upload/images/sign", json={"urls": [ATTACHMENT_PATH, traversal]})
    assert res.status_code == 200
    urls = res.json()["urls"]
    q = _query(urls[0])
    assert verify_image_signature(ATTACHMENT_PATH, q["sig"], int(q["exp"]))
    assert urls[1] == traversal


def test_upload_attachment_rejects_oversize(api_client, as_user, local_attachments, monkeypatch):
    from app.api import upload

    monkeypatch.setattr(upload, "MAX_ATTACHMENT_BYTES", 1024)
    as_user([])
    res = _upload(api_client, content=b"x" * 2048)
    assert res.status_code == 413


@pytest.mark.parametrize("filename", ["evil.exe", "run.BAT", "script.ps1", "x.scr", "lib.dll"])
def test_upload_attachment_blocks_executables(api_client, as_user, local_attachments, filename):
    as_user([])
    assert _upload(api_client, filename=filename).status_code == 400


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("../../x.txt", "x.txt"),
        ("..\\..\\x.txt", "x.txt"),
        ("..", "attachment"),
    ],
)
def test_upload_attachment_sanitizes_filename(api_client, as_user, local_attachments, raw_name, expected):
    as_user([])
    res = _upload(api_client, filename=raw_name, content=b"data", content_type="text/plain")
    assert res.status_code == 200
    assert res.json()["name"] == expected

    path = _local_url_path(res.json()["url"])
    date_dir, stored = path[len("/api/upload/attachments/"):].split("/")
    assert stored.endswith("-" + expected)
    # 只落在附件目录内,没有逃逸到上级目录
    assert (local_attachments / date_dir / stored).is_file()
    assert not (local_attachments.parent / "x.txt").exists()


def _attachment_files(root) -> list:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_upload_attachment_same_content_dedupes(api_client, as_user, local_attachments):
    """同内容两次上传(文件名不同)→ 同一 URL,且不重复落盘。"""
    as_user([])
    content = b"identical-payload"
    first = _upload(api_client, filename="v1.pdf", content=content)
    second = _upload(api_client, filename="v2-复本.pdf", content=content)
    assert first.status_code == second.status_code == 200
    assert first.json()["url"] == second.json()["url"]
    # 第二次仍返回本次上传的原名(展示名),但 URL 指向同一份文件
    assert second.json()["name"] == "v2-复本.pdf"

    files = _attachment_files(local_attachments)
    assert len(files) == 1
    # 文件名前缀为内容 sha256 前 12 位
    assert len(files[0].name.split("-", 1)[0]) == 12
    assert files[0].read_bytes() == content

    # 去重后的 URL 仍可通过签名正常取回
    path = _local_url_path(second.json()["url"])
    signed = sign_image_url(path)
    base, _, query = signed.partition("?")
    got = api_client.get(quote(base) + "?" + query)
    assert got.status_code == 200
    assert got.content == content


def test_upload_attachment_same_name_different_content(api_client, as_user, local_attachments):
    """同名不同内容 → 两个不同 URL,目录里两个文件。"""
    as_user([])
    first = _upload(api_client, filename="报告.pdf", content=b"content-one")
    second = _upload(api_client, filename="报告.pdf", content=b"content-two")
    assert first.status_code == second.status_code == 200
    assert first.json()["url"] != second.json()["url"]
    assert len(_attachment_files(local_attachments)) == 2


def test_upload_attachment_dedupes_after_filename_sanitize(api_client, as_user, local_attachments):
    """原名含穿越路径时,净化后仍按内容去重且不逃逸目录。"""
    as_user([])
    content = b"traversal-dedupe"
    first = _upload(api_client, filename="../../evil.txt", content=content, content_type="text/plain")
    second = _upload(api_client, filename="..\\..\\evil.txt", content=content, content_type="text/plain")
    assert first.json()["url"] == second.json()["url"]
    files = _attachment_files(local_attachments)
    assert len(files) == 1
    assert files[0].name.endswith("-evil.txt")
    assert not (local_attachments.parent / "evil.txt").exists()


def test_find_existing_attachment_rejects_bad_prefix(tmp_path):
    """hash 前缀查找只认严格 12 位小写十六进制,`../../`/通配符等不误判。"""
    from app.api.upload import _find_existing_attachment

    date_path = tmp_path / "attachments"
    date_path.mkdir(parents=True)
    hit = date_path / "deadbeef0000-x.txt"
    hit.write_text("x")
    (tmp_path / "outside.txt").write_text("outside")

    assert _find_existing_attachment(date_path, "deadbeef0000") == hit
    for evil in ("../../", "..", "*", "deadbeef0000/../..", "deadbeef00", "ABCDEF012345", "", None):
        assert _find_existing_attachment(date_path, evil) is None
    # 目录不存在时同样返回 None(不抛错)
    assert _find_existing_attachment(tmp_path / "nope", "deadbeef0000") is None
