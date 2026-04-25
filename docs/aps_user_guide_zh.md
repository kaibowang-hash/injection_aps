# Injection APS 完整使用与逻辑说明

## 1. 文档目的

这份文档说明当前已经落地在 `injection_aps` app 中的 APS 一期逻辑，重点回答 4 个问题：

1. APS 的数据从哪里来。
2. 每一步是怎么算出来的。
3. 审批后是怎么下推到正式执行层的。
4. 现场遇到插单、改单、删单、模具缺失、执行偏差时，应该怎么处理。

本文只描述当前代码已经实现并可使用的能力，不把未来规划中的功能写成现成功能。

### 1.1 近期更新摘要

相较于早期版本，当前 APS 已经补上了下面这些关键能力：

1. `APS运算单` 作为强上下文对象，`看板`、`执行管理` 等页面不再默认模糊显示，而是明确先选 run 再进入。
2. `看板` 已支持：
   - 图表区全屏
   - 横向缩放
   - sticky 时间轴
   - segment 右键快捷菜单
   - Gantt 拖拽换机 / 改序 / 右边缘拉伸
   - `segment_name` 直接显示在 bar 上
   - 按 `segment_name` 搜索并高亮定位
3. Gantt 拖拽阻断提示不再只给技术报错，而会明确说明：
   - 当前段是谁
   - 目标机台和目标开始时间
   - 重算后结束时间
   - 冲突段是谁
   - 建议动作是什么
4. `执行管理` 中的异常已经和插单影响分析分开：
   - 行级动作为 `来源`
   - `处理建议`
   - 页面级独立保留 `插单影响分析`
5. 异常诊断已结构化，不再只是短消息，能展示：
   - 需求量 / 已排量 / 未排量
   - 候选模具
   - 候选机台
   - 关键根因
   - 建议动作
6. 工单与白/夜班排产已经改成“对账式下发”，目标是：
   - 工单做稳定执行容器
   - `Production Scheduling / Scheduling Item` 做频繁变更层
   - 避免每次 Gantt 微调都制造一批废工单
7. 看板弹窗和结果详情现在会显示：
   - 小时产能
   - 日产能
   - 产能来源
8. 主要分析表和结果表已经支持 `xlsx` 导出。

---

## 2. 系统定位

当前 `Injection APS` 的定位是：

1. 它是注塑计划与排程建议层。
2. 它不是现场执行层。
3. 它不替代你们现在已经在用的正式执行链。

当前正式执行链固定为：

`Work Order -> 每天白班 / 晚班的 Work Order Scheduling -> Manufacture Stock Entry`

因此 APS 的正确理解是：

1. APS 负责承接需求、计算净需求、试算排程、形成建议。
2. APS 负责把建议送到“工单建议审核”和“白夜班排产建议审核”。
3. 审核通过后，才正式创建或更新标准 `Work Order`、`Work Order Scheduling`。
4. 生产执行以后，APS 再回读 `Work Order`、`Scheduling Item`、`Manufacture Stock Entry`，做滚动偏差监控和重排建议。

当前正式流程为：

`客户排期导入 -> 需求池 -> 净需求 -> APS Trial Run -> Run 审批 -> 工单建议审核 -> 正式工单 -> 白夜班排产建议审核 -> 正式 Work Order Scheduling -> 现场执行 -> Manufacture 入库 -> APS 执行回写 -> 偏差预警 / 重排建议`

---

## 3. 当前范围

### 3.1 已实现

当前已实现并建议正式使用的内容：

1. 客户排期导入、版本对比、版本生效。
2. 需求池重建。
3. 净需求重算。
4. APS Trial Run 试算。
5. 模具主数据门禁校验。
6. 同机主段重叠校验。
7. 工单建议批次生成与人工审核。
8. 白班 / 晚班排产建议批次生成与人工审核。
9. 正式 `Work Order` 创建与 APS 追踪字段回写。
10. 正式 `Work Order Scheduling` 创建 / 更新与段级追踪回写。
11. Gantt 可视化查看、结果详情、备注维护。
12. 执行反馈同步与偏差异常。
13. 插单影响分析。
14. 变更申请对象 `APS Change Request`。

### 3.2 尚未实现或暂不自动化

当前没有实现，或者故意不自动化的内容：

1. 不自动后台改正式排产。
2. 不自动后台减量或取消正式工单。
3. 不自动下采购单。
4. 不做全自动冻结区审批流门户。
5. 不做自由拉伸时长的前端甘特编辑。
6. 不把 APS 当成 MES 替代品。

---

## 4. 关键对象与作用

| 对象 | 作用 | 是否正式执行对象 |
| --- | --- | --- |
| `APS Schedule Import Batch` | 记录一次客户排期导入批次 | 否 |
| `Customer Delivery Schedule` | 当前有效客户交付计划版本 | 否 |
| `Customer Delivery Schedule Item` | 客户排期明细行 | 否 |
| `APS Demand Pool` | APS 统一需求池 | 否 |
| `APS Net Requirement` | 净需求结果 | 否 |
| `APS Planning Run` | 一次 APS 运算头档 | 否 |
| `APS Schedule Result` | 一条物料需求的排程结果 | 否 |
| `APS Schedule Segment` | 结果下的机台时间段 | 否 |
| `APS Work Order Proposal Batch` | 工单建议审核批次 | 否 |
| `APS Shift Schedule Proposal Batch` | 白夜班排产建议审核批次 | 否 |
| `APS Release Batch` | 正式落地日志 | 否 |
| `APS Exception Log` | 异常与风险中心 | 否 |
| `APS Change Request` | 插单 / 改单 / 删单等变更申请 | 否 |
| `Work Order` | 正式生产工单 | 是 |
| `Work Order Scheduling` | 正式白班 / 晚班排产单 | 是 |
| `Scheduling Item` | 正式班次排产明细行 | 是 |
| `Stock Entry` | Manufacture 入库确认 | 是 |

---

## 5. APS 用到的数据从哪里来

## 5.1 客户需求来源

APS 当前会从下面 3 类来源收口需求：

1. `Customer Delivery Schedule`
2. `Sales Order Backlog`
3. `Safety Stock`

另外还有保留的来源类型：

1. `Urgent Order`
2. `Trial Production`
3. `Complaint Replenishment`

但一期默认主要还是前 3 类。

### 5.1.1 Customer Delivery Schedule

来源对象：

1. `Customer Delivery Schedule`
2. `Customer Delivery Schedule Item`

产生方式：

