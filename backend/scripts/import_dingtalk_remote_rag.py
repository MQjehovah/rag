"""通过老师服务器后端接口，将最新版钉钉Markdown导入现有“钉钉知识库”。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.remote_rag import DingTalkRemoteRAGImporter  # noqa: E402


def print_preflight(result, plan_path="") -> int:
    visible = {
        "pipeline_version": "dingtalk-markdown-pipeline-v24",
        "total": result["total"],
        "passed": result["passed"],
        "failed": result["failed"],
        "failures": result["failures"],
        "plan_path": str(plan_path),
    }
    print(json.dumps(visible, ensure_ascii=False, indent=2), flush=True)
    return 1 if result["failed"] else 0


async def run(args) -> int:
    importer = DingTalkRemoteRAGImporter()

    def show_progress(document, index, total, status):
        print(
            f"[{index}/{total}] {status}: {document.title}",
            flush=True,
        )

    try:
        try:
            if args.dry_run:
                result = importer.preflight(args.document_id, args.limit)
                plan_path = importer.write_preflight_plan(result)
                return print_preflight(result, plan_path)
            if args.remote_check:
                result = await importer.inspect_remote_target()
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 0
            if args.audit_integrity:
                result = await importer.audit_remote_integrity(
                    concurrency=args.audit_concurrency,
                    write_report=True,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 1 if result["issue_count"] else 0
            if args.repair_integrity:
                result = await importer.repair_remote_integrity(
                    concurrency=args.audit_concurrency,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 0 if result["status"] in {"repaired", "already_consistent"} else 1
            if args.search_query:
                result = await importer.verify_remote_search(args.search_query)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 0 if result["passed"] else 1
            result = await importer.import_manifest(
                document_ids=args.document_id,
                limit=args.limit,
                force=args.force,
                snapshot=not args.no_snapshot,
                on_progress=show_progress,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 1 if result.get("failed") else 0
        except Exception as exc:
            print(json.dumps({
                "status": "failed",
                "error": str(exc),
            }, ensure_ascii=False, indent=2), flush=True)
            return 1
    finally:
        await importer.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "只把通过最新转换质量门禁的Markdown，通过HTTP接口导入老师服务器"
            "现有的“钉钉知识库”。"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只执行本地质量门禁，不连接或写入老师服务器。",
    )
    parser.add_argument(
        "--remote-check",
        action="store_true",
        help="只读核对老师服务器中唯一的“钉钉知识库”和页面数量，不写入页面。",
    )
    parser.add_argument(
        "--search-query",
        action="append",
        default=[],
        help="只读验证远端检索，可重复传入多个查询词。",
    )
    parser.add_argument(
        "--audit-integrity",
        action="store_true",
        help="只读回查全部受跟踪远程页面，生成内容哈希、来源元数据和二进制污染报告。",
    )
    parser.add_argument(
        "--repair-integrity",
        action="store_true",
        help="先审计和快照，再只修复内容漂移或来源元数据异常的受跟踪页面。",
    )
    parser.add_argument(
        "--audit-concurrency",
        type=int,
        default=4,
        help="远端完整性回读并发数，默认4，范围1到12。",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="只处理指定钉钉文档ID，可重复传入。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="按稳定排序只处理前N份，0表示不限制。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使远程内容未变化，也重新写入并建立索引。",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="不生成写入前远程快照；正式同步不建议使用。",
    )
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (
        args.dry_run,
        args.remote_check,
        args.search_query,
        args.audit_integrity,
        args.repair_integrity,
    ))
    if selected_modes > 1:
        parser.error("各只读/修复模式不能同时使用")
    if args.limit < 0:
        parser.error("--limit不能小于0")
    if not 1 <= args.audit_concurrency <= 12:
        parser.error("--audit-concurrency必须在1到12之间")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
