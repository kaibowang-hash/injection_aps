# Phase 2：Demand Commitment、准入和跨 Run 所有权

## 1. 必读

- 全局契约 INV-01/02/03/05/06
- ADR-005/006
- 架构中的 Commitment、Admission、Stock Coverage、Run Transition
- 计算规格第 3—4、9 节

## 2. 前置条件

Phase 1 Complete；所有 Active Schedule Item 都有 Identity 或明确迁移异常。

## 3. 目标

使新 Run 只规划真正未覆盖的新需求；建立 P0 必排、P1 备货建议、P2 库存建议；旧 Run 的有效承诺在新 Run 中引用或转移，不重复生产。

## 4. In Scope

- Demand Commitment、Demand Admission、Stock Coverage Allocation。
- P0 基线和 P1/P2 候选。
- 当前 FG 库存唯一分配。
- Frozen/Carried/Reschedulable/Completed/Excess 分类。
- Run 所有权原子转移和跨 Run 引用。
- Existing WO/WIP 作为 supply commitment 的明确规则。
- Demand Admission Workbench。

## 5. Out of Scope

- 新时间视窗和欠量状态（Phase 3）。
- CP-SAT（Phase 4）。
- 自动班次重排。

## 6. Schema/Patch

创建三个 DocType，扩展 Planning Run baseline link/commitment totals。回填仍有效 Run：

- Frozen 正式 WOS 作为 referenced supply。
- 未开始且可复用 WO 作为 carried supply。
- 多 Run 争用同一 Identity 进入人工所有权确认。

## 7. 后端

`demand_ledger.py`：

- schedule open、stock coverage、carried remaining、新计划量。
- produced→stock coverage 转换，防双扣。
- Transfer/Reference/Supersede 原子操作。
- 公司锁和 stable allocation order。

`demand_admission.py`：

- P0 自动构建；
- P1 使用开放 SO 减开放客户排期和现有 P1；
- P2 使用 projected unallocated FG；
- 保存 PMC 选择快照和理由。

`run_transition.py`：基于执行和冻结状态分类。已开工任务不是可重排高优先需求，而是固定供给和资源锚点。

V2 Demand/Net 构建必须读取 Commitment，不再用 `modified` 解决同日优先。

## 8. UI/API

- Prepare Baseline 返回 P0、stock、carried、new qty。
- Admission Workbench P0 锁定；P1/P2 默认空。
- P1/P2 选择变化使旧 solver/analysis fingerprint 失效。
- Run 页面显示 source run 和 carry-forward 摘要。

## 9. 测试

- 两个客户需求只分配一次相同 FG 库存。
- Run2 引用 Frozen Run1，不重新计划。
- 可调任务所有权从 Run1 转给 Run2，Run1 Superseded。
- 已生产库存不同时作 produced 和 stock coverage。
- P1 = open SO - open schedule - active P1。
- P0 不可取消；P1/P2 未选择不进入 Formal。
- 并发两个 Run 只有一个获得 owner。
- Existing WO 不完整/多义不错误扣减。

## 10. Exit Criteria

- R-006—R-008、R-016 的所有权部分 Verified。
- 所有数量通过 Commitment 守恒检查。
- 不存在同 Identity 双 Formal owner。
- Admission 决策可审计并进入指纹。

## 11. 交接给 Phase 3

提供不可变 Run Demand Baseline 和 selected admission 集合；Phase 3 只改变时间/状态/可应用性，不重算另一套需求数量。
