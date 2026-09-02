# V3 评测基线（P0-BE-02）

V3 计划 211 行：「保存当前 Recall、Citation、平均延迟和 Judge 结果为 V3 baseline」。

## 数据来源与限制

| 项 | 说明 |
| --- | --- |
| 检索指标来源 | `metrics-w12-v1.md`（2026-08-11 活体评测，脚本 `scripts/eval_baseline.py`，端点 `/api/search`） |
| Judge 指标来源 | `metrics-w8.md`（2026-08-10 活体评测，脚本 `scripts/eval_v2.py`，端点 `/api/search/v2`） |
| 存档时间 | 2026-08-18 |
| 数据日期 | 2026-08-10 ~ 2026-08-11 |

**限制（重要）**：

1. 存档当日（2026-08-18）外部模型服务不可用，无法重跑活体评测：
   - `bms-cn.xzrobot.com` embedding/rerank 端点 400 "Model not found in the model list"（`/rag/v1/models` 返回空列表，模型未加载）；
   - 本地 ollama（11434）502；
   - 智谱 LLM key 401 认证失败。
2. 数据采集于 P0 改造（2026-08-17/18）之前。P0 均为工程基建（WAL、线程化、短事务、Alembic 基线、测试隔离），**未改动检索算法与分值逻辑**，故作为「V3 改动前基线」语义成立；但延迟数字基于旧代码路径，P0 性能改造（BE-03/04/05/06）的收益不反映在本基线中，服务恢复后建议补跑一次校准。
3. Judge 指标（W8，2026-08-10）与检索指标（W12-v1，2026-08-11）采集日期相差一天，期间无检索代码变更，可合并视为 V3 基线。

## 总体指标（V3 Baseline）

### /api/search（Page 检索，eval_baseline.py）

_数据日期 2026-08-11，后端 http://127.0.0.1:8000，top_k=10，评测集 tests/eval/eval_dataset.jsonl（70 题）_

| 指标 | 数值 |
| --- | --- |
| 题目总数 | 70 |
| Recall@5 | **18.6%** |
| Recall@10 | **30.0%** |
| 引用准确率（Citation Precision） | **6.2%** |
| 平均响应时长(ms) | **3097** |
| P95 响应时长(ms) | 3863 |
| 失败题数 | 0 |

### /api/search/v2（KO 检索 + Judge，eval_v2.py）

_数据日期 2026-08-10，top_k=10；存量 published KO 仅 9 个，KO 指标如实反映审核覆盖不足_

| 指标 | 数值 |
| --- | --- |
| 题目总数 | 70 |
| KO Recall@5 | **7.1%** |
| KO Recall@10 | **7.1%** |
| 审判通过率（judgment_pass_rate） | **14.3%** |
| 不确定率（uncertain_rate） | **85.7%** |
| 失败题数 | 0 |

## 按题型分布（/api/search）

| 题型 | 题数 | Recall@5 | Recall@10 | 引用准确率 | 平均时长(ms) |
| --- | --- | --- | --- | --- | --- |
| decision | 10 | 40.0% | 50.0% | 10.0% | 3322 |
| deployment | 15 | 20.0% | 20.0% | 3.3% | 3071 |
| diagnostic | 15 | 6.7% | 33.3% | 6.0% | 3155 |
| fact | 20 | 25.0% | 35.0% | 9.2% | 3077 |
| version | 10 | 0.0% | 10.0% | 1.0% | 2863 |

## 意图分布（/api/search/v2）

- fact_lookup：38
- procedure：22
- diagnostic：7
- decision：2
- version_diff：1

## V3 目标对照（V3 计划 9.5 阶段目标 / 16 节试点目标）

| 指标 | V3 基线 | V3 目标 |
| --- | --- | --- |
| Page Recall@10（/api/search Recall@10） | 30.0% | ≥ 50% |
| Card Recall@10（KO Recall@10） | 7.1% | ≥ 40% |
| 引用准确率 | 6.2% | ≥ 60% |

平均延迟与审判通过率在 V3 计划中无明确数字目标，作为基线记录供改进前后对比。

## 结论与建议

- Recall@10 基线仅 30.0%，主要瓶颈：published KO 覆盖不足（403 个 KO 仅 9 个 published）、chunk 检索召回弱（W12 报告结论：向量召回或分词链路有重大缺陷）。
- 审判通过率 14.3% 低不代表回答错误——published KO 缺证据/冲突时 Judge 正确拒答（uncertain 85.7%），P1 Evidence 层落地是提升前提。
- 外部服务恢复后应补跑活体评测：重启后端（加载 P0 改动）→ `scripts/eval_baseline.py` + `scripts/eval_v2.py` → 校准本基线。
