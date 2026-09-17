# Grader v2 人工审阅表（校准澄清，真实 v1 对照）

- v1 列 = 真实 wiki-default-grader/v1 引擎；参考为 v1-probe-references 代理参考（每行 v1_limitation 标注限制），不冒充实测官方参考答案。
- v2 列 = 保守原型；needs_review 不计通过、不晋升、不缩分母。
- 指标同时展示正确判定/误通过/误拒绝/待审率/故障率；待审率不等同质量达标。

| 样例 | 类别 | 预期质量 | v1（真实引擎） | v2 | 依据 |
|---|---|---|---|---|---|
| S1 | faithful_synonym | good | fail（v1误拒绝） | needs_review（v2待审（保守，非误通过）） | 候选与来源语义一致，仅用同义词表达主题；原型 coverage 为精确词匹配会误报缺项——语义覆盖需人工/模型或受控同义通道，不承诺用词表解决一般语义。 |
| S10 | unsupported_new_fact | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 候选在忠实内容后新增来源未记载的事实断言；无词表可一般性识别，原型未设 C 级通道时放行 → 记录为误通过盲区，需结构化声明与人工/模型通道。 |
| S11 | value_ok_subject_or_condition_wrong | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 候选把变电站例外数值写入通用建筑场景且无开放项；原型不判主体 → 误通过。 |
| S12 | general_rule_vs_exception | bad | fail（v1正确拒绝） | needs_review（v2待审（保守，非误通过）） | 平原与高海拔两组数值均出现但互换；无开放项时原型放行 → 误通过盲区。 |
| S2 | faithful_synonym | good | fail（v1误拒绝） | needs_review（v2待审（保守，非误通过）） | 数值 0.10 mm 与区间以同义句式给出；原型数值检查只依赖单位与数值，不受措辞影响。 |
| S3 | keywords_negation_or_causal_opposite | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 候选含全部关键词与数值，却写「不应执行断电复核」等相反要求；原型不解析否定/因果关系 → 误通过；属 C 级语义差异。 |
| S4 | value_ok_subject_or_condition_wrong | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 候选数值齐全但把例外写成通用；原型不越权判定主体语义，保留 C 级开放项。 |
| S5 | unit_and_version_mixup | bad | fail（v1正确拒绝） | fail（v2正确拒绝） | 候选将电压写成 60 mV（无 60 V 出现）；原型按单位归一化定位 V 数值 → 判 fail。 |
| S6 | unit_and_version_mixup | bad | fail（v1正确拒绝） | fail（v2正确拒绝） | 旧版参数 3600 s 无对应 v1 区段且新版区段独存 → 版本归属缺失判 fail。 |
| S7 | general_rule_vs_exception | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 通用与洁净室数值齐全但归属相反；原型不越权判定适用范围映射，保留 C 级开放项。 |
| S8 | citation_unsupported_conclusion | bad | pass（v1误通过（缺陷复现）） | needs_review（v2待审（保守，非误通过）） | 候选带引用 [src-7]，被引资料与结论无关；原型不得以引用 ID 判通过 → 交人工或独立模型。 |
| S9 | key_omission | bad | fail（v1正确拒绝） | needs_review（v2待审（保守，非误通过）） | 候选遗漏全部预防/长期措施类必需项与对应数值；coverage/claims 缺失判 fail。 |

## 汇总

- v1（真实引擎 + 代理参考）：误通过 6、误拒绝 2、正确拒绝 4（缺陷复现归因 v1）；
- v2：正确拒绝 2、待审 10；无误通过/误拒绝（未判定一律待审）；
- metrics：needs_review_rate=0.8333，failure_rate=0.0000；待审不等于通过；待审率高不代表质量达标，也不代表零错误——指标必须与人工/模型判定补齐后再解释
- 待人工争议样例：S1/S3/S4/S7/S8/S10（v2-human-review.json）。
- 验收样例：v2-acceptance-review.json（A1–A5，pending，未用于开发）。
