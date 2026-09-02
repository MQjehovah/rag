"""知识库盘点脚本（T1.1）。

通过后端 API 登录，拉取所有 Notebook 与 Page，按以下维度统计：
- Notebook 分布
- 文件类型分布（按扩展名 / MIME）
- 来源类型分布（dingtalk / 本地上传 / 手写）
- 更新时间分布（近 7 天 / 30 天 / 90 天 / 更早）
- 索引状态分布（current / stale / missing / empty）

用法：
    cd backend
    python scripts/inventory.py --base-url http://127.0.0.1:8000 \
        --username admin --password 123456 \
        --output ../docs/inventory-report.md

也可通过环境变量 INVENTORY_BASE_URL / INVENTORY_USERNAME / INVENTORY_PASSWORD 配置。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PAGE_SIZE = 100
INDEX_TIME_BUCKETS = [
    ("近 7 天", 7),
    ("近 30 天", 30),
    ("近 90 天", 90),
    ("更早", None),
]


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.token: str | None = None
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        resp = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败：{resp.status_code} {resp.text}")
        self.token = resp.json()["token"]
        self.client.headers["Authorization"] = f"Bearer {self.token}"

    def list_notebooks(self) -> list[dict]:
        resp = self.client.get("/api/notebooks")
        resp.raise_for_status()
        return resp.json()

    def list_pages(self) -> Iterable[dict]:
        page = 1
        while True:
            resp = self.client.get(
                "/api/pages",
                params={"page": page, "page_size": DEFAULT_PAGE_SIZE},
            )
            resp.raise_for_status()
            data = resp.json()
            yield from data["items"]
            if page * DEFAULT_PAGE_SIZE >= data["total"]:
                return
            page += 1

    def get_page(self, page_id: str) -> dict:
        resp = self.client.get(f"/api/pages/{page_id}")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.client.close()


def _infer_extension(page: dict) -> str:
    source_path = page.get("source_path") or ""
    if source_path:
        suffix = Path(source_path).suffix.lower().lstrip(".")
        if suffix:
            return suffix or "unknown"
    title = page.get("title") or ""
    if title:
        suffix = Path(title).suffix.lower().lstrip(".")
        if suffix:
            return suffix
    return "无扩展名"


def _bucket_updated(updated_str: str, now: datetime) -> str:
    try:
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "未知时间"
    if updated.tzinfo is not None:
        updated = updated.astimezone(None).replace(tzinfo=None)
    delta_days = (now - updated).days
    for label, days in INDEX_TIME_BUCKETS:
        if days is None or delta_days <= days:
            return label
    return "未知时间"


def collect_full_pages(client: ApiClient) -> list[dict]:
    summaries = list(client.list_pages())
    full_pages: list[dict] = []
    total = len(summaries)
    for idx, item in enumerate(summaries, 1):
        page = client.get_page(item["id"])
        full_pages.append(page)
        if idx % 25 == 0 or idx == total:
            print(f"  拉取页面详情 {idx}/{total}", file=sys.stderr)
    return full_pages


def build_report(notebooks: list[dict], pages: list[dict], generated_at: datetime) -> str:
    nb_name = {nb["id"]: nb["name"] for nb in notebooks}
    total = len(pages)

    def _counter(get_key) -> collections.Counter:
        c: collections.Counter = collections.Counter()
        for p in pages:
            c[get_key(p)] += 1
        return c

    by_notebook = _counter(lambda p: nb_name.get(p.get("notebook_id")) or "未分组")
    by_source_type = _counter(lambda p: p.get("source_type") or "本地")
    by_extension = _counter(_infer_extension)
    by_status = _counter(lambda p: p.get("index_status") or "missing")
    by_bucket = collections.Counter(_bucket_updated(p.get("updated_at") or "", generated_at) for p in pages)

    def _md_table(header: list[str], rows: list[list[str]]) -> str:
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
        ]
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    def _dist_rows(counter: collections.Counter, total_count: int) -> list[list[str]]:
        rows = []
        for key, count in counter.most_common():
            pct = (count / total_count * 100) if total_count else 0.0
            rows.append([str(key), str(count), f"{pct:.1f}%"])
        return rows

    sections: list[str] = []
    sections.append("# 知识库盘点报告\n")
    sections.append(f"_生成时间：{generated_at.isoformat(timespec='seconds')}_\n")
    sections.append("## 总览\n")
    sections.append(_md_table(
        ["指标", "数量"],
        [
            ["Notebook 总数", str(len(notebooks))],
            ["Page 总数", str(total)],
            ["已索引(current)", str(by_status.get("current", 0))],
            ["索引过时(stale)", str(by_status.get("stale", 0))],
            ["索引缺失(missing)", str(by_status.get("missing", 0))],
            ["空内容(empty)", str(by_status.get("empty", 0))],
        ],
    ))
    sections.append("")

    sections.append("## 按 Notebook 分布\n")
    sections.append(_md_table(["Notebook", "Page 数", "占比"], _dist_rows(by_notebook, total)))
    sections.append("")

    sections.append("## 按来源(source_type)分布\n")
    sections.append(_md_table(["来源", "Page 数", "占比"], _dist_rows(by_source_type, total)))
    sections.append("")

    sections.append("## 按文件扩展名分布\n")
    sections.append(_md_table(["扩展名", "Page 数", "占比"], _dist_rows(by_extension, total)))
    sections.append("")

    sections.append("## 按索引状态分布\n")
    status_order = ["current", "stale", "missing", "empty"]
    status_rows = [
        [s, str(by_status.get(s, 0)), f"{(by_status.get(s, 0) / total * 100):.1f}%" if total else "0.0%"]
        for s in status_order
    ]
    for k, v in by_status.items():
        if k not in status_order:
            status_rows.append([k, str(v), f"{(v / total * 100):.1f}%" if total else "0.0%"])
    sections.append(_md_table(["索引状态", "Page 数", "占比"], status_rows))
    sections.append("")

    sections.append("## 按更新时间分布\n")
    bucket_rows = [[label, str(by_bucket.get(label, 0))] for label, _ in INDEX_TIME_BUCKETS]
    if by_bucket.get("未知时间"):
        bucket_rows.append(["未知时间", str(by_bucket["未知时间"])])
    sections.append(_md_table(["时间段", "Page 数"], bucket_rows))
    sections.append("")

    sections.append("## 后续建议\n")
    stale_pct = (by_status.get("stale", 0) / total * 100) if total else 0.0
    missing_pct = (by_status.get("missing", 0) / total * 100) if total else 0.0
    notes = []
    if total == 0:
        notes.append("- 知识库当前为空，建议先同步钉钉文档或手动上传资料。")
    else:
        if stale_pct > 20:
            notes.append(f"- stale 占比 {stale_pct:.1f}%，建议调用 `/api/pages/reindex-all` 重建索引。")
        if missing_pct > 10:
            notes.append(f"- missing 占比 {missing_pct:.1f}%，存在未索引的 Page，运行 W3 编译器前需先补齐。")
        recent_90 = sum(by_bucket.get(label, 0) for label in ("近 7 天", "近 30 天", "近 90 天"))
        if recent_90 == 0:
            notes.append("- 近 90 天没有任何更新，建议确认同步链路是否还在运行。")
        if len(by_extension) == 1:
            notes.append("- 文件类型高度单一，建议在评测集中覆盖多种文档形态。")
    if not notes:
        notes.append("- 知识库整体健康，可进入 W2 数据模型阶段。")
    sections.extend(notes)
    sections.append("")

    return "\n".join(sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知识库盘点")
    parser.add_argument("--base-url", default=os.environ.get("INVENTORY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.environ.get("INVENTORY_USERNAME", "admin"))
    parser.add_argument("--password", default=os.environ.get("INVENTORY_PASSWORD", "123456"))
    parser.add_argument("--output", default="../docs/inventory-report.md")
    parser.add_argument("--json-out", default=None, help="可选：原始 JSON 数据导出路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    parsed_url = urlparse(args.base_url)
    if not parsed_url.scheme or not parsed_url.netloc:
        print(f"非法的 base_url：{args.base_url}", file=sys.stderr)
        return 2

    print(f"连接后端：{args.base_url}", file=sys.stderr)
    client = ApiClient(args.base_url, args.username, args.password)
    try:
        notebooks = client.list_notebooks()
        print(f"Notebook 数量：{len(notebooks)}", file=sys.stderr)
        pages = collect_full_pages(client)
        print(f"Page 总数：{len(pages)}", file=sys.stderr)
    finally:
        client.close()

    generated_at = datetime.now()
    report = build_report(notebooks, pages, generated_at)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"报告已写入：{output_path}", file=sys.stderr)

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": generated_at.isoformat(),
                    "notebooks": notebooks,
                    "pages": pages,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"原始数据已导出：{json_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
