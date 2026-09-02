# APS V2 运维与故障处理手册

## 1. 安全边界

- 生产启用、migrate、build、restart、clear-cache 和数据修复必须走正式变更审批；开发证据不构成生产授权。
- 不覆盖 Workspace、Custom HTML Block、Client Script、Property Setter 或 Customize Form 布局。
- 不通过 SQL 手工改 Commitment owner、Result、Segment、Stock/Delivery Allocation 来“修平”报表。
- 回退开关不撤销已经提交的 Work Order、Work Order Scheduling、Stock Entry、Delivery Plan 或 Delivery Note。

## 2. 日常监控

| 指标 | 建议告警 | 处理入口 |
|---|---|---|
| Hard Blocked 数量 | 大于 0 | Constraint Resolution Center |
| Unknown / conservation mismatch | 大于 0 | Progress 下钻、Commitment/Allocation 守恒 |
| Formal owner conflict | 任意一条 | Demand ledger ownership repair，不允许手工双 owner |
| Critical Unplanned / Late | 超出班次容忍值 | Scenario、Recovery、Shift Replan |
| Solver timeout/failed | 任意 Formal | Solver Job、输入指纹、资源/BOM blocker |
| Execution data freshness | 超过 APS Settings 阈值 | Actual refresh、MES/Stock Entry 同步 |
| Unallocated Delivery | 持续增长 | Delivery matching queue；不得阻塞出货 |
| Progress runtime | 常用筛选明显高于历史基线 | 检查索引、筛选范围、SQL 慢查询 |
| Shift Proposal backlog | 跨班次未审 | Shift Replan Center |

## 3. 标准诊断顺序

1. 记录用户、页面、Company、Plant Floor、Run、Demand Identity、时间和输入指纹。
2. 确认 Feature Flag 和 Solver engine；不要把 Single Run 审计视图误当当前投影。
3. 查看 Run 的状态词：Ready、Acknowledgment Required、Hard Blocked、Applied with Exceptions 或 Applied。
4. 从 Progress 日期单元格下钻 Demand → Commitment → Result/Segment/Campaign → WO/WOS/Stock → DP/DN。
5. 检查数量守恒 delta、owner conflict 和被权限过滤的范围。
6. 检查 Solver Job、Exception/Resolution、Audit Log 和最近一次输入/输出 fingerprint。
7. 只用受控动作重新分析、确认、Override、Exclude 或 Replan；不可 Force Apply All。

## 4. 故障场景

### 4.1 Progress 是 Unknown

- 无 Demand Identity：仅对旧数据运行唯一匹配回填；多义必须人工确认。
- owner conflict：停止 Formal Apply，恢复每个 Demand Identity 唯一 active Formal owner。
- conservation mismatch：分别核对有效需求、已交货、库存分配、carried/new plan 和未计划量；不要把 Actual、Original、Current、Forecast 相加。
- 无 Forecast：刷新实际并重新运行 Shift Forecast；Current Plan 存在不等于 Forecast 已生成。

### 4.2 Delivery 数量不一致

- 以 ERPNext Delivery Note 为业务事实，以 APS Delivery Allocation 为 V2 精确归属。
- 检查 Delivery Plan Item Qty 的 Demand Identity、Delivery Note Item 血缘、退货原 Allocation 和 `is_effective`。
- 无唯一归属进入 Unallocated Delivery；继续允许正常出货，匹配完成后重算 Progress。

### 4.3 Campaign 重复占用或显示多个机器条

- 校验所有 Campaign outputs 指向同一个 `capacity_owner_segment`。
- 机器视图只渲染 owner；派生 output 必须带 `is_campaign_derived` 和 owner 映射。
- 资源校验以 Campaign owner 为准，不能靠前端隐藏消除后端重叠。

### 4.4 Solver/Apply 被阻止

- 产能不足：应保留最大可行量、Critical Unplanned 和 Recovery；通常要求风险确认，不是 Hard Blocked。
- 原料 Short：只提示，不允许作为 Apply blocker。
- BOM 环、负数量、冻结重叠、损坏输入或过期 fingerprint：不可 Override，先修复并重算。
- 临时参数/兼容资源：按权限发起和审批 Override，并记录范围、原因和到期时间。

## 5. 性能与容量

- Progress 强制服务端分页，单页最多 200 行；矩阵可见列最多 31，日期范围最多 366 天。
- 查询索引覆盖 Schedule Identity/Date、Commitment owner/run、Result commitment/run、Stock identity/status 和 Delivery identity/effective/date。
- 10,000 行/事件聚合基线由 Phase 8 自动测试记录；实际生产上线前仍要使用目标车间的典型 Run 记录响应时间、数据库耗时和慢查询。
- 超标时先缩小客户/物料/日期筛选并检查索引，不通过调大超时掩盖问题。

## 6. 支持升级

- 业务归属/风险确认：PMC → GMC。
- 机器、模具、冻结和 Emergency Replan：Manufacturing Manager。
- 出货匹配：Sales/Stock Manager 与 PMC。
- 程序、迁移、权限、性能：System Manager/应用维护人员。
- 升级材料至少包含 Run、Identity、页面筛选、错误原文、时间、fingerprint 和可读来源单据；不得复制生产密码、Token 或客户敏感附件。
