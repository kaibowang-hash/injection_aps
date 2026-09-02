# APS V2 UI、API、权限与审计规格

## 1. UX 总原则

- 结果、原因、影响、动作按顺序呈现。
- 按钮不得无解释消失；禁用时显示原因和下一步入口。
- P0/P1/P2、Original/Current/Forecast/Actual 使用一致颜色和词汇。
- 重要数量可以下钻到来源单据。
- 自动建议与用户确认明确分开。

## 2. 页面

### 2.1 Schedule Import & Revision

步骤：来源 → 客户/Scope → 映射 → 模式确认 → 差异 → 导入。

显示推荐模式和理由，以及新增/修改/不变/保留/取消数量。Full Replacement 的取消项红色警告；Incremental 的重合相加橙色警告。导入前必须确认模式。

### 2.2 Demand Admission Workbench

卡片：P0 必排、准时覆盖、预计欠量、剩余机时、P1/P2 推荐、换模、利用率。

P0 锁定；P1/P2 表格显示推荐数量、连续生产、减少换模、库存天数和利用率收益。保存选择后重新求解。

### 2.3 Run Console

时间轴显示 overdue、Demand、Freeze、Restricted、Recovery。阶段条显示需求→准入→BOM/Campaign→求解→异常→Apply→WO→Shift Release。

不能 Apply 时固定显示 blocker 数量、原因和 Resolution Center 链接。

### 2.4 Constraint Resolution Center

分组：必须修复、可临时 Override、可排除后部分释放。显示影响客户/数量、建议、负责人和状态。

动作：打开主数据、运行级参数、批准替代资源、排除本次 Release、重新分析。无 Force Apply All。

### 2.5 Scenario Comparison

至少显示推荐、交付优先、换模最少：准时率、延期量/最大延期、critical unplanned、换模次数/分钟、利用率、移动量、P1/P2 完成量。

选择非推荐方案必须填写理由。

### 2.6 Gantt V2

图层：Original、Current、Forecast、Actual、Freeze/Restricted、Downtime、Changeover、Risk。

原计划虚线、当前实色、Forecast 斜纹、Actual 进度、延期红框。Campaign 在机器视图只显示一个条，内部列出多输出；展开看 WO/需求/实际。派生联产品不可单独拖动。

### 2.7 Work Order Proposal

新增 Campaign、Output Role、Commitment、SO=`Not Required`、Source Reason、Excess。按 Campaign 折叠。整组输出工单原子 Apply。

### 2.8 Shift Replan Center

三栏：当前执行、下一班建议、后续交付影响。显示数据新鲜度、基线差异和操作：刷新、分析、比较、送审、应用、Emergency。

### 2.9 Customer Schedule Progress V2

明细和日期矩阵两种模式。每个物料显示客户排期、Original Plan、Current Plan、Forecast、Actual Good、Delivery Plan、Actual Delivered、Shortage/Recovery。

颜色：绿满足、黄风险可补、红延期/未排、蓝提前/库存、灰未知。单元格下钻到 Identity、Commitment、Segment、Campaign、WO、WOS、Stock Entry、DP、DN。

## 3. API

### Revision

- `recommend_schedule_revision_mode`
- `preview_schedule_revision`
- `apply_schedule_revision`
- `resolve_schedule_identity_ambiguity`

### Demand/Admission

- `prepare_run_demand_baseline`
- `get_demand_admission_candidates`
- `save_demand_admission_decisions`
- `preview_admission_impact`

### Solver/Apply

- `analyze_v2_schedule`
- `get_solver_scenarios`
- `select_solver_scenario`
- `acknowledge_schedule_risks`
- `apply_v2_schedule`

### Resolution

- `get_constraint_resolutions`
- `request_temporary_override`
- `approve_temporary_override`
- `exclude_commitment_from_release`
- `recompute_after_resolution`

### Shift

- `create_shift_replan_cycle`
- `refresh_shift_actuals`
- `analyze_shift_replan`
- `get_shift_replan_diff`
- `generate_shift_replan_proposals`
- `apply_shift_replan_proposal`

### Progress

- `get_customer_schedule_progress_v2`
- `get_progress_matrix`
- `get_progress_cell_drilldown`

## 4. API 共同契约

写 API 必须：

1. 校验动作角色和文档权限。
2. 明确 company/plant floor scope。
3. 获取固定顺序锁。
4. 接受/返回 input fingerprint。
5. 具备幂等键。
6. 返回 machine-readable code、用户说明、next action。
7. 保存 Audit Log。
8. 不以 HTTP 成功掩盖业务失败。

后台 Solver 返回 Job ID，页面轮询 Draft/Running/Feasible/Failed/Applied；失败保存阶段和可重试性。

## 5. 权限

PMC：导入、确认模式、Trial、P1/P2、查看方案、发起 Override/Exclude、生成 Replan 草案。

GMC：审批 Run、确认风险、普通 Override/Exclude、应用 WO/Shift Proposal。

Manufacturing Manager：机器/模具非标准兼容 Override、Emergency Replan、执行冻结。

Manufacturing User：执行反馈和查看。

Sales：查看排期/交付影响，可维护授权范围的客户排期，但不能释放生产。

System Manager：技术维护；没有业务角色时不能替代 GMC 接受客户交付风险。

## 6. Override 权限

- PMC 可申请，不能批准自己的高风险 Override。
- 周期/临时能力：GMC 批准。
- 非标准机器/模具兼容：Manufacturing Manager 批准并要求到期时间。
- Exclude P0：GMC 批准。
- BOM 环、负数量、锁定重叠、并发指纹：不可批准。

## 7. 审计

记录 Revision 推荐/选择、Identity 消歧、P1/P2、Solver 方案、Acknowledgment、Override、Exclude、所有权转移、Shift Diff、Campaign 工单、Delivery Legacy Match。

每条记录：actor、timestamp、reason、scope、before/after、input/output fingerprint、source/target docs。

## 8. 出货兼容

- APS 字段自动填充，不增加正常用户步骤。
- 手工 DP 可自动建议需求链接。
- 没有 APS 链接仍允许 DP 提交和 DN 下推。
- 歧义进入 Unallocated Delivery，仅影响 APS 追踪，不改变 ERPNext 出货。
- 退货必须沿原 Delivery Allocation 反冲。
