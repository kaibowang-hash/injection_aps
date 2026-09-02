# Phase 6 交接记录（COMPLETE）

## 状态

- Phase：6 Co-product Production Campaign。
- 状态：COMPLETE。
- 证据：[Phase 6 isolated evidence](../evidence/phase_06/2026-08-14_isolated.md)。
- 安全状态：`enable_coproduct_campaign=0`，全部 V2 Flag 关闭，`jce.1` 零触碰。

## 已交付

1. Family Mold 单需求/多需求识别、可靠 yield、最大模次与分产出 covered/excess。
2. Solver 单 capacity owner、成员级交期/优先级目标和独立 Validator 指标。
3. Campaign/Output、主/派生 Result 与 Segment 的精确血缘；旧模糊 credit fail closed。
4. Work Order Proposal 整组审核与事务内原子 Apply；每个产出独立无 SO WO。
5. Shift Proposal/WOS 每产出独立明细、共享 capacity owner；冻结区间去重。
6. Stock Entry/Production Allocation 按各 WO 独立回写 good/scrap，不复制产量。
7. Replan/Change 只移动 owner 并同步所有产出；Resize/Cancel 要求重跑。

## Phase 7 输入

- Campaign 输出需求可作为 BOM 根或制造子件，BOM Pegging 必须仍按具体 Result/Commitment 传递，不能只按 Item 聚合。
- Campaign capacity owner 与 BOM precedence 同时存在时，只约束 owner task；其成员产出完成时间由 Campaign 投影。
- 原料继续 Advisory-only；Phase 7 只对配置为可制造的层级创建 production task。