1. 先导入 Excel 或 JSON。
2. 系统与当前 `Active` 版本比差异。
3. 新版本正式导入后，旧版本转为 `Superseded`。
4. 新版本成为唯一 `Active` 版本。

APS 取数口径：

1. 只读取 `status = Active` 的客户排期。
2. 只读取排期行里的可排产物料。
3. 当前只允许 `Item.item_group` 属于：
   - `Plastic Part`
   - `Sub-assemblies`

排期行进入需求池时的数量口径：

1. 导入时 `balance_qty = max(qty - delivered_qty, 0)`
2. 重建需求池时 `open_qty = max(balance_qty - allocated_qty, 0)`
3. 如果 `open_qty <= 0`，这条行不会进入需求池。

所以对客户排期来说，APS 真正拿来算计划的，是“还没交、还没被分配掉的剩余量”。

### 5.1.2 Sales Order Backlog

来源对象：

1. `Sales Order`
2. `Sales Order Item`

APS 取数口径：

1. 只取已提交 `docstatus = 1` 的销售订单。
2. 排除 `Closed / Completed / Cancelled`。
3. 每行 `open_qty = max(qty - delivered_qty, 0)`。

非常关键的一点：

当前 APS 不把 Sales Order 当作近期主驱动。

如果同一个 `company + customer + item_code` 已经存在 `Active` 的客户排期，APS 会跳过这条 SO backlog，不再重复把它塞进需求池。

也就是说：

1. 有活跃客户排期时，以客户排期为主。
2. Sales Order 主要承担“合同边界 / 兜底需求”的角色。

这正是为了适配你们“Sales Order 多数为框架订单，不宜直接拿交期做短期排产驱动”的实际业务。

### 5.1.3 Safety Stock

来源对象：

1. `Item`
2. `Bin`

APS 从 `APS Settings` 里读取安全库存字段映射，默认是：

1. `Item.safety_stock`

逻辑：

1. 读取 Item 的安全库存值。
2. 读取该物料当前可用库存。
3. `shortage = max(safety_stock - available_stock, 0)`
4. `shortage > 0` 时，生成一条 `Safety Stock` 类型需求。

---

## 5.2 物料、颜色、材料、FDA 数据来源

主要来源：

1. `Item`
2. `Mold Default Material`
3. `APS Settings` 字段映射

默认字段映射为：

1. 食品级 / FDA：`Item.custom_food_grade`
2. 首件标识：`Item.custom_is_first_article`
3. 颜色：`Item.color`
4. 材料：`Item.material`
5. 安全库存：`Item.safety_stock`
6. 最大库存：`Item.max_stock_qty`
7. 最小批量：`Item.min_order_qty`

如果 `Item` 上缺少颜色或材料，APS 会尝试从 `Mold Default Material` 取模具默认材料和颜色：

1. 优先找到该物料的主模具。
2. 再读该模具的第一行 `Mold Default Material`。
3. 用其中的 `material_item` 和 `color_spec` 做补充。

这也是为什么 APS 有时会依赖模具侧数据来补全物料上下文。

---

## 5.3 库存、在制与正式执行数据来源

### 5.3.1 可用库存

来源对象：

1. `Bin`
2. `Warehouse`

口径：

安全库存转需求时仍按 ERPNext 自由库存判断：

`free_stock = max(sum(actual_qty - reserved_qty), 0)`

净需求重算时，APS 会把“本次需求池已代表的 Sales Order 预留”加回，避免同一销售需求既进入需求池、又通过 `Bin.reserved_qty` 被重复扣减：

`available_stock_for_net = max(sum(actual_qty) - max(sum(reserved_qty) - aps_sales_order_reservation_credit, 0), 0)`

`Safety Stock` 需求行本身已经是自由库存扣减后的缺口，净需求阶段不会再给这类行分摊库存，只会继续用未完工工单抵扣缺口。

APS 读取的是所有非组仓的库存汇总，可按公司过滤。

### 5.3.2 已开未完工工单量

来源对象：

1. `Work Order`

口径：

`open_work_order_qty = sum(max(qty - produced_qty, 0))`

只统计：

1. `docstatus = 1`
2. 状态不在 `Completed / Closed / Cancelled`

### 5.3.3 正式执行反馈

来源对象：

1. `Work Order`
2. `Scheduling Item`
3. `Stock Entry`

用途分别是：

1. `Work Order.produced_qty`：工单层面的主完工量。
2. `Scheduling Item.completed_qty / from_time / to_time`：白夜班实际执行进度。
3. `Stock Entry`：制造入库确认，以及“今日已入库”统计。

---

## 5.4 模具、机台与规则数据来源

## 5.4.1 模具主数据

APS 排程的模具真源是：

1. `Mold`
2. `Mold Product`

APS 当前会从这里读取：

1. 模具编号
2. 模具名称
3. 模具状态
4. 最小配机吨位 `machine_tonnage`
5. 是否 Family Mold
6. `cycle_time_seconds`
7. `output_qty`
8. `cavity_output_qty`
9. `cavity_count`
10. 默认产品 / 优先级

有效产出数的优先级为：

1. `cavity_output_qty`
2. `output_qty`
3. `cavity_count`，但仅在 `0 < cavity_count <= 128` 时才作为保底
4. 否则按 1 处理

也就是说，APS 不会盲目把异常 `cavity_count` 当真实穴数放大产能。

## 5.4.2 机台能力

APS 首选：

1. `APS Machine Capability`

读取字段：

1. `workstation`
2. `plant_floor`
3. `machine_tonnage`
4. `risk_category`
5. `hourly_capacity_qty`
6. `daily_capacity_qty`
7. `queue_sequence`
8. `machine_status`
9. `max_run_hours`

如果没有维护 `APS Machine Capability`，APS 会退回读取：

1. `Workstation`

这时只作为 fallback，机会成本和准确性都会下降。

## 5.4.3 APS Mould-Machine Rule 的真实定位

`APS Mould-Machine Rule` 不是模具主数据替代品。

它的定位是：

1. 某物料 / 某模具只能上哪些机台。
2. 多台都能上时的优先级。
3. 临时禁排某台机。
4. 补充 `min_tonnage / max_tonnage` 限制。

它不负责回答“这个物料有哪些模具”。

这一点非常关键：

1. 没有 `Mold + Mold Product` 时，APS 不能靠 `APS Mould-Machine Rule` 硬排出来。
2. Trial Run 中会显示 blocked。
3. Run 审批和正式下推时会被硬拦住。

## 5.4.4 换色规则

来源对象：

1. `APS Color Transition Rule`

用途：

1. 给换色增加 penalty。
2. 给某些换色关系增加 setup 分钟。
3. 也可以配置成阻断型。

