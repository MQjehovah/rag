"""种子：内置「产品知识库（按产品分根页）」编译模板(幂等, 存在则更新内容)。

用法(容器内): PYTHONPATH=/app python scripts/seed_compile_templates.py
"""
import uuid

from app.config import settings
from app.models.database import CompileTemplate, get_engine, get_session, init_db

NAME = "产品知识库（按产品分根页）"

DESCRIPTION = (
    "从笔记抽取『产品』相关内容：每个产品一个根页面；每个产品下固定三个子页面 —— "
    "产品手册 / 使用说明 / 售后维护。适合『产品知识库』空间。"
)

PROMPT = """你是企业产品知识库编辑。请从「新笔记」中**只抽取与产品相关**的内容，按"产品"维度组织进知识库。
每个产品是一棵子树；同一产品的信息必须并入其对应页面，不得新建重复页；与产品无关的内容不要产出页面。"""

RULES = """【必须严格遵守的输出结构 —— 产品知识库】
先识别本笔记涉及的“产品” <P>（从笔记标题/正文判定，如 Skywalker GT、SW50、Titan810；型号别名要归一）。

对每个产品 <P>，只允许出现下面这 4 个页面，标题必须**逐字**一致：
1) 产品根页：title = "<P>"                       parent = ""(顶层)
2) 产品手册：title = "<P> · 产品手册"              parent = "<P>"
3) 使用说明：title = "<P> · 使用说明"              parent = "<P>"
4) 售后维护：title = "<P> · 售后维护"              parent = "<P>"
（子页标题用 " · " 分隔；子页不存在就 create，存在就 update 合并。）

【硬性禁止】
- 禁止使用任何其它标题作为页面（例如「<P> 部署手册」「<P> 设备组成」「<P> 使用教程」都不允许）；
  这类内容一律并入上面对应的固定子页。
- 不得为同一产品新建第 5 个页面；产品根页正文只放“无法归入三个子页的补充说明 + 子页导航”。
- 与产品无关的内容不要产出任何页面。

【内容归类】把笔记内容归入对应子页：
- 规格 / 型号 / 结构 / 部件 / 关键参数 / 接口 / 选型            → "<P> · 产品手册"
- 安装 / 部署 / 启动 / 操作步骤 / 配置 / 使用注意事项          → "<P> · 使用说明"
- 故障现象 / 排查 / 维修 / 保养 / 备件耗材 / 退换保修 / 客服流程 → "<P> · 售后维护"

【抽取口径】
- 只抽取产品相关内容；不同产品的信息不要混入同一页面。
- 每篇笔记最多 create/update 共 2 个页面：优先“根页 + 内容最匹配的那个子页”。

【示例（务必模仿此标题与 parent）】
- 笔记《Skywalker GT 部署手册》：产品 <P>="Skywalker GT"。
  ops = [
    {"action":"create","title":"Skywalker GT","category":"产品","parent":"","content":"# Skywalker GT\\n## 产品概述\\n...\\n## 子页面\\n- [[Skywalker GT · 产品手册]] ...","summary":"Skywalker GT 产品总览"},
    {"action":"create","title":"Skywalker GT · 使用说明","category":"产品","parent":"Skywalker GT","content":"# Skywalker GT · 使用说明\\n## 安装与启动\\n...","summary":"Skywalker GT 部署与操作步骤"}
  ]
- 笔记《Titan810 电池保养与更换》→ update「Titan810 · 售后维护」，parent="Titan810"。
- 笔记《SW50 负载 50kg、续航 8h》→ update「SW50 · 产品手册」，parent="SW50"。"""

TEMPLATE = """以下给出四种页面结构，按本次产出页面的类型选用其一；占位符 <产品> 请替换为实际产品名/型号。

—— 产品根页（parent 留空）——
# <产品>
## 产品概述
<!-- 1-2 句：这是什么产品、面向什么场景 -->
## 子页面
- [[<产品> · 产品手册]]
- [[<产品> · 使用说明]]
- [[<产品> · 售后维护]]

—— 产品手册 ——
# <产品> · 产品手册
## 型号与规格
## 结构与部件
## 关键参数
## 接口 / 选型
## 相关页面
- [[<产品>]]

—— 使用说明 ——
# <产品> · 使用说明
## 安装与启动
## 操作步骤
1. 步骤 → 结果
## 配置项
## 注意事项
## 相关页面
- [[<产品>]]

—— 售后维护 ——
# <产品> · 售后维护
## 常见故障与排查
- 现象 → 原因 → 处理
## 日常保养
## 备件与耗材
## 退换 / 保修
## 相关页面
- [[<产品>]]"""


def main() -> None:
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        t = db.query(CompileTemplate).filter(CompileTemplate.name == NAME).first()
        if t is None:
            t = CompileTemplate(id=str(uuid.uuid4()), name=NAME)
            db.add(t)
            print("[seed] created template:", NAME)
        else:
            print("[seed] updating existing template:", NAME)
        t.description = DESCRIPTION
        t.compiler_kind = "wiki"
        t.prompt = PROMPT
        t.rules = RULES
        t.template = TEMPLATE
        db.commit()
        print("[seed] id:", t.id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
