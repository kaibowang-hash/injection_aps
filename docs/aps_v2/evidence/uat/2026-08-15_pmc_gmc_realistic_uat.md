# APS V2 PMC/GMC 真实业务 UAT（2026-08-15）

> 历史发现记录：本文保留首次 UAT 的原始失败证据。所列 P0/P1 已在后续修复，并由全新数据完成复验；当前结论请以 [修复复验报告](./2026-08-15_pmc_gmc_realistic_uat_retest.md) 为准。

## 1. 结论

**当前版本不满足生产灰度条件。**

本轮不是 mock 测试，而是在隔离站点 `aps-opt-fixture.localhost` 创建真实 Frappe 业务单据，以独立 PMC、GMC 账号执行 Trial、Formal、分析、风险确认、Apply、一致性复核、Run 审批、工单建议、Gantt、Progress 和原料提示。

核心数据守恒和资源不重叠通过，但发现 3 个发布阻断及 3 个重要工作流/UI 问题：

1. 注塑周期小于 1 分钟时被向上取整为 1 分钟，并且同一连续任务跨 Bucket 被重复收取 setup，导致产能、准时量和延期量显著失真。
2. V2 Apply 没有生成后续审批门禁要求的 Applied Plan/Resource fingerprint，Formal Run 无法审批，工单建议链路中断。
3. Apply 前没有可审阅的机台、模具、开始/结束时间排程明细，GMC 只能看到汇总量，却被要求先确认并 Apply。
4. PMC/GMC 缺少 APS Demand Identity 读取权限，Progress 原始结果正确但经 API 权限过滤后静默变成 0 行。
5. 两个非推荐方案因延期目标口径不一致被误判 `Failed`；UI 仍显示可点击的“Select”，点击后才由后端拒绝。
6. 前端按钮角色与后端权限不一致；PMC 会看到风险确认/Apply 类动作，但实际调用被后端拒绝。Run Console 还会把无法通过 Applied fingerprint 门禁的 Confirm Run 显示为 enabled。

因此，Phase 4 和 Phase 8 的既有 `COMPLETE/Verified` 结论已被本轮真实角色 UAT 证据推翻；修复和重新验收前不得启用生产 Formal V2。

## 2. 安全范围

- 唯一写入站点：`aps-opt-fixture.localhost`。
- 隔离 Bench：`/home/ubuntu/frappe-bench/isolated/aps-v2-bench`。
- 数据库仅使用隔离站点配置的本机实例。
- `jce.1` 未作为任何命令目标，未执行 migrate、patch、run-tests、build、restart、clear-cache、scheduler 或写 API。
- UI 登录测试期间曾仅在隔离站点临时允许密码登录，截图结束后已恢复原值。
- UAT 结束后关闭全部 V2 Feature Flag，Solver 恢复 Legacy，并删除临时 PMC/GMC 用户；业务测试单据和 JSON 证据保留。

## 3. 真实场景与数据

| 数据 | 值 |
|---|---|
| Company | Jichen (Thailand) Co., Ltd |
| Customer | APS-V2-FIXTURE-CUSTOMER |
| Plant Floor | APS-V2-FIXTURE-FLOOR |
| Machine | APS-V2-FIXTURE-MACHINE-120T |
| Item | APS-UAT-260815-FG |
| Raw Material | APS-UAT-260815-RAW，库存 0 |
| Mold | APS-UAT-260815-MOLD |
| Mold cycle | 43.2 秒，1 件/模 |
| Customer Schedule | APS-CDS-00217，2026-08-15，1200 件，无 SO |
| Net Requirement | APS-NET-01025，库存 0、在制 WO 0、净需求 1200 |
| Trial | APS-RUN-00042 / APS-RES-00242 |
| Formal | APS-RUN-00043 / APS-RES-00243 |
| Due time | 2026-08-15 20:00 |
| Default setup | 30 分钟 |

一套数据同时覆盖：客户排期无 SO、Trial 只读、Formal 风险确认、产能不足/Recovery、原料 0 advisory、PMC/GMC 权限分工、Apply/审批/工单建议、Gantt 和客户排期 Progress。

## 4. 通过项

