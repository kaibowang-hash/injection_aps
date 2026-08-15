# APS V2 PMC/GMC 真实业务 UAT 修复复验（2026-08-15）

## 1. 结论

本次针对首次真实 UAT 发现的 Solver、Apply 证据、权限和 UI 工作流问题完成修复，并在隔离站点 `aps-opt-fixture.localhost` 使用全新业务数据从头复验。

**本轮覆盖范围内的 P0/P1 均已关闭，Phase 4 与 Phase 8 可恢复为 `COMPLETE`（工程实现与隔离验证完成；生产仍未启用）。**

最终真实流程完成：

```text
客户排期/Identity/Net Requirement
→ PMC Trial + Formal 分析
→ 三方案和 Proposed Tasks 审阅
→ GMC 风险确认与 Formal Apply
→ Applied Plan/Resource 证据
→ GMC 审批 Run
→ Work Order Proposal 生成
→ PMC/GMC Progress + Gantt 复核
```

后续剩余测试已经补齐：在同一隔离站点完成真实正式 WO、WOS、制造入库、Delivery Plan 分配和 Delivery Note 下推全链路，并在 Chromium 140 / Playwright 1.55 中分别以 PMC、GMC 身份完成页面与权限验收。全链路业务单据测试采用 rollback-only 事务，提交逻辑真实执行，但最终不在隔离数据库遗留正式业务单据。

## 2. 安全范围和最终状态

- 唯一写入站点：`aps-opt-fixture.localhost`。
- 隔离 Bench：`/home/ubuntu/frappe-bench/isolated/aps-v2-bench`。
- `jce.1` 没有作为任何命令目标；未对其执行 migrate、patch、run-tests、build、restart、clear-cache 或业务写入。
- 最终 V2 状态已恢复：`enable_aps_v2=0`、`solver_engine=Legacy`、Shift/Campaign/BOM 三个组件开关均为 0。
- 临时用户 `aps-uat-pmc@example.com`、`aps-uat-gmc@example.com` 已删除。
- 原始复验 JSON 保存在隔离 Bench：`evidence/aps_v2_uat_20260815/`。

## 3. 最终真实数据

| 数据 | 值 |
|---|---|
| Company | Jichen (Thailand) Co., Ltd |
| Customer | APS-V2-FIXTURE-CUSTOMER |
| Machine | APS-V2-FIXTURE-MACHINE-120T |
| Mold | APS-UAT-260815-MOLD |
| Cycle | 43.2 秒/件 |
| Raw Material | APS-UAT-260815-RAW，库存 0 |
| Customer Schedule | APS-CDS-00221，1200 件 |
| Demand Identity | 8on5s4vacd |
| Net Requirement | APS-NET-01029，净需求 1200 |
| Trial Run | APS-RUN-00050 |
| Formal Run | APS-RUN-00051 |
| Work Order Proposal | APS-WOP-00004，1 行 |
| Due | 2026-08-15 20:00 |

## 4. 首轮问题与修复结果

| 首轮问题 | 修复 | 复验结果 |
|---|---|---|
| 43.2 秒被取整成 1 分钟 | Solver 使用固定精度微分钟，按真实 fractional cycle 计算 | 958 件准时，242 件延期，符合手算 |
| 跨 Bucket 重复收基础 setup | 每个 Demand + Alternative 连续 Campaign 只在最早使用 Bucket 收一次 setup | 两段任务 setup 合计 30 分钟 |
| outcome 使用 Bucket end | 指标、Validator 和展示统一使用最终 sequenced task end | Recovery 在 22:55 完成，最大延期 175 分钟 |
| 非推荐方案被误判 Failed | Validator 的 P0 floor 与显示/最终任务口径统一 | Recommended、Delivery Priority、Minimum Changeover 全部 Optimal/valid |
| Apply 前看不到建议 | Scenario API 和 Planning Run 分析返回只读 task preview；页面显示机台、模具、数量、周期和起止时间 | 3 个方案均可在 Apply 前看到 2 条任务 |
| V2 Apply 缺少正式证据 | V2 Apply 统一生成 consistency、fulfillment、plan/resource fingerprints | 两个 Applied fingerprint 均存在 |
| Run 审批后指纹误过期 | 审批事务在完成受控 Segment 锁定后重新绑定证据；外部变化仍 fail closed | GMC 审批成功，随后工单建议成功 |
| Applied 后仍可换方案 | API、页面共同锁定 scenario selection | 页面返回 locked，后端直接调用 ValidationError |
| PMC/GMC Progress 空白 | Demand Identity 增加 PMC/GMC 只读权限 | 两个角色均返回 1 行、Recovery 242 |
| 前端角色与后端不一致 | 共享 action role map、Planning Run、Shift Replan 页面统一角色边界 | PMC 不能 Ack/Apply；GMC 可审批和 Apply |
| 禁用按钮没有原因 | Confirm/WO/Shift/Recalculate 返回明确 disabled reason | GMC 可看到已审批、已生成、下一前置步骤 |
| 已审批 Run 可原地重算 | V2 页面禁用并由后端硬拒绝，要求新 Run 或受控变更流程 | UI disabled；直接 API 调用被拒绝 |

