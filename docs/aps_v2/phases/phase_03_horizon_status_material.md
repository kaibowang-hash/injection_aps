# Phase 3：时间视窗、状态、材料退出和异常解决

## 1. 必读

- 全局契约 INV-02/05/07/10
- ADR-007/008/015
- 计算规格第 5、13 节
- UI Resolution Center 和权限规格

## 2. 前置条件

Phase 2 Complete；Run Demand Baseline 数量已由 Commitment 提供。

## 3. 目标

修正计划天数和逾期漏排；增加 Freeze/Restricted/Recovery；使产能欠量成为可确认风险；材料完全退出硬约束；允许排除非法单条需求后部分应用。

## 4. In Scope

- Planning Run 四类视窗字段和设置。
- 逾期 P0 带入。
- Ready/Acknowledgment/Hard Blocked/Applied with Exceptions。
- Constraint Resolution DocType/服务/UI。
- 临时周期/能力/适配 Override。
- Exclude From Release。
- 从 capacity analysis/apply/fingerprint 移除材料。
- 页面显示禁用原因和下一步。

## 5. Out of Scope

- CP-SAT 全局排程；本阶段可以用 Legacy 排程验证状态语义。
- 自动求最早恢复最优解；Phase 4 完成。
- Shift Replan。

## 6. Schema/Patch

扩展 Run/Result/Segment 时间与 readiness 字段；创建 Constraint Resolution；设置 due-time policy、freeze/restricted/recovery defaults。

旧状态映射只作迁移显示：Balanced/Suggestion Ready→Ready；Confirmation Required→Acknowledgment；Blocked 逐行重新分类，不能直接全部映射 Hard Blocked。

## 7. 后端

- Horizon end 使用 `days - 1`。
- Query 纳入 `due < start AND open qty > 0` 的逾期 Commitment。
- Recovery 不加载该区间新 Demand。
- 删除 `material_ready_qty` 对 schedulable qty 的 min、material shortage blocked check、Apply material revalidation、material Bin fingerprint/lock。
- 保留 Material Readiness 只读异步展示。
- Blocker registry 明确 Never/Temporary/Exclude Only。
- Exclude 后重新计算 readiness；排除项不得生成正式 Segment，下一 Run 保持 P0。

## 8. UI/API

Run Console 显示四区间。分析完成后无论状态如何都显示状态按钮区域：Ready→Apply；Acknowledgment→查看并确认；Hard Blocked→Resolution Center。

Resolution API 不能直接写最终 Segment；任何 Override/Exclude 后调用重新分析生成新 fingerprint。

## 9. 测试

- 14 天严格覆盖 14 个自然日。
- UI 默认读取 Settings。
- 8.1 open 在 8.2 Run 中出现并标记 overdue。
- Recovery 不引入新远期需求。
- 原料 0/变化不改变 qty/readiness/fingerprint。
- 1200/1000 为 Acknowledgment，不是 Hard Blocked。
- BOM 环/负 qty/locked overlap 不可 Override。
- 临时周期 Override 到期失效。
- 排除一条后其余 Applied with Exceptions，排除量下一 Run 仍为 P0。
- 无权限用户不能确认/Override/Exclude。

## 10. Exit Criteria

- R-009—R-013、R-024 核心状态部分 Verified。
- 当前“分析完成但无按钮”场景被可解释 UI 消除。
- 所有材料硬约束路径和测试已移除/改为 advisory。
- Partial Apply 数量守恒。

## 11. 交接给 Phase 4

提供完整 Solver Horizon、Frozen intervals、合法 Override、selected Commitment、readiness 分类接口。
