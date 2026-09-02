# APS V2 用户操作指南

## 1. 适用范围

本指南面向 PMC、GMC、制造经理、制造用户和销售查看人员。APS V2 不改变现有生产与出货顺序：

```text
发工单 → 工单排产 → 工单入库 → 出货计划分配销售订单 → 下推销售出库
```

APS 在后台用 Demand Identity 把客户排期、生产承诺、工单/排产、入库、Delivery Plan 和 Delivery Note 汇总到同一需求；它不会要求工单先绑定销售订单，也不会因 APS 血缘缺失阻止正常出货。

## 2. 一次完整计划

1. 在 Schedule Console 导入客户排期，检查系统推荐的 Full Replacement、Partial Revision 或 Incremental Demand，并明确确认模式。
2. 在 Demand Admission Workbench 检查 P0/P1/P2。P0 必排且不能取消；P1/P2 由 PMC 选择。
3. 在 Run Console 检查 Demand、Freeze、Restricted、Recovery 四类时间窗口，以及材料提示、BOM、Campaign 和约束状态。
4. 运行 Scenario Analysis。Trial 会在 V2 投影前保存 Legacy baseline，并在 Scenario Comparison 显示 Legacy/V2/delta；Trial 始终只读，只有 Formal Run 可以 Apply。系统先保证 P0 交付，再比较换模、利用率等软目标；原料 Short/Unknown 只提示，不阻塞计划或 Apply。
5. 如有 Acknowledgment Required，确认延期、欠量、提前生产或超产风险；如有 Hard Blocked，在 Resolution Center 修复、受控 Override 或批准排除。
6. 选择方案并 Apply；随后在 Work Order Proposal 和 Shift Proposal 中分组复核并下发。
7. 执行期间使用 Shift Replan Center 生成差异建议。系统不会自动移动已开工或冻结任务。
8. 使用 Customer Schedule Progress 和 Gantt 追踪计划与实际。

## 3. Customer Schedule Progress V2

### 3.1 默认投影

- 未选择 APS Run：显示“当前有效跨 Run 投影”。每个 Demand Identity 使用它唯一的当前 Formal owner，不会按状态或修改时间随便挑一个旧 Run。
- 明确选择 APS Run：显示醒目的“单 Run 视图”。它只用于审计该 Run，不代表当前整体执行事实。
- 开关关闭时：页面保持原 Legacy 视图，V2 的模式选择和工具栏隐藏。

### 3.2 明细字段

| 字段 | 含义 |
|---|---|
| Schedule | 当前有效客户排期数量 |
| Original Plan | Solver/基线首次确认的生产计划 |
| Current Plan | 经过批准变更后的当前计划 |
| Forecast | 根据最新执行事实预计的完成时间/数量 |
| Actual Good / Scrap | 实际良品和废品，来自精确 Result/Segment 执行事实 |
| Delivery Plan | 已关联 Demand Identity 的出货计划数量 |
| Delivered | APS Delivery Allocation 中有效的 Delivery Note 数量 |
| Stock Covered | 当前仍有效且未消费/释放的成品库存分配 |
| Shortage / Recovery | 未排数量、恢复数量和预计恢复完成时间 |

Original、Current、Forecast 是比较层，不能相加成可供应数量。Delivery Note 只有形成精确 Allocation 才计入 V2 Delivered；旧排期字段中尚未分配的交货量会单独标识，不伪装成精确事实。

### 3.3 状态颜色

| 状态 | 颜色 | 含义 |
|---|---|---|
| Delivered / On Track | 绿 | 已交付，或 Forecast 可在有效交期前覆盖 |
| Stock Covered | 蓝 | 已交货加有效成品库存已覆盖需求 |
| At Risk | 黄 | 临近交期，或存在欠量但已有 Recovery 预测 |
| Late / Uncovered | 红 | Forecast 晚于交期，或当前投影仍未覆盖 |
| Unknown | 灰 | Identity/owner/守恒/Forecast 不足，不能安全判断 |

Unknown 不是“没有风险”，必须打开原因和来源单据处理。

### 3.4 日期矩阵和下钻

- “Detail”显示逐需求汇总；“Date Matrix”按日期显示 Schedule、Original、Current、Forecast、Good、Scrap、DP、DN、Stock、Shortage、Recovery。
- 每次最多加载 200 行；日期窗口最多显示 31 列、查询范围最多 366 天。使用 Previous/Next Rows 和 Earlier/Later Dates 翻页。
- 禁用按钮会显示原因，例如已在第一页、没有更多结果或没有更晚日期。
- 点击非空日期单元格可查看该日的精确来源；无权限的单据不会返回链接。
- Export Excel 只导出当前筛选页和当前可见日期窗口，避免误把未加载数据当成完整全集。

## 4. Gantt V2

- Original：虚线基线；Current：主计划实色条；Forecast：斜纹预测；Actual：绿色实际层/进度。
- 机器视图中一个 Production Campaign 只显示一个资源条，避免联产品重复占用机器或模具。
- Campaign 条显示 `C×输出数`，点击或右键“Campaign Outputs”可展开每个输出的每周期数量、计划量、需求覆盖、Excess、良品/废品、Result 和 Work Order。
- 派生联产品不能单独拖动；必须调整 Campaign 的 capacity owner。Mold/Risk 等审计视图可以保留各输出明细。
- BOM dependency、Forecast 风险、冻结、停机和受限区继续显示；服务端仍会在 Apply 前独立验证资源不重叠与 BOM precedence。

## 5. 常见问题

- “分析完成但没有确认按钮”：先看 Run Console 的当前步骤、disabled reason 和 Resolution Center；产能不足通常是 Acknowledgment Required，不应是 Hard Blocked。
- “Trial 为什么不能应用”：Trial 专用于 Legacy/V2 只读对照。Apply 按钮会保留显示但处于禁用状态并说明原因；请在业务批准后创建或批准 Formal Run。
- “原料不足还能排吗”：可以。原料仅为 Ready/Short/Unknown 信息提示，不改变数量、求解可行性、指纹或 Apply。
- “为什么 Progress 选择两个 Run”：默认不是选两个 Run，而是每条需求读取自己的当前 Formal owner；这是跨 Run 连续计划的正确视图。
- “为什么 Delivered 与旧页面不一致”：V2 只采用可追踪的 Delivery Allocation；打开来源检查未匹配 Delivery Note 或 Unallocated Delivery。
- “关闭 V2 会删除数据吗”：不会。关闭开关只停止新 V2 Formal 路径；已下发的 WO/WOS 和历史血缘保留只读。