| 检查 | 结果 |
|---|---|
| Solver runtime | OR-Tools 9.4.1874，可用且兼容 |
| Trial / Formal 分析 | 两个 Job 均返回 Optimal；每个生成 3 个方案 |
| Trial 写保护 | PMC/GMC 均无法 Apply Trial；后端明确提示 Trial read-only |
| PMC 正式权限边界 | PMC Apply 和风险确认均被后端拒绝 |
| GMC 风险确认 | 成功记录理由和 solution fingerprint |
| Formal Apply | 成功写入 1 个 Result、3 个 Segment |
| 数量守恒 | 1200 = on-time 690 + late 510 + unscheduled 0 |
| 资源守恒 | 3 个 Segment 同机无重叠 |
| Apply 后一致性 | `valid=true`，planned/scheduled/covered 均为 1200 |
| 原料策略 | 0 库存显示 `Short`；原料字段从 Solver 输入移除；排产量仍为 1200 |
| Gantt API | PMC 可读取 3 个任务，数量汇总为 1200 |
| Progress 核心服务 | 管理员原始 V2 projection 返回 1 行、schedule/current/forecast 1200、recovery 510 |
| 页面路由 | PMC/GMC 登录后 5 个 APS 路由均 HTTP 200，无登录重定向 |
| 前端定制保护 | 6/6 PASS |
| UI runtime guards | 9/9 PASS |

这些通过项只能证明数量记账、资源重叠保护、原料退出硬约束和部分权限门禁有效，不能抵消以下发布阻断。

## 5. P0：周期和 setup 时间模型导致产能失真

### 实际结果

Solver 把 1200 件拆成：

| Task | 占用分钟 | 生产开始 | 结束 | 数量 | setup |
|---|---:|---:|---:|---:|---:|
| 1 | 0—720 | 30 | 720 | 690 | 30 |
| 2 | 720—960 | 750 | 960 | 210 | 30 |
| 3 | 960—1290 | 990 | 1290 | 300 | 30 |

最终显示准时 690、延期 510、setup 90 分钟、最大延期 720 分钟。

### 应有结果

模具周期 43.2 秒等于 0.72 分钟。到 20:00 的 12 小时窗口扣除一次 30 分钟 setup 后：

```text
floor((720 - 30) / 0.72) = 958 件准时
recovery = 1200 - 958 = 242 件
```

同一机器、模具、需求连续跨班次，不应因为 Bucket 边界再次收取基础 setup。若连续运行，剩余 242 件约在 22:54 完成，而不是次日 05:30；即使业务规则要求在班次切换重新 setup，也应显式配置，不能由 Bucket 拆分自动产生。

### 根因

- `input_builder.py` 把 `0.72` 分钟用 `ceil` 转成整数 `1` 分钟。
- `capacity_solver.py` 为每个 `demand × alternative × bucket` 的 used allocation 都加一次 `base_setup_minutes`。
- outcome 使用 Bucket end，而不是第二层 sequencing 后的真实 task end，进一步放大 recovery completion 和 weighted tardiness。

这违反“Campaign duration = cycles × mold cycle + setup/changeover”的计算规格，并会系统性低估常见的秒级注塑产能。

### 修复验收

1. 时间使用秒级整数或固定 time scale；43.2 秒不得变成 60 秒。
2. 基础 setup 按连续 campaign/sequence 收取，不按 Bucket allocation 重复收取。
3. on-time、late、recovery completion 和 weighted tardiness 使用最终 sequenced task 时间重算。
4. 增加 20s、43.2s、59.9s、60s、61s 周期的 property/integration tests。
5. 对本场景，按明确的班次 setup 规则得到可手算、可复现的准时量和恢复时间。

## 6. P0：V2 Apply 后无法审批和生成工单建议

### 复现

1. PMC 分析 Formal Run。
2. GMC 确认延期风险。
3. GMC Apply V2：成功，状态 `Applied`，创建 3 个 Segment。
4. 一致性复核：`valid=true`。
5. GMC Confirm Run：失败：`Applied capacity evidence predates the current plan-state guard. Analyze and Apply again.`
6. Work Order proposal：因 Run 未 Approved 而失败。

### 根因

V2 `apply_v2_schedule` 只更新 solver/capacity 状态和 solution fingerprint，没有像正式 Capacity Apply 一样写入：

- `applied_plan_fingerprint`
- `applied_resource_fingerprint`

而 `approve_planning_run` 必须调用 `assert_applied_capacity_current`；该门禁明确要求上述 fingerprint。当前 V2 Analyze → Acknowledge → Apply 路径无论重跑多少次都无法生成它们。

### 影响

链路在正式审批处硬中断：

```text
客户排期 → V2 Solver → Apply → [无法 Confirm Run]
                              → 无 WO Proposal
                              → 无正式 WO/WOS
                              → 无法继续真实入库/出货闭环 UAT
```

### 修复验收