## 5. 计算准确性证据

场景：1200 件、43.2 秒/件、08:00—20:00 可用窗口、首次 setup 30 分钟。

```text
floor((720 - 30) / 0.72) = 958 件准时
1200 - 958 = 242 件恢复量
242 × 0.72 = 174.24 分钟，分钟边界结束为 22:55
```

系统最终结果：

| 指标 | 结果 |
|---|---:|
| Planned / Scheduled | 1200 / 1200 |
| On-time | 958 |
| Late / Recovery | 242 |
| Unscheduled | 0 |
| Setup | 30 分钟，只计一次 |
| Max lateness | 175 分钟 |
| Segment overlap | 0 |
| Quantity conservation | PASS |

正式任务为：

1. 08:00 occupied、08:30 production、20:00 end，958 件，setup 30。
2. 20:00 production、22:55 end，242 件，setup 0。

## 6. PMC/GMC 工作流和体验结果

- PMC：可做 Trial/Formal 分析、查看三方案任务、Gantt 和 Progress；不能确认风险、Apply 或审批。
- GMC：可确认明确的 242 件延期风险、Apply、审批 Run、生成工单建议。
- Apply 前 Confirm Run 明确禁用并提示先 Apply analyzed capacity plan。
- Apply 后方案选择被锁定，防止展示方案与已落地计划漂移。
- 工单建议生成后：Recalculate、Confirm、WO Proposal、Shift Proposal 均保持可见但禁用，并分别说明新 Run/受控变更、已经审批、建议已生成、需先审核并 Apply 工单建议。
- Scenario 页面展示真实 Proposed Tasks；不会要求 GMC 在看不到机台/模具/时间的情况下盲目 Apply。
- Progress 中 PMC/GMC 均看到 1 行：schedule/current/forecast 1200，recovery 242，守恒异常 0。
- Gantt 返回 2 个任务，数量汇总 1200，无机台重叠。

## 7. 原料口径

原料库存 0 时仍返回 `Short` advisory；Solver 输入中的 `material_ready_qty` 和 `material_requirements` 被移除，计划量仍为 1200。原料没有重新成为 APS 可排数量或 Apply 的硬约束。

## 8. 自动测试

最终串行回归结果：

| 模块 | 结果 |
|---|---:|
| Phase 4 Solver | 14/14 PASS |
| Phase 4 Contracts / Integration | 7/7 + 3/3 PASS |
| Phase 5 Contracts / Shift / Integration | 7/7 + 7/7 + 3/3 PASS |
| Phase 6 Contracts / Co-product / Integration | 4/4 + 7/7 + 4/4 PASS |
| Phase 7 Contracts / BOM / Integration | 3/3 + 8/8 + 3/3 PASS |
| Phase 8 Contracts / Progress / Integration | 9/9 + 8/8 + 5/5 PASS |
| Permission / Workflow Guards | 53/53 PASS |
| Capacity Balance | 95/95 PASS |
| Frontend Customization Safety | 6/6 PASS |
| UI Runtime Guards | 9/9 PASS |
| 全应用最终回归 | 759/759 PASS，94.910 秒 |
| **定向模块合计（原复验口径）** | **255/255 PASS** |

真实 UAT 的 19 个布尔不变量全部为 `true`，`observations=[]`。测试覆盖数量守恒、fractional cycle、setup 一次、三方案有效、预览可见、Apply 证据、GMC 审批、WO Proposal、角色边界、方案锁定、已审批重算门禁、Progress、Gantt/NoOverlap、Trial 只读和原料 advisory-only。

