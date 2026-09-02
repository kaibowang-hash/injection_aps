# APS V2 需求追踪矩阵

状态：`Planned`、`Implemented`、`Verified`、`Deferred`。

| ID | 需求 | 契约/决策 | 实施 Phase | 最低验收测试 | 状态 |
|---|---|---|---|---|---|
| R-001 | 重叠排期不重复、不漏算 | INV-01/02, ADR-001 | 1 | A/B Partial、Full、Incremental | Verified |
| R-002 | 稳定 Demand Identity | INV-01/12 | 1 | 日期移动、外部行 ID、歧义 | Verified |
| R-003 | WO 不强制 SO | INV-08, ADR-002 | 1 | 无 SO 工单创建和入库 | Verified |
| R-004 | Delivery Plan 晚绑定 SO | INV-08/09, ADR-003 | 1 | DP FIFO→DN→需求回写 | Verified |
| R-005 | 交货歧义不阻塞出货 | INV-09, ADR-004 | 1 | Unallocated Delivery | Verified |
| R-006 | P0/P1/P2 准入 | INV-06, ADR-005 | 2 | P0 锁定、P1/P2 选择 | Verified |
| R-007 | 跨 Run 不重复计划 | INV-01/02/03 | 2 | Run1/Run2 承接 | Verified |
| R-008 | 成品库存不重复覆盖 | INV-03 | 2 | 两需求/两 Run 库存分配 | Verified |
| R-009 | 逾期需求带入 | INV-02 | 3 | 8.1 需求在 8.2 Run | Verified |
| R-010 | Recovery Horizon | ADR-015 | 3/4 | 欠量最早补齐 | Implemented |
| R-011 | 原料不阻塞 | INV-07, ADR-008 | 3 | 原料 0 仍可 Apply | Verified |
| R-012 | 产能欠量可确认下发 | 状态契约 | 3/4 | 1200 需求/1000 产能 | Implemented |
| R-013 | Hard Blocker 受控处理 | INV-10, ADR-007 | 3 | Override/Exclude/不可覆盖 | Verified |
| R-014 | 全局交付优先求解 | INV-06, ADR-014 | 4 | 单机 A/B 争用 | Implemented |
| R-015 | 减少换模、吨位匹配、利用率软优化 | ADR-005/006 | 4 | 三方案指标比较 | Implemented |
| R-016 | 冻结执行任务 | INV-05 | 2/4/5 | 已开工任务不移动 | Verified |
| R-017 | 每班次滚动重排 | INV-11, ADR-009/010 | 5 | 实际延迟→差异 Proposal | Verified |
| R-018 | Forecast 延期传播 | INV-11 | 5/7 | 同机/同模/BOM 影响 | Verified |
| R-019 | 联产品自动工单 | ADR-011 | 6 | 主/联产品两 WO | Verified |
| R-020 | 联产品资源只计一次 | INV-04, ADR-012 | 6 | 单 Campaign No-Overlap | Verified |
| R-021 | 多层 BOM C→A→X | ADR-013 | 7 | Precedence/Pegging | Verified |
| R-022 | Progress 日期矩阵 | INV-12 | 8 | Schedule/Plan/Forecast/Actual/Delivery | Implemented |
| R-023 | Gantt 单 Campaign 多输出 | ADR-012 | 6/8 | Campaign 展开和拖动保护 | Verified |
| R-024 | 按钮禁用有原因 | UI 契约 | 3/8 | Hard Blocked 空状态和动作 | Implemented |
| R-025 | Feature Flag 可回退 | 技术边界 | 0-8 | Flag Off Legacy 回归 | Verified |
| R-026 | 生产站点和既有前端定制不被开发/迁移覆盖 | INV-13, ADR-016 | 0-8 | after_migrate/Workspace/Custom HTML/Property Setter 静态与迁移差异测试 | Verified |

> 2026-08-15 真实 PMC/GMC UAT 推翻了部分历史 Verified 结论：秒级周期/重复 setup 影响 R-010/R-012/R-014；三方案延期口径影响 R-015；Demand Identity 权限导致 R-022 对 PMC/GMC 空白；按钮与后端权限漂移影响 R-024。详见 [UAT 报告](./evidence/uat/2026-08-15_pmc_gmc_realistic_uat.md)。修复和重验前保持 `Implemented`，不得用于生产发布签字。
