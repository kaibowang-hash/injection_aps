# Phase 3 交接记录（COMPLETE）

## 状态

- Phase：3 Horizon/Status/Material
- 完成时间：2026-08-14 20:02 +08:00
- 证据：[Phase 3 isolated evidence](../evidence/phase_03/2026-08-14_isolated.md)
- 安全状态：全部 V2 Flag 关闭，Solver 为 Legacy，生产站点 `jce.1` 零触碰。

## 已交付

1. 精确 Demand/Freeze/Restricted/Recovery 视窗和逾期 P0 分类。
2. Ready/Acknowledgment Required/Hard Blocked/Applied with Exceptions 状态模型。
3. 显式 Blocker Registry、Constraint Resolution DocType/API/Page、临时覆盖双人高风险控制、到期和指纹保护。
4. Commitment 级排除及剩余可行计划部分应用。
5. Material advisory-only；不参与数量、状态、指纹或 Apply 锁。
6. Run 表单的时间视窗、就绪原因、风险确认和约束处理入口；V2 Flag Off 不显示新增动作。
7. Patch、中文翻译、权限、静态/集成/全量回归证据。

## Phase 4 入口契约

- 只在 `solver_engine=CP-SAT` 且 V2 总开关开启时进入新 Solver；Legacy 路径必须保持原样。
- Solver 读取 Phase 2 Commitment 和 Phase 3 时间/状态契约；不得重新定义需求量或把 Recovery 当新需求。
- Frozen execution 不得移动；产能欠量仍是 Acknowledgment，不得升格为不可覆盖硬阻塞。
- 原料继续 advisory-only；Phase 4 不得把材料库存加入 Solver hard constraint。
- 所有新 Solver 结果必须可解释、确定性、可复现，并保留 fallback/timeout 证据。
