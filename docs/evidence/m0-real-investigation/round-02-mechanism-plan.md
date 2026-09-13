# M0-02：动态证据合同与最小步骤恢复候选方案

状态：**候选，供独立实施前审查；尚未授权本执行者开始代码实现，尚无本方案运行通过证据。**

日期：2026-09-10。工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，分支 `chore/m0-02-convergence`。本文件仅记录有界设计验证方案，不打开产品实施门槛，不修改现有 v2 合同或功能 passes。

## 目标、依据与边界

补齐首个调查流程的动态可见证据、真实 Compose 身份与 PG 步骤重建接缝。依据 [SPEC](../../../SPEC.md)、[C3 §5–7](../../design/technical-proposal-2026-09-07.md)、[ADR-0003](../../adr/0003-business-state-recovery-authority.md)、[M0 计划](../../plans/m0-validation-plan-2026-09-07.md)与[首纵向流程计划](../../plans/first-vertical-investigation-2026-09-09.md)。PostgreSQL 已提交业务记录继续是唯一跨进程恢复权威。

费用、请求总额、trace 总额和绝对期限以 [round-02-authorization.json](round-02-authorization.json) 为准；**每 Run/步骤子额度、输入输出容量、单请求与工具总 wall-time、重试次数及清理上界均为 pending calibration**，必须在实际调用前形成并冻结相容的执行合同。不得重置旧账本或释放旧 unknown 占用。本执行者不调用真实模型、不控制环境、不读取 `.env` 或实际 provider 私有推理内容；父执行者负责环境、报告与真模型调用。

只实现 M0 有界实验模块：版本化外部合同、PG 步骤存储和断点驱动入口。单 worker、单活跃调查；不建设 UI、通用调度平台、多版本滚动系统、观察者或恢复健康认证能力。网络查询权限仍由可信边界执行；模型没有 shell/SQL/云命令或目标写权限。评测 ground truth 不进入调查输入。

## 已核查事实与复用边界

- `scripts/m0/outcomes.py` 的 v2 checker 绑定初始 Subject/control generation、初始 visible evidence，且要求 evidence/action target 等于主体；不能直接表示动态依赖查询。
- `scripts/m0/budget.py` 已实现累计预留、unknown、稳定请求身份、数据库时钟期限与成功提交后才返回 created。它尚无步骤记录、请求次数或人控/lease 校验，且 API 自开事务。
- `scripts/m0/adapters.py` 的 History 保存完整消息组和同 provider/Run 配对，但明确为 attempt-local，不是恢复权威。
- `scripts/m0/runtime.py` 仅检查 Python/依赖锁一致性。
- `scripts/m0_control_probe.py` 已有 generation/epoch/lease 条件采纳和持久快照验证；没有 current Run/owner、接收记录、ModelStep、ToolOperation 或新请求发起门控，不能将此探针称为步骤恢复通过。

## v3 外部合同

保持原 `m0-public-v2` DTO、fixture、schema 和 checker 不变；新增显式 `m0-public-v3` 版本及分派，拒绝隐式升级。外部入口仍为 `IncidentScenario -> IncidentOutcome`，模型只生成候选报告，可信运行器填充最终执行事实。

### 关注主体、授权范围与来源

`Subject` 表示关注对象；可信 `AccessScope` 表示获准 integration、service、source interface、时间窗和 policy revision；`EvidenceSource` 记录实际来源与实际覆盖目标。三者独立，不通过把来源改成主体来通过检查。

目标使用明确判别变体，保留 Kubernetes 原结构；Compose 变体区分 integration/deployment instance、逻辑 service 与不可变 container ID/image digest，并绑定版本化 telemetry service/instance 映射。容器重建或同名实例不静默继承身份，未知映射明确 unknown。混合 trace 列出实际覆盖对象；查询相关性不产生权限。对超出授权的混合结果，可信边界在交付前拒绝或按已冻结规则过滤并登记新 view；不能把未授权内容直接交给模型。

