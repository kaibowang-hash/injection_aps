# APS V2 灰度启用与回退方案

## 1. 当前发布结论

Phase 0—8 的工程实现和隔离验证完成，不代表已经授权在生产启用。`jce.1` 未被迁移、测试、构建、重启、清缓存或打开 V2。正式范围必须由 GMC/Manufacturing Manager 确认，并走生产变更流程。

## 2. 上线前门禁

- 目标版本、数据库备份、回退负责人、观察窗口和支持联系人已确认。
- 在最新脱敏副本完成两次 migrate、各 Phase Patch 幂等复跑、权限/翻译/定制保护和 Flag-Off Legacy 回归。
- 生产数据先生成只读 owner/open-demand/Run/WO/WOS/DP/DN 快照；歧义只报告不猜测。
- OR-Tools 运行环境兼容；当前内嵌版本锁定 `9.4.1874`，不得升级 Frappe protobuf。
- GMC 和 Manufacturing Manager 书面确认试点 Company/Plant Floor、起止时间、用户、容忍差异和回退触发条件。

## 3. 分阶段灰度

1. **Flag Off**：迁移 schema 后继续 Legacy；核对既有用户页面、Workspace/Custom HTML/Property Setter、工单和出货路径。
2. **Trial 对比**：只运行 V2 Trial，不生成正式单据。系统在 V2 投影前保存 Legacy baseline/fingerprint，比较页展示 Legacy/V2/delta；Trial 前端不可点击 Apply，后端也拒绝 Trial Apply。逐需求比较准时量、延期量、未排量、换模和资源利用率，并解释差异。
3. **单 Plant Floor Formal**：只对已批准车间启用 Formal；其他车间保持 Legacy。一个 Demand Identity 只允许一个 Formal owner。
4. **两个完整滚动周期**：PMC/GMC 每班次对照 Schedule、Commitment、Result、WO/WOS、Stock、DP/DN 和 Progress 守恒，记录每个差异原因。
5. **组件开关**：Shift Replan、Co-product Campaign、Multi-level BOM 分别启用和验收，不一次性扩大范围。
6. **逐车间扩展**：只有前一范围无 P0/P1 缺陷、监控稳定且业务签字后才能扩大。
7. **停止 Legacy Formal**：最后一步才停止新 Legacy Formal；Legacy 历史继续只读，不删除 schema 或历史单据。

## 4. 建议回退触发条件

- 同一 Demand Identity 出现多个 active Formal owner。
- 数量守恒或 Campaign 资源单计无法在当班内解释。
- 新 V2 Apply 生成错误 WO/WOS 范围或影响冻结/执行中任务。
- 正常 Delivery Plan/Delivery Note 被 APS 阻塞。
- 权限泄漏、下钻返回无权单据或既有前端定制被覆盖。
- Solver/Progress 性能持续超过批准阈值并影响生产操作。

## 5. 回退步骤

1. 停止扩大灰度，保存当前 owner、开放需求、Run、Proposal、WO/WOS、DP/DN 和异常快照。
2. 关闭 `enable_aps_v2`；同时按启用顺序关闭 Shift Replan、Co-product Campaign 和 Multi-level BOM 组件开关，并把 Solver engine 切回 Legacy。
3. 禁止创建新的 V2 Formal，但不删除或取消已提交 WO/WOS/Stock Entry/DP/DN；由原业务流程继续执行或按单据状态受控处理。
4. 保留 V2 schema、Demand Identity、Commitment、Allocation、Campaign、Pegging 和审计记录只读，用于解释已发生事实。
5. 运行 Flag-Off Legacy 五模块、权限、定制保护和 Progress Legacy 回归；确认 `formal_v2_writes_enabled=false`。
6. 记录触发原因、影响范围、最后安全指纹、未完成需求所有权和后续修复计划。

## 6. 回退验证清单

- `mode=Legacy`、`enable_aps_v2=0`、`solver_engine=Legacy`、所有组件 Flag=0。
- 客户排期导入、工单创建、工单排产、工单入库、Delivery Plan 分配 SO、Delivery Note 下推均沿既有路径。
- 已有 V2 Work Order/WOS 不被重复创建；开放需求不丢失、不被新 Run 重复拥有。
- Workspace、Custom HTML Block、Client Script、Property Setter 和 Customize Form 不变。
- 回退不进行 schema downgrade，不删除历史事实或审计。