1. V2 Apply 在同一事务和固定锁顺序内，Apply 后基于落地 Result/Segment 生成两类 fingerprint。
2. Analyze/Apply 未发生外部变化时，GMC 可审批 Run。
3. 数量、日期、机台、模具、血缘、库存/资源发生变化时审批仍 fail closed。
4. 重试 Apply 幂等；不能重复 Segment。
5. 完成无 SO Work Order Proposal → WO → WOS 的真实链路测试。

## 7. P0/P1：Apply 前没有完整方案可审阅

Planning Run 的 V2 analysis JSON 只包含汇总 on-time/late/unscheduled，需求行 `allocations=[]`，没有机台、模具、开始/结束时间。`get_solver_scenarios` 又只返回方案摘要，不返回 tasks。正式 Segment/Gantt task 只有 Apply 后才生成。

因此当前用户路径是：

```text
Analyze 完成
→ 只能看 690/510 等汇总
→ 看不到将在哪台机、使用哪个模具、何时开始/结束、为何拆成 3 段
→ GMC 被要求先确认风险并 Apply
```

这正是“分析完成但看不到建议”的代码级原因。对于正式排程，Apply 前无法审阅具体变更属于发布安全缺口。

建议增加只读 Proposed Gantt/Task table，以 Solver Job + solution fingerprint 为来源，至少显示：Demand/Result、machine、mold、occupied/production start、end、qty/cycles、setup/changeover、horizon zone、late/unscheduled、assignment reason，并明确 `Proposed / Not Applied`。Trial 和 Formal 都可预览；只有 Formal + GMC 可 Apply。

## 8. P1：PMC/GMC 的 Progress 页面静默空白

### 证据

- 管理员直接调用 `progress_v2.get_progress_detail`：1 行，1200 件，Late，Recovery 510。
- PMC/GMC 调用公开 API：均为 0 行。
- Company、Customer、Schedule、Schedule Item、Item、Commitment、Result 权限均通过。
- 两个角色对 `APS Demand Identity` 均无 read 权限。

`APS Demand Identity` DocType 只给 System Manager、Manufacturing Manager、Sales User、Stock Manager；没有 PMC/GMC。公开 API 的 sanitizer 要求 Identity 可读，否则直接 `continue` 删除整行，页面只显示空表，没有权限原因。

### 修复验收

1. 按权限规格给 PMC/GMC 必要的 Identity read/report 权限，或在 scoped access 层实现等价安全授权。
2. 若行被权限过滤，响应必须返回 `permission_filtered`、原始/返回行数和可理解的空态原因。
3. PMC/GMC 本场景 Progress 均返回 1 行；无跨 Company/Customer 数据泄漏。

## 9. P1：三方案比较和 Validator 口径不一致

本场景中 Recommended 合法；Delivery Priority 和 Minimum Changeover 的可见指标完全相同，却都被标为 `Failed`，warning 为 “A lower objective increased P0 weighted tardiness.”

根因是推荐方案的 CP objective 按“每个 late bucket 的数量 × 各自 bucket delay”累计，而 Validator/显示指标按“全部 late qty × 最终 recovery completion delay”计算。方案验证把两种不同口径直接比较，晚量跨多个 Bucket 时会把后续方案误判为 regression。

UI 还忽略 `valid=false`：Failed 行仍显示可点击 Select。实际点击、填写理由后，后端才返回 `Select a validated solver scenario.`

修复要求：

1. Solver objective、Validator 和 UI 指标使用同一个正式 weighted tardiness 定义。
2. 三方案先固定同口径的前三层交付最优值。
3. `valid=false` 行不可选，显示 warning 和修复/重算原因。
4. 页面展示 explanation/warnings，而不只显示 `CP-SAT / Failed`。

## 10. P1：按钮和角色工作流不一致

### Planning Run

共享前端 role map 允许 PMC 执行 `confirm_capacity_balance` 和 `apply_capacity_balance`，但 V2 API 分别要求 approval/release 权限。真实测试中 PMC 点击对应动作必然被拒绝。

Run Console 当前把 GMC 的 Confirm Run 显示 enabled，因为它只检查 `consistency_status=Valid` 和 Run 状态，没有检查 Applied plan/resource fingerprint；点击后才失败。对于 PMC，Confirm/WO/Shift actions 会禁用并给出角色原因，这部分表现正确。

### Shift Replan Center

页面无角色显隐判断，对所有可进入页面的用户都渲染 Acknowledge、Generate、Approve、Apply。后端 Approve/Apply 只允许 GMC。PMC 应能创建/比较/送审，但不应看到可执行的审批/应用按钮，或至少必须 disabled 并显示“仅 GMC”。

