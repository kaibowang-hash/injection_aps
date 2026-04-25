# Injection APS 权限矩阵

本文档说明 `injection_aps` 在安装或迁移时自动维护的角色与权限。权限初始化入口为：

- `injection_aps.install.after_install`
- `injection_aps.install.after_migrate`
- `injection_aps.services.permissions.ensure_roles_and_permissions`

## 新增角色

| 角色 | 中文定位 | 说明 |
|---|---|---|
| PMC | 物控员 | 维护客户需求、查看净需求、运行试算、查看排产、预审建议。 |
| GMC | 物控经理 | 物控侧审批人，可审批计划、释放工单/排班建议、维护 APS 关键配置。 |

## 角色边界

| 角色 | 可以做 | 不建议/不允许做 |
|---|---|---|
| PMC | 导入客户计划、重建需求/净需求、查看需求池/净需求/甘特、运行试算、查看异常、预审建议、填写计划备注 | 不审批 APS Run，不应用正式工单或正式排班 |
| GMC | 审批 APS Run、生成和应用工单建议、生成和应用排班建议、维护关键配置 | 无 |
| Sales Manager / Sales User | 查看客户计划、交期影响、排产结果、异常；维护客户计划 | 不释放生产 |
| Purchase Manager / Purchase User | 查看客户计划、需求池、净需求、排产甘特、异常、工单/排班建议、释放状态 | 不修改 APS 排产，不应用正式工单/排班 |
| Manufacturing Manager | 生产侧完整执行权限 | 无 |
| Manufacturing User | 查看排产和释放状态，同步执行反馈 | 不审批 APS Run，不应用正式释放 |
| Stock Manager / Stock User | 查看需求池、净需求、库存相关结果、排产和异常 | 不审批 APS，不释放生产 |

## MRP 预留

采购角色已纳入 `APS_MRP_ROLES`，后续接 MRP 时可复用这一组角色：

- `PMC`
- `GMC`
- `Purchase Manager`
- `Purchase User`
- `Manufacturing Manager`
- `Stock Manager`
- `Stock User`

当前已经为这些角色预留常用只读链路：`Item`、`BOM`、`Warehouse`、`Work Order`、`Material Request`、`Purchase Order`、`Supplier`、`Bin` 等。

## Link 字段依赖权限

为避免“能打开 APS 页面但 Link 字段搜不到数据”，安装/迁移时会给 APS 相关角色补以下依赖单据的只读/选择权限：

| 依赖单据 | 用途 |
|---|---|
| Company | 公司过滤和单据归属 |
| Item / BOM / Warehouse / Bin | 物料、BOM、仓库、库存与未来 MRP |
| Customer / Sales Order | 客户计划和销售需求来源 |
| Work Order / Work Order Scheduling / Scheduling Item | 正式生产执行链路 |
| Workstation / Plant Floor | 机台和车间排产 |
| Mold / Mold Product / Mold Default Material | 模具、穴数、周期和默认材料依据 |
| Delivery Plan | 与交付计划联动 |
| Material Request / Purchase Order / Supplier | 后续 MRP 和采购执行预留 |
| User / Employee / Asset / Address / Location / UOM | 审核人、执行人员、资产、地点和单位等辅助 Link 字段 |

## API 权限分层

代码中按动作风险分为以下角色组：

| 角色组 | 用途 |
|---|---|
| `APS_READ_ROLES` | 看页面、看排产、导出当前页面数据 |
| `APS_DEMAND_ROLES` | 维护客户计划、预览/导入需求 |
| `APS_PLAN_ROLES` | 重建需求/净需求、运行试算、生成计划、重建异常、维护计划备注 |
| `APS_APPROVE_ROLES` | 审批 APS Run、批准变更 |
| `APS_RELEASE_ROLES` | 应用正式工单/排班、释放到执行层 |
| `APS_EXECUTION_ROLES` | 同步执行反馈 |
| `APS_MRP_ROLES` | 后续 MRP 预留 |
| `APS_ADMIN_ROLES` | 权限、基础能力同步、引用修复等维护动作 |

## 注意

系统会通过 `Custom DocPerm` 为标准和依赖单据补只读权限，不直接修改 ERPNext、`zelin_pp`、`mold_management` 的原始 JSON。这样更适合升级，但也意味着安装或迁移后需要执行一次 `bench migrate` 才能完整刷新权限。
