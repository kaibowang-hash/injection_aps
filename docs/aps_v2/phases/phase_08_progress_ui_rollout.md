# Phase 8：Progress/Gantt V2、可用性收尾和正式切换

## 1. 必读

- 全部全局契约和 Accepted ADR
- `../03_UI_API_SECURITY_SPEC.md`
- `../04_MIGRATION_TEST_ROLLOUT.md`
- TRACEABILITY_MATRIX

## 2. 前置条件

Phase 0—7 Complete，所有后端事实和链接稳定。

## 3. 目标

完成统一追踪、直观 UI、下钻、翻译、性能、迁移演练和灰度切换，使 PMC 能在系统内解释客户排期、原计划、当前计划、预测、实际生产、出货计划、实际出货和欠量恢复。

## 4. In Scope

- Customer Schedule Progress V2 明细/矩阵。
- Gantt Original/Current/Forecast/Actual 完整图层。
- Run/Admission/Resolution/Scenario/Replan 页面 UX 收尾。
- 全链路下钻和导出。
- 工作区入口、中文翻译、帮助文本、空状态。
- 服务端聚合、分页、虚拟滚动和性能优化。
- 生产脱敏副本完整迁移演练。
- Trial 对比、单车间灰度、Formal 切换和回退演练。

## 5. Out of Scope

- 新业务模型和新求解目标。
- 为 UI 方便改变已经 Verified 的数量公式。

## 6. Progress 数据契约

每个 demand/date cell 返回：schedule、original/current plan、forecast、actual good/scrap、DP planned、DN delivered、stock covered、shortage/recovery、status/reason、source docs。聚合总量必须与 Commitment/Allocation 守恒。

一个页面可以选当前有效跨 Run Projection；不能默认只挑某个旧状态优先 Run。用户选择单 Run 时明确标识“单 Run 视图”。

## 7. UI 完成清单

- 所有按钮有 visible/disabled reason。
- 状态词汇与全局契约一致。
- Campaign 机器视图单条。
- Dependency/Forecast 风险可视。
- 日期矩阵 sticky header、虚拟列、筛选、导出。
- 下钻链接权限安全。
- 中英文无硬编码遗漏。
- 手机不要求完整 Gantt 编辑，但关键风险/审批可读。

## 8. 测试与灰度

- TRACEABILITY 每一 R-ID 更新 Verified 证据。
- 全量权限矩阵。
- UI runtime/static/browser tests。
- 10,000 行和实际 Run 规模性能。
- Feature Flag Off/Trial/单车间 Formal/回退。
- 两个完整滚动周期人工对照，记录差异原因。

## 9. Exit Criteria

- R-022、R-023、R-024、R-025 Verified。
- 全局 Definition of Done 中列出的全部项目均满足。
- 没有 P0/P1 缺陷和未决 ADR。
- 用户文档、操作培训、故障处理和回退说明完成。
- `enable_aps_v2` 的正式启用范围有 GMC/Manufacturing Manager 确认。

## 10. 最终交接

保存完整版本清单、迁移记录、测试证据、性能基线、已知限制、监控指标、支持联系人和回退步骤。关闭 Legacy Formal 前不得删除 Legacy 只读能力。
