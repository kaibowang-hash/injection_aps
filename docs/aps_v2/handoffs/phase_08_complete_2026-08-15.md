# Phase 8 完成交接记录

## 1. 目标与结果

- Phase：8 Progress/UI/Rollout
- 状态：COMPLETE（工程实现与隔离验证）；生产未启用
- 完成目标：Progress 明细/日期矩阵、跨 Run 有效投影、精确下钻、Gantt 四层和 Campaign 单条、多语言/权限/性能/迁移/回退/用户运维文档。
- 未完成目标：没有执行生产 Formal 灰度；必须另行获得 GMC/Manufacturing Manager 对范围和窗口的确认。

## 2. 修改范围

- 新增服务：`services/progress_v2.py`。
- 新增 Patch：`implement_aps_v2_phase8_progress_ui.py`，只建查询索引。
- API：V2 detail、matrix、cell drilldown；原 Progress API 在 Flag On 时分派，Flag Off 保持 Legacy。
- UI：Customer Schedule Progress Detail/Date Matrix、分页/虚拟列/导出/下钻；Gantt Original/Current/Forecast/Actual 和 Campaign outputs。
- 测试：Phase 8 unit/contract/integration，权限过滤、Campaign Gantt、10,000 行/事件、UI runtime。
- 文档：用户指南、运维/故障处理、灰度回退、Phase 8 evidence。

## 3. 关键实现决策

- 使用 ADR-002/003/004/008/012/013/016/017；没有新增或未决 ADR。
- 默认 Progress 不选某个旧 Run，而是逐 Demand Identity 读取当前 Formal owner。
- DP/DN 与生产事实独立汇总到 Identity，不改变工单/出货业务顺序。
- 材料继续只提示；Campaign 只由单一 capacity owner 占用资源。
- 权限在 API 返回前过滤行、Commitment、Result、Run、event/cell source documents。

## 4. 测试证据

- Baseline：Phase 0 全量 `600 tests / 3 failures / 8 errors`。
- Unit/Performance：8/8。
- Contracts：6/6。
- Integration：Phase 8 5/5；Campaign Gantt 3/3。
- Permission：53/53。
- UI：static/translation 17/17；Node browser-like runtime 9/9。
- Migration：首轮 migrate、Patch 直接复跑、第二轮 migrate均成功；五组索引存在。
- Feature Flag On：真实跨 Run/DP/DN/Stock/Actual/Matrix/API 集成通过。
- Feature Flag Off：Legacy 五模块 133/133，Progress Legacy 7/7，最终 capabilities fail closed。
- Phase 3—8 最终模块验收：96/96；全部定向门禁合计 321/321。
- Full App：747 项；仅 Phase 0 已知 3 failures/8 errors，无新增失败。

详细结果见 [Phase 8 isolated evidence](../evidence/phase_08/2026-08-15_isolated.md) 和 [Phase 3—8 completion audit](../evidence/phase_08/2026-08-15_completion_audit.md)。

## 5. 数据验证

- 需求数量守恒：Commitment demand/solver partition delta 集成验证为 0；Mismatch 显示 Unknown。
- 资源不重叠：Campaign 两输出共享一个 owner，后端和 Gantt 集成验证。
- 跨 Run 所有权：两条 Identity 分别由两个 Formal Run 当前 owner 投影；Single Run 不借用另一 owner。
- Delivery/Production 血缘：Schedule/Identity/Commitment/Result/Segment/Stock/DP/DN 精确来源下钻通过。

## 6. 已知限制和发布门禁

- Phase 0 已记录的 3 failures/8 errors 仍存在，未由本项目扩散；具体清单保存在 evidence。
- 自动化 UI 采用静态契约和 Node VM runtime guard；没有在生产 bench 执行资产构建或生产浏览器测试。正式发布窗口应按用户指南进行一次真实浏览器烟测。
- 10,000 行门禁验证聚合计算；目标车间正式灰度前仍需记录其实际 Run 的端到端 SQL/响应基线。
- 正式启用范围和两个滚动周期人工对照需要 GMC/Manufacturing Manager 业务签字。

## 7. 回退

- `enable_aps_v2=0`，Solver 切回 Legacy，并关闭 Shift/Campaign/BOM 组件开关。
- schema 和历史血缘保留只读；不取消、删除或重复创建已提交 WO/WOS/Stock/DP/DN。
- 回退后运行 Legacy 五模块、权限和定制保护门禁并保存 owner/open-demand 快照。

## 8. 项目最终状态

Phase 0—8 的开发和隔离验证全部完成。下一步不是继续开发 Phase，而是由业务和运维依据 [灰度启用与回退方案](../APS_V2_ROLLOUT_ROLLBACK_ZH.md) 发起独立的生产发布审批。
