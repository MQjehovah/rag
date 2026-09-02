"""Card Graph 使用的可解释实体规则。与旧 KO 持久层无关。

规则按 entity_type 分组，抽取顺序即列表顺序（更具体的类型靠前，避免
宽泛类型抢占）。文本在进入规则前已做 NFKC 归一化，因此这里的模式
统一写标准 CJK 字符（如「工作站」而非「⼯作站」）。

新增类型（P0 实体抽取率扩充）：
- software：远程/终端软件名（MobaXterm/Xshell/PuTTY/WinSCP 等）
- tool：硬件诊断/连接工具
- system：工作站/服务器/上位机/操作系统等系统级实体
- artifact：固件/升级包/镜像/日志等产物
- operation：OTA 升级/烧录/部署等操作流程实体
"""
from __future__ import annotations

ENTITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("product", [
        r"\bTitan\s*\d{3,}\b",
        r"\bSkywalker\s*\d{2,}\b",
        r"\bSKYWALKER\s*\d{2,}\b",
        r"\bT\-\d{3,}\b",
    ]),
    ("component", [
        r"(电池模块|驱动电机|底盘|充电桩|传感器|控制器|显示器|踏板|方向盘|雷达|相机|轮毂|电池|电机)",
    ]),
    ("software", [
        r"\bMobaXterm\b",
        r"\bXshell\b",
        r"\bPuTTY\b",
        r"\bWinSCP\b",
        r"\bFileZilla\b",
        r"\bSecureCRT\b",
        r"\bSSH\b",
        r"\bSCP\b",
    ]),
    ("tool", [
        r"(万用表|示波器|逻辑分析仪|编程器|调试器|烧录器|串口工具|网线|USB\s*转串口|JTAG|UART)",
    ]),
    ("system", [
        r"(工作站|服务器|上位机|下位机|主控|机器人|操作系统|Linux|Ubuntu|Windows|工控机)",
    ]),
    ("artifact", [
        r"(固件|升级包|镜像|安装包|烧录包|OTA\s*包|日志|日志文件|配置文件|补丁包|诊断报告)",
    ]),
    ("version", [
        r"\b[Vv](\d+(?:\.\d+){0,2})\b",
    ]),
    ("error_code", [
        r"\b[Ee](\d{2,4})\b",
    ]),
    ("parameter", [
        r"(?:驱动|电压|电流|容量|功率|扭矩|压力|转速)\s*(?:最低|最大|标准)?\s*(\d+(?:\.\d+)?)",
        r"(?:驱动|电压|电流|容量|功率|扭矩)\s*要求\s*(?:为|是)?\s*(\d+(?:\.\d+)?)",
    ]),
    ("operation", [
        r"(OTA\s*升级|固件升级|在线升级|远程升级|本地升级|烧录|刷机|部署|等待升级|升级结束|重新充电|更换轮胎|更换电池|重新启动|重新充电)",
    ]),
    # J-3：故障实体（确定性故障/告警名）。追加在末尾，不抢占前面的具体类型。
    ("fault", [
        r"(电池告警|电池故障|电池异常|水箱漏水|水箱故障|电机故障|系统故障|传感器故障|控制器故障|模块故障|设备故障|充电故障|通讯故障|通信故障|过热告警|过载告警|低电量告警|高温告警|故障告警)",
    ]),
]
