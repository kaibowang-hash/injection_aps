# Phase 5 交接记录（COMPLETE）

## 状态

- Phase：5 Shift Replan。
- 状态：COMPLETE。
- 证据：[Phase 5 isolated evidence](../evidence/phase_05/2026-08-14_isolated.md)。
- 安全状态：`enable_shift_replan=0`，全部 V2 Flag 关闭，`jce.1` 零触碰。

## 已交付

1. Actual cutoff/freshness、稳定实际速率与标准速率回退确认。
2. 良品剩余公式、Forecast end、同机/同模延期传播和本地 CP-SAT。
3. Original/Current/Forecast 分层与执行/Frozen 不移动。
4. Scheduled Shift、Emergency Manual、幂等 Cycle 和显式审计。
5. Work Order 数量边界与 Shift Schedule 时间建议分离；WOS Proposal 审核后才原子 Apply Current。
6. 完整 Shift Replan Center、权限、API、中文、迁移和 Flag-Off 验证。

## 下一 Phase 输入

- Campaign 只允许一个 capacity owner；派生输出不能形成第二个 Replan task。
- Campaign 的多个输出 WO/WOS 需要共享同一 `custom_aps_replan_cycle`。
- Phase 6 Gantt 先保留数据接口；单条 Campaign 多输出的最终 UI 在 Phase 8 完成。
