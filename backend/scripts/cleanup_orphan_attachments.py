"""孤儿附件清理工具(默认 dry-run)。

背景: 笔记正文、历史版本、wiki 页与页面评论里以自由文本形式存放附件 URL。
引用被删掉后, 磁盘/MinIO 上的文件就成了孤儿。本工具对账两个集合:

- 引用集: pages.content / page_revisions.content / wiki_pages.content /
  page_comments.content(实际列名以 models 为准, 评论列为 content);
- 文件集: 本地 <uploads-dir>/attachments/** 与(可选)MinIO `attachments/` 前缀。

输出差集清单与总大小, 默认只报告; `--apply` 才真正删除。

安全边界(为何不做「删页即删文件」):
- 引用是正文自由文本(含回收站页面、历史版本、评论), 删除/修改/版本裁剪等事件
  多点且并发, 即时删文件会在恢复版本或还原回收站时造成附件 404; 统一由此
  离线对账工具兜底。
- 数据库任一引用表读取失败立即中止, 绝不把「读不到引用」当成「无引用」删文件。
- 只处理 attachments/ 前缀, 不触碰图片上传目录与缓存。
- 本地文件名/URL 一律先做路径段校验(`..` 拒绝), 防目录穿越。

用法(backend 目录):
    python scripts/cleanup_orphan_attachments.py                    # dry-run 报告
    python scripts/cleanup_orphan_attachments.py --apply            # 执行删除
    python scripts/cleanup_orphan_attachments.py --uploads-dir data/uploads
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

from app.config import settings
from app.models.database import (
    Page,
    PageComment,
    PageRevision,
    WikiPage,
    get_engine,
    get_session,
)

# 本地 URL(/api/upload/attachments/...)与 MinIO URL(.../attachments/...)里
# 都含 /attachments/ 片段;裸相对路径(attachments/ 开头)也要能提取。
_CANDIDATE_RE = re.compile(
    r"(?:/attachments/|(?<![\w/])attachments/)"
    r"[^\s\"'<>()\[\]?#]+(?:/[^\s\"'<>()\[\]?#]+)*",
    re.IGNORECASE,
)

_TRAILING_PUNCT = ".,;:!?。，；：！？"


def normalize_attachment_key(raw: str | None) -> str | None:
    """把附件路径/URL 归一化为 `attachments/<date8>/<file>`;非法形态返回 None。

    - 接受本地 URL、MinIO URL、`attachments/...` 相对路径与裸 `<date8>/<file>`;
    - 剥离查询串(`?sig=` 等)与锚点, 做 URL 解码;
    - 只接受恰好两段(日期目录 + 文件名)且无 `..`/空段;日期须为 8 位数字。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = unquote(text).replace("\\", "/")
    marker = "/attachments/"
    idx = text.lower().find(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    elif text.lower().startswith("attachments/"):
        text = text[len("attachments/"):]
    text = text.strip("/").rstrip(_TRAILING_PUNCT)
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if len(parts) != 2 or any(p == ".." for p in parts):
        return None
    date_dir, file_name = parts
    if len(date_dir) != 8 or not date_dir.isdigit():
        return None
    return f"attachments/{date_dir}/{file_name}"


def extract_attachment_keys(text: str | None) -> set[str]:
    """从任意文本(笔记 Markdown/HTML/评论)提取附件引用键集合(纯函数)。"""
    if not text:
        return set()
    keys: set[str] = set()
    for match in _CANDIDATE_RE.finditer(text):
        key = normalize_attachment_key(match.group(0))
        if key:
            keys.add(key)
    return keys


def collect_referenced_keys(texts: Iterable[str | None]) -> set[str]:
    """把多段文本的引用键求并集(纯函数)。"""
    keys: set[str] = set()
    for text in texts:
        keys |= extract_attachment_keys(text)
    return keys


def keys_from_relative_paths(rel_paths: Iterable[str]) -> set[str]:
    """把相对路径(如 `attachments\\20240101\\a.pdf`)归一化为文件键集合(纯函数)。

    非法路径(含 `..`、缺日期目录等)直接忽略, 留在原地不参与删除。
    """
    keys: set[str] = set()
    for raw in rel_paths:
        key = normalize_attachment_key(str(raw))
        if key:
            keys.add(key)
    return keys


def compute_orphans(file_keys: Iterable[str], referenced_keys: Iterable[str]) -> set[str]:
    """孤儿 = 文件键 - 引用键(纯函数)。"""
    return set(file_keys) - set(referenced_keys)


def format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def collect_db_referenced_keys(session) -> set[str]:
    """读取四张引用表并求并集;任一表读取失败会抛出, 由调用方中止。"""
    keys: set[str] = set()
    for model, column in (
        (Page, Page.content),
        (PageRevision, PageRevision.content),
        (WikiPage, WikiPage.content),
        (PageComment, PageComment.content),
    ):
        for (value,) in session.query(column).yield_per(500):
            keys |= extract_attachment_keys(value)
    return keys


def scan_local_files(uploads_dir: Path) -> tuple[dict[str, Path], int]:
    """扫描 <uploads-dir>/attachments/**;返回 (键→路径, 被忽略的非法路径数)。"""
    files: dict[str, Path] = {}
    skipped = 0
    attachments_dir = uploads_dir / "attachments"
    if not attachments_dir.is_dir():
        return files, skipped
    for path in attachments_dir.rglob("*"):
        if not path.is_file():
            continue
        key = normalize_attachment_key(path.relative_to(uploads_dir).as_posix())
        if key:
            files[key] = path
        else:
            skipped += 1
    return files, skipped


def scan_minio_objects() -> tuple[dict[str, int] | None, str]:
    """列出 MinIO `attachments/` 前缀;不可用返回 (None, 原因)。"""
    try:
        from minio import Minio
    except ImportError:
        return None, "minio 未安装, 已跳过 MinIO"
    if not settings.minio_endpoint:
        return None, "未配置 MinIO, 已跳过"
    try:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        objects: dict[str, int] = {}
        for obj in client.list_objects(settings.minio_bucket, prefix="attachments/", recursive=True):
            key = normalize_attachment_key(obj.object_name)
            if key:
                objects[key] = int(obj.size or 0)
        return objects, f"MinIO 可用, 列出 {len(objects)} 个对象"
    except Exception as exc:
        return None, f"MinIO 不可用({type(exc).__name__}), 已跳过"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="孤儿附件清理(默认 dry-run, --apply 才删除)")
    parser.add_argument("--apply", action="store_true", help="真正删除孤儿文件(默认只报告)")
    parser.add_argument("--uploads-dir", default="data/uploads", help="本地上传根目录(默认 data/uploads)")
    args = parser.parse_args(argv)

    engine = get_engine(settings.database_url)
    session = get_session(engine)
    try:
        referenced = collect_db_referenced_keys(session)
    except Exception as exc:
        print(f"[abort] 读取数据库引用集失败, 未删除任何文件: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()

    uploads_dir = Path(args.uploads_dir)
    local_files, skipped = scan_local_files(uploads_dir)
    minio_objects, minio_note = scan_minio_objects()
    minio_keys = set(minio_objects or {})

    file_keys = set(local_files) | minio_keys
    orphans = compute_orphans(file_keys, referenced)

    sizes: dict[str, int] = {}
    for key in orphans:
        size = 0
        if key in local_files:
            try:
                size = max(size, local_files[key].stat().st_size)
            except OSError:
                pass
        if key in minio_keys:
            size = max(size, minio_objects.get(key, 0))
        sizes[key] = size
    total_bytes = sum(sizes.values())

    print(f"[scan] 数据库引用 {len(referenced)} 个")
    print(f"[scan] 本地文件 {len(local_files)} 个 (跳过非法路径 {skipped} 个) @ {uploads_dir / 'attachments'}")
    print(f"[scan] {minio_note}")
    print(f"[scan] 孤儿 {len(orphans)} 个, 合计 {format_size(total_bytes)}")
    if orphans:
        for key in sorted(orphans):
            stores = []
            if key in local_files:
                stores.append("local")
            if key in minio_keys:
                stores.append("minio")
            print(f"  - {key}  [{format_size(sizes[key])}] ({'+'.join(stores)})")

    if not args.apply:
        print("[dry-run] 未删除任何文件;确认清单后加 --apply 执行删除")
        return 0

    removed_local = 0
    failed_local = 0
    removed_minio = 0
    failed_minio = 0

    minio_client = None
    if minio_keys:
        try:
            from minio import Minio

            minio_client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
        except Exception as exc:
            print(f"[warn] MinIO 客户端创建失败, 跳过 MinIO 删除: {exc}", file=sys.stderr)

    for key in sorted(orphans):
        if key in local_files:
            try:
                local_files[key].unlink()
                removed_local += 1
            except OSError as exc:
                failed_local += 1
                print(f"[error] 本地删除失败 {key}: {exc}", file=sys.stderr)
        if key in minio_keys and minio_client is not None:
            try:
                minio_client.remove_object(settings.minio_bucket, key)
                removed_minio += 1
            except Exception as exc:
                failed_minio += 1
                print(f"[error] MinIO 删除失败 {key}: {exc}", file=sys.stderr)

    print(f"[apply] 本地删除 {removed_local} 个(失败 {failed_local})")
    print(f"[apply] MinIO 删除 {removed_minio} 个(失败 {failed_minio})")
    return 1 if (failed_local or failed_minio) else 0


if __name__ == "__main__":
    raise SystemExit(main())
