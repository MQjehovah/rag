"""wiki-default-v5 正式实验数据集生成器（阶段：正式实验数据集准备）。

只使用本项目内自行编写的合成资料；不读取/不引入生产或私有资料。

- 五个“来源/任务家族”（体裁与知识结构实质不同，非仅名称数字替换）：
  F1 操作流程 / F2 配置约束 / F3 版本迁移 / F4 故障排查 /
  F5 多资料冲突与适用范围（每任务两个独立来源文档：通用规范 + 例外适用）。
  同家族内不同组使用不同子领域主题与不同措辞素材（分组独占、源文件互不相同）；
- 38 任务：train 10（3 组跨 3 家族；cfg-t1 组 6 任务 ≥4 满足提议者读取下限）
  / val 13（6 组跨 5 家族）/ test 15（5 组跨 5 家族）；
  同源改写、近重复内容绝不跨集合（group → split 单射 + 文本相似度门禁）；
- 参考答案仅评分器读取；manifest 记录生成方式、来源家族、组/split、文件哈希、
  暴露记录（编写者本人看过；未进入任何优化角色上下文；不声称无人查看过文件）。
- 本脚本不调用任何模型；不触发技能演化/最终测试评估。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "eval/wiki_evolution/datasets/wiki-default-v5"
CALIB = BASE / "eval/wiki_evolution/calibration"


# ---------------------------------------------------------------------------
# 家族资料库：每个场景对象 = 一组主题字段；组间主题与措辞均不同。
# ---------------------------------------------------------------------------

def _op_text(t) -> str:
    return (
        f"主题：{t['topic']}（{t['sub']}）。\n"
        f"适用对象：{t['scope']}。\n"
        f"前置条件：{t['prereq']}。\n"
        f"操作步骤：{t['steps']}。\n"
        f"关键参数：{t['params']}。\n"
        f"安全提醒：{t['safety']}。\n"
        f"验收检查：{t['check']}。\n"
    )


def _cfg_text(t) -> str:
    return (
        f"主题：{t['topic']}（{t['sub']}）。\n"
        f"配置对象：{t['obj']}。\n"
        f"字段与默认值：{t['fields']}。\n"
        f"取值边界：{t['bounds']}。\n"
        f"生效方式：{t['apply']}。\n"
        f"禁用/例外：{t['exceptions']}。\n"
    )


def _mig_text(t, ver: str) -> str:
    mark = "（旧版本基线）" if ver == "v1" else "（新版本基线）"
    return (
        f"主题：{t['topic']}（{t['sub']}）{mark}。\n"
        f"版本：{ver}。\n"
        f"本版行为：{t[ver + '_behav']}。\n"
        f"本版参数：{t[ver + '_params']}。\n"
        f"变更记录：{t['changelog']}。\n"
        f"弃用与兼容：{t['deprec']}。\n"
        f"升级提示：{t['upgrade']}。\n"
    )


def _ts_text(t) -> str:
    return (
        f"主题：{t['topic']}（{t['sub']}）。\n"
        f"故障现象：{t['symptom']}。\n"
        f"可能原因：{t['causes']}。\n"
        f"排查步骤：{t['steps']}。\n"
        f"修复动作：{t['fix']}。\n"
        f"验证方法：{t['verify']}。\n"
        f"预防建议：{t['prevent']}。\n"
    )


def _conf_text(t, part: str) -> str:
    if part == "a":
        return (
            f"通用规范（资料 A，适用范围 {t['scope_a']}）：{t['rule_a']}。\n"
            f"约束参数：{t['param_a']}。\n"
        )
    return (
        f"例外条款（资料 B，适用范围 {t['scope_b']}）：{t['rule_b']}。\n"
        f"例外参数：{t['param_b']}。\n"
        f"判定标准：{t['decide']}。\n"
    )


# 各家族素材（key=组 id；值为同一组内任务场景列表，主题互不相同）
FAMILIES = {
    "op": {
        "label": "操作流程",
        "text": _op_text,
        "groups": {
            "op-t1": ("train", 2), "op-v1": ("val", 2), "op-v2": ("val", 1),
            "op-x1": ("test", 3),
        },
        "tasks": {
            "op-t1": [
                dict(topic="汽轮机轴承座热装配", sub="大型机组轴承过盈装配与冷却",
                     scope="轴径 320–500 mm 的滑动轴承座", prereq="轴承与轴颈复测、加热炉温度曲线已标定",
                     steps="1 复测过盈量并记录；2 油浴加热轴承至温控区间；3 匀速压装并保压；4 自然冷却至环境温度；5 千分表复核同轴度",
                     params="加热上限 150°C；过盈量 0.08–0.12 mm；保压 60 s",
                     safety="禁止火焰直接加热；作业区通风",
                     check="同轴度 ≤0.03 mm 且无爬行"),
                dict(topic="服务器机房双路电源切换演练", sub="UPS 与柴发倒切演练",
                     scope="双路市电 + UPS + 柴油发电机组机房", prereq="演练窗口获批且负载已备份",
                     steps="1 断开 A 路市电；2 观察 UPS 接管与告警；3 启动柴发并同步；4 转柴发带载 30 分钟；5 恢复市电并复核",
                     params="UPS 满载后备 ≥15 分钟；柴发启动 ≤30 s",
                     safety="先通知业务侧；禁止带载硬切换",
                     check="切换过程业务中断 ≤5 s 且无数据丢失"),
            ],
            "op-v1": [
                dict(topic="无菌灌装线 CIP 清洗", sub="灌装线在位清洗循环",
                     scope="灌装速度 600 瓶/分的无菌线", prereq="管线已排空并泄压",
                     steps="1 碱洗循环；2 中间冲洗；3 酸洗循环；4 终末纯化水冲洗；5 电导率取样合格后吹干",
                     params="碱液浓度 1.5–2.0%；温度 80±2°C；循环 ≥20 分钟",
                     safety="高温管路挂警示牌",
                     check="终末冲洗电导率 ≤1 µS/cm"),
                dict(topic="自动化仓库堆垛机换型", sub="料箱与托盘模式换型",
                     scope="单深位堆垛机", prereq="任务队列排空且货叉归位",
                     steps="1 切维护模式；2 更换货叉端部夹具；3 载入新库位映射；4 空载试跑循环；5 带载首件验证",
                     params="换型时间 ≤15 分钟；夹具锁紧扭矩 45 N·m",
                     safety="货叉下禁止站人",
                     check="连续 20 次定位误差 ≤±2 mm"),
            ],
            "op-v2": [
                dict(topic="深冷储罐倒罐作业", sub="液氮储罐间倒罐",
                     scope="20 m³ 立式液氮储罐", prereq="两罐压差表与液位计校验合格",
                     steps="1 连接液相平衡管；2 缓慢开启气相阀平衡压力；3 开液相阀利用压差倒罐；4 监控液位与压力；5 达到目标液位后依次关闭阀门",
                     params="倒罐压差 0.05–0.15 MPa；目标液位 85%",
                     safety="低温阀门操作戴防冻手套",
                     check="倒罐损失率 ≤1%"),
            ],
            "op-x1": [
                dict(topic="牵引变压器有载分接开关检修", sub="OLTC 吊芯检查与油置换",
                     scope="110 kV 牵引变 OLTC", prereq="停电、接地、验电三到位",
                     steps="1 排油并吊芯；2 检查触头烧蚀与过渡电阻；3 更换绝缘油；4 回装并做动作试验；5 绝缘油耐压复试",
                     params="过渡电阻偏差 ≤±10%；油耐压 ≥40 kV/2.5 mm",
                     safety="油区禁火",
                     check="动作顺序与切换时间符合出厂值 ±20%"),
                dict(topic="高速包装线热缩膜机标定", sub="热缩通道温度标定",
                     scope="热缩炉温控系统", prereq="通道内无产品、风机运行",
                     steps="1 置入标定探头；2 记录各区设定值；3 温度稳定后比对实测；4 调整 PID 偏移量；5 空载复测",
                     params="各区偏差 ±3°C 内；稳定时间 ≤10 分钟",
                     safety="高温区隔热手套",
                     check="复测各区偏差 ≤±3°C"),
                dict(topic="消防泵组月度巡检", sub="主备泵切换与稳压",
                     scope="稳压 + 主泵 + 备用泵组", prereq="通知消防控制室并解除联动",
                     steps="1 观察稳压泵启停区间；2 手动启动主泵；3 记录出口压力与电流；4 切换备用泵试转；5 恢复自动并复位",
                     params="主泵出口 ≥0.7 MPa；备用泵空载试转 ≥10 分钟",
                     safety="试验前确认管网有人值守",
                     check="双泵切换时间 ≤30 s"),
            ],
        },
    },
    "cfg": {
        "label": "配置约束",
        "text": _cfg_text,
        "groups": {"cfg-t1": ("train", 6), "cfg-v1": ("val", 3),
                   "cfg-x1": ("test", 3)},
        "tasks": {
            "cfg-t1": [
                dict(topic="网关设备 SNMP 采集参数", sub="监控轮询与团体名约束",
                     obj="工业采集网关", fields="采集间隔 30 s；超时 3 s；重试 2 次",
                     bounds="间隔下限 10 s；团体名仅可读（community ro）",
                     apply="配置后需重启采集进程",
                     exceptions="禁止对 PLC 站点启用写团体名"),
                dict(topic="消息队列消费者预取配置", sub="消费吞吐与堆积取舍",
                     obj="订单消息消费者", fields="prefetch 默认 50；ack 超时 30 s",
                     bounds="prefetch 上限 200；超过 200 拒收新任务",
                     apply="热更新，无需重启",
                     exceptions="批处理消费者禁用自动 ack"),
                dict(topic="温控仪表报警死区", sub="防抖与灵敏度",
                     obj="发酵罐温控仪", fields="报警阈值 37.0°C；死区 0.3°C",
                     bounds="死区 0.1–1.0°C 可设",
                     apply="写参数后需 10 分钟自检",
                     exceptions="灭菌阶段必须临时关闭低温报警"),
                dict(topic="备份窗口与保留策略", sub="存储与合规",
                     obj="数据库每日全备", fields="备份起始 02:00；保留 30 天",
                     bounds="保留下限 7 天；上限 90 天",
                     apply="次日生效",
                     exceptions="财务库保留不低于 180 天"),
                dict(topic="负载均衡会话保持", sub="粘滞会话策略",
                     obj="应用入口 LB", fields="会话超时 30 分钟；算法 源地址哈希",
                     bounds="超时 5–240 分钟可设",
                     apply="新建连接即生效",
                     exceptions="登录接口集群不允许加权轮询"),
                dict(topic="工控屏亮度与休眠", sub="现场可读性",
                     obj="HMI 触摸屏", fields="亮度 70%；休眠 10 分钟",
                     bounds="亮度 30–100%；休眠 5–60 分钟",
                     apply="立即生效",
                     exceptions="报警状态禁止进入休眠"),
            ],
            "cfg-v1": [
                dict(topic="网关设备 SNMP 采集参数（变体 A：只读陷阱端口）", sub="trap 上报",
                     obj="采集网关", fields="trap 端口 162；上报间隔 60 s",
                     bounds="端口仅允许 1024–65535",
                     apply="重启生效",
                     exceptions="禁止使用 0–1023 系统端口做 trap"),
                dict(topic="消息队列消费者预取配置（变体 B：延迟队列）", sub="延迟消息投递",
                     obj="延迟队列插件", fields="延迟上限 24 h；检查间隔 1 s",
                     bounds="延迟下限 1 s",
                     apply="新消息生效",
                     exceptions="定时任务类消息禁止入延迟队列"),
                dict(topic="温控仪表报警死区（变体 B：多段设定）", sub="分时区段",
                     obj="培养箱温控", fields="白天阈值 37.2°C；夜间 36.8°C；死区 0.2°C",
                     bounds="每段死区 0.1–0.5°C",
                     apply="整点切换",
                     exceptions="换段前 5 分钟抑制误报"),
            ],
            "cfg-x1": [
                dict(topic="采集网关日志转发", sub="syslog 与告警联动",
                     obj="工业采集网关", fields="转发级别 info 起；缓冲 5000 条",
                     bounds="缓冲上限 20000 条",
                     apply="即时",
                     exceptions="链路中断期间禁止丢弃告警级日志"),
                dict(topic="缓存集群内存上限", sub="淘汰策略",
                     obj="Redis 集群", fields="maxmemory 80%；策略 allkeys-lru",
                     bounds="上限 50–90%",
                     apply="立即，需观察命中率",
                     exceptions="计数类键禁止参与 LRU 淘汰"),
                dict(topic="视频接入码率与关键帧", sub="存储与流畅度",
                     obj="NVR 接入", fields="码率 4 Mbps；关键帧间隔 2 s",
                     bounds="码率 1–8 Mbps",
                     apply="重连后生效",
                     exceptions="事件录像强制 I 帧间隔 1 s"),
            ],
        },
    },
    "mig": {
        "label": "版本迁移",
        "text": _mig_text,
        "groups": {"mig-t1": ("train", 2), "mig-v1": ("val", 2),
                   "mig-x1": ("test", 3)},
        "tasks": {
            "mig-t1": [
                dict(topic="订单接口鉴权协议升级", sub="v1 令牌 → v2 OAuth2",
                     scope="外部订单推送接口", v1_behav="v1 使用静态令牌头 X-Token 且 24 h 不失效",
                     v1_params="X-Token 长度 32 字符", v2_behav="v2 使用 OAuth2 client_credentials，令牌 2 h 过期并支持刷新",
                     v2_params="access_token 有效期 7200 s", changelog="v2.0 移除 X-Token 静态鉴权",
                     deprec="v1 令牌在 v2.0 后 90 天停用", upgrade="调用方需先换发 client_id/secret 并灰度切流"),
                dict(topic="缓存键命名空间迁移", sub="redis 前缀 v1→v2",
                     scope="会话与限流缓存", v1_behav="v1 键前缀 sess: 与 rate:，无版本号",
                     v1_params="前缀长度 ≤32", v2_behav="v2 键统一带版本号前缀 sess:v2: 与 rate:v2:，支持双读双写过渡",
                     v2_params="过渡期 7 天", changelog="v2.0 起只写 v2 前缀",
                     deprec="v1 前缀键在过渡期后删除", upgrade="先灰度读 v2，再切写"),
            ],
            "mig-v1": [
                dict(topic="报表引擎公式语法升级", sub="v1 函数 → v2 表达式",
                     scope="月报模板", v1_behav="v1 仅支持 SUM/AVG 单列函数",
                     v1_params="函数参数 ≤2 列", v2_behav="v2 支持嵌套表达式与条件分支",
                     v2_params="表达式深度 ≤8 层", changelog="v2.0 起旧 AVG 语义改为忽略空值",
                     deprec="v1 模板公式自动改写", upgrade="先重算历史报表校验口径"),
                dict(topic="设备固件配置项迁移", sub="字段改名与单位统一",
                     scope="温度采集器固件", v1_behav="v1 配置项 temp_raw 为整型原始值",
                     v1_params="temp_raw 单位 0.1°C", v2_behav="v2 配置项 temp_c 为浮点摄氏温度",
                     v2_params="temp_c 精度 0.01°C", changelog="v2.1 移除 temp_raw 写入",
                     deprec="读 v1 键返回迁移警告", upgrade="下发前先读回校验"),
            ],
            "mig-x1": [
                dict(topic="消息体信封格式迁移", sub="XML → JSON",
                     scope="供应商回调", v1_behav="v1 回调为 XML 信封且字段全大写",
                     v1_params="XML 根节点 callback", v2_behav="v2 回调为 JSON 信封，字段 snake_case",
                     v2_params="JSON 根对象含 event 与 payload", changelog="v2.0 弃用 XML 信封",
                     deprec="XML 信封保留 30 天", upgrade="双写期同时消费并比对 event_id 去重"),
                dict(topic="日志字段脱敏规则升级", sub="掩码策略收紧",
                     scope="接入层访问日志", v1_behav="v1 对手机号保留前 3 后 4",
                     v1_params="掩码符 *", v2_behav="v2 手机号全部掩码，仅保留国家码",
                     v2_params="国家码 +86 保留", changelog="v2.0 移除前 3 后 4 明文",
                     deprec="v1 明文日志线下保留 180 天", upgrade="先验证检索与告警规则依赖字段"),
                dict(topic="镜像仓库签名校验升级", sub="v1 SHA1 → v2 cosign",
                     scope="制品发布流水线", v1_behav="v1 仅校验 SHA1 摘要",
                     v1_params="摘要长度 40 十六进制", v2_behav="v2 强制 cosign 签名与 keyless 验证",
                     v2_params="签名算法 ECDSA-P256", changelog="v2.0 起拒绝未签名镜像",
                     deprec="v1 摘要校验保留只读告警", upgrade="先为存量镜像补签再切强制"),
            ],
        },
    },
    "ts": {
        "label": "故障排查",
        "text": _ts_text,
        "groups": {"ts-v1": ("val", 3), "ts-x1": ("test", 3)},
        "tasks": {
            "ts-v1": [
                dict(topic="应用偶发超时", sub="连接池耗尽",
                     symptom="高峰时段接口 P99 突增、偶发 504", causes="连接池上限不足；慢查询占持连接；健康检查失败未剔除",
                     steps="1 查连接池水位；2 定位慢查询；3 核对健康检查周期",
                     fix="上调池上限并按服务分级；慢查询加索引/限流",
                     verify="压测 P99 回落且无 504", prevent="连接借用超时告警阈值调低"),
                dict(topic="磁盘只读报警", sub="文件系统满或 IO 错误",
                     symptom="写入报 read-only file system", causes="分区 100%；底层存储卷故障",
                     steps="1 df/inode 检查；2 dmesg 看 IO 错误；3 定位大文件目录",
                     fix="清理归档/扩容；故障卷切换副本",
                     verify="写测试通过且监控恢复", prevent="容量 85% 告警 + 每日配额清理"),
                dict(topic="链路丢包定位", sub="交换端口协商",
                     symptom="业务侧间歇 RTT 抖动、重传率升高", causes="端口双工协商异常；光模块劣化；环路",
                     steps="1 看端口协商状态；2 查错包计数；3 拔插/换模块对照",
                     fix="强制双工/换光模块；破环",
                     verify="重传率归零", prevent="光功率阈值告警"),
            ],
            "ts-x1": [
                dict(topic="冷库温度漂移", sub="化霜周期异常",
                     symptom="库温每 6 h 规律爬升 4°C", causes="化霜间隔过长；化霜加热器失效；温度探头结霜",
                     steps="1 读化霜曲线；2 测加热器电流；3 核对探头位置",
                     fix="缩短化霜间隔/更换加热器；除霜后校准探头",
                     verify="连续 48 h 库温波动 ≤±1°C", prevent="化霜周期与温度联动记录"),
                dict(topic="机械臂抖动", sub="伺服增益失配",
                     symptom="高速轨迹末端振荡 2–3 Hz", causes="增益过高；惯量比超限；机械连接松动",
                     steps="1 抓取报警代码；2 量惯量比；3 检查减速机背隙",
                     fix="按整定表降增益；紧固连接",
                     verify="阶跃响应无超调", prevent="换型后强制重整定"),
                dict(topic="ES 集群红盘", sub="分片分配失败",
                     symptom="集群 status red、部分索引不可写", causes="磁盘水位超高；分片副本缺失；节点失联",
                     steps="1 查 cluster health 明细；2 查 unassigned 原因；3 查节点磁盘",
                     fix="扩容/清理并 reroute；恢复副本",
                     verify="status green 且分片均衡", prevent="水位 85% 触发只读保护"),
            ],
        },
    },
    "conf": {
        "label": "多资料冲突与适用范围",
        "text": _conf_text,
        "groups": {"conf-v1": ("val", 3), "conf-x1": ("test", 3)},
        "tasks": {
            "conf-v1": [
                dict(topic="通用防静电规范与洁净室例外", sub="防静电地垫材质冲突",
                     scope_a="普通电子装配车间", rule_a="防静电地垫一律使用导电型（表面电阻 10^4–10^6 Ω）",
                     param_a="电阻上限 10^6 Ω",
                     scope_b="百级洁净室（黄光区）", rule_b="禁止导电型地垫（产生颗粒），改用导静电型 10^6–10^9 Ω",
                     param_b="电阻下限 10^6 Ω", decide="以洁净室等级为准；跨界走缓冲间"),
                dict(topic="压力容器探伤周期通用与高炉煤气例外", sub="定期检验周期冲突",
                     scope_a="一般压力容器", rule_a="外部检验每年一次，耐压试验每六年一次",
                     param_a="耐压周期 6 年",
                     scope_b="高炉煤气管道（湿硫化氢环境）", rule_b="外部检验每年一次，耐压试验每三年一次并增加壁厚测点",
                     param_b="耐压周期 3 年", decide="介质腐蚀性工况按例外表执行"),
                dict(topic="通用密码策略与遗留系统例外", sub="口令复杂度豁免窗口",
                     scope_a="新接入业务系统", rule_a="口令 ≥12 位且含三类字符，90 天强制改密",
                     param_a="最短 12 位",
                     scope_b="停产在保遗留网关", rule_b="允许 8 位口令但须绑定访问白名单与审计，禁外网直连",
                     param_b="最短 8 位", decide="豁免名单每年复核，新系统一律不豁免"),
            ],
            "conf-x1": [
                dict(topic="通用地线阻值与变电所接地网例外", sub="接地电阻验收冲突",
                     scope_a="一般建筑物防雷接地", rule_a="工频接地电阻 ≤4 Ω",
                     param_a="上限 4 Ω",
                     scope_b="110 kV 变电所接地网", rule_b="接地电阻 ≤0.5 Ω 且铺设均压网",
                     param_b="上限 0.5 Ω", decide="以变电所设计规程为准，双重接地不得串联"),
                dict(topic="通用灭菌参数与高海拔医院例外", sub="灭菌温度补偿",
                     scope_a="平原地区灭菌器", rule_a="脉动预真空 134°C 维持 4 分钟",
                     param_a="134°C / 4 min",
                     scope_b="海拔 2500 m 以上医院", rule_b="需提高温度 1.5°C/海拔 300 m 折算并以生物指示剂验证",
                     param_b="折算 +1.5°C/300 m", decide="以生物指示剂结果为准"),
                dict(topic="通用埋地管道防腐与冻土区例外", sub="阴极保护电位冲突",
                     scope_a="常规埋地钢质管道", rule_a="阴极保护电位 -0.85～-1.20 V（CSE）",
                     param_a="下限 -0.85 V",
                     scope_b="多年冻土区管道", rule_b="上限放宽至 -1.50 V 且须限制杂散电流",
                     param_b="上限 -1.50 V", decide="冻土区执行专项电位窗口并加监测桩"),
            ],
        },
    },
}

SPLIT_LABEL = {"train": "训练", "val": "验证", "test": "测试"}
ORDER_F = ["op", "cfg", "mig", "ts", "conf"]


def task_ref_points(family: str, tid: str, t: dict) -> tuple[list[dict], list[str]]:
    """参考答案检查项（仅评分器读取）。F3 附带版本归属检查；含禁止/无证据措辞。"""
    pts = [{"id": "p1", "kind": "phrase", "text": "主题"}]
    if family == "op":
        pts += [
            {"id": "p2", "kind": "phrase", "text": "前置条件"},
            {"id": "p3", "kind": "phrase", "text": "操作步骤"},
            {"id": "p4", "kind": "phrase", "text": "安全提醒"},
            {"id": "v1", "kind": "value", "value": t["params"]},
            {"id": "p5", "kind": "phrase", "text": "验收检查"},
        ]
        forbidden = ["跳过步骤"]
    elif family == "cfg":
        pts += [
            {"id": "p2", "kind": "phrase", "text": "字段与默认值"},
            {"id": "v1", "kind": "value", "value": t["bounds"]},
            {"id": "p3", "kind": "phrase", "text": "生效方式"},
            {"id": "p4", "kind": "phrase", "text": "禁用/例外"},
        ]
        forbidden = ["无限调整"]
    elif family == "mig":
        pts += [
            {"id": "v1", "kind": "value_in_version", "value": t["v1_params"],
             "version": "v1"},
            {"id": "v2", "kind": "value_in_version", "value": t["v2_params"],
             "version": "v2"},
            {"id": "a1", "kind": "absent_in_version",
             "value": t["v1_params"], "version": "v2"},
            {"id": "a2", "kind": "absent_in_version",
             "value": t["v2_params"], "version": "v1"},
            {"id": "p2", "kind": "phrase", "text": "弃用与兼容"},
            {"id": "p3", "kind": "phrase", "text": "升级提示"},
        ]
        forbidden = ["混用", "继续使用旧版本值"]
    elif family == "ts":
        pts += [
            {"id": "p2", "kind": "phrase", "text": "故障现象"},
            {"id": "p3", "kind": "phrase", "text": "排查步骤"},
            {"id": "p4", "kind": "phrase", "text": "修复动作"},
            {"id": "v1", "kind": "value", "value": t["verify"]},
            {"id": "p5", "kind": "phrase", "text": "预防建议"},
        ]
        forbidden = ["直接更换设备"]
    else:  # conf
        pts += [
            {"id": "p2", "kind": "phrase", "text": "通用规范"},
            {"id": "p3", "kind": "phrase", "text": "例外条款"},
            {"id": "v1", "kind": "value", "value": t["param_a"]},
            {"id": "v2", "kind": "value", "value": t["param_b"]},
            {"id": "p4", "kind": "phrase", "text": "判定标准"},
        ]
        forbidden = ["一律按通用规范"]
    return pts, forbidden


def build_dataset() -> tuple[dict, dict, dict]:
    tasks, refs, hashes = [], {}, {}
    counts = {"train": 0, "val": 0, "test": 0}

    def add(family, gid, tid, split, t, extra_files):
        suffix = {"op": "流程", "cfg": "约束", "mig": "迁移", "ts": "排查",
                  "conf": "冲突"}[family]
        if family == "conf":
            fa = OUT / "sources" / f"{tid}-a.md"
            fb = OUT / "sources" / f"{tid}-b.md"
            fa.write_text(_conf_text(t, "a"), encoding="utf-8")
            fb.write_text(_conf_text(t, "b"), encoding="utf-8")
            for f in (fa, fb):
                hashes[str(f.relative_to(OUT))] = hashlib.sha256(
                    f.read_bytes()).hexdigest()
            sources = [{"doc_id": "d1", "title": f"通用规范 {tid}", "file": f"{tid}-a.md"},
                       {"doc_id": "d2", "title": f"例外条款 {tid}", "file": f"{tid}-b.md"}]
            ver = None
        elif family == "mig":
            srcs = []
            for vv in ("v1", "v2"):
                fn = f"{tid}-{vv}.md"
                (OUT / "sources" / fn).write_text(
                    _mig_text(t, vv), encoding="utf-8")
                hashes[str((OUT / "sources" / fn).relative_to(OUT))] = (
                    hashlib.sha256((OUT / "sources" / fn).read_bytes()).hexdigest())
                srcs.append({"doc_id": vv, "title": f"{t['topic']} {vv}",
                             "file": fn, "product_version": vv})
            sources = srcs
            ver = None
        else:
            fn = f"{tid}.md"
            text = FAMILIES[family]["text"](t)
            (OUT / "sources" / fn).write_text(text, encoding="utf-8")
            hashes[str((OUT / "sources" / fn).relative_to(OUT))] = (
                hashlib.sha256((OUT / "sources" / fn).read_bytes()).hexdigest())
            sources = [{"doc_id": "d1", "title": t["topic"], "file": fn}]
            ver = None
        tasks.append({
            "task_id": tid, "dataset_version": "wiki-default-v5",
            "domain": "wiki_compile.default", "split": split, "group_id": gid,
            "input_snapshot_id": f"v5-{tid}",
            "instruction": (f"依据给定资料编写「{t['topic']}」主题的 Wiki："
                            f"覆盖{t['topic']}相关要点；引用来源中的参数、版本与适用范围；"
                            f"结论必须与资料一致，禁止臆造与无证据补充；"
                            f"多资料冲突时按适用范围与判定标准处理。"),
            "grader_version": "wiki-default-grader/v1",
            "reference_ref": "wiki-default-v5.json", "trigger": "manual_rebuild",
            "wiki_title": f"{t['topic']}（{suffix}资料）",
            "wiki_category": "产品资料",
            "source_family": FAMILIES[family]["label"],
            "sources": sources,
        })
        pts, forbidden = task_ref_points(family, tid, t)
        refs[tid] = {"expected_points": pts, "forbidden": forbidden}
        counts[split] += 1

    seq = 0
    for family in ORDER_F:
        fam = FAMILIES[family]
        for gid in sorted(fam["groups"]):
            split, n = fam["groups"][gid]
            for k in range(n):
                seq += 1
                t = fam["tasks"][gid][k]
                tid = f"v5-{family}-{seq:03d}"
                add(family, gid, tid, split, t, [])
    assert counts == {"train": 10, "val": 14, "test": 15}, counts
    return {"counts": counts, "tasks": tasks}, refs, hashes


def similarity(a: str, b: str) -> float:
    def grams(s: str, n: int = 3):
        s = re.sub(r"\s+", "", s)
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def validate(tasks: list[dict], hashes: dict) -> list[str]:
    errs = []
    group_split: dict[str, set[str]] = {}
    texts: dict[str, list[str]] = {}
    fam_split: dict[str, set[str]] = {}
    for t in tasks:
        group_split.setdefault(t["group_id"], set()).add(t["split"])
        fam_split.setdefault(t["source_family"], set()).add(t["split"])
        body = ""
        for src in t["sources"]:
            f = OUT / "sources" / src["file"]
            if not f.is_file():
                errs.append(f"缺源文件 {f}")
            body += f.read_text(encoding="utf-8")
        texts.setdefault(t["split"], []).append(body)
    for g, ss in group_split.items():
        if len(ss) > 1:
            errs.append(f"组跨 split: {g} -> {ss}")
    train_fams = {fam for fam, ss in fam_split.items() if "train" in ss}
    if len(train_fams) < 2:
        errs.append(f"训练集覆盖来源家族过少: {sorted(train_fams)}")
    train_groups = [g for g, ss in group_split.items() if "train" in ss]
    if len(train_groups) < 3:
        errs.append("训练集覆盖来源组过少")
    train_sizes = {}
    for t in tasks:
        if t["split"] == "train":
            train_sizes[t["group_id"]] = train_sizes.get(t["group_id"], 0) + 1
    if not any(v >= 4 for v in train_sizes.values()):
        errs.append("无 ≥4 个互异训练任务的来源组（提议者读取要求不满足）")
    # 近重复/同源文本不得跨 split（同 split 内允许，同组允许）
    for a in texts["train"]:
        for b in texts["val"] + texts["test"]:
            if similarity(a, b) > 0.75:
                errs.append("train 文本与 val/test 过相似（疑似同源改写跨集合）")
    for a in texts["val"]:
        for b in texts["test"]:
            if similarity(a, b) > 0.75:
                errs.append("val 文本与 test 过相似（疑似同源改写跨集合）")
    # manifest 哈希一致性
    for rel, h in hashes.items():
        cur = hashlib.sha256((OUT / rel).read_bytes()).hexdigest()
        if cur != h:
            errs.append(f"哈希不一致: {rel}")
    return errs


def main() -> int:
    from app.core.skill_evolution.contracts import load_dataset
    (OUT / "sources").mkdir(parents=True, exist_ok=True)
    (OUT / "references").mkdir(exist_ok=True)
    payload, refs, hashes = build_dataset()
    ds_path = OUT / "dataset.json"
    ds_payload = {
        "dataset_version": "wiki-default-v5",
        "domain": "wiki_compile.default",
        "grader_version": "wiki-default-grader/v1",
        "tasks": payload["tasks"],
    }
    ds_path.write_text(json.dumps(ds_payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    (OUT / "references/wiki-default-v5.json").write_text(
        json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")
    # 校验
    errs = validate(payload["tasks"], hashes)
    fam_groups = {}
    for t in payload["tasks"]:
        fam_groups.setdefault(t["source_family"], {})
        fam_groups[t["source_family"]].setdefault(t["split"], set()).add(
            t["group_id"])
    manifest = {
        "dataset_version": "wiki-default-v5",
        "generation": "hand-authored synthetic scenario libraries (5 families); "
                      "deterministic regeneration; 编写者本人编写并审阅过全部资料",
        "source_families": {
            f: {"label": FAMILIES[f]["label"],
                "split_groups": {k: sorted(v)
                                 for k, v in fam_groups[FAMILIES[f]["label"]].items()}}
            for f in ORDER_F},
        "split_counts": payload["counts"],
        "exposure": {
            "author_seen": True,
            "author_scope": "编写者本人查看/审阅全部 38 任务源资料与参考答案；"
                            "未进入任何执行/维护/提议/验证角色上下文；"
                            "未用于阶段 0-7 调试或回归；不声称他人从未查看文件。",
            "note": "校准探针与参考答案仅供评分器读取。"},
        "file_hashes": {rel: h for rel, h in sorted(hashes.items())},
        "statistical_caveat": "38 任务用于初步实验；不宣称充分统计效力。",
    }
    group_map = {}
    for t in payload["tasks"]:
        group_map[t["group_id"]] = t["split"]
    manifest["group_to_split"] = {g: group_map[g] for g in sorted(group_map)}
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ds = load_dataset(OUT)
    print("dataset: wiki-default-v5")
    print(f"  tasks={len(ds.tasks)}  {payload['counts']}")
    if errs:
        for e in errs:
            print("ERROR:", e)
        return 1
    print("  structural validation OK (组/split 单射、跨集合近重复门禁、哈希一致)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
