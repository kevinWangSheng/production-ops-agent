# M1-01 产品包与领域类型/状态机

- 状态：进行中（实现与本地检查完成；独立审查未执行，未推送、未建 PR）
- 更新日期：2026-09-14
- 依据：[C3 技术方案第 2、3、4 节](../design/technical-proposal-2026-09-07.md)、
  [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md) 的 Product workflow 与
  Runtime and human control requirements、[AGENTS.md](../../AGENTS.md)
- 工作区：worktree `.claude/worktrees/agent-a05a6ef24509ac2e6`，
  分支 `feature/m1-01-domain-types`（起点 `5af96ae`）

## 目标与范围

在 `opspilot/` 下建立产品包，实现 C3 第 4 节列举的持久对象类型与状态机。

**范围内**：类型定义、状态机、以及让这三组语义可判定所必需的纯函数
（条件更新、水位推进、采纳判定）。

**范围外**：持久化与 DDL、FastAPI 路由、HTTP、模型调用、Tool Gateway、页面。
不修改 `pyproject.toml` 的依赖或打包配置，不动 `scripts/` 下的 M0 代码，
不改 `feature_list.json`，不改 `SPEC.md` 的门槛陈述。

本任务是实施准备性质的类型层。**不是产品功能验收**：`feature_list.json`
的 11 个 `passes` 全部保持 `false`，SPEC 门槛陈述未改动。

## 前提与完成条件

- 前提：ROADMAP 记录 2026-09-13 用户选择 gate 决策 B「有界开放 M1-01」；
  本任务对应 [M1-01 拆分](../evidence/m0-real-investigation/m0-exit-matrix.md)
  中「PG 业务状态与断点」子任务的类型层部分，不含其持久化部分。
  依赖仅使用已在 m0 依赖组中的 `pydantic==2.13.5`。
- 完成条件：`make check` 通过并保留实际输出；每个状态机有拒绝非法转移的
  确定性测试；类型与 C3 第 4 节逐项对应，缺项与多出项在本记录写明理由。

## 必要上下文

- C3 第 4 节「核心数据与生命周期」：持久对象清单与三组语义区分。
- C3 第 6、7、10、11 节：调度/租约、恢复与提交一致性、观察采纳规则、outbox。
  第 4 节的对象在这些节里才有可判定的规则，实现时按节引用，未越出第 4 节的对象集合。
- `scripts/m0/outcomes.py` 的 `DTO` 写法（`extra="forbid", strict=True, frozen=True`）
  与 `scripts/m0/contracts.py` 的固定错误码写法：本包沿用同一风格，但不 import M0 代码。
- [工作包 3 控制合同记录](../evidence/m0-real-investigation/round-06-control-contracts.md)：
  该轮把「pause/resume 完整状态机」和「独立 observer 授权」明确记为产品缺口，
  本任务在类型/状态机层补上这两项的确定性表达（仍不含 PG 持久化）。

## 执行进展与证据

### 交付文件

新增 `opspilot/` 包（10 个模块）与 2 个测试文件，未修改任何既有文件：

| 文件 | 内容 |
|---|---|
| `opspilot/__init__.py` | 包声明 |
| `opspilot/domain/base.py` | `DTO`、标量别名、`DomainError` 固定码、`StateMachine` |
| `opspilot/domain/intake.py` | `Integration`、`Target`、`InputEvent`、投递键 |
| `opspilot/domain/control.py` | `ControlState`、`SuspensionState`、`ScopeVersions`、条件更新与优先级判定 |
| `opspilot/domain/subjects.py` | `SubjectRef`、`Incident`、`ReleaseObservation` 及两台状态机 |
| `opspilot/domain/runs.py` | `Run`、`ModelStep`、`InvestigationResult`、`Conclusion` 及结论归属 |
| `opspilot/domain/evidence.py` | `ToolOperation`、`Evidence` 及两台状态机 |
| `opspilot/domain/scheduling.py` | `Job`、`Schedule`、执行键、租约身份 |
| `opspilot/domain/observation.py` | `ObservationSession`、`HealthSample`、采纳与健康判定 |
| `opspilot/domain/records.py` | `SubjectEvent`、`AuditRecord`、`ExportOutboxEntry` |
| `opspilot/domain/knowledge.py` | `Postmortem`、`KnowledgeRevision` |
| `tests/test_domain_state_machines.py` | 10 台状态机的非法转移拒绝 |
| `tests/test_domain_contracts.py` | 三组语义与逐对象不变量 |

