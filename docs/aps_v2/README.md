# Injection APS V2 文档索引与执行路线

## 1. 使用方式

本目录是 APS V2 的唯一开发事实来源。任何开发人员或 AI 代理必须从本文件开始，不得只读取某个 Phase 后直接修改代码。

强制阅读顺序：

1. [AI 执行协议](./AI_EXECUTION_PROTOCOL.md)
2. [生产服务器安全约束](./PRODUCTION_SAFETY.md)
3. [全局不可变契约](./00_GLOBAL_CONTRACT.md)
4. [已确认设计决策](./DECISIONS.md)
5. [目标架构与数据模型](./01_TARGET_ARCHITECTURE.md)
6. [计算与求解规格](./02_CALCULATION_AND_SOLVER_SPEC.md)
7. [UI、API、权限与审计规格](./03_UI_API_SECURITY_SPEC.md)
8. [迁移、测试与上线规格](./04_MIGRATION_TEST_ROLLOUT.md)
9. [实施状态台账](./IMPLEMENTATION_STATUS.md)
10. 当前待执行的 Phase 文件

开发结束后必须更新：

- [实施状态台账](./IMPLEMENTATION_STATUS.md)
- [需求追踪矩阵](./TRACEABILITY_MATRIX.md)
- 必要时更新 [设计决策记录](./DECISIONS.md)
- 使用 [Phase 交接模板](./HANDOFF_TEMPLATE.md) 保存本阶段证据
- 启动新执行会话时使用 [Phase 执行提示模板](./PHASE_EXECUTION_PROMPT.md)

最终用户与运维资料：

- [APS V2 用户操作指南](./APS_V2_USER_GUIDE_ZH.md)
- [APS V2 运维与故障处理手册](./APS_V2_OPERATIONS_RUNBOOK_ZH.md)
- [APS V2 灰度启用与回退方案](./APS_V2_ROLLOUT_ROLLBACK_ZH.md)
- [PMC/GMC 真实业务 UAT 修复复验](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat_retest.md)

## 2. 文档优先级

发生描述冲突时，按以下顺序裁决：

```text
用户最新明确确认
> 00_GLOBAL_CONTRACT.md
> DECISIONS.md 中 Accepted 决策
> 共享规格 01/02/03/04
> 当前 Phase 文件
> 代码注释和旧文档
```

执行 AI 不得自行选择较低层文档覆盖较高层规则。发现冲突时停止相关实现，记录到状态台账的 Blocker，并向用户请求决策。

## 3. 单一事实来源

| 内容 | 唯一维护文件 |
|---|---|
| 总目标、业务边界、不变量 | `00_GLOBAL_CONTRACT.md` |
| 生产站点、命令和前端定制保护边界 | `PRODUCTION_SAFETY.md` |
| 用户已确认/修正的设计决策 | `DECISIONS.md` |
| DocType、字段、模块和数据流 | `01_TARGET_ARCHITECTURE.md` |
| 数量公式、时间、优先级、CP-SAT | `02_CALCULATION_AND_SOLVER_SPEC.md` |
| 页面、API、权限、审计 | `03_UI_API_SECURITY_SPEC.md` |
| 迁移、测试、性能、灰度和回退 | `04_MIGRATION_TEST_ROLLOUT.md` |
| 工作是否完成、证据在哪里 | `IMPLEMENTATION_STATUS.md` |
| 需求由哪个 Phase/测试覆盖 | `TRACEABILITY_MATRIX.md` |
| 给新 AI 的阶段启动指令 | `PHASE_EXECUTION_PROMPT.md` |

Phase 文件只描述该阶段如何落地，不应复制并改写共享公式。

## 4. Phase 依赖图

```text
Phase 0  基线与安全护栏
   ↓
Phase 1  排期 Revision + Demand Identity + Delivery Plan 血缘
   ↓
Phase 2  Demand Commitment + P0/P1/P2 + 跨 Run 所有权
   ↓
Phase 3  视窗 + 状态 + 材料退出 + 异常解决
   ↓
Phase 4  CP-SAT 有限产能求解
   ↓
Phase 5  每班次 Shift Replan
   ↓
Phase 6  联产品 Campaign
   ↓
Phase 7  多层 BOM
   ↓
Phase 8  Progress/Gantt/UI 收尾与正式切换
```

不得跳过 Phase 0—3 直接建设求解器。否则求解器会优化重复、遗漏或无所有权的需求。

## 5. Phase 文件

| Phase | 文件 | 主要交付 |
|---|---|---|
| 0 | [phase_00_baseline.md](./phases/phase_00_baseline.md) | 基线、Feature Flag、Fixture、V1/V2 对比框架 |
| 1 | [phase_01_revision_delivery.md](./phases/phase_01_revision_delivery.md) | 排期版本、稳定需求身份、DP/DN 血缘、WO 无 SO |
| 2 | [phase_02_commitment_admission.md](./phases/phase_02_commitment_admission.md) | 承诺台账、准入、库存覆盖、跨 Run 防重复 |
| 3 | [phase_03_horizon_status_material.md](./phases/phase_03_horizon_status_material.md) | 逾期/恢复视窗、状态、部分应用、材料退出 |
| 4 | [phase_04_solver.md](./phases/phase_04_solver.md) | CP-SAT 数量分配、顺序、方案比较和验证 |
| 5 | [phase_05_shift_replan.md](./phases/phase_05_shift_replan.md) | 执行预测、每班次差异重排、排产建议 |
| 6 | [phase_06_coproduct.md](./phases/phase_06_coproduct.md) | 联产品 Campaign、工单、入库、Gantt |
| 7 | [phase_07_multilevel_bom.md](./phases/phase_07_multilevel_bom.md) | BOM 展开、Pegging、C→A→X 前置约束 |
| 8 | [phase_08_progress_ui_rollout.md](./phases/phase_08_progress_ui_rollout.md) | 追踪矩阵、Gantt V2、全量灰度切换 |

## 6. 阶段门禁

进入下一 Phase 前，当前 Phase 必须同时满足：

1. Schema 和 Patch 可重复迁移。
2. 新服务/API 具备权限和幂等保护。
3. 单元、集成、权限和 UI 契约测试通过。
4. Legacy 行为在 Feature Flag 关闭时不变。
5. 数量守恒和资源不重叠检查通过。
6. `IMPLEMENTATION_STATUS.md` 已更新证据链接和已知限制。
7. Phase 交接记录完整。
8. 没有未处理的 P0/P1 缺陷。

## 7. 禁止事项

- 不得为了让测试通过而改变已确认业务公式。
- 不得把原料恢复为 APS 硬约束。
- 不得重新强制 Work Order 关联 Sales Order。
- 不得让 Delivery Plan/DN 血缘阻塞现有正常出货。
- 不得将联产品多个输出重复计入机器产能。
- 不得自动移动已开工、转料或冻结任务。
- 不得提供无审计的 `Force Apply All`。
- 不得在新 Run 重复拥有上一 Run 的相同需求数量。
- 不得直接修改 ERPNext/zelin_pp 原始 DocType JSON；使用 injection_aps 自定义字段和安全扩展。
- 不得在开发过程中对 `jce.1` 执行任何写库、迁移、构建、重启、清缓存或测试数据操作。
- 不得覆盖、重排、删除既有 Workspace、Custom HTML Block、Client Script、Property Setter 或其他前端定制。
