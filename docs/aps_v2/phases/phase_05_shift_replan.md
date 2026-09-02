# Phase 5：每班次 Shift Replan 与延期传播

## 1. 必读

- 全局契约 INV-05/11/12
- ADR-009/010
- 计算规格 Forecast
- UI Shift Replan、权限和审计

## 2. 前置条件

Phase 4 Complete；正式 CP-SAT 结果和 Frozen 状态可用。

## 3. 目标

每班次开始前刷新实际进度，计算当前 Forecast，对下一班次和后续 Flexible 任务局部重排，生成可审核差异 Proposal；不自动修改执行中任务。

## 4. In Scope

- APS Replan Cycle。
- Actual data cutoff/freshness。
- Forecast rate/end。
- 同机/同模后续影响传播。
- Scheduled Shift/Emergency Manual。
- Work Order quantity diff 与 Shift Schedule diff 分离。
- 下一班次默认释放范围。
- Shift Replan Center。

## 5. Out of Scope

- BOM 上层传播等 Phase 7 完成后扩展。
- 自动 Apply。
- 修改正在生产任务。

## 6. Schema/Patch

创建 Replan Cycle；扩展 Segment forecast/baseline/replan 字段；WOS/Scheduling Item 增加 replan link。增加 scheduler 配置和 hook，默认关闭。

## 7. 后端流程

1. 创建幂等 Cycle。
2. 刷新 WO/WOS/Stock Entry/机器/模具/Downtime/DN/Schedule Revision。
3. 保存 execution cutoff。
4. 校验 freshness；不足时标准速度 fallback 需 GMC 确认。
5. 计算 remaining/forecast。
6. 分类 Frozen/Restricted/Flexible。
7. CP-SAT 局部求解到 Recovery end。
8. 生成 baseline diff。
9. 仅对缺少/可更新数量生成 WO Proposal。
10. 对下一班次生成 Shift Proposal。
11. 审批/Apply 后更新 Current Plan，保留 Original。

普通 Cycle 不允许动当前开始班次；Emergency 只可动尚未开工，需更高权限和原因。

## 8. 差异类型

Unchanged、Moved Earlier/Later、Machine/Mold Changed、Qty Changed、Split/Merged、Added、Cancelled Before Start、Frozen、Delayed by Execution。

每条 diff 保存 before/after、交付影响、原因和 solver explanation。

## 9. 测试

- 延期任务 Forecast 使用实际稳定速度；不足样本回退标准速度。
- 正在生产和 Material Transfer 不移动。
- 下一任务 Forecast 顺延，但 Planned 未经 Apply 不变。
- 普通 Cycle 只生成 Proposal。
- WO 已存在且数量不变不重建。
- WOS 下一班次按新建议生成。
- 重复触发同幂等键不重复 Proposal。
- Stale data 警告/权限。
- Emergency 权限和审计。

## 10. Exit Criteria

- R-017、R-018 非 BOM 部分 Verified。
- 定时任务永不自动 Apply。
- Original/Current/Forecast 数据可同时查询。
- Handoff 明确 Phase 7 如何接入 BOM 传播。