如果没有定义颜色切换规则，APS 不会额外施加颜色 penalty。

---

## 5.5 Item 名称与 item_code 不一致时如何处理

你们现场存在一类典型情况：

1. `Item.name` 与对外看到的 `item_code` 不一致。

当前 APS 在关键重建步骤前都会执行引用修复和名称解析：

1. `repair_item_references(...)`
2. `_resolve_item_name(...)`

实际效果是：

1. 如果排期、需求池、SO 行里带的是“可识别但不是标准 `Item.name` 的引用”，APS 会先尝试修正为真实 `Item.name`。
2. 修不出来时，不会静默排进去，而是记 warning 或 blocked。

因此 APS 现在的设计目标是：

1. 允许现场存在 `item_code` 与 `doc.name` 不完全一致的情况。
2. 但要求最终能被解析到真实的 Item 主档。

---

## 6. 需求池是怎么生成的

`rebuild_demand_pool(company)` 的真实动作分 4 步：

1. 先修一次 Item 引用。
2. 删除旧的系统生成需求池行。
3. 重拉活跃客户排期。
4. 再补 SO backlog 和安全库存。

### 6.1 客户排期转需求池

条件：

1. `Customer Delivery Schedule.status = Active`
2. 物料必须属于 `Plastic Part / Sub-assemblies`
3. `open_qty > 0`

每条 `Customer Delivery Schedule Item` 会生成一条 `APS Demand Pool`：

1. `demand_source = Customer Delivery Schedule`
2. `source_doctype = Customer Delivery Schedule`
3. `source_name = 排期单名称`
4. `sales_order = 排期行挂的 SO`
5. `remark = change_type`

### 6.2 SO backlog 转需求池

条件：

1. SO 已提交，且未关闭 / 完成 / 取消。
2. 物料属于可排产范围。
3. 同 `company + customer + item_code` 没有活跃客户排期覆盖。

每条符合条件的 SO backlog 会生成一条 `APS Demand Pool`：

1. `demand_source = Sales Order Backlog`
2. `source_doctype = Sales Order`
3. `source_name = SO 单号`

### 6.3 安全库存转需求池

条件：

1. Item 维护了安全库存。
2. 物料属于可排产范围。
3. 当前可用库存低于安全库存。

生成：

1. `demand_source = Safety Stock`
2. `source_doctype = Item`
3. `source_name = item`

### 6.4 需求优先级

APS 会给需求打分，当前基础优先级为：

1. `Urgent Order = 1000`
2. `Customer Delivery Schedule = 800`
3. `Sales Order Backlog = 600`
4. `Safety Stock = 400`
5. `Trial Production = 300`
6. `Complaint Replenishment = 300`

另外还会叠加：

1. 紧急标识 bonus
2. 越接近到期日，优先级越高

所以 APS 的实际排序逻辑是：

1. 先看来源优先级。
2. 再看是否 urgent。
3. 再看到期日期远近。

---

## 7. 净需求是怎么算的

`rebuild_net_requirements(company)` 会把 `APS Demand Pool` 按下面维度分组：

1. `company`
2. `customer`
3. `item_code`
4. `demand_date`

然后逐组计算。

### 7.1 公式

当前真实公式为：

`net_requirement_qty = max(demand_qty - available_stock_qty - open_work_order_qty + safety_stock_gap_qty, 0)`

其中：

1. `demand_qty`：同组需求池数量汇总。
2. `available_stock_qty`：本次 APS 可用库存。它会扣除外部销售预留，但会加回当前需求池已覆盖的 Sales Order 预留，避免销售需求重复扣减。
3. `open_work_order_qty`：已开未完工工单量。
4. `safety_stock_gap_qty = max(safety_stock - available_stock, 0)`
5. `overstock_qty = max(available_stock - max_stock, 0)`，只有维护了最大库存时才展示为提示值，不参与当前净需求扣减。

### 7.2 计划数量 planning_qty

算完净需求后，还会考虑最小经济批量：

1. 如果 `net_requirement_qty > 0`
2. 且维护了 `minimum_batch_qty`
3. 则 `planning_qty = max(net_requirement_qty, minimum_batch_qty)`

否则：

1. `planning_qty = net_requirement_qty`

### 7.3 净需求说明文字

每一条 `APS Net Requirement` 都会生成 `reason_text`，把本次建议开单原因明确写出来，例如：

1. 需求量是多少。
2. 扣掉了多少本次 APS 可用库存。
3. 扣掉了多少已开未完工工单。
4. 加回了多少安全库存缺口。
5. 展示了多少剩余超库存提示量。
6. 是否因为最小批量被抬高。

所以 APS 的净需求不是黑箱。

---

## 8. APS Trial Run 是怎么排出来的

`run_planning_run(...)` 当前真实流程如下。

## 8.1 Run 前置动作

一旦点击 Run，系统会先做这两件事：

1. 重建需求池
2. 重算净需求

也就是说，Trial Run 不是用旧快照直接算，而是先把需求基础刷新一遍再算。

## 8.2 本次 Run 的范围

Run 会按下面条件过滤净需求：

1. `company`
2. `customer`，可选
3. `item_code`，可选
4. `demand_date` 在计划视窗内
5. `net_requirement_qty > 0`

计划视窗默认来自 `APS Settings.planning_horizon_days`。

## 8.3 候选资源是怎么找的

APS 先找可用模具，再找机台，最终形成 `mold + workstation` 的候选 lane。

### 8.3.1 先找可用模具

`_get_available_mold_rows(item_code)` 的条件是：

1. `Mold.docstatus = 1`
2. `Mold Product.item_code = 当前物料`
3. 模具状态不在下面这些阻断状态中：
   - `Under Maintenance`
   - `Under External Maintenance`
   - `Scrapped`
   - `Outsourced`
   - `Pending Asset Link`

如果一个物料没有任何可用模具：

1. Trial Run 结果会变成 `Blocked`
2. 异常类型会是 `Mold Unavailable`
3. 结果详情中会显示没有可用模具

### 8.3.2 再找机台候选

APS 从 `APS Machine Capability` 取活跃机台，排除：

1. `Unavailable`
2. `Fault`
3. `Maintenance`
4. `Disabled`

然后逐台和模具做配对。

### 8.3.3 吨位校验

如果模具有 `machine_tonnage`，则：

1. 机台吨位必须 `>= mold.machine_tonnage`
2. 不满足直接排除，不进入候选

### 8.3.4 APS Mould-Machine Rule 只做二级限制

