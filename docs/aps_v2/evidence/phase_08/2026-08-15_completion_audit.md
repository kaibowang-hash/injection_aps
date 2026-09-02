# APS V2 Phase 3—8 完整收口审计（2026-08-15）

## 1. 审计结论

Phase 3—8 的代码、迁移、接口、权限、UI、回退和隔离数据验证已经全部完成。此次收口不是只复跑原有测试，而是重新逐项阅读 Phase 规格、追踪实际调用链，并修复审计中确认的缺口。

所有写操作只发生在隔离 Bench `/home/ubuntu/frappe-bench/isolated/aps-v2-bench` 的 `aps-opt-fixture.localhost`。生产站点 `jce.1` 没有作为任何命令目标；未执行生产 migrate、Patch、Fixture、run-tests、build、restart、clear-cache、队列或 scheduler 操作。

生产 Formal 灰度仍未执行。它需要 GMC/Manufacturing Manager 单独批准 Company、Plant Floor、窗口和回退条件，不属于本次开发授权。

## 2. 收口审计发现与修复

| 范围 | 审计发现 | 最终处理 |
|---|---|---|
| Trial / Formal | V2 Trial 分析后缺少真实 Legacy 对照，且 Apply 后端没有以 Run Type 做最后一道写保护 | Trial 分析在 V2 投影前把 Legacy 指标和 fingerprint 写入 Solver Job audit；比较页展示 Legacy/V2/delta；Trial Apply 按钮可见但 disabled 并显示原因；后端只允许 Formal Apply |
| 约束处理 | Resolution Center 重算无条件进入 Legacy Capacity Balance | V2 + CP-SAT 现在重新排队进入 V2 Solver；Legacy 仍进入 Capacity Balance |
| Shift API | UI/接口规格中的标准方法名与现有短方法名不完全一致 | 增加五个向后兼容标准 API alias，保留旧 API，避免破坏既有调用方 |
| Shift 刷新 | Cycle fingerprint 没有完整纳入最新 revision、执行/交付事实和停机窗口 | 创建 Cycle 前刷新生产与交付；source snapshot 纳入 Run、active revision、最新 Production/Delivery Allocation；零容量停机作为 Machine/Mold 精确 blocked interval 进入局部 CP-SAT |
| Shift 自动任务 | 单个车间刷新/分析异常可能中断其他 scope；缺少两个连续周期的完整证明 | scheduler 按 scope 隔离失败，始终 `applied=0`；测试两个连续班次 Cycle，验证同 cutoff 幂等、下一班新 Cycle、均不自动批准/应用，Original/Current 不变 |
| Campaign Apply | Shift Campaign 同步存在运行时 import 缺口；Campaign 整组 Apply 的中途失败缺少真实事务证明 | 修正稳定 import；注入第二张 WO 创建失败，验证第一张 WO、Campaign 状态及输出引用在 savepoint 中整组回滚 |
| UI | Scenario 页面未显示可审计 Trial baseline；Planning Run Trial 的 Apply 原因不够明确 | 增加 Trial comparison、只读提示、Late Qty 等中文；按钮保留可见并提供 title/ARIA 原因 |

## 3. Phase 3—8 验收结果

| Phase | 当前模块测试 | 覆盖重点 |
|---|---:|---|
| 3 Horizon/Status/Material | 16/16 PASS | 四类 horizon、逾期 P0、原料 advisory-only、blocker/override/exclude、V2 重算路由 |
| 4 Solver | 17/17 PASS | CP-SAT、词典序目标、独立 Validator、Trial 对照/只读、Formal Apply、stale fingerprint |
| 5 Shift Replan | 15/15 PASS | Forecast、局部 CP-SAT、停机、刷新、标准 API、两个连续 Cycle、永不自动 Apply |
| 6 Co-product | 15/15 PASS | 最大模次、多输出、单 capacity owner、两 WO/WOS、整组原子 Apply 与失败回滚 |
| 7 Multi-level BOM | 14/14 PASS | C→A→X、库存/WIP 唯一覆盖、Pegging、precedence、替代 BOM fingerprint |
| 8 Progress/UI/Rollout | 19/19 PASS | 跨 Run owner 投影、日期矩阵、Campaign Gantt、Trial UI、索引、10k 性能 |
| **合计** | **96/96 PASS** | Phase 3—8 当前实现 |

