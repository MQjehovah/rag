# W8 评测报告（/api/search/v2）

_生成时间：2026-08-10T16:28:28_
_后端：http://127.0.0.1:8000  top_k=10_
_存量 published KO 仅 9 个，指标如实反映 KO 审核覆盖不足。_

## 总体指标

| 指标 | 数值 |
| --- | --- |
| 题目总数 | 70 |
| KO Recall@5 | 7.1% |
| KO Recall@10 | 7.1% |
| 审判通过率 | 14.3% |
| 不确定率 | 85.7% |
| 失败题数 | 0 |

## 意图分布

- fact_lookup：38
- procedure：22
- diagnostic：7
- decision：2
- version_diff：1

## 结论

- 若 Recall@10 明显低于 W1 基线（Page 检索），说明 published KO 覆盖不足，需优先补齐审核。
- 审判通过率低不代表回答错误，而是 KO 缺证据/冲突时正确拒答（'不确定'导向）。