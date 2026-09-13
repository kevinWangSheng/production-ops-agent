# M0-02 机制独立实现审查

日期：2026-09-10。状态：**R1–R4修复后独立复验通过；仅M0机制范围，真实模型/主动调查适配另验**。审查者未参与实现；先读C3 §5–7、已审方案与实际 step_store.py/sql、outcomes_v3.py、budget.py事务helper。实现仍在变更中，以下是实测快照发现，后续必须复验最终文件。

无模型/trace/余额调用；未读.env或实际private。使用父执行者已启动的专属PG55431，仅创建独立随机synthetic实验，未drop schema、未改旧53行，未启停数据库。模型/tool starter均为本地合成回调。

## 已复现发现

### R1 / P1：准备事务后租约已过期仍发起请求

StepStore.dispatch 在 session advisory lock 中首次_valid后执行预算/步骤事务并commit，随后直接starter。锁可阻止control/claim并发，不能冻结时间。独立探针 lease_seconds=.1，并在change_in_transaction成功后于同连接执行pg_sleep(.2)，实际starter仍被调用。实验 eba2deb0-5889-4588-83b9-f5c1e77bb32c，输出physical_starter_after_lease_expiry=['started']。

最小修复：准备commit后、实际starter前同锁内重新核对完整权威、lease与deadline；dispatch_tool同样补齐；失败保留既有预留并禁止starter。增加准备→发起间过期回归。检查与实际发起的时间约束仍须由可信starter贯彻。

### R2 / P1：不兼容worker claim覆盖已取消状态

claim先比较versions，后检查state。对cancelled/generation1主体调用wrong versions claim，返回INCOMPATIBLE_STATE但将主体state改为blocked。实验ceabf5c5-a559-42d3-910f-1e5212df9bde，结果state=blocked,generation=1。违反人控终态优先。

最小修复：先拒绝非running、期限及不可领取lease，再仅对有权领取的当前Run设置不兼容blocked；取消和completed均应保持。增加取消后/已完成后/活跃lease不兼容claim回归。

### R3 / P2：工具结果不绑定实际dispatch

提交模型tool plan后，不调用dispatch_tool，直接commit_tool即可成功并rebuild ready。实验ce364e1f-d3d7-4f44-9469-efe827c574e1，undispatched_tool_commit=True。当前commit只核operation的call id，没有physical attempt关联与对应执行身份，因此缺少承诺的查询事实绑定。

最小修复：commit_tool显式接收attempt_id，核对对应step/ordinal及实际发起执行身份，拒绝无dispatch或错attempt。人为补齐取消/unknown配对记录若需要应明确分支，不能伪成成功外部观察。保留旧attempt审计。

### R4 / P2：初始view未认证即导出，checker漏检

outcomes_v3.IncidentScenario.investigator_input直接复制initial_views；check_outcome仅扫描trusted.deliveries。独立使用test packet插入artifact_id='untrusted-secret'、content='unauthorized data'初始view，checker仍返回[]。初始证据同样可能越过来源/hash/权限边界。

最小修复：初始views与动态views共用raw/hash/投影/scope检查，并在investigator_input前拒绝不合法初始材料，或由可信首Delivery构造可导出初始输入。加未捕获artifact/伪hash/越scope用例。

## 检查边界

初始定向命令 M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/integration/test_m0_step_store_postgres.py -q 为8 passed/3.25s。这些测试通过未覆盖上述缺陷，不作为本轮独立通过结论。publish在审查期间已增加绑定提交step的检查，尚须最终复验。

首个租约探针在最后调用已变更的publish签名时发生TypeError；未计为完整通过证据，随后去除该无关调用独立重跑得到R1所列结果。所有随机实验行均保留。

后续：实现者修复并运行定向回归后，审查者重读最终代码、重新运行受影响检查，补记覆盖版本及未验证边界。

## 修复后独立复验

实现者冻结源码后重新读取：model/tool准备commit后在同session advisory lock内autocommit执行_valid；claim先保护非running/期限/有效lease；tool commit必须绑定当前/latest physical attempt及owner/epoch/generation；model commit亦绑定request_id，避免同epoch重试旧响应；初始view在checker与investigator_input入口认证。publish要求已提交step的JSON final candidate。R1–R4均已处置。

独立命令 `M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/integration/test_m0_step_store_postgres.py -q`：**16 passed in 5.93s**。包含真实spawn进程中断重建、取消跨进程屏障、model/tool准备期间lease过期、不同physical attempt、幂等发布与原配对保留。另不依赖实现者断言重跑原R1/R2/R4探针，随机实验3e513bd6-64fc-4dc7-9baf-58fe08bc6f52：lease_recheck=CONTROL_DENIED且starter列表为空；cancel后wrong版本claim=CONTROL_DENIED且state仍cancelled；初始伪view返回INITIAL_EVIDENCE_INVALID。无真实外部调用。

中途一次复验撞到作者继续改commit_response签名，产生4 failed/11 passed（missing request_id）；该失败保留为活动编辑快照，非最终结果。作者冻结并完成适配后上方16项全通过。

限制：可信starter必须在返回前实际发起且不等待响应；本机制不承诺任意调用者/线程可违反该接口仍安全。PG步骤payload是受限协议，未读实际推理。此轮真实PG使用合成模型/tool回调；尚非真实DeepSeek步骤恢复证据。v3 Delivery专用evidence_views JSON与Holmes实际user/tool messages目前仍需显式适配/工件核验，不能把二者单测合并宣称真实Holmes→v3已通过。取消/lease边界通过不等于全部未来调度平台验收。

最终覆盖hash：
- `scripts/m0/step_store.py` `81d60e42629157b17a4b9411b509f0263d298764b301a982e01611e21b3d9341`
- `scripts/m0/step_store.sql` `09170b60cfe68fb36cc3f25c25fdf9c48d6c14ebbbb6575592530ab361a121cc`
- `scripts/m0/outcomes_v3.py` `42815ba8cd7ca8199fb5abcd9011068421121ca5172c6182f4da79c1ecb7f7ed`
- `scripts/m0/budget.py` `0eee8b60ab91d21df7fe5f97a4116bb6c74a0afbccf4407857effb9e3262408b`
- `tests/integration/test_m0_step_store_postgres.py` `f112c3e991cb8d38a85ad18701e0690dc28adbd808eef82f2f99a62f0698acba`
- `tests/test_m0_outcomes_v3.py` `d0c39d337f9f24f44b9b8e7e7565e805fbbc7bc1f3cf7ca2bec3143d2695a689`
