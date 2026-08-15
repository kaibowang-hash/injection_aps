# Phase 1：排期 Revision、Demand Identity 和 Delivery 血缘

## 1. 必读

- 全局契约 INV-01/02/08/09/12
- 决策 ADR-001/002/003/004
- `../01_TARGET_ARCHITECTURE.md` 的 Identity、Schedule、Delivery 实体
- `../02_CALCULATION_AND_SOLVER_SPEC.md` 第 1—2 节
- `../03_UI_API_SECURITY_SPEC.md` Import 和出货兼容

## 2. 前置条件

Phase 0 Complete；V2 仍保持 Trial/Flag Off。

## 3. 目标

建立跨版本稳定需求身份，正确处理 Full/Partial/Incremental，允许客户真实减少数量并形成 Excess；保留现有“WO→WOS→入库→DP分配SO→DN”操作，并为 DP/DN 增加明确 APS 血缘。

## 4. In Scope

- APS Demand Identity DocType。
- Customer Delivery Schedule/Item Revision 字段。
- 导入模式推荐＋用户确认＋差异预览。
- Demand Identity 继承/人工消歧。
- Delivery Plan Qty/Item 和 DN Item 自定义字段。
- DP 按 customer/company/date/address 正确分组。
- Delivery 同步从 SO/date 强相等迁移为明确血缘优先。
- 无链接/歧义不阻塞出货。
- Work Order 创建路径取消 SO 强制要求，但保留 APS Result/Run 追踪。

## 5. Out of Scope

- Commitment 所有权和 P0/P1/P2（Phase 2）。
- CP-SAT、视窗和状态重构。
- 联产品。

## 6. Schema/Patch

按架构规格创建 Identity 和 Schedule 字段；扩展 DP/DN 自定义字段。Patch：

1. Schema/Custom Field。
2. 现有 Active Schedule 唯一可映射 Identity 回填。
3. 多义记录写迁移报告，不自动合并。
4. 不修改历史 qty、delivered_qty 和 ERPNext SO 数据。

## 7. 后端

新增 `schedule_revision.py`：

- `recommend_mode(previous, incoming)` 只推荐。
- `preview_revision` 返回 Added/Changed/Unchanged/Retained/Cancelled/Date Moved/Excess。
- `resolve_identity` 优先 external line、previous item/date、唯一业务候选。
- `apply_revision` 在 company/scope 锁中原子执行。

新增 `delivery_fulfillment.py`：

- 明确链接继承 DP Qty→DP SO Item→DN Item。
- Legacy Controlled Match 只在无明确链接时运行。
- Ambiguous 写 Unallocated 状态，不抛出阻塞物流的 ValidationError。
- 退货沿原 allocation lineage 反冲。

修改现有 `_sync_delivery_plan`：按客户等范围分组，绝不能把多客户结果写入第一个客户的 DP。

WO Proposal：允许无 SO；如果有 SO 仍保留，但不是审批前提。

## 8. API/UI

API：推荐、预览、应用、消歧。导入 UI 必须显示推荐理由并强制确认。

DP：APS 字段自动填充；手工 DP 提供需求建议但不增加强制步骤。Unallocated Delivery 提供独立工作队列。

## 9. 关键测试

- 初次导入推荐 Full。
- B 覆盖子区间推荐 Partial，必须人工确认。
- Incremental 重合明确相加。
- 日期移动继承 Identity。
- 数量低于已执行保存 Revision 并产生 Excess。
- 多义 Identity 不猜测。
- WO 无 SO 创建/提交/入库。
- DP FIFO 分配多个 SO 后 DN 继承 Identity。
- 无链接 DN 正常提交，APS 记录 Unallocated。
- 退货反冲原需求。
- 多客户 APS Result 生成多个正确 DP。

## 10. Exit Criteria

- R-001—R-005 达到 Verified。
- 相同重叠排期在 Partial 下不增加有效需求。
- 现有出货操作无新增强制步骤。
- 旧 Delivery 数据未被错误重写。
- Flag Off 时当前导入/Delivery 兼容测试通过。

## 11. 交接给 Phase 2

提供稳定 Identity 服务、当前有效 Revision 查询和明确 delivered rollup API；Phase 2 不得自己重新实现排期版本逻辑。
