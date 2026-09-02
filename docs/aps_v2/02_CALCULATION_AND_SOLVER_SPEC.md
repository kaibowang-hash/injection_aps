# APS V2 计算与求解规格

本文件是所有数量、时间、优先级和 Solver 规则的唯一事实来源。Phase 文件不得重写另一套公式。

## 1. Revision 计算

Full Replacement：新有效集合等于输入；旧集合未出现身份取消。

Partial Revision：输入行替换对应身份，未触及旧身份继续有效。

Incremental Demand：输入创建独立增量身份，不覆盖历史需求。

减少数量：

```text
executed_floor_qty = max(delivered_qty, irreversible_started_or_produced_qty)
excess_qty = max(executed_floor_qty - revised_qty, 0)
open_revised_qty = max(revised_qty - delivered_qty, 0)
```

允许保存客户真实修订，Excess 单独处理。

## 2. 交货归属

优先级：

1. DN Item 明确 Demand Identity。
2. 从 DP Item 继承明确 Identity/Commitment。
3. 旧 schedule item 明确链接。
4. Legacy Controlled Match。
5. Unallocated Delivery。

Legacy Match 使用 company/customer/item，先最早逾期，再同日，再容差内最近未来需求。多 Scope 歧义、UOM 不同、退货无血缘时禁止猜测。

```text
schedule_open_qty = max(effective_schedule_qty - effective_delivered_qty, 0)
```

DP 数量不是 delivered；只有提交 DN 的有效分配是实际交货。

## 3. 成品覆盖和新计划量

P0 分配成品顺序：逾期最久 → 原交期最早 → service priority → identity 稳定序。

```text
allocatable_fg
= eligible_actual_stock
- active_stock_coverage_allocations
```

```text
new_plan_qty
= max(
    schedule_open_qty
    - stock_covered_qty
    - effective_carried_commitment_remaining,
    0
  )
```

实际入库形成库存后，Produced 和 Stock Coverage 必须互斥，不得双扣。

## 4. P1/P2

```text
P1_framework_candidate
= max(
    customer_item_open_so_qty
    - customer_item_open_schedule_qty
    - active_P1_commitment_qty,
    0
  )
```

P1 还受最大提前天数、剩余产能和最大备货天数/数量限制，不受原料限制。

```text
P2_safety_candidate
= max(
    safety_target
    - projected_unallocated_fg_at_horizon_end,
    0
  )
```

P1/P2 先生成建议，用户选择后才进入最终求解。

## 5. 时间规则

```text
demand_horizon_end_date = start_date + horizon_days - 1
```

日期需求的精确 due time 必须来自 APS Settings 的显式规则，例如“交付日前最后生产班结束”，不能隐藏采用次日 00:00。

逾期：original due 保留，earliest start 设为当前可控制时间，标记 overdue at run start。

Recovery Horizon 只安排已准入欠量，不引入恢复区间新需求。

## 6. BOM

只展开配置为 APS 生产物料组的制造半成品。

```text
child_gross_qty
= parent_plan_qty
   × component_qty / bom_output_qty
   ÷ (1 - loss_percent)
```

```text
child_production_qty
= max(child_gross_qty - child_stock_coverage - child_effective_WIP, 0)
```

前置关系：`parent.start >= child.available_time`。BOM 环 Hard Blocked 且不可 Override。

## 7. 联产品

```text
required_cycles_i = ceil(required_qty_i / output_per_cycle_i)
campaign_cycles = max(required_cycles_i)
planned_output_i = campaign_cycles × output_per_cycle_i
excess_output_i = max(planned_output_i - demand_covered_i, 0)
```

Campaign duration：cycles × mold cycle + setup/changeover/first article。机器和模具占用只属于一个 capacity owner。

## 8. 执行 Forecast

```text
remaining_qty = max(planned_qty - actual_good_qty, 0)
effective_rate = recent_stable_actual_rate if sample_sufficient else standard_rate
forecast_end = controllable_start + remaining_qty / effective_rate + unfinished_setup
```

Forecast 只影响预测和重排建议；正式 Current Plan 只有 Apply Proposal 后改变。

## 9. 需求与分配目标分离

需求准入：P0/P1/P2。

P0 内服务顺序：

1. 冻结执行事实先占资源。
2. 最大化 P0 准时量。
3. 最小化 P0 加权延期。
4. 最小化 P0 Critical Unplanned。
5. 原交期更早。
6. 客户 service priority。
7. 资源稀缺度。

机器分配次级目标：计划稳定 → 换模/换色/换料 → 连续生产 → 吨位差 → 等价机利用率方差 → P1/P2 完成量。

## 10. CP-SAT 两层模型

### 10.1 Shift Bucket Allocation

决定 demand/campaign 在 machine+mold+shift 的生产 cycles/qty，处理班次、停机、准时、恢复和未排。

### 10.2 Campaign Sequencing

在机器班次内确定精确 start/end、顺序和 transition setup。

时间整数分钟，数量优先整数 cycle；非模具物料按冻结的 Stock UOM scale 整数化。

### 10.3 主要变量

- cycles[demand, alternative, shift]
- selected alternative
- campaign start/end
- on-time/late/unscheduled cycles
- sequence-before
- changeover-required
- machine-load-minutes

### 10.4 硬约束

1. Frozen 固定。
2. 同机器 capacity owner NoOverlap。
3. 同模具 NoOverlap。
4. 机器/模具合法适配，或存在已批准未过期 Override。
5. 停机/无班次容量为零。
6. Campaign 多输出共享 cycles。
7. BOM precedence。
8. 执行中任务不换机、不取消。
9. P0 数量严格守恒为 on-time + recovery late + critical unplanned。
10. P1/P2 不能降低已可获得的 P0 最优交付结果。

### 10.5 分层求解目标

按顺序求解并固定前一层最优值：

1. max P0 on-time qty
2. min P0 weighted tardiness minutes
3. min P0 critical unplanned
4. min changes outside freeze
5. min setup/changeover minutes
6. max campaign continuity
7. min tonnage gap
8. min equivalent-machine utilization variance
9. max selected P1/P2 completion

不得以单一未校准权重混合所有目标。

## 11. Solver 运行契约

- Full Run 默认 120 秒；Shift Replan 默认 30 秒，可配置。
- Optimal 和 Feasible 都可展示；非最优需显示 gap/quality。
- 无 Feasible 时不写正式 Segment。
- Legacy heuristic 只作明确标记的 Fallback/对比，不得伪装 CP-SAT 结果。
- 固定 seed 和输入排序。
- Solver 输出必须经独立 Validator。

## 12. Validator

检查：

- Demand/Commitment/Result/Segment 数量守恒；
- Machine/Mold NoOverlap；
- Frozen 未改变；
- Campaign capacity owner 唯一；
- BOM precedence；
- Horizon 和 mode 合法；
- P1/P2 未侵占 P0 最优交付；
- Override 存在、批准、未过期且包含在指纹；
- 同一需求无双重 Formal owner。

## 13. 状态计算

Ready：无待确认风险和非法输入。

Acknowledgment Required：有 late、unscheduled、prebuild、P1/P2、fallback rate、below batch、excess、排除项等业务风险。

Hard Blocked：非法/缺失输入、BOM 环、不可解决资源主数据、锁定重叠、指纹过期、血缘损坏。

产能不足只产生 late/critical unplanned 和 Acknowledgment，不是 Hard Blocked。

排除 Hard Blocked Commitment 后，其余可以 `Applied with Exceptions`；排除量继续作为下一 Run P0。
