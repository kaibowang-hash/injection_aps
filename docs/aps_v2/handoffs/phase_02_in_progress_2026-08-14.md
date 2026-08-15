# Phase 2 交接记录（IN PROGRESS）

## 1. 目标与当前状态

- Phase：2 Commitment/Admission。
- 状态：IN_PROGRESS。
- 开始时间：2026-08-14 18:15 +08:00。
- 目标：Demand Commitment、P0/P1/P2 准入、成品库存唯一覆盖、跨 Run 所有权和冻结/承接分类。
- Feature Flag：`enable_aps_v2=0`；不得产生 V2 Formal 写入。

## 2. 进入门禁

- Phase 0、Phase 1 均为 COMPLETE，并有隔离迁移、权限、回退和 Flag-Off 证据。
- 可写测试环境仅为 `/home/ubuntu/frappe-bench/isolated/aps-v2-bench` 的 `aps-opt-fixture.localhost`。
- 隔离数据库为 loopback `127.0.0.1:13306/aps_v2_fixture_260814`；scheduler、email、async 均关闭。
- `jce.1` 未运行测试、迁移、Patch、Fixture、build、restart 或 clear-cache。

## 3. 进入基线

| 模块 | 结果 |
|---|---:|
| `test_schedule_import_safety` | 9/9 PASS |
| `test_delivery_sync` | 5/5 PASS |
| `test_existing_work_order_policy` | 19/19 PASS |
| `test_work_order_lineage_and_shift_precision` | 17/17 PASS |
| `test_import_transaction_guards` | 83/83 PASS |
| 合计 | 133/133 PASS |

## 4. 当前执行边界

- 只实现 Phase 2 In Scope；不实现 Phase 3 状态/恢复视窗、Phase 4 CP-SAT 或 Phase 5 自动重排。
- 现有工单、工单排产、入库、出货计划分配 SO、下推销售出库流程保持不变。
- Admission Workbench 仅在 V2 Flag 开启时可进入；Flag Off 保持 Legacy 页面和 API 行为。
- 任何前端资源迁移均不得覆盖 Workspace、Custom HTML、Client Script、Property Setter 或用户布局。

## 5. 待完成

- Schema/Patch、Commitment/Admission/Stock Coverage 服务、Run Transition、API/权限/幂等、Workbench UI。
- Unit/Integration/Permission/UI、两轮迁移、Flag-Off、数量守恒、并发所有权、性能和回退证据。
- 完成后替换为 COMPLETE Handoff，并更新状态台账和追踪矩阵。
