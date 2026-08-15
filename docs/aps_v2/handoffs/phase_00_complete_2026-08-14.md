# Phase 0 交接记录（COMPLETE）

## 1. 完成范围

- 建立默认关闭的 APS V2 配置和唯一 Flag Reader。
- 建立只读 Legacy 快照、稳定指纹和 V1/V2 Comparison API 壳。
- 建立严格隔离守卫、生产数据只读基线、独立可写 Fixture 站点和证据目录。
- 建立 11 个可重复输入 Fixture、前缀完整清理和跨 Phase 场景契约。
- 完成两轮 migrate、Patch 重跑、Flag-Off、实际权限、前端定制和 OR-Tools 兼容验证。
- 修复 Phase 0 新增 APS Settings 的中文翻译门禁。

完整证据见 [2026-08-14 isolated evidence](../evidence/phase_00/2026-08-14_isolated.md)。

## 2. 当前行为边界

- `enable_aps_v2=0`；所有附属 Flag 均为 `0`；`solver_engine=Legacy`。
- Phase 0 没有 Demand Identity/Commitment，没有 V2 Formal 写入，没有新 UI 入口。
- 原有“发工单→工单排产→工单入库→出货计划分配订单→销售出库”流程没有被 V2 接管。
- 只读 API 受 APS Role、Planning Run 和 Company/Plant Floor scope 权限共同保护。
- `jce.1` 未迁移、未测试、未安装 OR-Tools、未构建或重启。

## 3. 下一 Phase 输入

- Fixture 站点：`aps-opt-fixture.localhost`，当前保留最终 11 场景输入。
- 场景目录：`injection_aps/tests/v2_scenario_catalog.py`。
- 安全 Builder：`injection_aps/tests/v2_fixture_builder.py`。
- 隔离、Legacy、权限和 Solver 门禁：`injection_aps/tests/v2_phase0_gate.py`。
- Phase 1 必须读取 `phase_01_revision_delivery.md`，仍保持全部 Flag Off，只实施 Revision/Demand Identity/Delivery lineage 的 Trial 能力。

## 4. 必须保留的已知基线

- Legacy Fixture 指纹：`17d8ef72a76739da2e0c848dff4b84befe1f6830412a64d1fba457e59ea0c5fe`。
- V2 输入 Fixture 指纹：`385c0a298529e377aab24dc341ff6818e9bd526eb3dcf093d4b7b047ac239b8f`。
- 全量测试：600 项，77.699 秒，3 failures、8 errors；详见隔离证据，不能静默忽略或顺手改写 Legacy 行为。
- OR-Tools 内嵌兼容边界：`9.4.1874` + protobuf `3.20.3`；更高版本走隔离进程。

## 5. 回退

- 关闭 Flag 已是当前状态；新增 schema 和只读工具可保留。
- Fixture 可在严格守卫下用 `cleanup_phase0_scenario_fixtures(remove_masters=True)` 完整清理；禁止在其他站点运行。
- Phase 0 没有生产 V2 单据或前端资源需要回退。
