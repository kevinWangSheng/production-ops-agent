# F6 验收与采纳合同测试

验收测试由未参与实现的 Agent（Codex）按合同编写，依据为
[F6 五步原文](../../feature_list.json)、[Recovery observations](../../PRODUCT-CONSTRAINTS.md#recovery-observations)、
[C3 §4、§10、§13](../design/technical-proposal-2026-09-07.md) 及
[M1-02 计划](../tasks/2026-10-03-m1-02-recovery-observation.md)。
仅查看公开领域接口、外部验收入口及现有测试；未读取并行实现 worktree。
实现者不能为通过测试而修改断言；接口提议需按合同对齐后接线。

## 现在可执行的领域合同

[合同测试](../../tests/contracts/test_f6_observation.py) 共 54 个参数化实例：

- `current_sample…`、`equal_window_end…`：当前合法结果可采纳并推进水位，重试序号不能重复采纳；C3 §10「采样与提交」，支撑 F6 第 1、5 步。
- `invalid_sample…`：旧控制版本、旧 observation generation、规则 revision 不符、外会话、相等/倒退序号、倒退窗口只作历史；C3 §10 共同采纳条件，支撑全部步骤。
- `only_open_lifecycles…`、`suspension_and_original_deadline…`、`revoked_or_ended_authority…`：生命周期、暂停、原期限和授权栅栏；C3 §4 暂停、§10 授权与采纳、§13 期限，支撑全部步骤。
- `at_most_one_active_sampling_job`：每会话最多一个活动任务；C3 §10，支撑全部步骤。
- `no_profile…`、`missing_required_signals…`、`nonhealthy_samples…`：无 profile、必要信号缺失不能确认健康；no_data/stale/timeout/failed/degraded 可采纳，但不延长健康窗口；C3 §10 健康规则与 unknown 处理，支撑 F6 第 1–4 步。
- `observation_ends…`、`human_actions…`、`recovery_confirmation…`、`human_close…`：观察阶段才能确认恢复，未确认回 open，人工关闭不是 resolved，人工重开先回 open；C3 §4 生命周期、§10 结果所有权，支撑 F6 第 1、3、4 步。

`adopt_sample` 按公开前置条件只用于 `evaluate_sample` 通过之后；
这些测试不把它误当完整事务边界。纯函数层未能表达的 owner/epoch/lease、
当前 scope generation、暂停后的重新授权、持久次数预算与原子下一任务安排，
留给 M1-02 第 2–4 步集成验证，不能从上述通过结果推断已实现。
本次未发现所测公开合同与现有函数不一致，xfail 为 0；以后如发现实际不一致，
按 C3 原文写 strict xfail 并记录失败，不能改产品代码或削弱断言。

## 外部场景及接线门槛

[外部验收场景](../../tests/acceptance/test_f6_recovery.py) 共 26 个参数化实例，现全部 skip：

- F6 第 1 步 `step1_requires_all_signals…`（2）：人工处置后六类信号齐备，120 秒仍 observing_recovery，180 秒才 resolved；C3 §10 健康规则与持续窗口、§4 生命周期。待 M1-02 第 4 步。
- F6 第 1 步 `step1_each_required_signal_threshold…`（4）：deployment、pod、错误、延迟分别越过健康条件；`step1_old_or_discontinuous_data…`（3）：陈旧、覆盖缺口、处置前数据均不能形成健康持续窗口，到期 unknown 交接；C3 §10 阈值、新鲜度、覆盖与整改后新数据。待第 4 步。
- F6 第 2 步 `step2_falling_errors…`（4）：撤流量或流量 99 低于门槛 100、错误率为零，窗口到达仍 unknown，到原期限交接回 open；C3 §10 有效流量、unknown 有界继续。待第 4 步。
- F6 第 3 步 `step3_each_missing…`（6）：逐项移除 deployment、请求量、错误、延迟、pod、依赖遥测，到期 unknown 交接；C3 §10 必要信号缺失不能健康。待第 4 步。
- F6 第 4 步 `step4_continued_dependency…`（2）：其他信号正常、依赖持续异常，期限前 observing_recovery、期限后 open 交接，无部署、回滚或发布门动作；C3 §10 仍异常有界继续，§13 写操作为零。待第 4 步。
- F6 第 5 步 `step5_replay…`（4）：恢复、撤流量、缺测、持续异常分别重放；只传持久采样、冻结 profile 和处置时间，不传调查叙述，禁止遥测与模型访问，判定、窗口、生命周期和原因均须一致；C3 §10 记录来源/结果，M1-02 第 2、5 步持久依据与离线重放要求。另 `step5_replay_recomputes…`（1）只篡改已存判定 metadata，保持原始信号与冻结 profile 不变，重放须重算原正确结果，不能复述错误判定。待第 5 步。

每个场景核查最终生命周期、恢复判定、交接原因、采样原值与查询/来源/时间/
证据引用/hash，以及完整产品动作审计。动作白名单只允许只读查询、人工处置登记、
产品自己的观察/生命周期记录与交接；工程故障注入不属于产品动作。
此处 healthy 表示采样信号健康；只有 lifecycle=resolved 才表示持续窗口满足后的恢复确认。
120 秒健康采样不能冒充恢复确认。skip 不是 F6 通过；第 6 步仍须真实实验环境执行。

## 最小外部接口提议（尚未批准为产品 API）

保留 `IncidentScenario -> IncidentOutcome`，在测试 harness 提供：

- `run(scenario, profile, handled_at, observations, until)`：将合成原始遥测与人工登记送到真实产品驱动；不接受预设判定。
- `IncidentOutcome` 增加 `incident_lifecycle`、`recovery_verdict`、`recovery_reasons`、`healthy_window_seconds`、`observation_ended`、`recovery_samples`、`recovery_profile`、`recovery_handled_at`、`model_requests`；原始观察与重放均检查完整模型调用审计为空；保留现有人工交互、交接原因、actions、permissions。现有 `final_state` 属于调查 Run，不能冒充事故生命周期。
- 采样包含身份、目标、两类 generation、profile revision、序号、绝对窗口、disposition、outcome；每个信号含 value/source/query/observed_at/evidence_id/raw_sha256。profile 保存冻结的必要信号、阈值、最低流量、新鲜度、覆盖/窗口/频率、期限和次数。
- `replay(profile, handled_at, samples, allow_telemetry=False, allow_model=False)`：重算相同可观察结果，额外返回 `external_queries`、`model_requests` 空审计。输入只来自已存依据，不允许复制已存判定当重算。

原因码 `INSUFFICIENT_TRAFFIC`、`REQUIRED_TELEMETRY_MISSING`、`MISSING_SIGNAL:<name>`、
`DEPENDENCY_UNHEALTHY`、`CONTINUED_DEGRADATION` 是可读接口提议；合同要求的是相应语义。
接线 fixture 目前显式失败；解除 skip 前须提供真实驱动，不能用预制成功 outcome。
测试数字为合成边界，真实环境的数值仍由第 1 步基线校准后冻结。

## 本次执行证据

- 定向命令：`.venv/bin/python -m pytest tests/contracts/test_f6_observation.py tests/acceptance/test_f6_recovery.py -q`：54 passed、26 skipped、0 xfailed。
- `make check`：退出 0，锁/Ruff/mypy 通过，2654 passed、337 skipped、2 既有架构债 xfailed；独立静态审查发现的四项场景缺口已补齐并复验。详见同 PR 的[测试作者任务记录](../tasks/2026-10-07-f6-acceptance-tests.md)。
- 未启动实验环境，未执行模型或外部遥测调用；未改 `passes`。
