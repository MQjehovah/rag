"""skill_evolution 受控异常基类与分类。"""


class SkillEvolutionError(RuntimeError):
    """实验框架受控错误基类。"""


class DatasetError(SkillEvolutionError):
    """任务集/契约/分组校验失败（配置层错误，与编译结果无关）。"""


class SnapshotError(SkillEvolutionError):
    """输入快照缺失/损坏/标识冲突/封存失败。"""


class TraceError(SkillEvolutionError):
    """执行记录写入/封存/读取失败。"""


class GraderConfigError(SkillEvolutionError):
    """评分器配置或参考答案结构错误。"""


class SkillStoreError(SkillEvolutionError):
    """技能版本存储/绑定/解析错误（含缺失、损坏、Runtime 不兼容）。"""


class SkillPackageError(SkillEvolutionError):
    """技能包（SKILL.md/PURPOSE.md）结构或内容校验失败。"""