状态机以显式 `(state, trigger) -> state` 表实现，表外的组合一律
`ILLEGAL_TRANSITION`，不做推断。10 台机器登记在 `STATE_MACHINES`：
`incident`、`release_observation`、`run`、`tool_operation`、`evidence`、
`job`、`observation_session`、`export_outbox`、`postmortem`、`knowledge_revision`。

### 与 C3 第 4 节的逐项对应

| C3 第 4 节条目 | 本包实现 | 状态机 |
|---|---|---|
| `Integration / Target` | `Integration`、`Target` | — |
| `InputEvent` | `InputEvent`、`delivery_key`、`is_duplicate_delivery` | — |
| `Incident` | `Incident`（生命周期、`control`、`current_run_id`） | `INCIDENT_LIFECYCLE` |
| `ReleaseObservation` | `ReleaseObservation`、`ReleaseIdentity`、`ReleaseObservationPolicy` | `RELEASE_OBSERVATION` |
| `Run` | `Run`、`ModelProfile`、`BudgetLedger`、`claim_run`、`advance_watermark` | `RUN_EXECUTION` |
| `ModelStep` | `ModelStep`、`ToolPlanEntry`、`step_id`、`tool_operation_id` | — |
| `ToolOperation / Evidence` | `ToolOperation`、`QueryScope`、`QueryWindow`、`Evidence` | `TOOL_OPERATION`、`EVIDENCE_ADOPTION` |
| `Job / Schedule` | `Job`、`Schedule`、`execution_key`、`check_execution_identity` | `JOB` |
| `ObservationSession` | `ObservationSession`、`HealthSample`、`evaluate_sample` | `OBSERVATION_SESSION` |
| 主体事件、审计记录、导出 outbox | `SubjectEvent`、`AuditRecord`、`ExportOutboxEntry` | `EXPORT_OUTBOX` |
| `Postmortem / KnowledgeRevision` | `Postmortem`、`KnowledgeRevision` | `POSTMORTEM`、`KNOWLEDGE_REVISION` |
| 第 4 节「全局与目标级暂停」小节 | `SuspensionState`、`ScopeVersions`、四个判定函数 | — |

第 4 节 bullet 列表 11 项全部覆盖，无缺项。

### 多出的项及理由

以下类型不在第 4 节 bullet 列表中，但都是该节正文明确要求的语义载体：

- `ControlState`、`SuspensionState`、`ScopeVersions`：第 4 节
  「人工控制与结果所有权」「全局与目标级暂停」两个小节的状态。
  没有它们无法表达 `expected_version`、`control_generation` 与 scope generation。
- `Conclusion`、`ConclusionAcceptance`：第 4 节要求「每个主体只有一个当前 Run
  可以更新该主体的调查结论」「旧 Run 的迟到结果只能保存为历史」。
  注意第 4 节的 `Run` bullet **没有**把结果列为 Run 字段，因此结论按主体归属单独建模。
- `SubjectRef`：第 4 节「Run 显式绑定主体类型和 ID」的载体。
- `ReleaseIdentity`、`ReleaseObservationPolicy`：第 4 节「发布身份使用来源唯一
  发布 ID、目标和不可变发布版本」，以及第 10 节固定期限/跨度/频率配置。
- `ModelProfile`、`BudgetLedger`：`Run` bullet 里「模型配置」「累计预算」的载体。
- `QueryScope`、`QueryWindow`、`ToolPlanEntry`：`ToolOperation` bullet 里
  「查询范围」「时间窗」与 `ModelStep` bullet 里「工具计划」的载体。
