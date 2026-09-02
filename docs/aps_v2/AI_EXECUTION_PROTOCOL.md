# APS V2 AI 执行协议

## 1. 启动检查

执行任何 Phase 前，AI 必须：

1. 完整读取 `README.md`、`00_GLOBAL_CONTRACT.md`、`DECISIONS.md`。
2. 完整读取当前 Phase 明确要求的共享规格。
3. 检查 `IMPLEMENTATION_STATUS.md`，确认前置 Phase 已为 `COMPLETE`。
4. 读取仓库 `AGENTS.md`（如果存在）和现有工作树状态。
5. 识别用户已有未提交修改，不覆盖、不回退、不格式化无关文件。
6. 运行当前 Phase 的 Baseline Tests，记录结果。
7. 将当前 Phase 标记为 `IN_PROGRESS`；同一时间不得存在第二个 `IN_PROGRESS`。
8. 如果工作目录位于生产 bench，完整读取 `PRODUCTION_SAFETY.md`，并在任何命令前判定其是否可能写站点、改资产或改变运行态。

## 2. 执行循环

每个 Phase 按固定顺序：

```text
Schema/Custom Fields/Patch
→ Domain Service
→ API/Permissions/Idempotency
→ UI
→ Unit Tests
→ Integration/Permission/UI Tests
→ Migration Test
→ Documentation/Handoff
```

不能先写 UI 再临时决定后端公式，也不能只改 `planning.py` 而跳过 schema、迁移和测试。

## 3. Scope Lock

Phase 文件的 `In Scope` 是允许主动修改的范围；`Out of Scope` 不得顺便实现。发现后续 Phase 所需的基础能力时，只能：

- 在当前 Phase 建最小兼容接口；
- 在状态台账记录后续事项；
- 不提前实现完整后续功能。

## 4. 变更控制

遇到以下情况必须暂停相关实现并提交 ADR：

- 当前代码无法满足全局不变量；
- 两个共享规格冲突；
- 需要改变已确认业务流程；
- 需要新增会改变用户操作的强制步骤；
- 需要让出货、工单或正在生产任务受到新的阻塞；
- 需要使用不同求解模型或改变数量公式；
- 数据迁移无法唯一回填。

实现困难不是自行删减需求的理由。

## 5. 数据安全

- Patch 必须幂等。
- 不删除历史排期、Run、WO、WOS、Stock Entry、Delivery Plan、Delivery Note。
- 不直接更新 ERPNext 原生 delivered_qty/produced_qty 来迎合 APS。
- 回填存在歧义时生成异常，不猜测。
- Apply 使用事务、固定锁顺序和指纹。
- 破坏性清理只允许在专用测试 Fixture 中进行。

### 5.1 本项目生产服务器硬门禁

- `jce.1` 只读；不得把 `bench --site jce.1` 与 `migrate`、`execute` 写函数、`run-tests`、`clear-cache`、`install-app`、`uninstall-app` 或任何会改变数据库的命令组合。
- 不执行全 bench 的 `bench build`、`bench restart`、`supervisorctl`、`systemctl` 或会让生产用户加载新资产的操作。
- 允许的验证仅限源码读取、`git diff/status`、静态检查、编译检查，以及不初始化站点/数据库的 mock 单元测试。
- 所有 DB 集成、迁移和 UI 验证必须在生产脱敏副本或经用户明确授权的隔离站点进行；站点名中包含 `test` 不等于自动授权。

### 5.2 前端定制保护

- 已存在的 Workspace、Custom HTML Block、Client Script、Property Setter 和 Customize Form 布局不得被 recurring hook 覆盖。
- 创建型安装逻辑必须采用 create-if-missing；遇到同名已有记录时保留原值并记录冲突。
- JSON 无法解析、来源不明或无法证明由本次版本创建时，必须停止该资源更新，不能回退为空布局或默认模板。
- 需要升级应用 UI 时优先使用版本化的新资源或显式升级操作，并提供差异预览、备份和回退；不得在 `after_migrate` 静默刷新。

## 6. 测试门禁

AI 不得只报告“代码完成”。交接证据必须包含：

- 执行的测试命令；
- 通过/失败数量；
- 失败是否为既有问题；
- 新增测试覆盖的 Requirement ID；
- 迁移演练结果；
- Feature Flag 开/关结果；
- 数量守恒和资源不重叠验证结果；
- UI 截图或可重复验证步骤（适用时）。

## 7. 阶段完成规则

只有 Phase 文件的所有 Exit Criteria 满足时才标记 `COMPLETE`。以下任一情况必须保持 `BLOCKED` 或 `IN_PROGRESS`：

- 仅完成部分代码；
- Patch 未演练；
- 权限未验证；
- 测试失败；
- 文档和状态未更新；
- 有未确认 ADR；
- 只能在 Legacy 关闭或手工改数据库后运行。

## 8. 交接输出

使用 `HANDOFF_TEMPLATE.md` 创建或更新 Phase 交接记录，至少包含：

- 交付结果；
- 修改文件；
- schema/API/UI 变化；
- 测试证据；
- 已知限制；
- 数据迁移和回退；
- 下一 Phase 的明确输入；
- 用户需要确认的事项。

下一位 AI 应先验证交接证据，不能假设上一个 Phase 已正确完成。
