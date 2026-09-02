# Phase 0：基线、护栏和执行框架

## 1. 必读

- `../AI_EXECUTION_PROTOCOL.md`
- `../PRODUCTION_SAFETY.md`
- `../00_GLOBAL_CONTRACT.md`
- `../DECISIONS.md`
- `../04_MIGRATION_TEST_ROLLOUT.md`

## 2. 目标

在不改变任何正式 APS 行为的前提下，建立 V2 Feature Flag、测试数据、数量基线、性能基线和 Legacy/V2 对比框架。后续 Phase 不得在没有本阶段证据的情况下开始。

## 3. 前置条件

- 无；这是唯一可在其他 Phase 未完成时执行的阶段。
- 先记录工作树中用户已有修改，避免覆盖。

## 4. In Scope

1. APS Settings 增加 V2 开关和默认参数，但默认全部关闭。
2. 建立可重复的生产场景 Fixture。
3. 为当前 V1 保存行为契约：需求、Net、Result、Segment、capacity、WO/WOS、delivery。
4. 建立 V1/V2 对比报告接口壳，不运行 V2 正式写入。
5. 建立测试/迁移/性能执行脚本和结果目录约定。
6. 验证当前 Python/Frappe 环境能否安装并导入选定 OR-Tools 版本；此阶段不启用 Solver。
7. 清除 recurring migrate 对已有前端定制的覆盖路径，并用静态/mock 测试锁定 create-if-missing 行为。

## 5. Out of Scope

- 不创建 Demand Identity/Commitment。
- 不改变现有排程、材料、Delivery 或状态逻辑。
- 不生成任何 V2 Formal 单据。
- 不在 `jce.1` 或未经明确授权的站点运行 Patch、Fixture、DB 测试、migrate、build、restart 或 clear-cache。

## 6. Schema/配置

APS Settings：

- `enable_aps_v2 = 0`
- `solver_engine = Legacy`
- `enable_shift_replan = 0`
- `enable_coproduct_campaign = 0`
- `enable_multilevel_bom_planning = 0`
- `delivery_legacy_match_tolerance_days`
- `max_execution_staleness_minutes`
- `solver_time_limit_seconds = 120`
- `shift_solver_time_limit_seconds = 30`

使用幂等 Patch 和 `services/customizations.py` 默认值；不得改变现有 planning horizon 值。

## 7. 代码任务

- 新增 `services/v2_flags.py`，唯一负责 V2 开关读取。
- 新增 `services/v2_baseline.py`，生成不可变的 V1 快照和对比结构。
- API：`get_v2_capabilities`、`capture_legacy_baseline`、`get_legacy_v2_comparison`（后者先返回 Not Available）。
- 建立测试 fixture builder，不复用生产文档名称。
- 对当前关键函数增加契约测试，不重构业务代码。
- `after_migrate` 不刷新 Workspace/Custom HTML Block，不重排 Item `field_order`；前端资源只允许首次缺失时创建。

## 8. 必备 Fixture

- 重叠排期 A/B；
- 无 SO 客户排期；
- 逾期需求；
- 1200 需求/1000 产能；
- A/B 两模单机；
- 两个重叠 Run；
- 延期正式 WOS；
- Family Mold；
- C→A→X；
- Delivery Plan→多个 SO→DN；
- 原料为 0。

## 9. 测试

- Feature Flag 默认关闭。
- Flag 关闭时关键 API、状态和生成单据与 Phase 前一致。
- Patch 重跑。
- Fixture 创建/清理幂等且只影响测试前缀。
- 记录当前测试全量结果、运行时间和已知失败。

## 10. Exit Criteria

- 所有开关默认关闭。
- Legacy baseline 已存档并可重复生成。
- 11 个核心 Fixture 可重复创建。
- OR-Tools 兼容性有明确结论；不兼容则 ADR，不得静默换模型。
- `IMPLEMENTATION_STATUS`、TRACEABILITY 和 Handoff 已更新。

## 11. 回退

关闭开关即可；新增只读配置和测试能力可以保留。不得删除用户业务数据。
