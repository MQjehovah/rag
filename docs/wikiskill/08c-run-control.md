# 阶段 8C：实验运行控制（08c-run-control.md）

> 管理员创建/启动/暂停/恢复/取消演化实验。复用 orchestrator 预算与租约机制；
> 执行在独立 worker（默认独立子进程 evolution-run；测试等价线程执行器）中进行，
> 不污染生产全局 Registry/runner；不另建重复调度框架。本轮仅模拟模式与隔离环境
> 测试；真实模式启动默认被授权开关阻止。

## 1. 服务层（app/core/skill_evolution/control.py）

- `create_experiment_and_run(root, dataset_version, model_mode, …)`：校验数据集目录
  （服务端白名单 `eval/wiki_evolution/datasets/*`）、拆分、预算与经验模式；种子导入 +
  实验 + 排队 run（queued）；runner 配置含 dataset_dir/mode/real 标记。
- 评分器可用性（fail-closed）：数据集声明的 grader 必须在 GRADER_REGISTRY 中 ready
  （当前仅 `wiki-default-grader/v1`，purpose=机制验证）；v2 未注册 → 创建/启动拒绝，
  绝不静默回退。
- `start/resume/pause/cancel`：全部基于持久记录 + 状态 CAS + 启动租约（45s 兜底），
  重复点击/并发操作返回明确冲突；worker 领取时接管租约（orchestrator claim）；
  恢复沿用原 run（不重置 used 计数、不新建替代 run）；取消只置请求位/终态，不删除
  任何迭代/评估/门控/技能记录。
- 真实模式：创建可排队；启动需 BOTH：① 服务端授权 `wikiskill_evolution_admin_real_enabled=true`
  且角色模型配置可解析（默认 false → 409 未授权）；② **每次启动/恢复的显式确认**
  （confirm.explicit=true，且 dataset_version/迭代/三类预算必须与 run 记录一致）——
  全局开关不代替每次启动的参数确认；模型/数据范围/预算绑定运行记录；恢复不接受
  参数变更、不重置预算、不自动扩大授权。worker 侧由 build_run_actors 按 run 记录
  构造 executor/maintainer/proposer 真实适配器（CLI evolution-run 同路径）；任何
  环节配置缺失 → 拒绝执行，绝不静默回退模拟。

## 2. API（app/api/evolution_admin.py，前缀 /api/evolution-admin）

管理员 + 独立开关 `wikiskill_evolution_admin_enabled`（默认 false）。

| 端点 | 内容 |
| --- | --- |
| POST /experiments | 创建实验 + 排队 run（模拟/真实、迭代、预算上限、经验模式） |
| GET /runs/{run_id}?root | 控制视图（含 model_mode） |
| POST /runs/{run_id}/start \| resume \| pause \| cancel | 启动/恢复/暂停/取消（立即返回，不同步等待） |
| GET /meta | 数据集目录 + 评分器可用性/用途（v1=机制验证；v2 未就绪说明） |

## 3. 验收（实际执行，离线模拟）

- `tests/test_skill_evolution_stage8c.py` —— **9 passed**（控制/幂等/暂停恢复取消）；
- `tests/test_skill_evolution_stage8e_chain.py` —— **1 passed**（核查补）：管理入口
  创建 real run → start（未确认 409、参数不一致 409、正确确认 200）→ worker 用真实
  适配器（executor/maintainer/proposer 均构造）请求打到本地 HTTP stub（带凭据、
  model 正确；模拟实现被哨兵改造成实例化即抛错 → 证明未走模拟路径）→ run 收敛；
  全程离线、不触真实供应商。功能开关 503 / 未登录 401 /
  非管理员 403；meta（v3 数据目录 + v1 机制验证 + v2 未就绪文案 + 未验证标记）；
  参数与作用域校验（未知根/未知数据集 404、迭代 0 → 422）；真实 run 可创建但启动 409
  未授权；模拟全生命周期 启动→completed（线程 worker 等价子进程，全程零网络模型调用，
  控制无异常）；重复启动 409（启动租约）；queued 暂停→resume（不重置预算、不新建
  run）→completed；queued 取消→cancelled（实验记录仍在）；running 暂停请求位持久化 +
  收敛 paused + resume 继续同一 run。
- 未执行（如实注明）：真实子进程 worker 的多进程隔离联调未在本轮运行（仅线程等价
  执行器离线验证）；服务重启后 worker 恢复为既有 orchestrator 租约语义（阶段 6 已
  覆盖），本轮通过“重开会话后状态仍可查询”验证持久化。
