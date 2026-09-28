"""权限键目录(v1 模块级):键 → 名称/说明;前端角色编辑页按 group 展示。"""
PERMISSION_CATALOG = [
    {"group": "数据与管道", "items": [
        {"key": "sources.manage", "name": "数据源管理", "desc": "数据源增删改与 Jira 同步"},
        {"key": "pipeline.manage", "name": "编译管道与模板", "desc": "管道/模板增删改与运行"},
        {"key": "embedding.manage", "name": "嵌入模型管理", "desc": "嵌入档案增删改与重建索引"},
        {"key": "graph.manage", "name": "知识图谱管理", "desc": "图谱浏览与重建/社区摘要"},
    ]},
    {"group": "知识库", "items": [
        {"key": "wiki.admin", "name": "Wiki 空间管理", "desc": "空间增删改与全库重建"},
        {"key": "notebook.manage", "name": "笔记本管理", "desc": "笔记本增删改与组可见域"},
        {"key": "page.manage", "name": "全库页面管理", "desc": "管理他人页面/回收站清理"},
    ]},
    {"group": "系统管理", "items": [
        {"key": "user.manage", "name": "用户管理", "desc": "用户列表/新建/禁用/角色与组分配"},
        {"key": "role.manage", "name": "角色管理", "desc": "角色与权限键配置"},
        {"key": "group.manage", "name": "组管理", "desc": "组注册表与成员管理"},
    ]},
]
VALID_PERMISSION_KEYS = {i["key"] for g in PERMISSION_CATALOG for i in g["items"]}
