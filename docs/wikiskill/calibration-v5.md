# v5 评分校准（离线，未改冻结评分器 v1）

- 数据集：wiki-default-v5；评分器：`wiki-default-grader/v1`（未改动）。
- 探针为人工编写候选，直接运行现有评分器；无模型调用。

| 判别维度 | 探针任务 | 结果 | 说明 |
|---|---|---|---|
| synonym_ok_rewrite | v5-op-001（操作流程） | verdict=fail | 不能区分（phrase 精确匹配 → 合理同义被判失败） |
| keywords_ok_wrong_conclusion | v5-cfg-009（配置约束） | verdict=pass | 不能区分（值齐全即通过 → 错误结论误通过） |
| version_mixup | v5-mig-021（版本迁移） | verdict=fail | 能区分（value_in_version/absent_in_version 检出） |
| no_evidence_free_supplement | v5-ts-028（故障排查） | verdict=pass | 不能区分（无禁词自由补充不被拦截；forbidden 仅覆盖显式黑名单措辞） |
| key_omission | v5-ts-029（故障排查） | verdict=fail | 能区分（phrase/value 缺失检出） |
| applicability_confusable | v5-conf-034（多资料冲突与适用范围） | verdict=pass | 不能区分（适用范围张冠李戴但数值齐全 → 误通过） |

## 待人工确认

- 是否接受‘同义表达漏报’（建议 v1 维持现状或另立 grader 同义表版本）
- 关键词齐全但结论错误/适用范围混淆需语义级检查（建议另立 grader 版本，不覆盖 v1）
- 无证据自由补充是否纳入禁止规则（可能引入误伤）
- 人工复核全部 39 任务参考答案与探针文本
