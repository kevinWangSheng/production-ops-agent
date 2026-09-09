# 首条纵向流程实施入口独立审查

2026-09-09；范围：提交调查 → 主动查询证据 → 展示结论 → 人工跟进/取消 → 持久保存。只读审查既有代码、测试和证据；未操作环境、调用模型/trace、修改 SPEC 或 passes。另独立运行 `.venv/bin/python -m pytest tests/test_m0_adapters.py tests/test_m0_outcomes.py tests/test_m0_budget.py -q`：**117 passed in 1.25s**；未重跑 PG 故障/重启集成。

**判断：目前尚不能记录此纵向流程已具备实施入口。** 主要缺口是首片实际来源/身份/权限证据、调查步骤恢复与持久取消机制验证、以及适用于主动查询的冻结验收包。不是要求先完成整个产品。依据 [SPEC 实施条件](../../../SPEC.md#conditions-for-entering-implementation) 与 [M0 退出规则](../../plans/m0-validation-plan-2026-09-07.md)：先取得对应 M0 证据、解决不兼容并冻结验收与环境，再记录入口决定；允许有界机制实验，不要求全产品验收先通过。

## 已有证据与其边界

- **模型/工具往返与出口：** 本轮 [Flash 审查](flash-review.md)核查真实固定 fixture 两轮、最终严格 JSON、PG business/outbox/diagnostics 与 TRACE_VERIFIED；这不是实际事故调查。`scripts/m0/protocol.py:43` 的工具仅 read_fixture，不能证明真实来源权限。
- **预算持久性：** `scripts/m0/budget.py:125` 及 `tests/integration/test_m0_budget_postgres.py` 覆盖跨进程争用、unknown 预留跨重启、截止/提交确认丢失拒绝重复许可；[B 原始结果](../m0-b/results.md)记录实际 PG。它保存预算身份，不是事故/步骤恢复日志。
- **取消基础：** `scripts/m0/adapters.py:113` 的 Task.cancel/异常优先级和 `tests/test_m0_adapters.py:367,406` 覆盖模型请求/流清理、工具取消配对、剩余工具零执行。[A 证据](../m0-a/README.md)明确不覆盖同步工具硬超时或产品持久取消。`History`（同文件第 26 行）是 attempt-local 内存组。
- **最终提交：** `scripts/m0/live.py:226` 在同事务保存最终业务/outbox/诊断；`scripts/m0/live.sql` 没有调查步骤、工具操作、人工控制版本、owner/epoch/lease 的恢复状态。一次性实验 claim 防重放不能代替 C3 §6–7 的业务重建。
- **公开验收合同：** `scripts/m0/outcomes.py` 及 `tests/test_m0_outcomes.py` 已检查目标/版本、证据 hash/来源引用、实际动作对账、权限拒绝、独立健康/unknown 等结构条件。`check_outcome` 自身说明成功只代表合同一致，不证明因果真伪；静态合成 Action 记录不证明实际鉴权系统拒绝了写操作。
- **上游/环境：** [固定映射](../../testing/m0-upstream-mapping.md)是公开源码证据，并明确未运行基线和未验证来源权限。当前正在准备的 Compose OTel/Holmes 环境不纳入本报告的通过证据。

## 进入此首片前仍须完成的最小事项

1. **真实来源和精确身份。** 在明确分配的环境/费用合同内，验证首片实际工具的数据源、查询时间窗、版本/身份映射、留存/新鲜度、正常流量与故障事实；至少一个正常和一个故障调查形成来源可回查的记录，并记录固定 Holmes 基线/必要适配及不匹配因素。模型成功调用 fixture 或容器启动不足。依赖 C3 §8/12、M0 §4–5 和本轮合同。只需首片声明的数据源；未接入来源列为缺口，不能据此认证恢复。
2. **真实权限和运行边界。** 证明调查身份无法写入、执行任意 shell/SQL、读取越界目标；模型参数不能扩大真实范围。查询入口应是只读权限/鉴权代理或专属隔离实例，不能把可写遥测后端加一个工具名白名单当成全部安全边界。冻结请求数、成本预留、超时、最大结果和取消清理上界；验证阻塞查询可终止，后续调用被取消/撤权挡住。工程注入身份与调查身份分开。现有 180 秒模型超时等只是特定实验/候选值，不能自动变成首片已批准全部数值。
3. **有界 PG 状态验证，而非先造完整运行时。** 用小型 M0 实验验证 C3 §6–7 的接收与任务创建同事务、持久幂等键；模型响应/工具计划提交前后、工具结果提交前后、结论发布前后的断点重建。至少证明已提交观察保持原时间/配对、只重做未完成步骤、未知费用仍占用、DB 不可用不确认新输入/发送新请求。使用真实 PG，可用可控模型替身精确注入，并按 M0 §3 补关键真实 DeepSeek/PG 组合证据；无需让每个故障都产生付费请求。
4. **持久人工优先级与兼容边界。** 同一小型实验覆盖跟进/取消提交后旧任务返回：control version、Run、owner/epoch/有效 lease 条件不满足则不得覆盖新决定；重启后仍取消且无新调用。覆盖租约重派迟到结果、旧许可/证据无法安全重建时 handoff，以及兼容版本重建/不兼容版本 blocked(INCOMPATIBLE_STATE)。M0 §3 明确要求有界升级兼容机制；完整部署升级演练留 M3。若首片采用 LangGraph，补其在批准业务日志权威下的最小兼容/重建收益证据；不要另选架构或先建编排平台。
5. **冻结首片外部验收包并独立复核。** 映射原 F1/F2/F3/F7/F12 步骤的子集，固定输入/证据/最终状态/人控事件的观察入口、版本、运行环境、正常/故障/缺证据/错目标/注入/超时/预算拒绝/取消迟到/重启用例与数值边界。质量 rubric 和基线阈值要有开发证据，不从两次 fixture 调用估计泛化能力；失败计入分母。正式候选保留集结果不是 M0 入口前提，评测协议须在对应评测前冻结。达到证据条件后再由主任务记录入口决定及日期、更新 SPEC/ROADMAP；本审查不自行放行。

## 两处合同适配必须先讲清楚

**主动查询验收。** `scripts/m0/outcomes.py:329–344` 要求 outcome evidence 同时属于 evaluator.captured_evidence 和 agent_input.visible_evidence。当前 schema 是静态公开用例，直接把“调查开始时输入”作为整场景固定快照，会拒绝真实调查后来查询到的合法证据。冻结前明确由可信 harness 记录哪些证据在何时实际返回给调查者，并据该可见证据日志核验 outcome；必要时版本化扩展公开合同。不能把全部最终证据提前塞进初始 prompt，也不能由模型自报 visible 或把 evaluator 注入答案暴露给调查者。Subject 的 control_generation 与 outcome 比较亦需以可信最终控制记录验证，不能把初始 generation 当成人工跟进后的唯一期望。

**Compose 身份建议，尚未批准实施。** `scripts/m0/outcomes.py:29–34` 强制 `cluster_uid/namespace/resource_uid/revision`。Compose 的 project/service 名不是 Kubernetes UID，container ID 是实例身份，也不等于跨重建的服务身份；不要制造 `cluster_uid=compose`、假 namespace 或把 image tag 当不可变 revision。

建议以新 schema 版本引入显式 `kind=kubernetes|compose` 的目标变体：Kubernetes 保留现字段；Compose 使用可信注册的 integration/environment 身份、project/service 标识、实际 container instance ID 与镜像 digest/部署配置 revision 的明确关联。environment 的本地注册 ID 要标明来源，不能声称由 Kubernetes 提供；Compose labels、实际运行实例与遥测 service/instance 标签须实证核对并防名称重用误配。稳定服务目标与一次容器实例分开，缺少关联时返回 unknown/denied。先取得实际映射，再审查具体字段和匹配规则；这是对真实目标来源的合同适配，不是新增多集群平台。保留旧 v2 合成 schema，不能悄悄改写历史数据。Compose 不提供 pod/deployment UID 的证据，因此不能借此通过 F6 保留的 Kubernetes 恢复用例。

## 可留给产品实施和后续验收的工作

入口前要冻结与验证关键机制，**无需预先完成** FastAPI/Jinja/SSE 全部页面、完整调度器和所有用户操作、发布观察/独立恢复/复盘知识、告警风暴全量容量、完整发布升级、72 小时 soak、正式候选保留集结果。C3 §9 的认证/代理身份、CSRF/Origin、幂等/预期版本、SSE 持久游标应写入首片验收并在实现中完成，不需要先交付一套 UI 才允许实现 UI。首片涉及取消和持久保存，不能把对应关键人控/重建机制全部推迟到产品完成后再发现合同不兼容。

这些后续项目仍属于完整版本，全部原 feature passes 保持 false。入口可按首片范围打开；任何缺失证据都必须保留其具体边界，不得把正常基线或一次调查成功转换为全产品实施/验收完成。