- `HealthSample`、`SampleAcceptance`：`ObservationSession` bullet 里
  「已采纳采样水位」需要一个采样对象才能表达水位推进与拒绝。
- `DTO`、`DomainError`、`StateMachine`：基础设施，不是持久对象。

### 未建模的项及理由

- **`HealthProfile` 本体未建模**。第 4 节只要求 `ObservationSession` 绑定
  「健康规则版本」，本包以 `health_profile_revision: Text | None` 表达。
  profile 的内容（必要信号、阈值、最低样本、新鲜度、观察窗口）由第 10 节定义，
  属于另一项工作。因此 `confirms_health()` 取 `required_signals_present`
  这个由 Observer 依 profile 计算好的布尔量作为入参，不在本包内重算阈值。
  缺 profile 时仍可调查，但 `confirms_health()` 恒为 `False`。
- Run 的「模型配置」只记录版本标识，不含 provider 参数细节；
  「累计预算」只记录计数与金额，不含预算策略与预留算法。
- 无持久化、无 DDL、无路由、无模型调用。观察采纳里的
  owner/epoch/lease 校验落在 `check_execution_identity()`（Job 侧），
  `evaluate_sample()` 只判定会话绑定、水位与生命周期。

### 三组语义的实现方式

1. **事故与发布观察分开**：两台状态机状态集合不相交（有测试断言）。
   `ReleaseObservation` 的 `incident_id` 默认 `None`，
   `pending → observing → healthy` 全程无需任何 Incident。
   只有 `anomaly_detected` 要求带上创建或关联的 `incident_id`。
   `ObservationSession` 的 purpose 与 subject.kind 必须匹配，
   空 Incident 不能顶替发布主体。
2. **Run 执行状态与调查结果分开**：`Run` 没有 result 字段（有测试断言）；
   结果是 `InvestigationResult`，经 `Conclusion` 提交给主体。
   `evaluate_conclusion()` 返回 `current` 或 `history_only`，
   不抛异常——因为 C3 要求迟到结果**保留为历史**而不是丢弃。
3. **人工控制**：`apply_control(state, action, expected_version)` 匹配才应用并
   `control_generation + 1`，不匹配抛 `CONTROL_CONFLICT` 且不改状态。
   全局/目标暂停是独立的 scope 状态，优先于主体 resume、automatic 模式
   和 human_owned 下的单独观察授权；但不阻断事件接收与人工操作记录。

### 验证记录

- 检查对象：本分支全部改动；版本 Python 3.12.13、pytest 9.1.1、
  ruff 0.16.6、uv 0.10.8、pydantic 2.13.5。
- 命令：`make setup`（本 worktree 首次建 `.venv`）、`make check`。
- 结果：

  ```
  OK | 项目 Python 3.12 与开发工具 | Python [3, 12, 13]; tools={'pytest': '9.1.1', 'ruff': '0.16.6'}
  uv lock --check --offline --no-python-downloads → Resolved 38 packages
  .venv/bin/ruff check .        → All checks passed!
  .venv/bin/ruff format --check . → 401 files already formatted
  .venv/bin/python -m pytest    → 1036 passed, 54 skipped in 24.16s
  ```

  `make check` 退出码 0。其中本任务新增 236 项（`tests/test_domain_*.py`）。
  54 skipped 全为既有 M0 的 PostgreSQL / 真实模型 opt-in 用例，本任务未触及。
- 结论：类型层的确定性检查通过。这是**静态类型与单元合同测试**，
  不是集成运行、故障注入、soak 或生产证明，也不代表任何 feature 验收。

### 执行中的设计判断

按 C3 原文可直接推出的，不在此列；以下是我做的判断，需要复核：

1. **Incident 是唯一没有终态的状态机**。因为 closed 可人工 reopen。
   其余 9 台都有终态。测试用例显式记录了这一点。