如果某台机存在该物料的 `APS Mould-Machine Rule`：

1. 必须命中该规则才能进入候选
2. 可附带 `preferred`、`priority`、`min_tonnage`、`max_tonnage`

如果没有规则，但机台与模具本身合法：

1. 仍然可以进入候选

因此它是“收口或排序”，不是“从无到有造候选”。

## 8.4 单个候选 lane 的排程估算

对于每个候选 `mold + workstation`，APS 会算：

1. 最早可开始时间
2. setup / changeover 时间
3. 小时产能
4. 当前视窗内可排数量
5. 全量跑完会到几点
6. 风险与异常

### 8.4.1 最早可开始时间

基础逻辑：

1. 从 `now()` 开始。
2. 如果该机台已有锁定段，则从该机台最后一个锁定段结束后开始。

这意味着：

1. 已锁定段会占住机台窗口。
2. Trial Run 不会直接把锁定段当空气。

### 8.4.2 setup / changeover 逻辑

默认先给一笔基础 setup：

1. `APS Settings.default_setup_minutes`

然后再叠加：

1. 颜色切换
2. 材料切换
3. 首件确认
4. 换模

具体逻辑如下：

#### 颜色切换

如果有 `APS Color Transition Rule`：

1. 可提高 setup 分钟。
2. 可产生 Warning。
3. 如果规则配置为阻断，候选直接 blocked。

#### 材料切换

如果当前机台上一段材料和本次材料不同：

1. 额外加 15 分钟。
2. 生成 `Material Changeover` 警告。

#### 首件确认

如果物料是首件：

1. 增加 `APS Settings.default_first_article_minutes`
2. 生成 `First Article Confirmation` 警告。

#### 换模

如果当前机台上一段模具和本次模具不同：

1. 增加 `APS Settings.mold_change_penalty_minutes`
2. 生成 `Mould Changeover` 警告。

### 8.4.3 FDA 风险

如果物料需要 FDA，而机台风险类别是 `Non FDA`：

1. 该候选直接 blocked
2. 异常类型为 `FDA Conflict`

当前自动排程把 FDA 冲突视为硬约束。

### 8.4.4 产能怎么来

小时产能优先级如下：

1. 如果 `cycle_time_seconds > 0` 且 `effective_output_qty > 0`
   - `hourly_capacity_qty = 3600 / cycle_time_seconds * effective_output_qty`
2. 否则用 `APS Machine Capability.hourly_capacity_qty`
3. 再否则用 `APS Machine Capability.daily_capacity_qty / 24`
4. 再否则如果设置了 `missing_cycle_fallback_seconds`
   - 用 fallback cycle + effective output 估算
5. 最后退回 `APS Settings.default_hourly_capacity_qty`

所以当前设计原则是：

1. 先信模具周期和单模产出。
2. 机台产能只作 fallback。

### 8.4.5 候选评分

候选主要按以下顺序择优：

1. 全量跑完结束时间越早越好。
2. setup 越少越好。
3. `preferred` 越高越好。
4. `priority` 越靠前越好。
5. 模具优先级越靠前越好。

## 8.5 复制模并行与 Family Mold

### 8.5.1 复制模并行

如果同一个物料有多副可用模具，且形成多个互不重复的 `mold + workstation` lane，APS 会在下面情况下考虑并行：

1. 数量达到 `minimum_parallel_split_qty`
2. 或主 lane 放不下
3. 或主 lane 会超过交期

这时 APS 会把一条需求拆到多个 lane 上，同时打上：

1. `copy_mold_parallel = 1`
2. `parallel_group`
3. 异常提示 `Copy Mold Parallelized`

### 8.5.2 Family Mold

如果模具是 Family Mold，且同模上维护了多个 `Mold Product`：

1. APS 会按主物料跑出的 cycle 计算 sibling item 的联产量。
2. sibling item 只在 `Plastic Part / Sub-assemblies` 范围内才会被当作联产输出。
3. 联产段会以 `segment_kind = Family Co-Product` 写入结果。
4. 这些联产量会累计成 `family_credit_map`，优先抵扣后续 sibling item 的净需求。

也就是说：

1. APS 不是只把 Family Mold 当备注。
2. 它会真实影响后续净需求的覆盖关系。

## 8.6 结果状态怎么判

如果正常排进去且不逾期：

1. `status = Planned`
2. `risk_status = Normal`

如果能排但超交期或排不满：

1. `status = Risk`
2. `risk_status = Attention / Critical`

如果根本没有合法模具或合法候选机台：

1. `status = Blocked`
2. `risk_status = Blocked`

### 8.6.1 同机主段重叠校验

Run 落档后，系统会再跑一次 overlap 校验：

1. 同一 `workstation`
2. 排除 `Family Co-Product`
3. 如果主段时间互相覆盖，则报 `Primary Segment Overlap`

这一步是为了保证：

1. 一台机在同一时段不能生产两种不同的主产品。
2. Family 联产不算冲突。

---

## 9. Run 审批前后的门禁

`approve_planning_run(run_name)` 并不是简单改状态。

审批前会做两类硬校验。

## 9.1 模具准备度门禁

`validate_run_mold_readiness(...)` 会检查：

1. 该结果是否真的有可用的 `Mold + Mold Product`
2. 是否存在主段
3. `primary_mould_reference` 是否为空
4. 段上的模具是否还能从主数据里找到
5. 模具状态是否被阻断
6. 是否缺 `cycle_time_seconds` 或 `effective_output_qty`

只要存在这些问题：

1. Trial Run 可以看到 blocked 结果和异常
2. 但正式 `Approve` 会被阻止

## 9.2 同机主段重叠门禁

审批前会再次执行 overlap 校验。

只要存在主段真实重叠：

1. 审批被阻止
2. 异常日志记录为 `Primary Segment Overlap`

因此当前 APS 的真实规则是：

1. Trial 可以展示问题。
2. 但正式下游动作前必须先把硬问题处理掉。

---

## 10. 审批后怎么下推到正式执行层

当前 APS 不会在 Run 审批后直接后台创建并提交正式单据。

必须先走两个审核批次：

1. 工单建议审核
2. 白夜班排产建议审核

## 10.1 工单建议批次

调用：

`generate_work_order_proposals(run_name)`

前提：

1. Run 已审批
2. 模具准备度无 blocker

生成规则：

1. 只针对 `scheduled_qty > 0` 且 `status != Blocked` 的结果。
2. 一条 `APS Schedule Result` 对应一条工单建议。
3. 默认只看主段，不看 `Family Co-Product` 段。

系统会判断是否已存在正式工单：

