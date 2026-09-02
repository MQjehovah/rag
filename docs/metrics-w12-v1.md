# 基线评测报告

_生成时间：2026-08-11T08:55:42_

_后端：http://127.0.0.1:8000  top_k=10_

## 总体指标

| 指标 | 数值 |
| --- | --- |
| 题目总数 | 70 |
| Recall@5 | 18.6% |
| Recall@10 | 30.0% |
| 引用准确率 | 6.2% |
| 平均响应时长(ms) | 3097 |
| P95 响应时长(ms) | 3863 |
| 失败题数 | 0 |

## 按题型分布

| 题型 | 题数 | Recall@5 | Recall@10 | 引用准确率 | 平均时长(ms) |
| --- | --- | --- | --- | --- | --- |
| decision | 10 | 40.0% | 50.0% | 10.0% | 3322 |
| deployment | 15 | 20.0% | 20.0% | 3.3% | 3071 |
| diagnostic | 15 | 6.7% | 33.3% | 6.0% | 3155 |
| fact | 20 | 25.0% | 35.0% | 9.2% | 3077 |
| version | 10 | 0.0% | 10.0% | 1.0% | 2863 |

## 失败样例

| id | type | question | 失败原因 |
| --- | --- | --- | --- |
| q002 | fact | Titan 810 的电池接口在哪个位置? | Recall@10 未命中 |
| q003 | fact | Titan 810 第 18 页中标号 1 和 2 分别代表什么? | Recall@10 未命中 |
| q004 | fact | Titan 810 第 20 页标号 1~4 分别对应哪些部件? | Recall@10 未命中 |
| q005 | fact | Titan 810 标准电池容量是多少? | Recall@10 未命中 |
| q006 | fact | 部署工具中的 Select Map 是什么功能? | Recall@10 未命中 |
| q008 | fact | Task scheduling 在 Titan 810 系统中是什么? | Recall@10 未命中 |
| q010 | fact | fixed point 在 Titan 810 部署中是什么概念? | Recall@10 未命中 |
| q013 | fact | Titan 810 用的是什么品牌的电机? | Recall@10 未命中 |
| q014 | fact | Basement A 在地图配置中代表什么? | Recall@10 未命中 |
| q015 | fact | deceleration zone 在地图配置中是什么? | Recall@10 未命中 |
| q016 | fact | unidirectional 在路径规则中是什么意思? | Recall@10 未命中 |
| q018 | fact | Titan 810 的最大爬坡能力是多少? | Recall@10 未命中 |
| q020 | fact | Titan 810 的整机重量是多少? | Recall@10 未命中 |
| q022 | deployment | Titan 810 电池安装接口的方向应该怎样确认? | Recall@10 未命中 |
| q023 | deployment | 如何在部署工具中选择地图? | Recall@10 未命中 |
| q024 | deployment | 如何在地图中绘制禁行区? | Recall@10 未命中 |
| q025 | deployment | 如何在地图中绘制清洁区域? | Recall@10 未命中 |
| q026 | deployment | 创建清洁任务时应该依次点击哪些按钮? | Recall@10 未命中 |
| q027 | deployment | 如何对 Titan 810 进行首次开机配置? | Recall@10 未命中 |
| q028 | deployment | 如何连接 Titan 810 到 Wi-Fi 网络? | Recall@10 未命中 |
| q029 | deployment | 如何部署 Titan 810 到生产环境? | Recall@10 未命中 |
| q030 | deployment | Titan 810 安装固定螺钉的正确顺序是什么? | Recall@10 未命中 |
| q031 | deployment | 如何配置 Titan 810 的初始定位点? | Recall@10 未命中 |
| q034 | deployment | Titan 810 软件升级步骤是什么? | Recall@10 未命中 |
| q035 | deployment | 如何在地图编辑器中保存自定义区域? | Recall@10 未命中 |
| q036 | diagnostic | Titan 810 报错 E102 是什么故障? | Recall@10 未命中 |
| q037 | diagnostic | Titan 810 启动后无法定位怎么办? | Recall@10 未命中 |
| q039 | diagnostic | Titan 810 Wi-Fi 连接失败如何排查? | Recall@10 未命中 |
| q042 | diagnostic | Titan 810 在坡道上熄火如何处理? | Recall@10 未命中 |
| q043 | diagnostic | 地图加载失败如何排查? | Recall@10 未命中 |

## 后续建议

- Recall@10 < 50%，向量召回或分词链路有重大缺陷，应优先排查 Embedding/VectorStore。
- 引用准确率偏低，建议在 W3 调整 chunk 切分粒度或加入重排阈值过滤。
- 平均响应 3097ms 偏高，W4 需引入异步与缓存。
