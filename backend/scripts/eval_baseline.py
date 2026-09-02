"""基线评测脚本（T1.3）。

通过后端 `/api/search` 接口对评测集逐题检索，计算：
- Recall@5 / Recall@10：前 K 个结果中是否命中 hint 关键词
- 引用准确率（citation_precision）：命中关键词的结果数 / 返回结果数
- 平均响应时长（ms）

判定逻辑（hit）：题目 `expected_answer_hint` 按 `/` 切分为候选关键词集合，
若检索结果的 `title + content` 中包含任意一个候选关键词（子串匹配，忽略大小写与空格），
则该结果视为命中。

用法：
    cd backend
    python scripts/eval_baseline.py --base-url http://127.0.0.1:8000 \
        --username admin --password 123456 \
        --dataset tests/eval/eval_dataset.jsonl \
        --output ../docs/baseline-metrics.md

也可通过环境变量 EVAL_BASE_URL / EVAL_USERNAME / EVAL_PASSWORD 配置。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "tests" / "eval" / "eval_dataset.jsonl"
DEFAULT_TOP_K = 10
RECALL_K_VALUES = (5, 10)


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0):
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

    def search(self, query: str, top_k: int) -> dict:
        resp = self.client.post(
            "/api/search",
            json={"query": query, "top_k": top_k},
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.client.close()


def load_dataset(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"第 {line_no} 行不是合法 JSON：{exc}")
    return records


def split_hints(hint: str) -> list[str]:
    """把 `电池接口/固定螺钉` 拆成 `[电池接口, 固定螺钉]`，剔除空串。"""
    return [p.strip() for p in (hint or "").split("/") if p.strip()]


def _normalize(text: str) -> str:
    return (text or "").lower().replace(" ", "")


def result_is_hit(result: dict, hints: list[str]) -> bool:
    if not hints:
        return False
    haystack = _normalize((result.get("title") or "") + (result.get("content") or ""))
    return any(_normalize(h) in haystack for h in hints)


def compute_recall_at_k(results: list[dict], hints: list[str], k: int) -> bool:
    """Recall@K：前 K 个结果中至少有一个命中即视为召回。"""
    if not hints:
        return False
    for result in results[:k]:
        if result_is_hit(result, hints):
            return True
    return False


def compute_citation_precision(results: list[dict], hints: list[str]) -> float:
    """引用准确率：命中关键词的结果数 / 实际返回结果数。"""
    if not results:
        return 0.0
    hits = sum(1 for r in results if result_is_hit(r, hints))
    return hits / len(results)


def run_eval(
    client: ApiClient,
    records: list[dict],
    top_k: int = DEFAULT_TOP_K,
    progress: callable = None,
) -> list[dict]:
    """逐题运行检索并记录指标。返回每题的明细行。"""
    rows: list[dict] = []
    for idx, rec in enumerate(records, 1):
        hints = split_hints(rec.get("expected_answer_hint", ""))
        t0 = time.perf_counter()
        try:
            payload = client.search(rec["question"], top_k=top_k)
        except Exception as exc:
            rows.append({
                "id": rec["id"],
                "type": rec["type"],
                "question": rec["question"],
                "hints": hints,
                "error": str(exc),
                "latency_ms": 0.0,
                "result_count": 0,
                "recall@5": False,
                "recall@10": False,
                "citation_precision": 0.0,
            })
            if progress:
                progress(idx, len(records), rec, error=True)
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        results = payload.get("results") or []
        recall5 = compute_recall_at_k(results, hints, 5)
        recall10 = compute_recall_at_k(results, hints, 10)
        precision = compute_citation_precision(results, hints)
        rows.append({
            "id": rec["id"],
            "type": rec["type"],
            "question": rec["question"],
            "hints": hints,
            "latency_ms": elapsed_ms,
            "result_count": len(results),
            "recall@5": recall5,
            "recall@10": recall10,
            "citation_precision": precision,
            "top_titles": [r.get("title") or "" for r in results[:3]],
        })
        if progress:
            progress(idx, len(records), rec, error=False)
    return rows


def aggregate(rows: list[dict]) -> dict:
    """按类型聚合指标。返回 {type: metrics}，外加 `总体`。"""
    def _agg(subset: list[dict]) -> dict:
        n = len(subset)
        if n == 0:
            return {"count": 0}
        valid_latencies = [r["latency_ms"] for r in subset if r.get("latency_ms", 0) > 0]
        avg_ms = sum(valid_latencies) / len(valid_latencies) if valid_latencies else 0.0
        p95_ms = sorted(valid_latencies)[int(0.95 * (len(valid_latencies) - 1))] if len(valid_latencies) >= 2 else (valid_latencies[0] if valid_latencies else 0.0)
        return {
            "count": n,
            "recall@5": sum(1 for r in subset if r.get("recall@5")) / n,
            "recall@10": sum(1 for r in subset if r.get("recall@10")) / n,
            "citation_precision": sum(r.get("citation_precision", 0.0) for r in subset) / n,
            "avg_latency_ms": avg_ms,
            "p95_latency_ms": p95_ms,
            "errors": sum(1 for r in subset if r.get("error")),
        }

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    metrics = {t: _agg(subset) for t, subset in sorted(by_type.items())}
    metrics["总体"] = _agg(rows)
    return metrics


def build_report(rows: list[dict], metrics: dict, generated_at: float, base_url: str, top_k: int) -> str:
    sections: list[str] = []
    sections.append("# 基线评测报告\n")
    from datetime import datetime
    sections.append(f"_生成时间：{datetime.fromtimestamp(generated_at).isoformat(timespec='seconds')}_\n")
    sections.append(f"_后端：{base_url}  top_k={top_k}_\n")
    sections.append("## 总体指标\n")
    overall = metrics["总体"]
    sections.append("| 指标 | 数值 |")
    sections.append("| --- | --- |")
    sections.append(f"| 题目总数 | {overall['count']} |")
    sections.append(f"| Recall@5 | {overall['recall@5']:.1%} |")
    sections.append(f"| Recall@10 | {overall['recall@10']:.1%} |")
    sections.append(f"| 引用准确率 | {overall['citation_precision']:.1%} |")
    sections.append(f"| 平均响应时长(ms) | {overall['avg_latency_ms']:.0f} |")
    sections.append(f"| P95 响应时长(ms) | {overall['p95_latency_ms']:.0f} |")
    sections.append(f"| 失败题数 | {overall['errors']} |")
    sections.append("")
    sections.append("## 按题型分布\n")
    sections.append("| 题型 | 题数 | Recall@5 | Recall@10 | 引用准确率 | 平均时长(ms) |")
    sections.append("| --- | --- | --- | --- | --- | --- |")
    for t, m in metrics.items():
        if t == "总体":
            continue
        sections.append(
            f"| {t} | {m['count']} | {m['recall@5']:.1%} | {m['recall@10']:.1%} | "
            f"{m['citation_precision']:.1%} | {m['avg_latency_ms']:.0f} |"
        )
    sections.append("")

    sections.append("## 失败样例\n")
    failures = [r for r in rows if r.get("error") or not r.get("recall@10")]
    if not failures:
        sections.append("- 全部题目在 Recall@10 内命中。")
    else:
        sections.append("| id | type | question | 失败原因 |")
        sections.append("| --- | --- | --- | --- |")
        for r in failures[:30]:
            reason = r.get("error") or "Recall@10 未命中"
            sections.append(f"| {r['id']} | {r['type']} | {r['question']} | {reason} |")
    sections.append("")

    sections.append("## 后续建议\n")
    notes: list[str] = []
    if overall["count"] == 0:
        notes.append("- 评测集为空，请检查数据集路径与格式。")
    else:
        if overall["recall@10"] < 0.5:
            notes.append("- Recall@10 < 50%，向量召回或分词链路有重大缺陷，应优先排查 Embedding/VectorStore。")
        if overall["citation_precision"] < 0.3:
            notes.append("- 引用准确率偏低，建议在 W3 调整 chunk 切分粒度或加入重排阈值过滤。")
        if overall["avg_latency_ms"] > 3000:
            notes.append(f"- 平均响应 {overall['avg_latency_ms']:.0f}ms 偏高，W4 需引入异步与缓存。")
    if not notes:
        notes.append("- 基线表现健康，可进入 W2 数据模型与 W3 编译器阶段。")
    sections.extend(notes)
    sections.append("")
    return "\n".join(sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基线评测脚本")
    parser.add_argument("--base-url", default=os.environ.get("EVAL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.environ.get("EVAL_USERNAME", "admin"))
    parser.add_argument("--password", default=os.environ.get("EVAL_PASSWORD", "123456"))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default="../docs/baseline-metrics.md")
    parser.add_argument("--json-out", default=None, help="可选：逐题原始指标 JSON 导出路径")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def _progress(idx: int, total: int, rec: dict, error: bool) -> None:
    flag = "ERR" if error else "OK "
    print(f"[{idx}/{total}] {flag} {rec['id']} {rec['question'][:30]}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    dataset_path = Path(args.dataset)
    if not dataset_path.is_file():
        print(f"评测集不存在：{dataset_path}", file=sys.stderr)
        return 2

    records = load_dataset(dataset_path)
    print(f"加载评测集：{len(records)} 题", file=sys.stderr)

    print(f"连接后端：{args.base_url}", file=sys.stderr)
    client = ApiClient(args.base_url, args.username, args.password, timeout=120.0)
    try:
        rows = run_eval(client, records, top_k=args.top_k, progress=_progress)
    finally:
        client.close()

    generated_at = time.time()
    metrics = aggregate(rows)
    report = build_report(rows, metrics, generated_at, args.base_url, args.top_k)

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
                    "generated_at": generated_at,
                    "base_url": args.base_url,
                    "top_k": args.top_k,
                    "metrics": metrics,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"原始指标已导出：{json_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
