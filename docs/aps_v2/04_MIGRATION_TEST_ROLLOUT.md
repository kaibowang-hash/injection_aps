# APS V2 迁移、测试与上线规格

## 1. Patch 原则

- `jce.1` 禁止作为开发、Patch 演练或迁移验证目标；必须先使用生产脱敏副本。
- 每个 Phase 独立 Patch，按依赖顺序加入 `patches.txt`。
- Patch 幂等、可重跑、分批提交。
- 不删除历史业务单据。
- 外部/标准 DocType 只使用 Custom Field。
- 大数据回填保存进度和异常报告，不能长事务无检查点。
- Patch 不得覆盖 Workspace、Custom HTML Block、Client Script 或用户 Property Setter；确需升级 UI 时先输出差异并由管理员显式执行。

## 2. 回填

### Demand Identity

按 company/customer/scope/item 分批；外部行 ID 优先，其次 supersede/previous date。唯一匹配回填，多义进入异常，不猜测。

### Commitment

只回填仍有效 Approved/WO Proposed/Shift Proposed/Applied Run。Completed 历史只读。多个有效 Run 覆盖同一需求时，根据正式 WOS/执行/批准时间提出建议，由 GMC 确认所有权。

### Delivery

保留现有 APS Delivery Allocation。DP/DN 可明确追踪的补 Identity；后备匹配标记 Legacy Controlled Match；歧义进入 Unallocated。

### Co-product

旧 Family Segment 只有在 WO/Stock Entry 可建立精确输出血缘时才迁移为有效供给，否则仅历史展示。

## 3. 测试层

### Unit

Revision、Identity、Delivery match、Commitment、P1/P2、Horizon、Material advisory、Blocker、Override、BOM、Campaign、Forecast。

### Solver Property

NoOverlap、Frozen、数量守恒、Campaign 单计、BOM precedence、P1/P2 不损害 P0、确定性。

### Integration

使用 TRACEABILITY_MATRIX 的每个 R-ID 建场景测试，尤其：A/B 重叠、无 SO WO、DP FIFO/DN、逾期、1200/1000、单机两模、原料 0、Run 承接、Shift 延期、部分 Apply、联产品、C→A→X。

### Permission

逐角色覆盖 API、DocType、Override 自批准、正式 Apply、Emergency Replan。

### UI

按钮原因、模式确认、P0 锁定、方案比较、Campaign 拖动保护、Progress 下钻、翻译、空状态、分页导出。

### Migration

生产脱敏副本至少演练两次；Patch 重跑；Flag Off 回归；历史单据引用和总量不变；歧义报告完整。

## 4. 共同验收场景

1. A 8.1—8.14，B 8.8—8.14 相同：Partial 不重复。
2. B 修改重叠：只影响差额。
3. Revision 低于已执行：保存新数量并生成 Excess。
4. WO 无 SO 正常创建/排产/入库；DP 分配多个 SO；DN 回写需求。
5. 8.1 需求在 8.2 Run 中作为逾期 P0。
6. 日需求 1200、产能 1000：排最大量、显示恢复时间、可确认 Apply。
7. A/B 共用 120T：交付优先并比较换模方案。
8. 原料 0：不影响计划和 Apply。
9. Run2 不重复 Run1 未完成承诺。
10. 延期产生 Shift Proposal，不移动执行中任务。
11. 一条 Hard Blocked 排除后，其余 Applied with Exceptions。
12. Override 到期后不再有效。
13. Family A/B：两 WO、一 Campaign、一次资源占用。
14. C→A→X precedence。
15. Progress 数量与 Schedule/WO/Stock/DP/DN 守恒。

## 5. 性能目标

- Full Run 默认 120 秒内给出 Optimal/Feasible/明确超时。
- Shift Replan 30 秒默认上限。
- 10,000 排期行后台批处理。
- Progress 服务端聚合、分页、日期虚拟滚动。
- Solver 使用轻量不可变输入，不传 Frappe Document。

每个 Phase 需在脱敏实际规模数据上记录耗时、内存和 SQL 查询量；超标不得通过调大超时掩盖。

## 6. 灰度

1. V2 Flag Off 完成全部 Legacy 回归。
2. V2 Trial 与 Legacy 只读对比，不生成正式单据。
3. 单一 Plant Floor 灰度 Formal。
4. GMC 在至少两个完整滚动周期内双重核对。
5. 逐车间启用 Shift Replan。
6. Co-product/BOM 独立开关启用。
7. 稳定后停止 Legacy Formal，历史仍可读。

同一 Demand Identity 只能有一个 Formal owner。

## 7. 回退

关闭 Feature Flag 只停止新 V2 Formal，不删除已释放 WO/WOS。正式单据继续依执行状态管理。新 schema 可保留只读，避免破坏血缘。任何回退必须保存当前 owner 和开放需求快照。

## 8. 全局 Definition of Done

1. TRACEABILITY_MATRIX 全部目标项达到 Verified。
2. 逾期、Revision、跨 Run 不漏不重。
3. WO 无 SO 且 Delivery 完整追踪。
4. 原料不阻塞。
5. 欠量可确认并延续。
6. 无无审计强制通道。
7. Shift Replan 只提议。
8. 联产品工单独立、资源单计。
9. BOM 顺序可追溯。
10. Original/Current/Forecast/Actual 可见。
11. 一致性、权限、迁移、性能、翻译、回退全部验证。