1. 先找 `custom_aps_result_reference = 当前结果`
2. 找不到再按 `production_item + 未完成状态` 找一张可复用工单

然后给出建议动作：

1. `New`
2. `Keep Existing`
3. `Update Existing`
4. `Create Delta`
5. `Close Residual`
6. `Cancel Unstarted`

### 10.1.1 Apply 工单建议时怎么处理

调用：

`apply_work_order_proposals(batch_name)`

实际逻辑：

1. 只有 `review_status = Approved` 的行才会正式应用。
2. `New`
   - 创建并提交一张新的正式 `Work Order`
3. `Create Delta`
   - 现有工单保持不动
   - 额外需求拆成一张增量工单，保留追溯
4. `Keep Existing`
   - 只回写 APS 追踪字段，不改原工单数量
5. `Update Existing`
   - 在不低于已产数量的前提下，直接更新原工单数量与计划窗口
6. `Close Residual`
   - 已执行量保留，剩余未执行量关闭
7. `Cancel Unstarted`
   - 未开工工单可直接取消，并同步清理未冻结排产行

这样做的目的就是你提的那点：

1. 不能后台悄悄改正式工单。
2. 需要先审核，再落地。
3. 对已执行量只处理残量，不回滚历史执行。

### 10.1.2 正式工单上会回写哪些 APS 字段

创建或链接正式工单时，会写回：

1. `custom_aps_run`
2. `custom_aps_source`
3. `custom_aps_required_delivery_date`
4. `custom_aps_is_urgent`
5. `custom_aps_release_status`
6. `custom_aps_locked_for_reschedule`
7. `custom_aps_schedule_reference`
8. `custom_aps_result_reference`
9. `custom_aps_proposal_batch`

## 10.2 白夜班排产建议批次

调用：

`generate_shift_schedule_proposals(...)`

前提：

1. 至少有一个 `APS Work Order Proposal Batch` 已 `Applied`

生成规则：

1. 只取工单建议批次中 `review_status = Applied` 的行。
2. 每个结果下仍然只看主段。
3. 只取开始日期在 release horizon 内的段。

release horizon 默认来自：

1. `APS Settings.release_horizon_days`

### 10.2.1 班次怎么判

班次规则当前是：

1. `08:00 <= start_time < 20:00` 判为 `白班`
2. 其他判为 `晚班`

### 10.2.2 Apply 白夜班建议时怎么落正式单据

调用：

`apply_shift_schedule_proposals(batch_name)`

前提：

1. 无 overlap blocker
2. 无 mold blocker
3. 至少有一行 `review_status = Approved`

正式落地逻辑：

1. 按 `posting_date + company + plant_floor + shift_type` 找当天该班次的正式 `Work Order Scheduling`
2. 找到就增量写入 / 更新对应 `Scheduling Item`
3. 找不到就创建新的正式 `Work Order Scheduling`

这意味着当前现场模式仍然是：

1. 每天白班一张
2. 每天晚班一张

不是一段一张排产单。

### 10.2.3 哪些正式排产单不能被覆盖

如果当天班次的正式 `Work Order Scheduling.status` 已进入：

1. `Material Transfer`
2. `Job Card`
3. `Manufacture`

则 APS 视为“执行冻结”，不会覆盖，会直接抛错并生成异常：

1. `Shift Scheduling Frozen`

这表示：

1. 班次已经开始执行。
2. APS 只能给补救建议，不能直接回写覆盖正式单据。

### 10.2.4 Scheduling Item 上回写哪些 APS 字段

正式写入 `Scheduling Item` 时，会回写：

1. `custom_aps_run`
2. `custom_aps_result_reference`
3. `custom_aps_segment_reference`
4. `custom_aps_shift_proposal`

同时 APS 也会把对应 `APS Schedule Segment` 绑定到：

1. `linked_work_order`
2. `linked_work_order_scheduling`
3. `linked_scheduling_item`

## 10.3 APS Release Batch 的真实作用

当前 `APS Release Batch` 不是“后台自动放单器”。

它的作用是：

1. 记录本次正式落地日志
2. 记录实际生成了多少正式工单
3. 记录写入了哪些正式白夜班排产单

## 10.4 Gantt 改动后怎么闭环到正式执行层

这是当前版本非常重要的一条规则：

1. Gantt 拖拽 / 拉伸改的是 `APS Schedule Segment`
2. 不是直接改正式 `Work Order`
3. 也不是直接改正式 `Work Order Scheduling`

一旦在 `看板` 上做了人工改动，系统会这样处理：

1. 当前 `APS运算单` 回到“待确认”口径
2. 用户需要重新 `确认运算`
3. 然后生成新的：
   - `工单建议审核`
   - `白/夜班排产建议审核`

这两类建议的处理原则固定为：

1. **仅换机 / 改顺序 / 改时间**
   - 优先只改 `Production Scheduling / Scheduling Item`
   - 不主动新建工单
2. **数量边界变化**
   - 先看能否复用或更新原工单
   - 只有无法安全复用时，才生成 `delta` 工单
3. **已进入执行的正式白/夜班排产**
   - 继续视为冻结
   - APS 不直接覆盖
   - 只给补排 / 后移 / 新增残量建议

因此，Gantt 的正确理解是：

1. 它是计划与调度层
2. 它可以高频调整
3. 但正式执行层仍然通过“对账建议 -> 人工审核 -> 正式应用”闭环
4. 不会在你一拖完的瞬间直接偷偷改正式工单

---

## 11. Gantt、结果详情和备注

## 11.1 Gantt 上看到的是什么

`看板` 展示的是 `APS Schedule Segment`，但它现在已经不是单纯的“只读甘特图”，而是一个强 run 上下文页面。

进入规则固定为：

1. 必须先选择一个 `APS运算单`
2. 没选 run 时，页面只显示空态和最近 run
3. 选中 run 后，顶部会显示当前 run 上下文：
   - `APS运算单编号`
   - `Company`
   - `Plant Floors`
   - `Horizon`
   - `状态`
   - `审批状态`
   - `异常数`
   - `更新时间`

常见 `segment_kind` 包括：

1. `Primary`
2. `Family Co-Product`

并可能附带：

1. `parallel_group`
2. `family_group`
3. `risk_flags`
4. `segment_status`
5. `actual_status`
6. `segment_name`

## 11.2 看板工具栏和主要交互

当前工具栏包含：

1. 图例
2. 视图模式
3. 横向缩放
4. 图表区全屏 / 退出全屏
5. 刷新
6. 重置日期聚焦
7. `segment_name` 搜索

