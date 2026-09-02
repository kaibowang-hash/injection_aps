# Phase 1 交接记录（COMPLETE）

## 1. 目标与结果

- Phase：1 Revision/Delivery。
- 状态：COMPLETE。
- 完成目标：稳定 Demand Identity、Full/Partial/Incremental Revision、真实减少与 Excess、无 SO Work Order、Delivery Plan/DN 明确血缘、Unallocated Delivery 非阻塞队列。
- 未完成目标：无 Phase 1 Blocker；Commitment、P0/P1/P2、实时执行冻结、Solver、联产品和多层 BOM 按计划留给后续 Phase。

## 2. 修改范围

- 新增服务：`schedule_revision.py`、`delivery_fulfillment.py`。
- 新增 DocType：`APS Demand Identity`、`APS Unallocated Delivery`。
- 扩展：Customer Delivery Schedule/Header Item、APS Delivery Allocation、Delivery Plan Qty/Item、Delivery Note Item。
- Patch：`implement_aps_v2_phase1_revision_delivery.py`，只创建缺失字段并执行保守回填。
- API：模式推荐、Revision 预览/应用、身份消歧、Unallocated Delivery 处理。
- UI：Schedule Console 在 Flag On 时进入推荐/确认流程；Unallocated Delivery 提供独立列表和处理动作。

## 3. 关键实现决策

- 使用 ADR-001/002/003/004；文件只提供模式推荐，业务意图由用户显式确认。
- Stable Demand Identity 是排期跨 Revision 的所有权锚点；日期、数量和 SO 不是身份本身。
- WO 不强制 SO；SO 继续由现有 Delivery Plan 分配，APS 只增加隐藏血缘。
- 无法唯一归属的 Delivery 不阻塞物流，不猜测，进入独立 Queue。
- 请求幂等指纹不含可变 active-state；并发安全由独立 token 保证。

## 4. 测试证据

- Legacy 进入与退出基线：133/133 PASS。
- Phase 1：Revision 10/10、Delivery 8/8、Integration 8/8 PASS。
- Permission/API：51/51 PASS。
- UI/Customization/Flag：38/38 PASS。
- 最终全量：628 tests，3 failures、8 errors；与 Phase 0 已登记基线完全一致，Phase 1 新增回归为 0。
- Migration：隔离 migrate 重跑成功；backfill 重跑为 0/0/0/0。
- Performance：10,000 行核心 Revision 0.5285 秒，最大 RSS 106,136 KB。

完整证据见 [Phase 1 隔离证据](../evidence/phase_01/2026-08-14_isolated.md)。

## 5. 数据验证

- 需求数量：Partial 未触及身份保留，Incremental 独立相加，Full 遗漏身份归零；精确重试不重复。
- Production：真实无 SO WO、领料和制造入库通过；Phase 1 不重写现有生产/出货操作步骤。
- Delivery：DP→DN→Allocation→Identity rollup、退货反冲和 Unallocated queue 均通过。
- Migration：唯一记录回填；重复外部行不合并并生成幂等异常。

## 6. 已知限制和 Deferred

- 已开工未产出数量的统一实时冻结由 R-016 后续 Phase 实现；Phase 1 使用持久化 executed floor、produced 和 delivered。
- 自动 DP helper 已修复但未接入新的自动创建入口，避免改变既有用户操作。
- V2 Formal、Commitment owner、P0/P1/P2 均未启用。

## 7. 回退

- 保持或恢复 `enable_aps_v2=0` 即停止所有 Phase 1 新入口。
- 新增 schema 可安全保留只读；不得删除已有 Identity/Allocation/Queue 或任何正式 ERPNext 单据。
- Flag Off 的 Legacy import、delivery 和 Work Order 契约已有回归保护。

## 8. Phase 2 输入

- 可依赖：`APS Demand Identity`、`schedule_revision` 当前有效 Revision 查询、`delivery_fulfillment` delivered rollup、稳定 Schedule Item identity 字段。
- 不得依赖：旧 SO/date 相等作为 Demand owner；不得自行重做 Revision 合并逻辑。
- 必须先验证：所有 Active Schedule Item 有 Identity 或明确迁移异常；建立 Commitment owner 前先定义已开工未产出量和冻结状态的唯一归属。
- Phase 2 仍是 `NOT_STARTED`；必须单独通过进入门禁后才能标记 IN_PROGRESS。
