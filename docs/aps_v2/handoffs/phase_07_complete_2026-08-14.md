# Phase 7 交接记录（COMPLETE）

## 状态

- Phase：7 Multi-level BOM。
- 状态：COMPLETE。
- 证据：[Phase 7 isolated evidence](../evidence/phase_07/2026-08-14_isolated.md)。
- 安全状态：`enable_multilevel_bom_planning=0`，全部 V2 Flag 关闭，`jce.1` 零触碰。

## 已交付

1. 递归 BOM 建图/环检测、共享半成品合并、root allocation、损耗/UOM/批量/excess 守恒。
2. 半成品 hard-available 库存、无所有权 WIP 和跨 Run pre-WO claim 的唯一覆盖。
3. 默认/显式批准替代 BOM 快照、指纹、并发保护和变更后 fail closed。
4. 精确 APS BOM Pegging、Solver precedence、Campaign capacity-owner 映射和 Raw Material advisory leaf。
5. Result/Proposal/Work Order 的 exact BOM 复用与建单前指纹复验。
6. BOM Tree、Gantt dependency link、拖动预览拦截和 Forecast root risk 传播。

## Phase 8 输入

- Progress V2 必须使用稳定 Demand Identity/Commitment/Allocation/Pegging 血缘聚合，不能用物料+日期模糊猜测。
- 日期 cell 必须同时展示 schedule、Original/Current/Forecast/Actual、DP/DN、stock/shortage/recovery 和 source docs，总量与 Commitment/Allocation 守恒。
- Gantt 将 Campaign owner 渲染为单条机台 bar，成员 Result 作为可展开输出；已验证的 owner 同步和 BOM precedence 不得被 UI 聚合改变。