### raw artifact、投影与实际交付

可信 runner 保存原始 artifact ID、精确 bytes 的 SHA-256、来源、规范化查询、请求窗口及原采集时间。投影记录 raw hash、view hash、projection version、精确交付内容、保留字段/片段、总量/保留量与截断标志；确定性验证投影与原 artifact 的关系。原内容不需要作为外部模型 DTO 的正文。

`DeliveryRecord` 绑定 Run、ModelStep、physical request、InputSnapshot 和 view hash。工具返回只证明 runner 得到内容；实际序列化输入须登记其具体 view。压缩或截断产生新 view 和 hash，保留旧记录。区分 prepared、dispatched、response committed；发送超时不能证明 provider 已消费。报告引用只能来自生成报告的已提交请求快照及其明确保留的历史消息，不可引用只存在本地 raw 归档、已被截掉的片段。

工具证据只有 PG 提交后才能被消费。checker 从可信交付日志重算可引用集合，拒绝模型自报可见、hash 不符、伪造来源、缺失关联和未授权交付。哈希一致只证明内容身份，不自动证明因果结论正确。

### 当前人控与结果

Scenario 保存初始输入及外部人控事件；Outcome 的最终 Subject/current Run/control generation 与 PG 最终业务快照核对，不与初始 generation 强制相等。候选 claims/gaps/conclusion 不持有权限、最终人控或发布权。保留 completed-but-uncertain 与 failed/incomplete 的区分；首片没有独立 HealthProfile 证明时不能宣称恢复健康。

## 最少模块与持久记录

候选文件边界为新增 `scripts/m0/outcomes_v3.py`、`scripts/m0/step_store.py`、`scripts/m0/step_store.sql` 及一个断点实验入口，并增加对应 schema/定向测试。避免重写 v2、框架或环境连接器。预算复用现有算法；为共享事务可做经审查的小型事务内 helper 提取，不建设第二套账本权威。

实验独立 schema，创建时保留旧 schema/历史；不修改已有真实实验表。以下逻辑记录可合并到少量表，但必须保留唯一约束和引用：

1. 接收/主体/Run：幂等键、payload hash、输入水位、current Run、generation、状态、owner、epoch、lease、版本、绝对 deadline。
2. ModelStep：稳定 `(run_id, context_segment, logical_round)`、不可变 InputSnapshot、完整校验响应、工具计划、候选报告。
3. ToolOperation：稳定 `(step_id, tool_ordinal)`、参数、物理尝试、成功/失败/取消/unknown 结果及 artifact/view 引用。
4. 交付/审计：追加记录及采纳决定，和账本 physical request identity 关联；预算预留、请求/工具次数不得随重启减少。

受限协议状态只能在同 provider/Run 中供协议续传，不能进入报告、知识、trace 或 judge。测试使用合成哨兵检查边界，不读取或评分实际隐藏推理；真实续传的必要存取由受限程序完成。快照绑定可恢复的实际内容及哈希，不仅保存不可回读的版本名字。

## 提交与重建规则

