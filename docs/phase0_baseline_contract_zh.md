# APS Phase 0 安全基线与验收口径

## 目标与边界

Phase 0 只建立可重复的优化前基线，不修改 APS 生产逻辑，也不写入生产站点。

- 专用站点：`aps-opt-test.localhost`
- 优化分支：`codex/aps-optimization`
- 基线提交：`3a1fd63126cbd5a6f79a00eb212bf501b40c29c9`
- 场景脚本：`injection_aps.tests.phase0_baseline.run_phase0_gate`
- 清理保护：造数与清理函数只允许在上述专用站点运行，其他站点会直接拒绝。

## 当前数量口径

这些定义描述优化前代码实际使用的口径，不代表最终正确口径。Phase 1 需要统一并替换有冲突的定义。

| 指标 | 优化前主要来源 | 当前计算方式 | 已知风险 |
| --- | --- | --- | --- |
| 客户需求 | Active Customer Delivery Schedule Item | `qty`；进入需求池时使用 `max(balance_qty - allocated_qty, 0)` | 页面和重建服务使用的起点不同 |
| 已交货 | Customer Delivery Schedule Item | `delivered_qty` | 尚未证明与 Delivery Note 稳定同步 |
| 剩余交货 | Customer Delivery Schedule Item | `max(qty - delivered_qty, 0)`，通常存入 `balance_qty` | `balance_qty` 是存储值，可能陈旧 |
| 可用库存 | Bin | `max(actual_qty - external_reserved_qty, 0)` | 客户间按日期顺序分配，过滤顺序会影响结果 |
| 净需求 | APS Net Requirement | `net_requirement_qty` | 与 `planning_qty`、现有工单策略存在多套展示口径 |
| 计划数量 | APS Schedule Result | `planned_qty` | 与 APS Planning Run 的 `total_net_requirement_qty` 名称不一致 |
| 排产数量 | APS Schedule Result / APS Schedule Segment | Result `scheduled_qty`；Segment 为 `sum(planned_qty)` | 两处可不一致，计划头也可能继续引用 Result 数量 |
| 欠产 | APS Schedule Result | `max(planned_qty - scheduled_qty, 0)` 存入 `unscheduled_qty` | 依赖可能失真的 Result `scheduled_qty` |
| 超产 | 无统一字段 | 基线按 `max(scheduled_qty - planned_qty, 0)` 计算 | 页面与接口尚无统一来源 |
| 已生产 | Schedule Item / Segment / Work Order | `produced_qty`、`actual_completed_qty`、Work Order `produced_qty` 并存 | 同一工单拆段时可能重复分配工单产量 |
| 在制 | 无统一字段 | 由工单状态、排程段状态和已生产数量间接推断 | 尚无单一、可对账口径 |
| 延期 | Customer Progress | 未覆盖且已过交期，或预计完成晚于交期 | Gantt 主要沿用 Result `risk_status`，可能仍显示 Normal |

## 标准业务场景

脚本每次先清理专用站点中的 APS 事务数据，再重建以下场景：

1. 备货型订单：需求 100，可用库存 40。
2. 边生产边交货：需求 100，已生产 60，已交货 30。
3. 明天交货突然取消：原需求 50，预览空版本。
4. 临时加量：40 增加到 70。
5. 已开工后减量：需求 100，已生产 40，减到 70。
6. 多客户共用同一物料：客户 A 需求 30，客户 B 需求 70。
7. 同一工单拆成多个排程段：总量 100，拆成 40 和 60。

## 固化的优化前问题

基线同时保存七个问题样本，其中前六个来自优化计划，第七个是在独立站点安装时发现的环境问题：

1. 计划头、结果头与排程段数量不一致。
2. 已延期任务在甘特图仍显示 Normal。
3. 同一日期、同一物料的重复行只保留最后一行。
4. 矩阵导入默认跳过零数量。
5. Append 保留多个 Active 版本并叠加需求。
6. Cancel 变更请求仍调用插单影响分析。
7. 全新站点安装时，Workspace 在角色创建前引用 GMC，导致首次安装钩子失败。

## Phase 0 验收门槛

以下条件必须全部满足，Phase 0 才能标记完成：

- 专用站点、数据库和生产站点完全分离。
- 源测试站点数据库备份和 APS 配置快照都有校验信息。
- 七类业务场景可以自动清理并重建。
- 七个优化前问题都有结构化证据。
- 同样的场景连续完整运行两次，归一化签名完全一致。
- 工作台、排期台、净需求、客户进度、计划台、甘特图和发布中心接口响应已归档。
- 数据库数量及计划头、结果头、排程段对账结果已归档。
- 关键页面截图已归档。
- 优化 worktree 中没有业务逻辑修改，也没有意外 `.pyc` 改动。

页面截图缺失、接口失败、数据库对账无法重复，或两轮签名不同，都视为 Phase 0 未完成。
