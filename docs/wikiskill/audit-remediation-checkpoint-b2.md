# B2 执行检查点（audit-remediation-checkpoint-b2.md）

任务：完整技能集合 + B3 集合语义 CAS。以下为本轮**已落地**的安全增量与**未完成**
函数/测试清单。未完成 ≠ 外部阻塞；需要继续实施后才可声明 B2/B3(集合) 完成。

## 已修改（本轮，向后兼容、已通过回归）
- `alembic/versions/653bbcf9847b_p52_binding_set_columns.py`（新 head，P51→P52）：
  `evolution_skill_bindings` 增加 `rev`（default 1）、`set_hash`、`members_json`
  （nullable；NULL=旧单技能绑定兼容读）。单 head 校验通过；仅隔离库使用。
- `app/models/evolution.py`：EvolutionSkillBinding 增加三列 + `_REQUIRED_COLUMNS`
  映射更新（create_all 使全新实验库自动包含；旧行无迁移时由 server_default/读路径
  兼容）。
- `app/core/skill_evolution/business_ops.py`：新增 `binding_set_hash(members)` 规范
  集合哈希（供后续写入/校验复用）。
- 兼容性回归（隔离 SQLite，46 passed）：stage2/4/8b/8d/8k（8d/8k 为单技能 CAS/幂等
  既有语义，仍通过）。生产库未执行迁移。

## 未完成函数 / 改造点（下一步按序实施）
1. `business_ops._accepted_target` → 返回**完整成员列表**（不再 members[0]）；
   `promote` 复制循环（逐成员校验 domain/runtime/hash/长度上限→全有或全无）、
   绑定写入改为集合物化（members_json+set_hash+rev+1）、审计事件记录集合与哈希。
2. `skill_store.bind/unbind/resolve_binding`（experiment/business 共用）→ 集合语义：
   members_json 物化 + set_hash/rev 维护；读路径兼容 NULL 旧行；empty=显式空集合。
3. `business_ops.rollback/effective_history` → 历史“生效集合”（members+hash）链，
   回退恢复完整集合。
4. 编译注入：`resolve_business_binding`/`render_instruction_block`/注入 wrapper 支持
   多成员（按固定顺序逐成员输出）；`executor` freeze pin payload 由单版本扩展为
   `members[]`，`_apply_pin` 校验全部版本并在任一损坏时 fail loud；新 run 才读
   新绑定（既有逐 run pin 语义保持）。
5. B3 集合 CAS：绑定条件更新 WHERE `rev=:expected_rev AND set_hash=:expected`；
   首次无绑定并发创建用 唯一键冲突/条件插入 收敛为一方成功一方 409；每次切换
   rev+1（A→B→A 后旧 rev 请求 409，防 ABA）；幂等键重放返回**历史操作结果**
   （事件记录 to_set/members），不误报当前绑定状态；`current_set_hash` 改为集合哈希。
6. 实验侧多成员：gate/experience/评估集合已为 FrozenSkillSet 多成员——只需把
   晋升入口的集合（而非首成员）贯通，复用 FrozenSkillSet 契约，不另造格式。

## 待写反例测试（tests/test_skill_evolution_stage8l.py 草拟）
- 双技能共同注入（顺序=实验评估集合顺序）；第二成员损坏→整次不生效；
  重复 skill_id / 集合超限 / 不兼容版本（domain/runtime）拒绝；
  回退恢复完整集合；同一 compile run 中断后仍用原完整集合；新 run 用新绑定；
  A→B→回退A→新操作键再晋升B；同键不同请求拒绝；
  两独立 DB 连接竞争同预期修订→仅一方成功；A→B→A 旧修订请求仍拒（ABA）；
  首次绑定并发；审计失败→版本复制/绑定/事件整体回滚。

## 已验证命令与结果
```powershell
# backend/
python -m alembic heads                      # 653bbcf9847b (head, 单头)
python -m pytest tests/test_skill_evolution_stage2.py ^
  tests/test_skill_evolution_stage4.py tests/test_skill_evolution_stage8b.py ^
  tests/test_skill_evolution_stage8d.py tests/test_skill_evolution_stage8k.py -q
# → 46 passed
```

## 下一条命令
实现 1–3（business_ops/skill_store 集合写入与读取 + rollback/audit 集合化），
随后 4（注入/freeze）与 5（rev+set_hash CAS），再写 stage8l 反例并全量回归。
B1、B4 联调（预览确认/浏览器）、B5 保持开放。

## 轮次 2 续接（追加；未完成，勿重做规划）
- 已再确认：P52 单 head、模型列、`binding_set_hash` 就绪且回归 46 passed（上文）。
- **下一条命令**（从该函数开始，逐步实现并每步跑 8d/8k 防回归）：
  1) 修改 `business_ops.promote`：目标=完整 accepted members（改 `_accepted_target`
     → 返回全部成员列表，弃 members[0]）；逐成员校验（experiment 库版本存在、
     hash、domain==DOMAIN、runtime==RUNTIME_REF、skill_id 唯一、len≤MAX_SET_MEMBERS）；
     全部校验通过才 `_copy_version_into_business`（逐成员，任一失败抛错不落任何行）；
     绑定写改 `_materialize_set_binding(db, ws, members, expected_rev, expected_hash)`
     （INSERT..WHERE NOT EXISTS 防首绑竞争；UPDATE..WHERE id AND rev=:r AND
     set_hash=:h；成功后 rev=expected_rev+1 并写 members_json/set_hash）；审计事件
     增 to_members_json/from_members_json/request_fingerprint(覆盖整组 members)；
     返回含 members/set_hash/rev。
  2) `rollback`：`effective_history` 改按事件记录“生效集合”，回退目标=上一不同集合，
     同样走 CAS(rev+set_hash)。
  3) `business_state`：current 返回 `{members, set_hash, rev, 首成员版本字段兼容}`
     （8d/8k 旧断言字段保留）。
  4) skill_store `resolve_binding/bind/unbind`：读 NULL→旧单技能兼容；写即物化
     members_json/set_hash/rev；empty 写 `[]` + set_hash(空)。
  5) executor 冻结/注入：pin payload 增 `members[]`+`set_hash`；wrapper 按顺序对
     每成员输出指令块；`_apply_pin` 全量校验；旧 pin（无 members）兼容读取为单成员。
  6) CAS/幂等：expected_rev+expected_set_hash 均需匹配；A→B→A 后旧 rev 请求 409；
     幂等键指纹覆盖整组 members；重放返回历史操作结果（含 members）。
  7) 隔离升级测试：建 P51 库→`alembic upgrade 653bbcf9847b`→写入旧单技能绑定与
     历史审计事件→升级后按 NULL 兼容读、写入后物化、rev=1；历史事件可解析。
  8) stage8l 反例（见上）逐条实现后：`pytest tests/test_skill_evolution_stage8l.py
     tests/test_skill_evolution_stage8d.py tests/test_skill_evolution_stage8k.py
     tests/test_skill_evolution_stage8h_e3.py -q`，再全量回归 + 前端构建。
- 本轮未做第 1–8 步（窗口耗尽，非外部阻塞；文件状态为 P52/模型/哈希工具已落且
  回归通过，代码其余保持上一轮通过状态 46+）。B1、B4 联调、B5 保持开放。