### 建议

- 后端返回每个动作的 `allowed/enabled/disabled_reason/required_roles`，前端不再维护一套容易漂移的静态 role map。
- Confirm Run enabled 条件必须包含完整 `assert_applied_capacity_current` 的只读 preflight 结果。
- 所有按钮满足 UI 契约：“不可用时不无解释消失；禁用时显示原因和下一步入口”。

## 11. 体验评估

| 角色任务 | 评价 | 说明 |
|---|---|---|
| PMC 创建/分析 Trial | 基本可用 | 分析成功，Trial 后端只读保护正确 |
| PMC 比较方案 | 不可用 | 只有汇总，无 task preview；Failed 方案仍显示可选 |
| PMC 发起 Formal 分析 | 可运行但易困惑 | “Analyze Capacity”完成后没有完整建议；按钮分散在 Capacity/APS V2 分组 |
| PMC 查看 Progress | 不可用 | 静默 0 行 |
| GMC 确认风险 | API 可用 | 有理由和 fingerprint 审计；但确认前缺少完整排程明细 |
| GMC Apply | 可执行 | Segment 正确写入且守恒；但时间计算有显著误差 |
| GMC Confirm Run | 阻断 | 按钮显示 enabled，后端固定失败 |
| WO/WOS 建议 | 阻断 | Run 无法 Approved |
| Gantt | Apply 后可读 | 3 个任务和 1200 汇总可读；Apply 前无 Proposed Gantt |
| Shift Replan | 未能完整 E2E | 上游 WO/WOS 被审批缺陷阻断；源码另有角色按钮漂移 |

文案方面，Run Console 仍以 `Recalc Console` 为主标题，流程 banner 为 `Recalculate → Confirm Run → WO Proposal...`，没有把 V2 的 Analyze → Compare → Acknowledge → Apply 明确放进主路径，容易重复触发用户当前的困惑。

## 12. 浏览器覆盖限制

服务器没有 Chromium、Firefox、Playwright 或 Selenium。真实 PMC/GMC HTTP 登录和 5 个 Desk 路由加载均成功（200、无登录重定向），但现有 `wkhtmltoimage 0.12.6` 的旧 Qt WebKit 无法执行当前 Frappe 前端，返回 `UnknownContentError` 并生成空白图。

因此本轮 UI 判断基于：真实角色 API、持久化状态、页面源码/状态机、Node runtime guards 和 HTTP 路由；**没有声称完成现代浏览器像素级/交互级验证**。修复后必须在隔离环境提供 Chromium/Playwright，完成截图、console error、按钮点击、键盘/ARIA 和 1440/1024 分辨率测试。

## 13. 未完成的下游验收

由于 V2 Apply 后无法审批，本轮不能合法继续：

- Work Order Proposal 审核和 Apply；
- 无 SO Work Order 创建；
- Work Order Scheduling / Shift Proposal；
- 工单入库和实际良品/废品回写；
- Delivery Plan 多 SO 分配；
- Delivery Note 下推和 Demand Identity 履约回写；
- 两个完整滚动周期 PMC/GMC 对照。

这些不能引用旧单元测试替代真实 E2E。修复 P0 后必须从客户排期重新跑到 DN。

## 14. 建议修复顺序

1. **时间/周期模型**：秒级 time scale、setup 连续性、sequenced completion 指标。
2. **V2 Apply/Approval 桥接**：生成正式 applied plan/resource evidence。
3. **Apply 前预览**：Proposed Task/Gantt + 风险明细。
4. **Demand Identity 权限和 Progress 空态**。
5. **方案目标/Validator 统一口径及 Failed 行 UI**。
6. **动作权限单一事实来源**：Run、Planning Form、Shift Replan 全部从后端 capability/action context 驱动。
7. 使用现代浏览器完成 PMC/GMC E2E，再继续 WO/WOS/Stock/DP/DN 两个滚动周期验收。

修复过程中不得改变原料 advisory-only、WO 无 SO、现有生产/出货顺序、Feature Flag 回退或既有前端定制保护。

## 15. 证据位置

隔离运行证据：

```text
/home/ubuntu/frappe-bench/isolated/aps-v2-bench/evidence/aps_v2_uat_20260815/
  prepared.json
  workflow_result.json
  diagnostics.json
  ui_workflow_context.json
  finished.json
  ui/http_ui_report.json
```

无头 WebKit 的空白 PNG 仅用于证明该 renderer 不兼容，不作为产品截图证据。
