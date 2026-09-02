# Phase 0 交接记录（IN_PROGRESS）

> 历史快照：本文件已由 [Phase 0 COMPLETE 交接](./phase_00_complete_2026-08-14.md) 取代，不得继续把其中的旧 Blocker 当作当前状态。

## 1. 目标与结果

- Phase：0 Baseline。
- 状态：IN_PROGRESS。
- 完成目标：生产/前端保护；默认关闭 V2 配置；统一 Flag Reader；只读 Legacy 快照与稳定指纹；Comparison API 壳；11 场景只读目录；静态和 mock 门禁。
- 未完成目标：隔离数据库 Fixture、迁移重跑、Legacy 集成/权限/UI/性能回归、OR-Tools 兼容性。

## 2. 修改范围

- Schema：APS Settings 新增隐藏 V2 字段，所有功能开关默认 0，Solver 默认 Legacy。
- Service：`v2_flags.py`、`v2_baseline.py`。
- API：`get_v2_capabilities`、`capture_legacy_baseline`、`get_legacy_v2_comparison`；均只读且受 APS/Run scope 权限保护。
- UI：没有新按钮、页面或操作变化；V2 配置字段在 Phase 0 隐藏。
- 前端保护：应用的 recurring `after_migrate` 不执行任何隐式站点写入；标准 Workspace/Page 使用非刷新时间戳，Custom HTML 只在缺失时创建。
- Fixture：仅数据目录，不允许写库。

## 3. 关键实现决策

- ADR-016：`jce.1` 开发零触碰，已有前端定制只保留不覆盖。
- Legacy baseline 不在 Phase 0 API 内落库或写文件；返回规范化内容和 SHA-256，由调用者在获批环境归档。
- 缺少新 schema 时 Flag Reader 仍以关闭值运行，避免为了能力探测先 migrate。
- Phase 0 不导入、不运行 CP-SAT，也不生成正式 V2 数据。

## 4. 测试证据

- Baseline：83 tests / 17 个无 site context 环境错误，已记录。
- Unit：16/16 通过（含 14 个新增测试和 2 个既有兼容测试）。
- Static/JSON/compile：通过。
- Integration/Permission/UI/Migration：未执行，等待获批隔离副本。
- Feature Flag Off：纯单元契约通过；实际站点回归未执行。
- Feature Flag On：只证明 formal writes 始终 False；未运行 Solver。

详细证据见 [2026-08-14 static evidence](../evidence/phase_00/2026-08-14_static.md)。

## 5. 数据验证

- 需求数量守恒：尚未在 DB Fixture 验证。
- 资源不重叠：尚未在 DB Fixture 验证。
- 跨 Run 所有权：场景已登记，尚未实现 V2。
- Delivery/Production 血缘：Legacy 快照字段已覆盖，实际数据未采集。

## 6. 已知限制和 Blocker

- 不得使用 `jce.1`。
- `jce-test.1` 未获写入授权。
- 当前 bench 没有 OR-Tools，且不能在生产环境直接安装。
- 旧 `tests/phase0_baseline.py` 只允许 `aps-opt-test.localhost`，当前并不存在该站点；不要为了通过检查放宽其站点保护。

## 7. 回退

- 所有 V2 Flag 默认关闭；新增 schema 可保留。
- 本阶段未创建 V2 正式单据，也未写任何站点数据。
- UI 无新增入口，不需要资产回退。

## 8. 下一步输入

- 创建生产脱敏、完全隔离且不会共享生产 assets/process/cache 的测试 bench/site，或由用户明确指定授权目标。
- 在隔离环境执行两次 migrate、Patch 重跑、11 场景 Fixture、Legacy Flag-Off 回归和实际 baseline 捕获。
- 单独验证并锁定 OR-Tools 版本；完成前不得进入 Phase 1。