附加门禁：

- Permission/Workflow Guards：53/53 PASS。
- Frontend Customization Safety：6/6 PASS。
- UI Static/Translation：17/17 PASS。
- UI Runtime Guards：9/9 PASS。
- Flag-Off Legacy 专项：客户排期、交付同步、既有工单、工单血缘、导入事务共 133/133 PASS；Legacy Progress 7/7 PASS。
- 上述定向验收合计 321/321 PASS。
- 全部 Python 文件 `py_compile` PASS；`git diff --check` PASS。

## 4. 全应用回归

最终隔离全应用结果：

```text
Ran 747 tests in 95.365s
FAILED (failures=3, errors=8)
```

失败/错误集合与开发前隔离基线逐项一致，没有 Phase 3—8 新回归：

- Capacity Balance plan consistency：2 errors。
- Change Engine stale snapshot：6 errors。
- Legacy execution FIFO 期望 `60/40`、实际 `65/35`：1 failure。
- 既有 Phase 6 quantity audit：2 failures。

这些是 Phase 0 已登记的既有基线，未在本项目中掩盖、改测试跳过或放宽断言。

## 5. 迁移幂等与定制保护

在同一隔离副本完成：第一轮 migrate → Phase 3—8 六个 Patch 逐个直接重跑 → 第二轮 migrate。所有步骤退出码均为 0。

迁移前、第二次迁移后、全量测试后的前端定制语义指纹均完全相同：

```text
feec2201de9d891acd943345f81f1ce78f1711980932ca585d1f1dad7b5bc2dc
```

记录数保持：Workspace 1、Page 10、Custom HTML 1、Client Script 0、Property Setter 20、受控 Custom Field 54。六类 section fingerprint 也逐项相同，证明 recurring migrate 没有覆盖既有前端定制或用户布局。

迁移和测试后 Feature Flag 均恢复到安全状态：

```json
{
  "enable_aps_v2": 0,
  "solver_engine": "Legacy",
  "enable_shift_replan": 0,
  "enable_coproduct_campaign": 0,
  "enable_multilevel_bom_planning": 0
}
```

## 6. 灰度流程的隔离证明

- Flag Off：Legacy 专项 140/140，既有页面和生产/出货路径不变。
- Trial：真实 DocType 分析保存 Legacy baseline 和 V2 方案；不新增正式 Segment；UI 明确只读；服务端拒绝 Apply。
- 单 Plant Floor Formal：真实隔离 Fixture 完成 Solver 分析、方案选择、指纹重验和正式 Apply。
- 两个滚动周期：在同一隔离 Plant Floor 生成两个连续班次 Cycle，确认不自动批准、不自动 Apply，执行层未被静默改写。
- 组件验证：Shift、Campaign、BOM 分别由独立 Flag 和集成测试覆盖；关闭开关不删除 schema、血缘或已存在业务事实。

这些结果满足工程和隔离验证门禁；生产灰度、生产浏览器烟测、业务签字和实际生产规模 SQL/响应基线仍按发布方案执行。

## 7. 最终安全状态

- Phase 0—8 工程实现和隔离验收完成。
- 原料继续是 Ready/Short/Unknown advisory，不进入 Solver hard constraint、Apply 锁或可排数量。
- 既有业务顺序保持为：工单建议审核 → 工单 → 工单排产/WOS → 工单入库 → 出货计划分配订单 → 下推销售出库。
- 新 Demand Identity/Commitment/Campaign/Pegging/Allocation 只补充 APS 血缘和决策事实，不替代 Delivery Plan、Delivery Note 或生产单据状态机。
- 当前代码尚未发布或启用到生产；生产发布必须走独立审批与回退流程。
