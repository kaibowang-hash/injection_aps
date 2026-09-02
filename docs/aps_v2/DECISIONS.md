# APS V2 已确认设计决策

状态含义：`Accepted` 为当前约束；`Superseded` 必须指向新决策；`Proposed` 不得直接实施为正式行为。

| ID | 状态 | 决策 | 理由/边界 |
|---|---|---|---|
| ADR-001 | Accepted | 排期导入类型由系统推荐、用户确认 | 文件内容无法可靠区分部分修订和增量需求；自动接口必须显式提供或使用已配置来源契约 |
| ADR-002 | Accepted | Work Order 不强制关联 SO | 工单关联 APS Commitment/Campaign；SO 由 Delivery Plan 晚绑定 |
| ADR-003 | Accepted | 复用现有 Delivery Plan 的 FIFO SO 分配 | 不重复建设 SO 分配台账；APS 增加需求血缘字段 |
| ADR-004 | Accepted | 粗略交货匹配只作后备且不阻塞出货 | 新数据使用 Demand Identity/DP/DN 明确链接；歧义进入待匹配 |
| ADR-005 | Accepted | P0/P1/P2 是需求准入，不是机器优先级 | 吨位、连续性、换模、利用率属于交付之后的分配目标 |
| ADR-006 | Accepted | 利用率平衡为软目标 | 强制平均可能增加换模、能耗并损害交付 |
| ADR-007 | Accepted | 不提供通用 Force Apply All | 允许临时 Override、修复或排除单条需求后部分应用 |
| ADR-008 | Accepted | 原料退出 APS 硬约束 | 只显示 Ready/Short/Unknown，不影响数量、状态、指纹和 Apply |
| ADR-009 | Accepted | 每班次执行 Shift Replan | 刷新实际后生成差异 Proposal；不自动改变执行中和冻结任务 |
| ADR-010 | Accepted | WO 与 WOS 分层更新 | WO 只在需求/数量变化时处理；WOS 每班次可重新建议 |
| ADR-011 | Accepted | 联产品自动创建输出工单 | 可以无 SO，但必须关联同一 Production Campaign |
| ADR-012 | Accepted | Gantt 机器视图显示一个 Campaign 条 | 多输出展开查看，避免视觉和计算上重复占用机器 |
| ADR-013 | Accepted | 多层 BOM 只展开需生产的半成品 | Raw Material 不进入 APS 硬约束；半成品建立 C→A→X 前置关系 |
| ADR-014 | Accepted | 使用 CP-SAT 分层目标求解 | 先完成需求身份/所有权，再引入全局有限产能求解 |
| ADR-015 | Accepted | 需求、冻结和恢复视窗分离 | Recovery 只安排视窗内欠量，并给出最早补齐时间 |
| ADR-016 | Accepted | 生产开发零触碰，已有前端定制只保留不覆盖 | `jce.1` 开发期间只读；迁移钩子不得刷新同名前端资源或重排用户布局；新 UI 默认关闭并经隔离验证后再发布 |
| ADR-017 | Accepted | Frappe 内嵌 Solver 暂锁 `ortools==9.4.1874` | 当前 `google-api-core` 要求 protobuf `<4`，OR-Tools 9.5+ 会升级到 protobuf 4—7；9.4 已在 Python 3.10/Frappe 隔离副本通过 CP-SAT。更高版本必须运行在依赖隔离进程，不得升级 Frappe 环境 protobuf |
| ADR-018 | Accepted | Phase 0 Fixture 保存输入事实和预期不变量，不伪造未来输出 | Demand Identity、Commitment、Campaign、Solver 和正式 Delivery 血缘必须由对应 Phase 的真实实现产生；提前把期望结果写进 DB 会掩盖实现缺失 |

## 新 ADR 模板

```markdown
### ADR-XXX：标题

- 状态：Proposed
- 日期：YYYY-MM-DD
- 触发问题：
- 不可违反的全局契约：
- 方案 A：
- 方案 B：
- 推荐：
- 数据/迁移/API/UI/测试影响：
- 用户决策：Pending
```
