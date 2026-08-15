# Phase 4：CP-SAT 有限产能求解

## 1. 必读

- 全局契约 INV-03/04/05/06/10
- ADR-005/006/014/015
- 完整 `../02_CALCULATION_AND_SOLVER_SPEC.md`
- 迁移/性能规格

## 2. 前置条件

Phase 3 Complete；OR-Tools 环境兼容性已在 Phase 0 确认；需求和视窗输入稳定。

## 3. 目标

用可验证的 CP-SAT 两层模型替代逐需求贪心正式求解，实现交付优先、欠量恢复、计划稳定、减少换模、连续生产、吨位匹配和等价机利用率软平衡。

## 4. In Scope

- OR-Tools 固定兼容依赖。
- Solver Input Snapshot 和整数化。
- Shift Bucket Allocation。
- Campaign/Task Sequencing（本阶段单输出任务也作为 campaign task）。
- 分层目标。
- Optimal/Feasible/Timeout/Fallback。
- 独立 Validator。
- 推荐/交付优先/换模最少方案比较。
- Solver 结果写入现有 Result/Segment/Commitment。

## 5. Out of Scope

- Family 多输出约束（Phase 6 接口扩展）。
- BOM precedence（Phase 7 接口扩展）。
- 自动班次重排（Phase 5）。

必须预留 generic multi-output 和 precedence 输入结构，但不得伪造未实现功能。

## 6. 模块

- `solver/models.py`：不可变 dataclass。
- `solver/input_builder.py`：Frappe→solver snapshot。
- `solver/capacity_solver.py`。
- `solver/sequence_solver.py`。
- `solver/objectives.py`。
- `solver/validator.py`。
- `solver/scenarios.py`。
- `solver/serialization.py`：指纹和 explanation。

Solver 模块不得 import Frappe Document 或写数据库；编排层负责事务保存。

## 7. 实现顺序

1. Integer units 和 capacity calendar。
2. Frozen intervals/no-overlap。
3. P0 on-time/late/unplanned conservation。
4. 分层目标 1—4。
5. transition setup、continuity、tonnage、utilization。
6. P1/P2 residual。
7. 精确 sequencing。
8. scenarios/explanation。
9. validator/persistence。
10. background job/progress/cancel。

## 8. UI/API

Run Analyze 启动作业；显示 solver phase/runtime/status/gap。Scenario Comparison 按共享 UI 规格。选择方案后重新验证 fingerprint，风险确认后 Apply。

不能在无 Feasible 解时保存空正式 Segment；Fallback 必须明显标记并需确认。

## 9. 测试

- 每条计算规格硬约束 property test。
- Frozen 不移动。
- 单机 A/B 三种顺序比较，推荐交付不劣于其他方案。
- 换模更少不能恶化前层固定最优交付。
- 吨位/利用率仅在等价交付结果生效。
- Recovery 返回最早补齐。
- P1/P2 不侵占 P0。
- Feasible timeout 可保存 Trial；无 feasible 不写。
- deterministic seed。
- Validator 能拒绝人工构造的非法解。
- 实际规模性能基线。

## 10. Exit Criteria

- R-010、R-012、R-014、R-015、R-016 Solver 部分 Verified。
- Solver/Validator 无共享实现导致的自证循环；Validator 独立计算关键不变量。
- Scenario 指标与 Result/Segment 总量一致。
- Legacy Flag Off 回归通过。

## 11. 回退

`solver_engine=Legacy` 停止新 CP-SAT Run；已 Applied V2 单据保留执行。Solver schema 和快照保留只读。
