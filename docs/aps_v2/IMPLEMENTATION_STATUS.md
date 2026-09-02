# APS V2 实施状态台账

> 此文件是执行状态的唯一事实来源。初始状态均为 `NOT_STARTED`。

允许状态：`NOT_STARTED`、`IN_PROGRESS`、`BLOCKED`、`COMPLETE`、`ROLLED_BACK`。

| Phase | 状态 | 开始时间 | 完成时间 | 执行者 | Feature Flag | 测试证据 | Handoff | Blocker |
|---|---|---|---|---|---|---|---|---|
| 0 Baseline | COMPLETE | 2026-08-14 14:30 +08:00 | 2026-08-14 16:13 +08:00 | Codex | `enable_aps_v2=0` | [Isolated evidence](./evidence/phase_00/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_00_complete_2026-08-14.md) |  |
| 1 Revision/Delivery | COMPLETE | 2026-08-14 16:36 +08:00 | 2026-08-14 17:54 +08:00 | Codex | `enable_aps_v2=0` | [Isolated evidence](./evidence/phase_01/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_01_complete_2026-08-14.md) |  |
| 2 Commitment/Admission | COMPLETE | 2026-08-14 18:15 +08:00 | 2026-08-14 19:05 +08:00 | Codex | `enable_aps_v2=0` | [Isolated evidence](./evidence/phase_02/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_02_complete_2026-08-14.md) |  |
| 3 Horizon/Status/Material | COMPLETE | 2026-08-14 19:07 +08:00 | 2026-08-14 20:02 +08:00 | Codex | `enable_aps_v2=0` | [Isolated evidence](./evidence/phase_03/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_03_complete_2026-08-14.md) |  |
| 4 Solver | COMPLETE | 2026-08-14 20:02 +08:00 | 2026-08-15 02:29 +08:00 | Codex | `solver_engine=Legacy` | [Original evidence](./evidence/phase_04/2026-08-14_isolated.md); [Initial PMC/GMC UAT](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat.md); [Fix retest](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat_retest.md) | [Historical handoff](./handoffs/phase_04_complete_2026-08-14.md) |  |
| 5 Shift Replan | COMPLETE | 2026-08-14 21:32 +08:00 | 2026-08-14 22:18 +08:00 | Codex | `enable_shift_replan=0` | [Isolated evidence](./evidence/phase_05/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_05_complete_2026-08-14.md) |  |
| 6 Co-product | COMPLETE | 2026-08-14 22:18 +08:00 | 2026-08-14 22:30 +08:00 | Codex | `enable_coproduct_campaign=0` | [Isolated evidence](./evidence/phase_06/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_06_complete_2026-08-14.md) |  |
| 7 Multi-level BOM | COMPLETE | 2026-08-14 22:30 +08:00 | 2026-08-14 23:18 +08:00 | Codex | `enable_multilevel_bom_planning=0` | [Isolated evidence](./evidence/phase_07/2026-08-14_isolated.md) | [Complete handoff](./handoffs/phase_07_complete_2026-08-14.md) |  |
| 8 Progress/UI/Rollout | COMPLETE | 2026-08-14 23:18 +08:00 | 2026-08-15 02:29 +08:00 | Codex | `enable_aps_v2=0` | [Historical completion audit](./evidence/phase_08/2026-08-15_completion_audit.md); [Initial PMC/GMC UAT](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat.md); [Fix retest](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat_retest.md) | [Historical handoff](./handoffs/phase_08_complete_2026-08-15.md) |  |

## 当前全局约束

1. `jce.1` 继续严格只读；Phase 0 完成不代表获得生产 migrate、build、restart、发布或启用 Flag 的授权。
2. 所有 DB/Fixture/Solver 测试继续只使用 `/home/ubuntu/frappe-bench/isolated/aps-v2-bench`。
3. Phase 0 的历史全量基线曾记录 3 failures、8 errors；本轮修复范围采用 250/250 定向串行回归，且 Capacity Balance 原 2 个旧 fixture error 已因补全冻结交期证据而通过。未在本轮重跑的历史全应用项目仍须按其原证据解释，不能凭定向结果推断全部消失。
4. 2026-08-15 首轮真实 PMC/GMC UAT 发现的 P0/P1 已修复并完成全新数据复验；19 个真实流程不变量全部通过，详见 [Fix retest](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat_retest.md)。
5. 生产尚未启用。工程和隔离验收完成不代表发布授权；仍需 GMC/Manufacturing Manager 对正式范围、窗口、现代浏览器烟测、下游 WO/WOS/入库/DP/DN 和两个滚动周期作独立确认。

## 状态更新规则

1. 一次只允许一个 Phase 为 `IN_PROGRESS`。
2. `COMPLETE` 必须链接测试和 Handoff 证据。
3. `BLOCKED` 必须说明需要谁作出什么决策。
4. Feature Flag 未验证关闭行为时不得标记 Complete。