- 接收、主体与初始 Run 同事务，提交成功后才确认。同键同 payload 返回原调查，不同 payload 明确冲突。
- 固定 ModelStep 输入和新物理请求的预算/次数预留同事务。逻辑步骤 ID 稳定；重试创建新 physical request ID，保留旧未知占用。现 `reserve()` 自开事务，不能把两个顺序调用声称为原子提交。
- 完整模型响应和工具计划原子提交后才执行工具；流式部分响应不授权任何工具。
- 工具结果和证据元数据提交后可消费。已提交工具结果复用为历史观察，保留采集时间；只补未完成操作。工具成功而提交失败允许有界重新查询，物理尝试和采集时间真实记录，不承诺外部 exactly-once。
- 每个 tool call 有成功、失败、取消或 unknown 记录；unknown 不能当成功。构建下一请求前确保消息组完整配对，缺项按冻结规则有界重查或 handoff。
- 发布通过单一条件更新检查 subject/current Run/control generation/owner/epoch/有效 lease；候选发布幂等，迟到拒绝留审计，不能改写业务最终状态。
- 取消/纠正提交后不发起新模型或工具请求，并使旧结果不可采纳。预算预留与网关发起均核查当前权威；单 worker 控制处理与实际发起须有共同串行化边界，测试明确其线性化顺序。取消前已发出请求只能有界清理，不能承诺撤销。不得在检查后排队等待任意时间，再不复核就发起。
- worker 重建领取新 epoch/owner/lease；旧 attempts 无法续租或采纳。保持原 deadline、次数和预算；数据库不可用不确认新输入、不发起未记录调用。
- 不兼容的模型协议、工具或业务状态版本持久转为 `blocked(INCOMPATIBLE_STATE)` 并 handoff，原记录保留。显式新 Run 只能继承仍获授权业务事实，不能搬运跨 provider/Run 私有协议状态；实验总预算与旧 unknown 不重置。撤权后旧受限证据不得进入新输入，无法安全重建则阻塞。

## 外部验收矩阵

每个机制用例先固定一次确定性断点验收，实际质量/重复次数另待校准冻结。断点是测试安排；判据只看请求日志、消息配对、持久业务事实和最终 Outcome，不检查模型思维链或要求特定图结构。

| 用例 | 外部判据 |
| --- | --- |
| 接收提交前后中断、ACK 丢失重投 | 未确认不冒称接收；已确认不丢；同键只有一个调查；异 payload 冲突 |
| 模型响应提交前后中断 | 提交前无工具；unknown 费用保留；提交后不重调已完成模型步骤 |
| 多工具间中断 | 已提交结果复用，仅补未完成操作；下一请求消息 ID 全部配对 |
| 工具成功但结果提交前中断 | 有界重查或 handoff；无虚构已提交证据；真实重复查询留痕 |
| 发布前后中断/ACK 丢失 | 最终发布一次；候选与历史审计保留 |
| cancel/correction 与发起/返回竞态 | 新 generation 保留；取消线性化后新请求为零；旧候选不覆盖 |
| 错 Run、错 owner、旧 epoch、过期 lease | 分别拒绝，原状态不变；不能仅测 generation |
| 重启时 unknown/次数/期限耗尽 | 占用、次数不减，期限不延长，无新请求 |
| 动态依赖与混合 trace | 获准实际来源可引用；未授权查询/交付拒绝；不伪标主体 |
| raw 有但 view 未交付/被截掉 | 引用拒绝；view/hash/投影篡改可检测 |
| 相同 service 名、容器实例变化 | 不默认为旧实例证据；实际映射版本可回读 |
| 不兼容版本与显式新 Run | 旧记录完整且 blocked；允许业务事实可接续，私有协议不越界 |
| 撤权及数据库不可用 | 不使用旧受限证据，不确认未持久输入，不发起未记录调用 |

先用可控模型替身和真实 PG 制造断点并记录业务时间线；再由父执行者在本轮独立分配的实际额度内用 DeepSeek/PG 校验关键续传链路。替身机制、真实 PG、真实模型组合分别报告，不把局部探针称为完整恢复通过。实际响应未完成/不兼容/预算失败均保留。

## 审查与完成条件

由未参与此方案的全新上下文 Agent 在实现前审查身份、交付证明、控制发起竞态、事务原子性、私有协议边界及预算延续；本方案作者不是独立验证者。父执行者给出审查结果与修订结论后，才分配 v3/step_store 实现。

本工作项完成需要上述必要确定性用例、真实 PG 断点工件、实际组合的适用验证、修复后的独立复验，以及父执行者统一维护的报告/任务状态。当前仅方案落盘；没有任何测试通过、产品验收或实施入口已开的声明。
