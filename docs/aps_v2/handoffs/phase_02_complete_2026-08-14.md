# Phase 2 交接记录（COMPLETE）

## 1. 目标与结果

- Phase：2 Commitment/Admission。
- 状态：COMPLETE，完成时间 2026-08-14 19:05 +08:00。
- 已完成：Demand Commitment、P0/P1/P2 准入、FG 唯一覆盖、Frozen/Carried/Reschedulable/Completed/Excess、跨 Run Reference/Transfer、Admission Workbench。
- 未完成：无 Phase 2 blocker；Phase 3/4/5 内容按边界延期。

## 2. 修改范围

- 新增 DocType：`APS Demand Commitment`、`APS Demand Admission`、`APS Stock Coverage Allocation`。
- 扩展 `APS Planning Run`：baseline/admission fingerprint、P0/stock/carried/new/P1/P2 汇总和来源 Run 统计。
- 外部字段：只 create-if-missing 新增 Work Order/Scheduling Item 的隐藏 `custom_aps_commitment`。
- 服务：`demand_ledger.py`、`demand_admission.py`、`run_transition.py`。
- Patch：`implement_aps_v2_phase2_commitment_admission.py`。
- API：Prepare Baseline、Get Candidates、Preview Impact、Save Admission Decisions。
- UI：`aps-demand-admission-workbench`；Run Console 的 V2 条件式入口和承接摘要。

## 3. 关键实现决策

- P0 必排；P1/P2 默认 0 且必须带理由保存。
- 库存先于 carried supply，二者与 newly planned 相互排斥；Produced 入库后不再重复作为 carried。
- Frozen 永不由普通新 Run 抢占；Trial 只 Reference；Formal ownership 写入在本阶段 Public API 中保持关闭。
- Existing WO 血缘缺失时采用保守供应量并保留 unresolved audit，不猜测。
- Completed/Excess 单独落 Commitment，不形成新计划量。

## 4. 测试证据

- 详细证据：[Phase 2 isolated evidence](../evidence/phase_02/2026-08-14_isolated.md)
- Phase 2：Unit 10/10、Contract 5/5、Integration 6/6。
- Permission 51/51；UI Static 17/17；UI Runtime 6/6。
- Legacy 五模块 133/133。
- 全量 649 tests，3 failures/8 errors 与 Phase 0/1 既有基线完全相同，新增回归为 0。
- Patch/backfill/二次 migrate 幂等；前端定制总指纹迁移前后均为 `75ebe9…37c`。

## 5. 数据验证

- P0 数量守恒：`requested=stock+carried+newly_planned`。
- Stock Coverage 明细与 Commitment stock qty 一致。
- 数据库拒绝同 Identity 两个 active Formal owner。
- Frozen Reference、Reschedulable Transfer、Completed/Excess 均通过真实 DB 测试。

## 6. 回退和生产安全

- `enable_aps_v2=0`、`solver_engine=Legacy`，其余 V2 开关全关。
- 新 schema 可保留只读；不删除或回滚任何正式 ERP 单据。
- `jce.1` 未执行任何命令；未 build、restart 或 clear-cache。
- 原有 WO→WOS→入库→DP 分配 SO→DN 流程保持不变。

## 7. Phase 3 输入

Phase 3 可直接依赖：

- `demand_ledger.get_run_demand_baseline(run)` 返回 selected active Commitments、terminal Commitments、来源 Run、数量守恒结果和指纹。
- `demand_admission.get_demand_admission_candidates(run)` 返回 P0/P1/P2 候选、选择和决策指纹。
- `run_transition.get_commitment_supply_snapshot(commitment)` 返回 Frozen/Carried/Completed、可验证供应锚点和 unresolved 工单。

Phase 3 必须遵守：

- 只消费 selected Commitment，不重新计算另一套需求数量。
- 只实现时间视窗、状态、可应用性和原料 Ready/Short/Unknown 提示；原料不得成为数量或 Apply 硬约束。
- 不启用 CP-SAT、不开始自动班次重排、不开放 Formal V2 写入。
- 进入 Phase 3 前重新确认 Flag Off、133 项兼容基线和本 Handoff。
