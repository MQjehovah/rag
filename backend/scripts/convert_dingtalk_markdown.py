"""将已下载到raw目录的钉钉原文件批量转换并保存为Markdown。"""

import argparse
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.dingtalk_converter import DingTalkMarkdownConverter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="读取本地raw原文件，转换并保存到markdown目录，不访问钉钉接口。"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="重新转换清单中的全部文档，包括已经转换成功的文档。",
    )
    parser.add_argument(
        "--no-retry-failed",
        action="store_true",
        help="本次不重试之前转换失败的文档。",
    )
    parser.add_argument(
        "--only-converted",
        action="store_true",
        help="只重新处理当前已转换成功且仍有效的文档，用于转换器升级后的安全重转。",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="只处理指定钉钉文档ID，可重复传入。",
    )
    parser.add_argument(
        "--extensions",
        default="",
        help="只处理指定格式，使用英文逗号分隔，例如 docx,pptx,xlsx。",
    )
    parser.add_argument(
        "--quarantine-invalid",
        action="store_true",
        help="转换前隔离历史生成的二进制异常Markdown，并将原文件恢复为待转换状态。",
    )
    parser.add_argument(
        "--quarantine-only",
        action="store_true",
        help="只执行异常Markdown隔离，不继续批量转换。",
    )
    args = parser.parse_args()

    if args.only_converted and not args.force:
        parser.error("--only-converted 必须与 --force 一起使用")

    converter = DingTalkMarkdownConverter()
    if args.quarantine_invalid or args.quarantine_only:
        audit = converter.quarantine_invalid_markdown()
        print(f"异常Markdown隔离完成：共 {audit['rejected']} 个", flush=True)
        if args.quarantine_only:
            return 0

    def show_progress(entry, index, total, status):
        title = entry.get("name") or entry.get("document_id") or "未命名"
        print(f"[{index}/{total}] {status}: {title}", flush=True)

    document_ids = list(args.document_id)
    if args.only_converted:
        manifest = converter.storage.read_manifest()
        requested_ids = set(args.document_id)
        document_ids = [
            str(entry.get("document_id") or "")
            for entry in manifest["documents"]
            if entry.get("source_status", "active") == "active"
            and entry.get("conversion_status") == "converted"
            and str(entry.get("document_id") or "").strip()
            and (
                not requested_ids
                or str(entry.get("document_id") or "") in requested_ids
            )
        ]
        if not document_ids:
            print("没有找到需要重新转换的有效文档", flush=True)
            return 0

    result = converter.convert_manifest(
        force=args.force,
        retry_failed=not args.no_retry_failed,
        extensions=[
            extension.strip() for extension in args.extensions.split(",")
            if extension.strip()
        ],
        document_ids=document_ids,
        on_progress=show_progress,
    )
    print(
        f"转换完成：共处理 {result['total']}，成功 {result['converted']}，"
        f"失败 {result['failed']}",
        flush=True,
    )
    for failure in result["failures"]:
        print(
            f"失败：{failure['name']}（{failure['document_id']}）：{failure['error']}",
            flush=True,
        )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
