# Phase 7：多层 BOM 与生产前置关系

## 1. 必读

- 全局契约 INV-03/05/07
- ADR-013
- 架构 BOM Pegging
- 计算规格 BOM
- Solver precedence

## 2. 前置条件

Phase 6 Complete；Commitment、Campaign 和 Solver 可表示 parent/child task。

## 3. 目标

递归展开需要制造的半成品，扣减明确半成品库存/WIP，建立 Pegging 和 C→A→X 前置关系，并把下层延期传播到客户交期。Raw Material 不成为硬约束。

## 4. In Scope

- APS BOM Pegging。
- APS 可生产物料组配置。
- 默认/替代 BOM 决策快照。
- 递归展开、损耗、批量、共享半成品合并。
- BOM 环检测。
- 半成品 stock/WIP coverage。
- Solver precedence。
- Shift Forecast 向父层传播。
- Progress 下钻 parent/child。

## 5. Out of Scope

- 原料采购、到货和库存可用性约束。
- 未批准替代物料自动替换。
- 工艺路线之外的供应链优化。

## 6. Schema/Patch

创建 Pegging；APS Settings 增加可生产 Item Group 和 BOM policy；Result/Commitment 增加 parent/root demand links。

## 7. 后端

- `bom_planning.py` 使用 DFS/topological graph，先检测环再计算。
- 冻结 BOM、conversion、loss snapshot。
- 共享半成品按 item/required bucket 合并，再保留多 parent pegging。
- Raw Material 节点记录为 informational leaf，不创建 APS machine demand。
- parent earliest start 受 child available time 约束。
- Child shortage/late 沿 pegging 聚合到 root customer demand。
- WO 创建顺序可以并行生成，但 WOS 必须满足 precedence。

## 8. UI

Demand/Result 详情增加 BOM Tree：需求、库存覆盖、需生产、计划完成、父任务影响。Gantt 显示 dependency link；拖动 parent 早于 child 时预览阻止或同步建议。

## 9. 测试

- C→A→X 顺序。
- 多父共享 A 合并数量但保持 pegging。
- 半成品库存减少 child production。
- Raw Material 0 不阻塞。
- BOM 环 Hard Blocked 且不可 Override。
- 损耗/批量/UOM 守恒。
- Child 延期更新 root Forecast 和风险。
- 替代 BOM 选择进入 fingerprint。

## 10. Exit Criteria

- R-018 BOM 传播、R-021 Verified。
- BOM 数量和根需求可双向追溯。
- Solver Validator 独立验证 precedence。