当前支持的主要交互包括：

1. 在时间轴上拖拽，聚焦某一天或某个日期区间
2. 在 `Machine` 视图中：
   - segment 拖拽换机 / 改顺序 / 平移时间
   - 右边缘拉伸，修改结束时间和数量
3. 右键 segment 打开快捷菜单
4. 搜索 `segment_name` 后自动高亮并滚动定位

搜索规则：

1. 输入完整或部分 `segment_name`
2. 点击 `查找`
3. 系统会高亮匹配 bar
4. 并尽量把它滚动到当前可视区域

## 11.3 Gantt 拖拽和拉伸的真实语义

当前 Gantt 的拖拽语义固定如下：

1. 横向拖动：
   - 保数量
   - 保时长
   - 只是整体平移开始/结束时间
2. 纵向拖动：
   - 尝试换到另一条机台 lane
3. 右边缘拉伸：
   - 开始时间固定
   - 结束时间变化
   - 数量按当前产能口径自动重算

拖动保存前一定会先做预检，检查：

1. 机台状态
2. 模具唯一占用
3. 吨位
4. FDA / 风险类别
5. 冻结区
6. 颜色 / 换模相关约束

如果不能移动，系统现在会给出结构化阻断说明，而不是只报一个英文技术错误。

阻断弹窗会明确展示：

1. 业务结论
2. 当前段是谁
3. 目标机台
4. 目标开始时间
5. 重算结束时间
6. 冲突段是谁
7. 建议动作
8. 技术阻断明细

这类提示尤其适合解释：

1. 同一模具后续已经有连续排产
2. 目标机台吨位不够
3. 模具与机台不匹配
4. 机台被停机 / 故障 / 保养状态过滤

## 11.4 点击单个 segment 会看到什么

`get_schedule_result_detail(result_name)` 会返回：

1. 结果头档
2. 所有段
3. 物料详情
4. 来源行
5. 异常行
6. 模具依据
7. 跳转 route

因此在 Gantt 弹窗中，应该能查看到：

1. 物料编号、名称、客户参考号、图纸
2. 需求来源与来源单据 link
3. 模具编号、模具状态、吨位、cycle、output、cavity
4. 段级工单、白夜班排产单、入库单 link
5. 当前风险和异常
6. `segment_name`
7. 小时产能
8. 日产能
9. 产能来源

日产能显示规则为：

1. 若当前有效产能来自模具周期 / 每周期产出或小时产能，则：
   - `日产能 = 小时产能 × 24`
2. 若当前有效来源本身就是显式 `daily_capacity_qty`，则直接显示该值
3. 同时显示 `产能来源`，避免用户只看到数字却不知道口径

## 11.5 备注分两层

当前备注口径固定为：

1. `APS Schedule Result.notes`
   - 结果级备注
   - PMC 备注
   - 总体备注
2. `APS Schedule Segment.segment_note`
   - 段级备注
   - MC / 现场备注
3. `APS Schedule Segment.manual_change_note`
   - 只表示人工改排原因
   - 不与现场备注混用

更新接口：

`update_schedule_notes(result_name=None, segment_name=None, result_note=None, segment_note=None)`

---

## 12. APS 怎么做滚动执行监控

当前 APS 是“滚动监控 + 人工确认重排”，不是“自动改正式计划”。

## 12.1 执行锚点

当前执行锚点固定为：

1. 主锚点：`Work Order`
2. 段级桥接：`Scheduling Item`
3. 完工确认：`Manufacture Stock Entry`

## 12.2 执行同步怎么做

调用：

`sync_execution_feedback_to_aps(run_name)`

对每个 segment：

1. 先找 `linked_scheduling_item`
2. 找不到再按 `custom_aps_segment_reference` 回查 `Scheduling Item`
3. 取 `completed_qty / from_time / to_time`
4. 再补读 `Work Order.produced_qty`

然后回写到段上：

1. `actual_status`
2. `actual_completed_qty`
3. `actual_start_time`
4. `actual_end_time`
5. `delay_minutes`
6. `last_execution_sync_on`

再按段级状态汇总到结果头档：

1. `actual_status`
2. `actual_progress_qty`
3. `actual_start_time`
4. `actual_end_time`
5. `delay_minutes`

## 12.3 执行状态怎么判

当前状态包括：

1. `Not Started`
2. `Running`
3. `Completed`
4. `Delayed`
5. `Slow Progress`
6. `No Recent Update`
7. `Overproduced`

判定原则：

1. 实际量超过计划量 102% 以上：
   - `Overproduced`
2. 实际量已达到计划量：
   - `Completed`
3. 有开始或已有产量：
   - `Running`
4. 已超过计划结束时间但还没完成：
   - `Delayed`
5. 已经走了很多时间，但完成比例明显落后于时间比例：
   - `Slow Progress`
6. 已到结束时间仍无更新：
   - `No Recent Update`

## 12.4 执行偏差异常

同步执行反馈时，APS 会自动维护这几类异常：

1. `Slow Progress`
2. `Delayed Execution`
3. `No Recent Update`
4. `Actual Output Mismatch`

但 APS 不会自动把正式工单或正式白夜班排产单改掉。

它只会：

1. 暴露异常
2. 提示风险
3. 引导 PMC 再生成新的建议 run 或变更建议

---

## 13. 推荐日常操作路线

下面给一条最推荐、也最符合你们当前流程的日常路线。

## 13.1 场景举例

假设今天是 `2026-04-23`，客户 A 发来新的 2 周交付计划，PMC 需要据此更新后续几天的正式工单和白夜班排产。

### 第一步：导入客户排期

入口：

1. `Schedule Import & Diff`

操作：

1. 选择 `Customer`
2. 选择 `Company`
3. 输入 `Version No`
4. 上传 Excel
5. 先 `Preview`
6. 检查 `Added / Advanced / Delayed / Reduced / Cancelled`
7. 确认无误后正式导入

系统结果：

1. 创建 `APS Schedule Import Batch`
2. 创建新的 `Customer Delivery Schedule`
3. 旧版本转 `Superseded`
4. 新版本为 `Active`

### 第二步：重建需求池并重算净需求

入口：

1. `Customer Delivery Schedule` 表单按钮
2. 或 `Net Requirement Workbench`

操作：

1. 点击 `重建需求池`
2. 再点击 `重算净需求`

系统结果：

1. 活跃客户排期进入 `APS Demand Pool`
2. 未被活跃排期覆盖的 SO backlog 作为补充需求进入
3. 安全库存不足的物料进入
4. 生成新的 `APS Net Requirement`

