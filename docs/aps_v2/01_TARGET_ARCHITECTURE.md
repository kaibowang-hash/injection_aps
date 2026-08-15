# APS V2 目标架构与数据模型

## 1. 目标调用链

```text
Schedule Revision Service
→ Demand Ledger
→ P0 Baseline Planning
→ Admission Suggestions (P1/P2)
→ BOM/Campaign Expansion
→ Solver Input Snapshot
→ Capacity + Sequence Solver
→ Independent Validator
→ Risk/Resolution Workflow
→ Apply Commitments/Segments
→ WO Proposal
→ Shift Replan/WOS Proposal
→ Execution + Delivery Fulfillment
→ Progress Projection
```

现有 `services/planning.py` 保留 API Facade，禁止继续把所有 V2 实现堆进该文件。

## 2. 服务模块

| 模块 | 唯一职责 | 不得承担 |
|---|---|---|
| `services/schedule_revision.py` | 导入模式推荐、Identity、Revision、重叠差异 | 排产和资源求解 |
| `services/demand_ledger.py` | Commitment、库存覆盖、跨 Run 所有权 | UI 拼装 |
| `services/demand_admission.py` | P0/P1/P2 候选和决策 | 机器选择 |
| `services/delivery_fulfillment.py` | DP/DN 需求血缘、后备匹配 | 改写 ERPNext 原生 SO 履约事实 |
| `services/bom_planning.py` | 半成品递归、Pegging、环检测 | 原料可用性硬约束 |
| `services/campaign_planning.py` | Family Mold、Campaign、输出和循环 | 重复占用资源 |
| `services/solver/input_builder.py` | 不可变求解输入、整数化、指纹 | 保存正式结果 |
| `services/solver/capacity_solver.py` | 班次桶数量分配 | Frappe 文档写入 |
| `services/solver/sequence_solver.py` | Campaign 精确排序、换模 | 修改冻结任务 |
| `services/solver/validator.py` | 数量、资源、依赖独立校验 | 自动修复非法解 |
| `services/run_transition.py` | Frozen/Carried/Reschedulable 和所有权转移 | 求解目标定义 |
| `services/shift_replan.py` | 实际刷新、Forecast、班次差异 | 自动 Apply |
| `services/constraint_resolution.py` | Blocker 分类、Override、Exclude | 通用强制通过 |
| `services/progress_v2.py` | 明细、矩阵、下钻、Projection | 修改业务单据 |

现有模块改造：

- `capacity_balance.py`：保留班次/日历基础能力，删除材料限量和材料指纹，最终由 solver 服务取代主分配逻辑。
- `delivery_sync.py`：从“排期日期＋SO 必须相等”改为显式 Demand Identity/DP 血缘优先。
- `execution_sync.py`：更新 Commitment、Campaign Output、Forecast、Replan Cycle。
- `consistency.py`：增加跨 Run 所有权、Campaign 单一 capacity owner、BOM precedence。
- `change_engine.py`：操作 Campaign 主 Segment，同步派生输出。
- `setup/resources.py`：只通过 Custom Field 扩展外部/标准 DocType。
- `api/app.py`：提供 V2 API，旧 API 保持兼容 Facade。

## 3. 核心实体

### 3.1 APS Demand Identity（新）

跨排期版本稳定身份。

| 字段 | 类型/约束 |
|---|---|
| name | UUID/Data |
| company | Link Company, required |
| customer | Link Customer, required |
| schedule_scope | Data, required |
| item_code | Link Item, required |
| customer_part_no | Data |
| external_line_reference | Data |
| current_schedule | Link Customer Delivery Schedule |
| current_schedule_item | Link Customer Delivery Schedule Item |
| status | Active/Closed/Cancelled |
| first_seen_on / last_revised_on | Datetime |

优先使用客户永久行 ID。没有时生成 UUID，通过旧行、`previous_schedule_date` 和人工消歧继承。交期和数量不得放进不可变身份。

### 3.2 Customer Delivery Schedule（扩展）

- `revision_mode`
- `recommended_revision_mode`
- `recommendation_reason`
- `supersedes_schedule`
- `effective_from/effective_to`
- `mode_confirmed_by/mode_confirmed_on`
- `source_contract`
- `revision_fingerprint`

Item 子表扩展：

- `demand_identity`
- `previous_schedule_item`
- `revision_action`
- `original_schedule_date/effective_schedule_date`
- `effective_qty/executed_floor_qty/excess_qty`
- `delivery_match_status`

### 3.3 APS Demand Commitment（新）

每个 Run 对 Demand Identity 的数量承诺快照和所有权记录。

| 字段组 | 字段 |
|---|---|
| 来源 | planning_run, source_run, demand_identity, schedule_item |
| 分类 | admission_class, service_priority, owner_state, execution_state |
| 时间 | original_due_date, effective_due_time |
| 数量 | requested_qty, stock_covered_qty, carried_qty, newly_planned_qty, on_time_qty, late_qty, unscheduled_qty, produced_qty, delivered_qty, remaining_qty |
| 状态 | Draft/Proposed/Approved/Released/In Progress/Completed/Cancelled/Excess |

