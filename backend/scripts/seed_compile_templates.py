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

RULES = """【产品知识库结构】
- 每个产品一个"根页面"，标题 = 产品名/型号(如「SW50」「Titan810」)。
- 每个产品根页下固定三个子页面，标题为：
  「<产品> · 产品手册」「<产品> · 使用说明」「<产品> · 售后维护」
  （用产品名做前缀，保证同一空间内标题唯一）。
- parent 规则：产品根页 parent 留空(顶层)；三个子页的 parent = 产品根页标题。

【内容归类】(把笔记内容归入对应子页)
- 规格 / 型号 / 结构 / 部件 / 关键参数 / 接口 / 选型  → 产品手册
- 安装 / 启动 / 操作步骤 / 配置 / 使用注意事项      → 使用说明
- 故障现象 / 排查 / 维修 / 保养 / 备件耗材 / 退换保修 → 售后维护
- 无法判断所属产品或不属于以上三类的产品信息        → 暂放该产品根页正文

【抽取口径】
- 只抽取产品相关内容；不同产品的信息不要混入同一页面。
- 涉及多个产品时，分别并入各自的产品子树(每篇笔记最多 create/update 共 2 页，聚焦最主要的)。
- 产品型号/别名要归一(如「SW50」「SW-50」视为同一产品)。

【示例】
- 笔记「SW50 盘刷/滚刷切换会改变推尘/扫地文案」→ 更新「SW50 · 使用说明」，parent=「SW50」。
- 笔记「Titan810 电池保养与更换周期」→ 更新「Titan810 · 售后维护」，parent=「Titan810」。
- 笔记「Titan810 负载 50kg、续航 8h」→ 更新「Titan810 · 产品手册」，parent=「Titan810」。"""

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
