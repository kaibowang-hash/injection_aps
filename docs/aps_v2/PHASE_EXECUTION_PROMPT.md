# APS V2 Phase 执行提示模板

将下面模板中的 `<PHASE>` 替换成具体阶段编号后交给新的开发 AI。不要要求一个 AI 在没有阶段验收的情况下连续实现全部 Phase。

```text
请实施 injection_aps 的 APS V2 Phase <PHASE>。

强制要求：
1. 从 apps/injection_aps/docs/aps_v2/README.md 开始，完整读取其中规定的全局文档和当前 Phase 文件。
2. 检查 IMPLEMENTATION_STATUS.md，验证所有前置 Phase 的代码、迁移和测试证据；前置条件不成立时不要开始编码。
3. 只实现当前 Phase 的 In Scope，不提前实现后续 Phase，不改变 00_GLOBAL_CONTRACT.md 和 Accepted ADR。
4. 先检查现有工作树，保留用户已有修改；使用 apply_patch 编辑文件。
5. 按 Schema/Patch → Service → API/Permission/Idempotency → UI → Tests → Migration → Handoff 顺序完成。
6. 所有写入必须有锁、指纹、幂等和审计；Feature Flag 关闭时 Legacy 行为必须保持。
7. 遇到规格冲突或必须改变已确认规则时，停止该部分并按 DECISIONS.md 模板提交 Proposed ADR，不得自行删减需求。
8. 完成后运行当前 Phase 的全部门禁测试，更新 TRACEABILITY_MATRIX、IMPLEMENTATION_STATUS，并按 HANDOFF_TEMPLATE.md 写交接证据。
9. 只有 Exit Criteria 全部满足才能声明 Phase Complete；请报告具体测试命令、结果、迁移验证、已知限制和下一 Phase 输入。
10. 当前 bench 承载生产站点 `jce.1`：开发期间绝对禁止写库、migrate、run-tests、build、restart、clear-cache；未经用户明确授权也不得写 `jce-test.1`。已有前端定制只能保留，不能覆盖、重排或删除。
```

## 为什么按 Phase 分会话

每个 Phase 都包含数据模型、后端、UI、迁移和测试。将多个 Phase 放进同一无门禁会话，会增加以下风险：

- 后续代码依赖尚未验证的临时接口；
- 同一公式在多个位置出现不同版本；
- AI 为完成后续页面而提前修改全局状态；
- 迁移和兼容性被拖到最后；
- 测试失败无法定位属于哪个阶段。

因此推荐“一 Phase 一验收一交接”，但允许同一个 AI 在每次完成门禁后继续下一 Phase。
