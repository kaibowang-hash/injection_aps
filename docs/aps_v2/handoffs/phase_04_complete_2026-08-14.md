# Phase 4 交接记录（COMPLETE）

## 状态

- Phase：4 CP-SAT 有限产能求解。
- 状态：COMPLETE。
- 证据：[Phase 4 isolated evidence](../evidence/phase_04/2026-08-14_isolated.md)。
- 安全状态：全部 V2 Flag 关闭，Solver 为 Legacy，`jce.1` 零触碰。

## 已交付

1. 不可变整数 Solver snapshot、稳定输入/方案指纹和固定 seed。
2. CP-SAT 全局数量分配、精确机台/模具顺序、Frozen/NoOverlap、九层词典序目标。
3. 推荐、交付优先、换型最少三方案；前三层交付结果是效率方案不可恶化的硬门槛。
4. 独立 Validator、清晰 Fallback、方案选择/风险确认/取消/Apply 作业状态。
5. `APS Solver Job`、Run/Result/Segment 投影和事务锁定 Apply。
6. 方案比较页、Planning Run 条件入口、中文翻译、权限、迁移和 Flag-Off 回归证据。

## 测试摘要

- Phase 4 Unit/Contract 14/14，DB Integration 2/2，UI 17/17，Legacy 133/133。
- 全量 700 项只保留既有 3 failures/8 errors，无新增回归。
- 250 需求三方案紧缩预算基线 15.21 秒；超时路径返回经过 Validator 的明确 Fallback。

## 回退

- 设置 `solver_engine=Legacy` 或关闭 `enable_aps_v2` 即停止新 CP-SAT Formal 运算。
- Solver schema、快照和已 Applied 单据保留只读；不删除已释放 WO/WOS。

## Phase 5 输入契约

- 以 Applied Segment 的 Original/Current 层和执行快照作为重排基线。
- 已开工、已转料、已完成和冻结任务不可移动。
- 每班次自动动作只能生成 Proposal，不能自动修改正式排产。
- 执行数据过期或使用标准速率回退时必须明确标识并确认。
