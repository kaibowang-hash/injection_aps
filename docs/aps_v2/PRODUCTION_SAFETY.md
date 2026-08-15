# APS V2 生产服务器安全约束

## 1. 受保护目标

- `jce.1`：生产站点，开发期间严格只读。
- 整个 `/home/ubuntu/frappe-bench`：生产运行 bench；共享 assets、进程和缓存均可能影响 `jce.1`。
- 既有前端定制：Workspace、Custom HTML Block、Client Script、Property Setter、Customize Form 字段顺序、角色快捷入口和用户 Desk 布局。

`jce-test.1` 当前数据库与 `jce.1` 分离，但在用户明确授权前仍不得将其视为可写测试环境。

## 2. 开发阶段允许的操作

- 读取源码、配置文件和日志。
- `git status`、`git diff`、`rg` 等只读检查。
- 修改 `apps/injection_aps` 工作树中的源码和文档，但不得覆盖用户已有未提交修改。
- Python 编译、静态检查和完全 mock、不会初始化站点或连接数据库的单元测试。

## 3. 开发阶段禁止的操作

- 对 `jce.1` 执行任何写 API、SQL、Console 写操作、Fixture、`run-tests`、Patch 或 migrate。
- 执行 `bench build`、`bench restart`、`bench clear-cache`、`supervisorctl`、`systemctl` 或其他改变生产运行态的命令。
- 安装/卸载 App、修改 `sites/apps.txt`、`site_config.json`、`common_site_config.json` 或生产任务调度状态。
- 删除、覆盖、重建、重排或重新导出已有前端定制。

## 4. 前端资源所有权规则

1. “名称由 Injection APS 使用”不等于“可以覆盖”。同名记录一旦存在，就按用户数据处理。
2. 首次安装可 create-if-missing；已有记录保持原样。
3. `after_migrate` 不负责刷新 Workspace、Custom HTML Block、Client Script 或 `field_order`。
4. 无法解析 Workspace JSON 时立即停止，不得把内容当成空数组保存。
5. UI 升级必须版本化或通过显式升级命令完成；升级前生成差异和备份，升级后可回退。
6. 卸载清理前必须识别用户修改并导出备份；本开发阶段不执行卸载验证。

## 5. 发布门禁

只有以下证据齐全后，才能另行提出生产发布步骤；本文件本身不构成发布授权：

1. 生产脱敏副本完成两次可重复迁移。
2. Feature Flag Off 的 Legacy 回归通过。
3. 前端资源修改前后指纹和差异报告证明既有定制未丢失。
4. DB、权限、任务调度、缓存和资产影响清单已评审。
5. 有数据库与前端定制备份、回退脚本和停机/观察方案。
6. 用户明确批准具体站点、版本、时间窗和命令。

## 6. 当前审计结果（2026-08-14）

- 上述 recurring migrate 风险已在当前工作树中关闭：`after_migrate` 不再执行隐式站点/前端写入。
- 同名 Custom HTML Block 和 Workspace 只 create-if-missing；已有内容不更新；无效 Workspace JSON 不保存。
- Custom Field 默认 `update=False`；Item `field_order` 只有显式 opt-in 才允许调整。
- 隔离生产数据副本两轮 migrate 后，Injection APS Workspace/Page/Custom HTML/Property Setter 没有内容覆盖；第二轮语义变化为 0。
- 全栈首次 migrate 只观察到 5 个既有 Item Custom Field 的派生 `idx` 重算，`insert_after`、label、type、hidden 均未变化；不得把 `idx` 重算扩大为布局重排授权。
- Mock/静态、迁移差异和最终 Flag-Off 门禁已通过，证据见 [Phase 0 isolated evidence](./evidence/phase_00/2026-08-14_isolated.md)。

这些修复尚未发布到 `jce.1`。任何生产发布仍必须满足第 5 节并取得用户对具体命令和时间窗的明确批准。