2. **`resolved` 只能由 `observing_recovery` 经 `recovery_confirmed` 到达**，
   没有任何人工 trigger 指向 `resolved`。这直接编码
   「人工关闭不等于系统独立确认恢复」。
3. **允许 `resolved --human_reopen--> open`**。C3 原文只写「已关闭事故…
   人工也可显式重新打开」。我把 reopen 也开给 resolved，理由是 resolved 不是
   人工终点，且第 10 节说「新异常或人工 reopen 使用新的阶段和授权」。
   **这是我的推断，不是 C3 字面。**
4. **`blocked` 不能回到 `running`**。第 7 节「禁止静默换版本续跑」，
   接续用新 Run。`blocked` 的出口只有 `human_cancel` 和 `handoff_failed`。
5. **`paused` 是 Run 的状态，但不是 ReleaseObservation 的状态**。
   这是 C3 原文的区分（第 4 节 Run 状态列表含 paused；发布观察
   「暂停保留状态并停止调度」），不是我加的不一致。
6. **采纳类决策返回 disposition 而不抛异常**（结论与采样两处），
   因为 C3 要求失效结果保留为历史。
7. **错误分工**：字段/形状校验走 pydantic `ValidationError`
   （沿用 `scripts/m0/outcomes.py` 既有写法），转移与控制违规走
   `DomainError` 固定码（沿用 `scripts/m0/contracts.py` 写法）。
8. **Job epoch 与 Run epoch 递增规则不同**：Job 在租约过期重排时 +1（第 6 节），
   Run 在每次执行尝试时 +1（第 7 节）。已在 docstring 写明，避免被当作不一致。
9. **`healthy_window_satisfied` 要求 `now >= released_at + max(最短跟踪跨度, 健康窗口)`**，
   并且 `ReleaseObservationPolicy` 在构造时就拒绝「所需窗口超过 deadline」。
10. **暂停的目标范围存 `resource_uid` 集合，入口只接受 `Target` 实例**，
    传字符串名字直接 `INVALID_INPUT`，对应「不能以模型提供的名称决定」。
11. **`Integration` 不含凭据字段**，靠 `extra="forbid"` 使凭据字段不可表达，
    并有测试断言 `token/password/credentials/api_key` 全部被拒。

### C3 与 PRODUCT-CONSTRAINTS 的一致性

**逐条核对后未发现冲突。** 两份文件在以下四点一致，本包按一致的语义实现：
发布观察不依赖 Incident 存在；人工关闭不等于独立恢复确认；
必要信号缺失不能判定恢复；查询失败/unknown 不等同健康。

## 下一步与交接

- 未执行：独立审查（AGENTS.md 要求非平凡功能在完成前接受独立验证或审查）。
  本记录的结论全部来自实现者自检，**不得标为独立验证通过**。
- 未执行：推送、PR、合并。按本次任务指示仅本地提交。
- 需要复核的判断：上节第 3 项（`resolved → human_reopen`）最需要确认。
- 其余不确定项：
  - `pending --anomaly_detected-->` 被禁止（异常须先有采样证据）。
    若来源事件可在 pending 阶段直接判异常，这条要改。
  - `observing_recovery --observation_ended_unconfirmed--> open`：
    PRODUCT-CONSTRAINTS「Continued degradation leaves the incident open」
    我读成回到 `open`；也可读成停留在 `observing_recovery`。
  - `waiting_human --deadline_expired--> failed`：C3 未明说 waiting_human 的超时归宿。
  - `BudgetLedger` 用整数 micros 记账，C3 未指定单位。
  - `ReleaseObservationPolicy.max_samples` 只留字段；
    第 10 节说具体数值在 M0 开发校准后冻结，本次不设默认值。
- 后续工作项（不在本任务内）：这些类型的 PostgreSQL 持久化与断点重建、
  `HealthProfile` 本体、Tool Gateway 合同、Jinja/SSE 入口。
