"""评分器注册表与状态（Grader v2 接线）。

状态机（服务端强制，不是文档提示）：
- `role`: mechanism（确定性 v1）| semantic（v2 语义评审）
- `calibration`: not_applicable（v1）| engineering_only（v2 未校准）
  | calibrated（v2 人工标签 + 真实校准完成后方可置）
- `allow_business_promotion`: True 仅当校准语义允许把该实验证据用于业务晋升。
  约束落实：
  * engineering_only 的评分器可在隔离环境以 stub 完整运行（工程可运行）；
  * 未校准时**自动晋升被服务端阻止**（gate 侧 unresolved/invalid 不晋升是保守语义；
    业务晋升侧检查本表 calibration/allow_business_promotion）；
  * 不同 grader_version 分数不可直接比较：create/run 校验数据集、实验、运行三方
    评分器一致；切换评分器必须新建实验并重建基线（本表校验 + 门控配置一致性）。
- v1 只用于机制验证（不构成真实效果证据）。
"""
from __future__ import annotations

# 语义评审 v2 引擎版本（grader_v2.evaluate 实现）。
GRADER_V2 = "wiki-default-grader/v2-prototype-2"

_REGISTRY: dict[str, dict] = {
    "wiki-default-grader/v1": {
        "role": "mechanism",
        "ready": True,
        "calibration": "not_applicable",
        "labels_approved": True,
        "real_calibration": True,
        "allow_business_promotion": True,
        "purpose": "机制验证",
        "description": "确定性程序化评分；仅机制验证（接受/拒绝/无效），不构成真实效果证据。",
    },
    GRADER_V2: {
        "role": "semantic",
        "ready": True,              # 工程可运行（stub 全流程可跑）
        "calibration": "engineering_only",   # 汇总态（派生）
        # 两个独立状态（必须分别建立，不互相代替）：
        #  labels_approved  = 人工标签已确认，只建立参考标准（不自动置 calibrated）；
        #  real_calibration= 真实评审校准证据（评分器/模型/提示词版本 + 证据 + 批准）。
        "labels_approved": True,     # 2026-09-08 人工标签批准（A1–A5，human-project-owner）
        "real_calibration": False,
        "allow_business_promotion": False,
        "purpose": "语义评审（原型 v2）",
        "description": ("保守语义评审原型；needs_review/invalid 不通过、不缩分母、"
                        "未决评估不能晋升。人工标签与真实校准为两个独立状态；任一"
                        "未完成则自动/业务晋升被服务端阻止。"),
    },
}


class GraderRegistryError(RuntimeError):
    pass


def grader_state(grader_version: str) -> dict:
    entry = _REGISTRY.get(grader_version)
    if entry is None:
        return {"grader_version": grader_version, "role": "unknown",
                "ready": False, "calibration": "unknown",
                "allow_business_promotion": False,
                "purpose": "未就绪",
                "description": "未注册的评分器：禁止用于自动评分（不静默回退）。"}
    return {"grader_version": grader_version, **dict(entry)}


def require_known_grader(grader_version: str, *, context: str) -> dict:
    state = grader_state(grader_version)
    if state["role"] == "unknown" or not state["ready"]:
        raise GraderRegistryError(
            f"{context}: 评分器 {grader_version} 未就绪/未注册"
            "（禁止用于自动评分，不静默回退）")
    return state


def require_business_promotable(grader_version: str) -> None:
    state = grader_state(grader_version)
    if state["role"] == "unknown":
        raise GraderRegistryError(
            f"评分器 {grader_version} 未注册：晋升被阻止")
    if not state.get("allow_business_promotion", False):
        labels = bool(state.get("labels_approved"))
        real = bool(state.get("real_calibration"))
        raise GraderRegistryError(
            f"评分器 {grader_version}：人工标签确认={labels}，真实校准={real}，"
            "二者为独立状态；解除正式晋升限制需要对应评分器/模型/提示词版本的"
            "校准证据与明确批准（本轮不代填）——晋升被服务端阻止（非文档提示）")