约束：相同 Demand Identity 的同一数量不能同时有两个 `Owned` Formal Commitment。新 Run 在公司锁内 Transfer 或 Reference。

### 3.4 APS Demand Admission（新）

保存 P0/P1/P2 Run 级准入：source、class、candidate/recommended/selected qty、mandatory、推荐原因、利用率收益、减少换模、库存天数、选择人和说明。

P0 自动锁定；P1/P2 默认不选。

### 3.5 APS Stock Coverage Allocation（新）

防止同一成品库存被多个需求或多个 Run 重复扣减。

字段：company、item、warehouse、demand_identity、commitment、owner_run、allocated_qty、consumed_qty、released_qty、status、source_snapshot_time、fingerprint。

生产入库后，实际良品进入库存覆盖或形成可交库存时，不能同时在 Commitment 中作为额外 produced coverage 重复扣减。

### 3.6 APS Constraint Resolution（新）

字段：run/result/commitment、blocker_code/category、overrideability、action、before/after JSON、reason、expiry、requester/approver、status、recomputed fingerprint。

### 3.7 APS Replan Cycle（新）

字段：company、plant_floor、baseline_run、shift date/type、execution cutoff、freshness、cycle type/status、solver metrics、变更数量、WO/Shift Proposal、用户、input/solution fingerprint。

幂等键：`company + plant_floor + shift_date + shift_type + execution_cutoff + baseline_run`。

### 3.8 APS Production Campaign（新）

一次真实机器＋模具生产活动。

- run、campaign_key、machine、mold、floor
- start/end、planned/actual cycles
- capacity_owner_segment
- family flag、status、source summary
- linked WOS

子表 `APS Campaign Output`：item、Primary/Co-product、output per cycle、planned/demand/excess、commitment、WO、Result、actual good/scrap、note。

### 3.9 APS BOM Pegging（新）

parent/child commitment、parent/component item、BOM、level、qty per parent、loss、required/stock/production qty、required available time、status。

## 4. 现有实体扩展

### APS Planning Run

- demand/freeze/restricted/recovery horizon
- baseline_run/latest_replan_cycle
- solver_engine/status/runtime/quality JSON
- release_readiness
- on_time/late/recovery/critical unplanned totals
- acknowledgment/unresolved blocker counts

保留旧 horizon 和 capacity status 字段作迁移镜像。

### APS Schedule Result

- commitment、admission/service priority
- original/effective due
- promised/recovery completion
- on-time/recovery/critical-unplanned qty
- shortage code/explanation
- acknowledgment、solver decision JSON

### APS Schedule Segment

- campaign、source run、baseline segment
- execution state
- baseline/current/forecast/solver start/end
- capacity owner
- assignment reason、replan cycle

### Work Order

新增 custom APS commitment、campaign、output role、capacity owner、source reason。SO/SO Item 不要求。

### Scheduling Item

新增 commitment、campaign、output role、capacity owner、replan cycle。

### Delivery Plan Item Qty / Delivery Plan Item

新增 demand identity、schedule item、commitment、required delivery date、match method。现有 FIFO SO 分配继续使用。

### Delivery Note Item

保留 schedule item，新增 demand identity、commitment、delivery plan detail。

## 5. 数据流和所有权

### 5.1 Revision

导入创建不可变历史排期，Demand Identity 指向当前有效行。替换/部分修订只改变当前 Revision，不删除历史。

### 5.2 Production

Commitment → Result → Campaign/Segment → WO → WOS/Scheduling Item → Stock Entry → Production Allocation/Commitment。

### 5.3 Delivery

Demand Identity → Delivery Plan Qty → DP SO Item 分配 → Delivery Note Item → APS Delivery Allocation。

DP 表示计划；提交 DN 表示实际。

### 5.4 Run Transition

- Frozen：旧 Run 保持资源和供给所有权，新 Run 引用。
- Carried：所有权原子转给新 Run。
- Reschedulable：数量转移后重新求解。
- Completed：通过库存/交付事实覆盖。
- Excess：独立记录，不作为原需求的未完成量。

## 6. 索引、锁和幂等

必要索引：

- Demand Identity：company/customer/scope/item/status；外部行 ID 唯一索引。
- Commitment：identity/status/owner；run/class。
- Stock Coverage：item/warehouse/status；identity/owner run。
- Admission：run/mandatory/selected。
- Campaign：run/machine/start/end；run/mold/start/end。
- Pegging：run/parent/child。
- Allocation：identity/source detail/effective。

锁顺序：Company → Planning Run/Replan Cycle → Demand Identity/Commitment → Resource rows → Proposal/WO/WOS → Allocation rows。

API 幂等键必须包含业务范围和 input fingerprint。重试不得创建重复 Revision、Commitment、Campaign、WO 或 WOS。

## 7. Feature Flags

- `enable_aps_v2`
- `solver_engine = Legacy/CP-SAT`
- `enable_shift_replan`
- `enable_coproduct_campaign`
- `enable_multilevel_bom_planning`
- `delivery_legacy_match_tolerance_days`
- `max_execution_staleness_minutes`
- `solver_time_limit_seconds`
- `shift_solver_time_limit_seconds`

同一 Demand Identity 不能同时存在 Legacy Formal 和 V2 Formal owner。
