# APS V2 全局不可变契约

## 1. 总目标

APS V2 必须生成数量正确、资源可执行、跨 Run 连续、可解释且可审计的生产计划。系统优先保证客户交付，在不能全部满足时安排最大可生产量、给出欠量和恢复时间，并允许有权限人员确认后释放可行部分。

## 2. 不改变的现有业务主流程

现有生产和出货操作顺序保持：

```text
发工单
→ 工单排产
→ 工单入库
→ 出货计划分配销售订单
→ 下推销售出库
```

APS Demand Identity 是追踪主键，不是要求单据按顺序创建的新业务单据链。

```text
客户排期 → Demand Identity
                 ├─ APS计划 → WO → WOS → Stock Entry
                 └─ Delivery Plan → 分配SO → Delivery Note
```

生产和出货事实汇总到同一 Demand Identity，但两条链可以独立发生。

## 3. 业务不变量

### INV-01 需求唯一性

同一有效客户需求数量不能被多个排期 Revision 或多个 Formal Run 重复拥有。

### INV-02 需求不丢失

逾期、旧 Run 未完成、被排除出本次 Release 的需求必须继续留在开放需求台账，直到交付、取消或形成有审计的 Excess 处理。

### INV-03 数量守恒

对每个 Demand Identity：

```text
有效需求数量
= 已交货
+ 当前成品覆盖
+ 有效生产承诺剩余
+ 新计划数量
+ 未计划数量
- 已确认超出需求的 Excess 调整
```

各项必须互斥，不得把同一实际入库同时算作 produced coverage 和库存 coverage。

### INV-04 资源守恒

同一机器和同一模具在同一时间只能被一个 capacity owner 占用。联产品 Campaign 多个输出只计算一次资源占用。

### INV-05 执行保护

已开工、已转料、已报工、已进入冻结区间的任务不允许普通 Run 或普通 Shift Replan 自动移动、取消或换机。

### INV-06 交付优先

P0 准时完成量和延期优先于换模、连续生产、吨位接近和利用率平衡。效率目标只能在不恶化更高层交付目标时生效。

### INV-07 原料不阻塞 APS

原料可用性只作为信息提示，不限制计划数量、不影响求解可行性、不影响 Apply 指纹，也不能使 Run Hard Blocked。

### INV-08 Work Order 不强制 SO

Work Order 可以无 Sales Order/Sales Order Item。生产追踪依赖 APS Commitment/Campaign；框架 SO 在 Delivery Plan 阶段分配。

### INV-09 出货不中断

缺少 APS Demand Identity 或 Delivery Plan 血缘时，可以进入待匹配队列，但不能因此阻止现有 Delivery Plan 提交或 Delivery Note 正常出货。ERPNext 原生销售订单交货事实保持权威。

### INV-10 受控 Override

没有通用 `Force Apply All`。任何可覆盖条件必须形成有范围、原因、审批人、有效期和新指纹的输入，再重新求解。数据损坏、BOM 环、锁定任务重叠和并发过期不可覆盖。

### INV-11 每班次只提议

定时 Shift Replan 只生成差异 Proposal，不自动修改正式 WOS。当前执行中任务只能通过受控 Emergency Replan 处理。

### INV-12 可追溯

所有 Revision、准入、方案选择、欠量确认、Override、排除、跨 Run 转移、工单释放、重排和粗略交货匹配必须记录用户、时间、理由和输入/输出指纹。

### INV-13 生产与用户定制不可被开发覆盖

- `jce.1` 是生产站点。开发阶段只允许读取，不得执行写库、migrate、patch、install、uninstall、build、restart、clear-cache、scheduler 或测试 Fixture。
- `jce-test.1` 虽使用独立数据库，但也不是默认可写环境；只有用户明确批准或建立隔离副本后才允许执行会写库的验证。
- 已有 Workspace、Custom HTML Block、Client Script、Property Setter、Customize Form 布局及其他用户前端定制均视为用户数据。安装或迁移逻辑只能创建缺失的应用资源，不能以同名资源或“应用所有权”为由覆盖、重排或删除已有内容。
- 标准 Page 的源码升级只允许修改 Injection APS 自己的代码文件；不得通过迁移脚本重置用户 Desk 布局。任何确需改变现有操作流程的 UI 必须走 Feature Flag、差异预览和明确上线批准。

## 4. 需求准入契约

- P0 / 必排：客户确认排期、逾期未完成、有效跨 Run 承诺、多层 BOM 依赖。
- P1 / 备货建议：框架 SO 剩余需求、连续生产和减少换模建议；默认需 PMC 选择。
- P2 / 库存建议：安全库存、长期预测、纯库存生产；默认关闭并需 PMC 选择。

P0/P1/P2 是准入类别，不是机器评分。

## 5. 时间契约

- Demand Horizon：纳入新需求。
- Freeze Horizon：不可自动移动。
- Restricted Horizon：变更需批准。
- Recovery Horizon：只承接本次已准入需求的延期，不读取该区间的新需求。
- 计划天数按自然日期包含首尾，`end = start + days - 1`。
- 早于开始日的开放需求作为逾期 P0 带入，不能过滤掉。

## 6. 状态契约

- Ready：合法且不需业务确认。
- Acknowledgment Required：延期、欠量、提前生产、P1/P2、备用参数、超产风险等可确认风险。
- Hard Blocked：仍存在无法形成合法计算输入或合法资源计划的问题。
- Applied with Exceptions：可行部分已释放，排除项仍作为 Critical Unplanned 保留。
- Applied：方案已通过指纹和一致性验证并落地。

产能不足本身不是 Hard Blocker。

## 7. 技术边界

- `planning.py` 保留兼容 Facade，新逻辑拆服务模块。
- 所有写操作在公司/车间范围内加锁并具备幂等键。
- Solver 输出必须经过独立 Validator。
- Feature Flag 关闭时 Legacy Formal 行为保持不变。
- 标准/外部 App 通过 Custom Field 和事件扩展，不直接修改其源码 DocType 定义。
- 重复执行的 `after_migrate` 不得承担前端模板刷新、Customize Form 排序或历史数据归一化；这类变化必须使用可审查的幂等 Patch，并在生产副本上先演练。

## 8. 变更规则

本文件内容只有在用户明确改变业务目标后才能修改。执行 AI 发现实现困难、性能问题或旧代码冲突时，不得降低不变量；必须提交 ADR，说明问题、可选方案、影响和建议，等待确认。
