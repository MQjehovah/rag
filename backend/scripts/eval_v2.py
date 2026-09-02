"""检索评测脚本：逐题调 /api/search/v2，统计 Card 检索与审判指标。

指标：
- Card Recall@5/10：card_citations 中 title/body 命中 expected_answer_hint 关键词
- judgment 通过率：judgment.passed 为 True 的比例
- uncertain 率：uncertain 为 True 的比例
- 意图分布

注意：普通问答只查 Published Card。Recall 偏低时应优先检查 Card 审核覆盖、
证据质量和检索链路。

用法：
    cd backend
    python scripts/eval_v2.py --output ../docs/metrics-w8.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "tests" / "eval" / "eval_dataset.jsonl"
TOP_K = 10


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.token: str | None = None
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        resp = self.client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败：{resp.status_code} {resp.text}")
        self.token = resp.json()["token"]
        self.client.headers["Authorization"] = f"Bearer {self.token}"

    def search_v2(self, question: str, top_k: int) -> dict:
        resp = self.client.post("/api/search/v2", json={"question": question, "top_k": top_k})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.client.close()


def load_dataset(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def split_hints(hint: str) -> list[str]:
    return [p.strip() for p in (hint or "").split("/") if p.strip()]


def _normalize(text: str) -> str:
    return (text or "").lower().replace(" ", "")


def card_hits(card_citations: list[dict], hints: list[str]) -> bool:
    if not hints:
        return False
    for card in card_citations:
        haystack = _normalize((card.get("title") or "") + (card.get("body") or ""))
        if any(_normalize(h) in haystack for h in hints):
            return True
    return False


def run_eval(client: ApiClient, records: list[dict], top_k: int = TOP_K) -> list[dict]:
    rows = []
    for idx, rec in enumerate(records, 1):
        hints = split_hints(rec.get("expected_answer_hint", ""))
        try:
            payload = client.search_v2(rec["question"], top_k=top_k)
        except Exception as exc:
            rows.append({"id": rec["id"], "type": rec["type"], "question": rec["question"],
                         "error": str(exc), "recall@5": False, "recall@10": False,
                         "judgment_passed": False, "uncertain": True, "intent": ""})
            print(f"[{idx}/{len(records)}] ERR {rec['id']} {exc}", file=sys.stderr)
            continue
        cards = payload.get("card_citations") or []
        recall5 = card_hits(cards[:5], hints)
        recall10 = card_hits(cards[:10], hints)
        rows.append({
            "id": rec["id"],
            "type": rec["type"],
            "question": rec["question"],
            "intent": payload.get("intent", ""),
            "recall@5": recall5,
            "recall@10": recall10,
            "judgment_passed": payload.get("judgment", {}).get("passed", False),
            "uncertain": payload.get("uncertain", True),
            "card_count": len(cards),
        })
        print(f"[{idx}/{len(records)}] OK {rec['id']} intent={payload.get('intent','')} "
              f"recall5={recall5} passed={payload.get('judgment',{}).get('passed',False)}",
              file=sys.stderr)
    return rows


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {}
    return {
        "count": n,
        "recall@5": sum(1 for r in rows if r.get("recall@5")) / n,
        "recall@10": sum(1 for r in rows if r.get("recall@10")) / n,
        "judgment_pass_rate": sum(1 for r in rows if r.get("judgment_passed")) / n,
        "uncertain_rate": sum(1 for r in rows if r.get("uncertain")) / n,
        "errors": sum(1 for r in rows if r.get("error")),
    }


def build_report(rows: list[dict], metrics: dict, base_url: str) -> str:
    from datetime import datetime
    sections = []
    sections.append("# W8 评测报告（/api/search/v2）\n")
    sections.append(f"_生成时间：{datetime.now().isoformat(timespec='seconds')}_")
    sections.append(f"_后端：{base_url}  top_k={TOP_K}_")
    sections.append(f"_存量 Published Card 共 {metrics.get('published_card_count', '?')} 个。_\n")
    sections.append("## 总体指标\n")
    sections.append("| 指标 | 数值 |")
    sections.append("| --- | --- |")
    sections.append(f"| 题目总数 | {metrics.get('count', 0)} |")
    sections.append(f"| Card Recall@5 | {metrics.get('recall@5', 0):.1%} |")
    sections.append(f"| Card Recall@10 | {metrics.get('recall@10', 0):.1%} |")
    sections.append(f"| 审判通过率 | {metrics.get('judgment_pass_rate', 0):.1%} |")
    sections.append(f"| 不确定率 | {metrics.get('uncertain_rate', 0):.1%} |")
    sections.append(f"| 失败题数 | {metrics.get('errors', 0)} |")
    sections.append("")
    sections.append("## 意图分布\n")
    intents = Counter(r.get("intent", "") for r in rows)
    for intent, count in intents.most_common():
        sections.append(f"- {intent or '(空)'}：{count}")
    sections.append("")
    sections.append("## 结论\n")
    sections.append("- 若 Recall@10 明显低于 Page 检索基线，优先补齐 Published Card 覆盖并检查召回链路。")
    sections.append("- 审判通过率低不代表回答错误，而是 Card 缺证据/冲突时正确拒答（'不确定'导向）。")
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="W8 评测脚本（v2 检索）")
    parser.add_argument("--base-url", default=os.environ.get("EVAL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.environ.get("EVAL_USERNAME", "admin"))
    parser.add_argument("--password", default=os.environ.get("EVAL_PASSWORD", "123456"))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default="../docs/metrics-w8.md")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        print(f"评测集不存在：{dataset_path}", file=sys.stderr)
        return 2
    records = load_dataset(dataset_path)
    print(f"加载评测集：{len(records)} 题", file=sys.stderr)

    # Published Card 数量（用于报告注明）
    try:
        import sqlite3
        conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "data" / "notes.db")
        pub_count = conn.execute("SELECT COUNT(*) FROM knowledge_cards WHERE status='published'").fetchone()[0]
        conn.close()
    except Exception:
        pub_count = "?"

    client = ApiClient(args.base_url, args.username, args.password, timeout=120.0)
    try:
        rows = run_eval(client, records, top_k=TOP_K)
    finally:
        client.close()

    metrics = aggregate(rows)
    metrics["published_card_count"] = pub_count
    report = build_report(rows, metrics, args.base_url)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"报告已写入：{output_path}", file=sys.stderr)
    print(f"总体：Recall@5={metrics['recall@5']:.1%} Recall@10={metrics['recall@10']:.1%} "
          f"审判通过率={metrics['judgment_pass_rate']:.1%} 不确定率={metrics['uncertain_rate']:.1%}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
