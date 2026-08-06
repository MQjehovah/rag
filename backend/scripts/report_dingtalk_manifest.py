"""输出钉钉同步清单汇总，并按需执行本地文件完整性审计。"""

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="查看manifest.json各阶段状态，或核验本地原文件与Markdown。"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="逐文件检查路径、大小、SHA-256、UTF-8和RAG回写字段。",
    )
    parser.add_argument(
        "--record-audit",
        action="store_true",
        help="执行审计并把本次审计摘要写回manifest.json。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="使用JSON格式输出，便于其他程序读取。",
    )
    args = parser.parse_args()

    storage = DingTalkLocalStorage()
    manifest = storage.read_manifest()
    result = {
        "manifest_path": str(storage.manifest_path),
        "version": manifest.get("version"),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "last_inventory_at": manifest.get("last_inventory_at"),
        "last_audit": manifest.get("last_audit"),
        "summary": manifest.get("summary"),
    }
    if args.audit or args.record_audit:
        result["audit"] = storage.audit_manifest(record=args.record_audit)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summary = result["summary"] or {}
        print(f"清单版本：{result['version']}")
        print(f"清单路径：{result['manifest_path']}")
        print(f"文档数量：{summary.get('document_count', 0)}")
        print(f"流水线状态：{summary.get('pipeline_status_counts', {})}")
        print(f"下载状态：{summary.get('download_status_counts', {})}")
        print(f"转换状态：{summary.get('conversion_status_counts', {})}")
        print(f"RAG状态：{summary.get('rag_status_counts', {})}")
        print(f"错误文档：{summary.get('error_document_count', 0)}")
        if "audit" in result:
            audit = result["audit"]
            print(
                f"完整性审计：{'通过' if audit['valid'] else '未通过'}，"
                f"错误 {audit['error_count']}，警告 {audit['warning_count']}"
            )
            for item in audit["errors"]:
                print(f"错误：{item['name']}（{item['document_id']}）：{item['message']}")
            for item in audit["warnings"]:
                print(f"警告：{item['name']}（{item['document_id']}）：{item['message']}")
    return 1 if result.get("audit", {}).get("error_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