## 9. 真实下游单据链路

隔离站点 rollback-only UAT 完整执行：

```text
APS-RUN-00052
→ WO-26-01754
→ APS-WOP-00005
→ APS-SSP-00001
→ APS-REL-00001
→ 2 条排产明细
→ 1 条 WOS
→ 2 张制造 Stock Entry
→ Delivery Plan 分配
→ 2 张 Delivery Note
```

结果：生产 120、交付 120，数量审计 `valid=true`、差异 0；生产和交付重复同步均为 0。12 个 PMC 日常变化场景全部通过，包括取消、增量、已开工减量阻断、分机、插单位移、JIT、白夜班、停机、重复导入、重复同步、DN 取消/退货和事务回滚。Delivery Plan → Delivery Note 独立集成测试 8/8 通过。

## 10. 现代浏览器 PMC/GMC 验收

- 浏览器：Chromium 140 / Playwright 1.55。
- PMC 无风险确认、Apply、Run 审批权限；GMC 可确认风险、Apply 并审批。
- Run Console、Planning Run 表单、三方案比较、Gantt、Release Center、Customer Progress 六类页面均可完成工作流。
- 页面均无全局横向溢出；最终 Run 为 `Approved + Applied + Valid`，1200 件、2 个任务。
- 页面截图和原始结果保存在隔离证据目录 `evidence/aps_v2_uat_20260815/browser/`。

隔离副本存在两个非 APS 产品问题：克隆未带入 `/files/Core.png`、`/files/Picture1.svg`、`/files/getsitelogo.svg`；`jce_custom/js/scanner_global.js` 在 Desk 报 `frappe.ready is not a function`。它们没有阻断 APS 页面及工作流，且本项目没有删除、覆盖或重置这些既有 customization。

## 11. 并发、锁与幂等

两个独立 Bench 进程同时对同一 Formal Run Apply：

- 首个请求正常 Apply，生成 2 条 Segment。
- 后到请求等待 Run 锁后识别同一已应用解，返回 `idempotent_replay=1`，新增 0 条。
- 最终数据库只有 2 条有效 Segment、2 个唯一 task key，数量 1200，无机台/模具重叠。
- Run、Solver Job、capacity balance、consistency 最终分别为 Applied / Applied / Applied / Valid。

为固定该行为，Apply 现在在锁内重新读取 input/selection fingerprint；发现完整相同解则幂等返回，发现半应用证据则 fail closed。

## 12. 规模、超时与 Fallback

压力输入：200 条需求、20 台机、560 个 12 小时产能桶、400 个替代资源、14 天窗口，配置总求解预算 3 秒。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 实际墙钟时间 | 23.14 秒 | 5.383 秒 |
| 峰值 RSS | 576,520 KiB | 262,132 KiB |
| 三方案有效 | 3/3 | 3/3 |
| 计划量 / 未计划量 | 101,760 / 0 | 101,760 / 0 |

原因为三套方案分别重建模型，且容量/顺序模型在建模耗尽预算后又重置了求解时间。修复后使用整轮全局 deadline，并把建模耗时计入剩余预算；时间耗尽时后续方案不再重复建模，而是生成明确标记、需 GMC acknowledgment 的有效 Fallback。强制模拟 Solver 不可用时，三方案 0.140 秒完成，均为有效且明确标记的 Fallback。班次重排的超时 Fallback 也会继续遵守机台/模具顺序和停机区间。

10,000 行 Progress 聚合/分页性能测试 8/8 通过，整个模块耗时 0.448 秒。

## 13. 最终门禁与限制

- 全应用：759/759 PASS，94.910 秒。
- UI runtime guard：9/9 PASS。
- `git diff --check`：PASS。
- 隔离测试结束后已删除 PMC/GMC 临时用户，V2/Shift/Campaign/BOM 开关全部关闭，Solver 模式恢复 Legacy。
- 临时浏览器服务已关闭。
- `jce.1` 未作为 migrate、patch、test、execute、build、restart、cache 或业务写入命令目标。

本轮按用户要求没有执行第 5 项“生产迁移与回滚演练”。因此当前结论是：代码质量与第 1—4 项技术门禁支持进入受控生产灰度，但在真正迁移/启用生产前，仍应把第 5 项作为发布门禁，并由 GMC/Manufacturing Manager 完成业务签字。
