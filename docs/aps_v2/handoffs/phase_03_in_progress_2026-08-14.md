# Phase 3 交接记录（IN PROGRESS）

## 1. 目标与当前状态

- Phase：3 Horizon/Status/Material。
- 状态：IN_PROGRESS，开始时间 2026-08-14 19:07 +08:00。
- 目标：四类时间视窗、逾期 P0、Ready/Acknowledgment/Hard Blocked/Applied with Exceptions、Constraint Resolution、材料 advisory-only 和可解释动作。
- Feature Flag：`enable_aps_v2=0`；正式 V2 写入继续关闭。

## 2. 进入门禁

- Phase 0—2 均 COMPLETE；Phase 2 提供稳定 Commitment/Admission baseline。
- 进入 Legacy 五模块 133/133 PASS。
- 进入全量 649 tests，3 failures/8 errors 与 Phase 0—2 已知基线完全相同，新增回归 0。
- 仅 `/home/ubuntu/frappe-bench/isolated/aps-v2-bench` 的 `aps-opt-fixture.localhost` 可写。
- `jce.1` 保持零触碰，禁止 migrate、tests、build、restart、clear-cache 或写操作。

## 3. Scope Lock

- 只实现 Phase 3 时间/状态/Resolution/材料提示；不实现 Phase 4 CP-SAT、Phase 5 Shift Replan。
- 材料 Ready/Short/Unknown 只读展示，不进入数量、状态、Apply 指纹或锁。
- 产能欠量是 Acknowledgment Required，不是 Hard Blocked。
- 不提供 Force Apply All；非法单条只能修复或受控 Exclude 后重新分析。