### 第三步：生成 APS Trial Run

入口：

1. `Net Requirement Workbench`
2. 或 `APS Run Console`

操作：

1. 选择公司、车间、视窗天数
2. 需要时可限定客户或物料
3. 点击 `Run Trial`

系统结果：

1. 生成 `APS Planning Run`
2. 生成 `APS Schedule Result`
3. 生成 `APS Schedule Segment`
4. 生成 `APS Exception Log`

PMC 此时要重点看：

1. 有哪些 `Blocked`
2. 哪些结果 `unscheduled_qty > 0`
3. 有没有 `FDA Conflict`
4. 有没有 `Late Delivery Risk`
5. 模具依据是否正确
6. 有没有短批量高换模风险

### 第四步：审核并调整计划口径

入口：

1. `APS Planning Run`
2. `Machine Schedule Gantt`
3. `Release & Exception Center`

操作建议：

1. 先处理 `Blocked`
2. 再处理明显不合理的风险段
3. 必要时做人工调机 / 改顺序
4. 补写结果备注和段备注

注意：

1. 没有模具主数据的物料，Trial 可看，但不能通过正式审批。
2. 同机主段重叠也不能审批通过。

### 第五步：Approve Run

操作：

1. 点击 `Approve Run`

系统会再做两道门：

1. 模具主数据门
2. 主段重叠门

全部通过后，Run 才会变成 `Approved`。

### 第六步：生成工单建议批次

操作：

1. 在 `APS Planning Run` 或 `APS Run Console` 点 `Generate Work Order Proposals`

PMC 在批次里逐行审核：

1. 哪些是 `New`
2. 哪些是 `Keep Existing`
3. 哪些是 `Update Existing`
4. 哪些需要 `Create Delta`
5. 哪些应当 `Close Residual / Cancel Unstarted`

只有把要落地的行改成 `Approved` 后，才点 `Apply Approved Rows`。

系统结果：

1. 创建 / 提交正式 `Work Order`
2. 或把现有工单与 APS 结果绑定 / 更新
3. 或关闭未执行残量、取消未开工工单

### 第七步：生成白夜班排产建议

操作：

1. 生成 `APS Shift Schedule Proposal Batch`
2. 审核每个段是否落到正确的日期、班次、机台
3. 把同意落地的行改成 `Approved`
4. 点击应用

系统结果：

1. 正式创建或更新当天 `Work Order Scheduling`
2. 在 `Scheduling Item` 上回写 APS 追踪字段
3. 在 `APS Schedule Segment` 上写回正式工单、排产单、排产行关联

### 第八步：现场执行后同步 APS 执行反馈

操作：

1. 每天至少 1 次执行 `Sync Execution Feedback`
2. 在 `Release & Exception Center` 或 `APS Run Console` 看执行健康摘要

看什么：

1. 运行中段数
2. 延误段数
3. 无更新段数
4. 今日入库笔数

如果发现偏差，再决定是否发起新的变更申请或重排建议。

---

## 14. 特殊情况流程

## 14.1 插单

推荐做法：

1. 先建 `APS Change Request`
2. 类型选插单
3. 填客户、物料、数量、要求日期
4. 先做影响分析

调用的是：

`analyze_insert_order_impact(...)`

系统会返回：

1. 候选模具
2. 候选 lane
3. 是否会并行拆分
4. Family 联产副产出
5. 可能被挤占的段
6. 影响到哪些客户
7. 额外换模 / 换色成本

实际处理建议：

1. 如果只是建议层，还没落正式白夜班排产：
   - 先改 APS 建议
2. 如果工单已正式生成但未进入执行：
   - 优先调整白夜班建议
   - 必要时再补一张紧急工单建议
3. 如果正式排产已进入 `Material Transfer / Job Card / Manufacture`：
   - 不直接覆盖
   - 只能做残量补排、追加紧急工单、或后续班次补救

## 14.2 提前交货

本质上和插单类似，只是来源是原有需求日期前移。

建议流程：

1. 建 `APS Change Request`
2. 先分析 impact
3. 看是否需要挪动当前白夜班建议
4. 已冻结的正式班次不直接覆盖

## 14.3 延期

延期的首选处理原则是：

1. 优先改 APS 建议或白夜班建议
2. 少动已正式提交的工单

如果工单未开始：

1. 可以让后续建议不再优先排它

如果工单已开始：

1. 不做取消
2. 视情况转为库存或保留执行

## 14.4 减量

当前减量动作不会自动改正式工单。

原因：

1. 为了保留追溯
2. 避免后台悄悄把正式单据改掉

因此当前流程是：

1. 工单建议批次里会识别成 `Close Residual` 或 `Cancel Unstarted`
2. 已执行量不会被回滚
3. 系统会优先关闭未执行残量或取消未开工工单
4. 若涉及已开工边界、冻结区或现场例外，再由 PMC / 管理层人工确认

## 14.5 删单 / 取消

同样：

1. APS 不会自动后台取消正式工单
2. 不会自动覆盖已执行的正式白夜班排产单

建议按以下口径处理：

1. 未开工、未冻结：
   - 可从建议层取消
2. 已生成工单但未执行：
   - 人工审核是否取消工单
3. 已进入执行：
   - 不直接取消
   - 评估转库存风险或残量收尾

## 14.6 模具主数据缺失

表现：

1. Trial Run 中该物料结果 `Blocked`
2. 异常会显示：
   - `Mold Master Missing`
   - `Mold Product Missing`
   - `Mold Cycle Missing`
   - `Mold Reference Empty`

处理方式：

1. 去 `Mold` / `Mold Product` 补齐主数据
2. 再重新 Run

结论很明确：

1. 没有完整模具主数据，APS 不应正式排产。

## 14.7 FDA 冲突

表现：

1. 自动排程直接 blocked
2. 异常为 `FDA Conflict`

处理方式：

1. 改用 FDA 合格机台
2. 或修正机台风险类别映射

## 14.8 正式白夜班排产已冻结

如果当天正式 `Work Order Scheduling` 已进入：

1. `Material Transfer`
2. `Job Card`
3. `Manufacture`

则 APS 会阻止覆盖。

正确动作是：

1. 生成残量重排建议
2. 调整后续班次
3. 不直接抹掉现场已执行班次

---

## 15. 页面与入口建议用法

## 15.1 Schedule Import & Diff

适合谁：

1. Sales
2. CS
3. PMC

最常用动作：

1. 预览导入
2. 正式导入
3. 看版本差异

新增导入能力：

