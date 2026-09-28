"""种子：默认「LLM Wiki」编译模板(幂等, 存在则更新内容)。

用途：不指定其它模板时，管道可选它来按"蒸馏、非切块"的方式生成 LLM Wiki。
用法(容器内): PYTHONPATH=/app python scripts/seed_default_wiki_template.py
"""
import uuid

from app.config import settings
from app.models.database import CompileTemplate, get_engine, get_session, init_db

NAME = "默认 Wiki 编译（LLM 蒸馏）"

DESCRIPTION = (
    "通用 LLM Wiki 编译规则：概念为纲、一事一页、先并入既有页再新建、按从属关系挂到父页下、"
    "维护「总览 + 子页导航」的多级结构，并用 [[双链]] 连接。适合默认知识库空间。"
)

PROMPT = """你是「LLM Wiki」知识库的主编。你的目标是把零散笔记**蒸馏**(而非切块堆砌)为可长期演进、层层可导航的知识条目。
你维护的是一个整体知识结构：每次只消化「这一篇」新笔记，但要把它放到全局结构中最合适的位置，
并与既有页面在命名、层级、术语、体例上保持一致。"""

RULES = """【LLM Wiki 编译规则】
1. 概念为纲，一事一页：页面代表一个可复用的概念 / 实体 / 流程 / 决策，而非一篇原文；标题用**简短偏正名词短语**。
2. 先定位后落笔：先读 <existing_pages> 索引，优先把新信息**并入已存在的相关页面**(update)；仅当确为新概念时才 create。
3. 层次归属：若该概念从属于某现有页面（如某模块属于某系统），parent 填该页面标题，形成「总览 → 子页」的多级结构；顶层概念 parent 填空字符串 ""。
4. 总览页：当一个主题积累出多个子页时，其父页正文应是「总览 + 子页导航([[子页]])」，不要重复子页正文。
5. 蒸馏而非摘抄：去重、合并同类、改写为通顺条目，剔除口语与流水账；命令 / 代码 / 参数 / 路径 / 数值 / 图片等原文要原样保留。
6. 保留人工修改：update 合并时不得丢弃被人润色/更正过的内容；同一事实冲突时保留更新或更具体者，并注明来源差异。
7. 交叉引用：正文中相关概念用 [[页面标题]] 双链，把知识连成网，避免孤立页面。
8. 每篇笔记最多 create/update 共 2 个页面；若笔记无价值、重复或信息量过低，返回空 ops。"""

TEMPLATE = """以下给出两类页面骨架，按本次产出页面的类型选用其一；占位符替换为实际内容。

—— 内容页（一事一页）——
# <页面标题>

（1-2 句：这是什么 / 解决什么问题）
## 概述
## 要点
- 
## 步骤 / 用法
1. 步骤 → 结果
## 示例
（最小可用示例；代码 / 命令原样保留）
## 注意事项
- 
## 关联概念
- [[相关页面标题]]
## 来源
- 来自笔记：《…》

—— 总览页（主题积累出多个子页时）——
# <主题>

（1-2 句：该主题的范围与用途）
## 组成 / 子主题
- [[子页标题]] —— 一句话说明
## 使用指引
## 关联概念
- [[相关页面标题]]"""


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
