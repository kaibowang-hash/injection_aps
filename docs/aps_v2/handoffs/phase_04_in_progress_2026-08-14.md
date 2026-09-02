# Phase 4 交接记录（IN PROGRESS）

## 目标与状态

- Phase：4 Solver
- 开始时间：2026-08-14 20:02 +08:00
- 当前状态：IN_PROGRESS
- Feature Flag：`enable_aps_v2=0`、`solver_engine=Legacy`

## Scope Lock

- 实现确定性 CP-SAT 适配层、全局交付优先目标、机台/模具/吨位/切换/冻结约束、可解释求解摘要、超时/不可用回退。
- 不实现 Phase 5 每班次自动重排，不实现 Phase 6 联产品，不实现 Phase 7 多层 BOM。
- 只在隔离站点测试；不访问 `jce.1`，不覆盖既有前端自定义。