1. 现在支持两类 Excel 导入口径：
   `长表`
   一行就是一条需求，直接按 `item_code / schedule_date / qty` 解析。
   `横表`
   一行是一个物料块，日期横向铺开；系统会先按映射识别日期列，再展开成逐日需求行。
2. 上传 Excel 后可先选择：
   `Sheet`
   `表头行号`
   `数据起始行号`
   `物料引用列`
   `行类别列`
   `需求行类别值`
   `日期列模式`
3. 对类似客户送货排期横表，建议默认：
   `解析模式 = 横表`
   `行类别列 = 类别`
   `需求行类别值 = 交货排期数量`
4. 系统会先读取工作簿并给出：
   可选 Sheet
   列位建议
   日期列识别结果
   示例行预览
5. 导入前必须同时确认：
   `排期范围`
   `导入策略`
   其中：
   `替代同范围`
   只替代同 `customer + company + schedule_scope` 下的旧生效排期。
   `按物料日期局部更新`
   只对当前导入里出现的 `物料 + 日期` 做覆盖，其余旧排期保留。
   `追加`
   不替代旧排期，直接把当前导入作为同客户下新的生效排期追加进去。
6. `待导入列表` 现在已经是可编辑的导入工作台，不只是只读预览：
   工具栏可新增明细行
   行级可编辑数量、日期、备注等字段
   行级可删除
   右键行也可直接打开编辑/删除快捷菜单
7. 预览区会保留：
   `序号`
   `Excel 行`
   `来源标记`
   `人工调整原因`
   这样导入前就能追溯每一条明细来自原始 Excel 还是人工调整。
8. 预览通过后，才正式导入到 `Customer Delivery Schedule`。

## 15.2 Net Requirement Workbench

适合谁：

1. PMC
2. 物控

最常用动作：

1. 重建需求池
2. 重算净需求
3. 从当前筛选上下文直接发起 Trial Run

## 15.3 APS Run Console

适合谁：

1. PMC
2. 管理层

最常用动作：

1. 新建 / 查看 Trial Run
2. 查看执行健康摘要
3. 进入 Gantt
4. 进入异常中心
5. 进入工单建议 / 班次建议审核

## 15.4 Machine Schedule Gantt

适合谁：

1. PMC
2. 生产主管

最常用动作：

1. 看机台时间轴
2. 点单段看详情
3. 写段备注
4. 看 `segment_name`
5. 搜索 `segment_name` 并高亮定位
6. 看来源、模具、工单、排产单、入库单 link
7. 做人工改机 / 改顺序 / 拉伸预检
8. 用图表区全屏和缩放看高密度排程

## 15.5 Release & Exception Center

适合谁：

1. PMC
2. 生产
3. 仓库
4. 管理层

最常用动作：

1. 看 blocker
2. 看执行偏差
3. 看工单建议批次
4. 看白夜班建议批次
5. 打开 `处理建议`
6. 看结构化根因说明
7. 跳到具体结果和 Gantt 上下文

当前页面中：

1. `插单影响分析` 是页面级独立工具
2. 不再和异常处理混在一起
3. 异常行级动作固定为：
   - `来源`
   - `处理建议`

---

## 16. 当前最重要的管理规则

当前 APS 一期请务必按下面规则理解和使用。

1. 计划口径由客户排期驱动，SO 更多是边界和兜底，不是短期主驱动。
2. 只有 `Plastic Part` 和 `Sub-assemblies` 会进入正式 APS 排产。
3. 模具主数据是硬前提，没有就不能正式审批和下推。
4. `APS Mould-Machine Rule` 只是限制规则，不是模具主数据替代品。
5. FDA 冲突、模具不可用、主段重叠，这些都是硬 blocker。
6. 工单必须先过建议审核，不能 Run 一审批就直接后台下单。
7. 减量和取消默认不自动改正式工单，避免破坏追溯。
8. 白夜班正式排产一旦进入执行状态，APS 不直接覆盖。
9. APS 负责滚动监控和建议，不自动改正式执行单据。
10. 结果备注和段备注是正式信息，不是临时聊天记录，建议 PMC 和 MC 都养成维护习惯。

---

## 17. 常见问题

### 17.1 为什么客户排期和 SO 都有，但需求池没有重复放大

因为当前逻辑里，只要同 `company + customer + item` 已有 `Active` 客户排期，SO backlog 就不再重复补进需求池。

### 17.2 为什么 Trial Run 能看到某物料，但 Approve 不让过

因为 Trial 允许你看 blocked 项和问题清单，但正式审批会被模具主数据门、主段重叠门拦住。

### 17.3 为什么有时会更新老工单，有时会新开一张增量工单

因为当前版本把 `Work Order` 视为稳定执行容器：

1. 若仍属于同一连续生产 campaign，且不跨执行边界，系统优先 `Update Existing`
2. 若增量跨冻结区、跨执行边界，或不适合改原容器，则拆成 `Create Delta`

### 17.4 为什么取消 / 减量不会回滚已生产数量

因为当前版本默认：

1. 已生产量永远不可回滚
2. 未开工部分可 `Cancel Unstarted`
3. 已开工但未执行完的部分可 `Close Residual`
4. 只有遇到冻结执行层或现场例外时，才要求人工审核

### 17.5 为什么正式白夜班排产有时不能回写

通常是因为那张 `Work Order Scheduling` 已进入：

1. `Material Transfer`
2. `Job Card`
3. `Manufacture`

此时 APS 视为执行冻结。

### 17.6 为什么有些段会出现 Family 联产，但不生成独立主排程

因为 `Family Co-Product` 本质是跟随主物料 cycle 一起产出的联产结果，不是独立主段。

---

## 18. 每日快速检查清单

PMC 每天建议至少做下面 8 件事：

1. 看当天是否有新的客户排期版本。
2. 重建需求池和净需求。
3. 对新增或变更量大的物料重跑 Trial Run。
4. 先处理 `Blocked` 和 `Critical` 异常。
5. 审核工单建议批次。
6. 审核当天和未来短窗的白夜班建议批次。
7. 同步执行反馈。
8. 看延误、慢进度、无更新段，决定是否发起重排建议。

---

## 19. 一句话总结

当前 APS 的真实工作方式不是“系统自动把一切都做完”，而是：

`先把需求来源讲清楚 -> 再把净需求算清楚 -> 再把模具和机台约束排清楚 -> 再由 PMC 审核工单和白夜班建议 -> 正式落地执行 -> 再滚动监控偏差并给出重排建议`

这正是它在你们现场最适合的定位：不是替代管理，而是把计划、审核、追溯和异常控制真正拉回到系统里。
