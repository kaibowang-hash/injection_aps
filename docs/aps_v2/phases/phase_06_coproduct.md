# Phase 6：联产品 Production Campaign

## 1. 必读

- 全局契约 INV-03/04/08/12
- ADR-011/012
- 架构 Campaign/Output
- 计算规格联产品公式
- Gantt/WO Proposal UI 规格

## 2. 前置条件

Phase 5 Complete；Solver 支持 generic campaign task；WO/WOS/Stock Entry 血缘稳定。

## 3. 目标

自动识别 Family Mold 多输出，按最大需求循环生成一次机器模具 Campaign，为每个输出生成工单建议和入库追踪，但资源只计一次。

## 4. In Scope

- APS Production Campaign/Campaign Output。
- Family output/yield master validation。
- Multi-output cycle coverage/excess。
- Primary/Co-product WO Proposal。
- Campaign 整组原子创建 WO。
- WOS capacity owner 和派生输出。
- Stock Entry/Production Allocation 按输出回写。
- Gantt 单 Campaign 条和展开。
- 现有 Family Co-Product scaffolding 迁移/替换。

## 5. Out of Scope

- 无可靠产出比例的模具自动猜测。
- 联产品输出独立占机台。
- 用备注代替 Campaign 链接。

## 6. Schema/Patch

创建 Campaign/Output；扩展 WO、Scheduling Item、Segment；回填旧 Family Segment 仅在有精确血缘时有效，否则历史只读。

## 7. 后端

- `campaign_planning.py` 读取 mold family outputs 和 output per cycle。
- 按共享公式求 cycles、planned、covered、excess。
- Solver 只接收一个 capacity owner interval，输出数量作为 campaign children。
- Work Order Proposal 每输出一行，Campaign group 一次 Apply/rollback。
- WO 可无 SO；必须有 campaign、output role、source reason。
- 只有一个需求驱动时生成用户指定备注；多输出均有需求时不写“被带出”说明。
- Stock Entry 通过 WO/output role 汇总到 Campaign Output，数量不互相复制。

## 8. Gantt

机器视图一个 F Campaign 条；输出 chips。展开显示 WO、需求覆盖、excess、actual good/scrap。物料视图可以镜像输出，但明确 derived/capacity counted once。拖动/拆分只作用 capacity owner 并同比同步输出。

## 9. 测试

- A/B 1:1，各需求不同，cycles=max。
- 只有 A 需求仍生成 B 输出 WO 和说明。
- A/B 都有需求不生成错误说明。
- 两 WO 一个 Campaign，机器/mold 只占一次。
- 任一 WO 创建失败整组回滚。
- 实际入库分别回写，不复制主 WO produced_qty。
- Gantt 无双重 capacity bar。
- Campaign 调整同步全部输出。

## 10. Exit Criteria

- R-019、R-020、R-023 Campaign 部分 Verified。
- 当前自动清空 family outputs 的逻辑被 V2 路径替换。
- Legacy Flag Off 行为不变。
