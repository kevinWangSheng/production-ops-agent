# F6 验收与采纳合同测试

验收测试由未参与实现的 Agent（Codex）按合同编写，依据为
[F6 五步原文](../../feature_list.json)、[Recovery observations](../../PRODUCT-CONSTRAINTS.md#recovery-observations)、
[C3 §4、§10、§13](../design/technical-proposal-2026-09-07.md) 与
[M1-02 计划](../tasks/2026-10-03-m1-02-recovery-observation.md)。
仅查看公开领域接口、外部入口及测试，不读并行实现。
实现者不能为通过测试修改断言；下面区分冻结语义与可由适配器自定的产品接口。

## 现在可执行

[领域合同](../../tests/contracts/test_f6_observation.py) 42 个实例：

- 当前结果合法采纳并推进水位、相等窗尾允许但重复序号拒绝：C3 §10，支撑 F6 第 1、5 步。
- 外会话、控制/观察 generation 小于或大于当前值、规则 revision 不符、相等/倒退序号与倒退窗口只作历史：C3 §10 共同采纳条件，支撑全部步骤。
- 仅 open/observing_recovery 可采纳；resolved/closed、suspension、原期限及撤销/结束授权挡住采纳；每会话最多一个活动任务：C3 §4/§10/§13，支撑全部步骤。
- 无 profile、缺必要信号不确认健康；no_data/stale/timeout/failed/degraded 可采纳，但不延长健康窗口：C3 §10，支撑第 1–4 步。
- 观察阶段才能确认恢复，未确认回 open，人工关闭不是 resolved；纯领域状态机的 close/reopen 保留合同校验：C3 §4/§10。pause/cancel/takeover 不是生命周期触发器，paused/cancelled 不是生命周期取值，已删除这些空转参数，不计入覆盖。

`adopt_sample` 只在 `evaluate_sample` 通过后调用，不冒充事务边界。
owner/epoch/lease、scope generation、暂停后重新授权、持久预算与原子任务安排，
仍需第 2–4 步集成验证。当前所测公开合同没有不一致，F6 xfail 为 0。

另有 [I/O 桩与主体绑定](../../tests/f6_boundary_support.py) 的 11 个可运行夹具检查，
位于[场景文件](../../tests/acceptance/test_f6_recovery.py)末尾：三个环境写动作均记录并抛错；
即使驱动吞掉遥测拒绝，重放计数也能检出；错事故结果被拒绝；run/replay 吞掉模型拒绝仍留下调用计数；缺失 reader、None 或非 bytes 均被拒绝；遥测桩确实返回测试提供的原始信号字节。
这些是夹具检查，不是 F6 产品验收通过。

## 外部场景及接线门槛

[外部场景](../../tests/acceptance/test_f6_recovery.py) 40 个实例均 skip：

- 第 1 步（22）：持续窗口前后 2、逐信号阈值 4、陈旧/时间缺口/处置前数据 3、同目标双事故隔离 4、H,H,D 与 H,H,D,H,H 窗口重置 2、退化后重新累计并达到持续窗口 2、跨目标身份拒绝 5。依据 C3 §10 健康规则、主体授权与持续窗口，§4 生命周期。窗未满只要求 `recovery_confirmed=False`，不冻结中间 verdict；两个事故互换被处置身份，另一个的生命周期、观察会话和采样均不得变动。合法 stale/覆盖缺口采样必须留底且 adopted、保存已用次数，恢复结果 unknown，全 stale 采样健康窗为 0；处置前数据单列，不冻结其采纳方式。H,H,D,H,H 精确为 120 秒；预先冻结更长期限与次数的 H,H,D,H,H,H 达 180 秒后 resolved，证明退化没有禁用累计。跨目标场景逐项改变 integration/cluster/namespace/resource/revision，先单独登记处置，再向原授权提交错误目标采样；只保存历史，水位、生命周期、健康窗与预算不推进，原授权身份/版本与处置审计不变，不能偷偷创建新授权。
- 第 2 步（5）：零/低流量及期限前后 4，先健康有流量、后撤流量且错误率下降 1；C3 §10 有效流量与 unknown 有界继续；180 秒/3 次时会话未结束、没有交接，原期限为 300 秒/5 次。
- 第 3 步（6）：逐项移除 deployment、请求量、错误、延迟、pod、依赖；到期 unknown 交接；C3 §10 必要信号缺失不能健康。
- 第 4 步（2）：持续依赖异常，期限前 observing_recovery、期限后 open 交接，期限前 observation_ended=False 且没有交接，无环境写动作；C3 §10 有界继续、§13 写操作为零。
- 第 5 步（5）：恢复、撤流量、缺测、异常四类重放，以及仅篡改已存判定的完整性场景；只用持久采样与冻结 profile/处置时间，不用调查叙述或遥测。篡改时接受重算原正确结果，或明确 unknown + 完整性不一致；不能复述错误判定。依据 F6 第 5 步及 M1-02 第 2、5 步。

第 1–4 步待 M1-02 第 4 步 [#86](https://github.com/kevinWangSheng/production-ops-agent/issues/86)；
重放待第 5 步 [#87](https://github.com/kevinWangSheng/production-ops-agent/issues/87)；
保存依据共同依赖第 2 步 [#84](https://github.com/kevinWangSheng/production-ops-agent/issues/84)。

第 4 步的「或按审核策略重新打开」以及新异常/人工 reopen 的新观察阶段，
未纳入运行时场景：M1-02 任务记录已将人工 close/reopen 移出范围。
已有纯领域状态机测试不代表该运行时路径完成。
最低样本数能否由单样本满足由合同/profile 决定，本次不新增该冻结要求（O8）。
skip 不代表 F6 通过，第 6 步真实实验验收仍未执行。

## 必须冻结的验收清单

以下是作者维护的测试规范；实现者不能改断言来迁就实现：

- `incident_lifecycle` 沿用 subjects.py 的 open/observing_recovery/resolved/closed；本切片到期未确认回 open。合法 unknown 采样的预算消耗必须持久记录（`used_sample_count`），不能丢采样而凭空交接。恢复确认与当前采样分开；窗未满、异常、缺测或无流量均不能确认恢复，H,H,D 重置窗口，不能跨 D 累计。
- 当前场景的终态恢复 verdict、交接语义与原因码：`INSUFFICIENT_TRAFFIC`、`REQUIRED_TELEMETRY_MISSING`、`MISSING_SIGNAL:<name>`、`DEPENDENCY_UNHEALTHY`、`CONTINUED_DEGRADATION`；篡改拒绝分支 `STORED_OBSERVATION_INTEGRITY_MISMATCH`。这些是 harness 规范化码，产品原始码可不同，由适配器做保义映射。
- 所有 outcome 的 `subject_id` 等于请求主体，已存采样绑定同一主体和事故不可变目标，目标同时匹配冻结 profile 和 fixture 预种记录；跨目标结果只能 history_only，不推进生命周期、水位、健康窗或预算；同目标另一事故的持久状态、会话、采样不变。错误目标提交前后完整 observation_sessions 集合不变；若暴露 sample_jobs，其完整集合也不变，不能只核对当前授权。
- 合法样本 `disposition == "adopted"`；无效身份/授权/水位只作历史。每个信号 value/source/query/observed_at 原样保存；样本与逐信号 evidence_id 唯一；每个信号的原始 bytes 由测试在修改刺激后冻结，遥测桩按 query/绝对窗口返回。每个 evidence_id 必须能取回与测试刺激逐字节相同的 bytes，实际 SHA-256 必须与记录一致，取不回或字节不同即失败。历史采样同样校验证据绑定。
- 原始 Observer 与重放 `model_requests == ()`；重放 `external_queries == ()`。完整产品 actions 只能有只读查询、处置登记、产品自身记录/生命周期转换、交接；permissions 不能授予环境写权限。
- 除产品审计外，必须由测试拥有的环境/遥测/模型桩证明 `driver.environment_writes == ()`、`driver.telemetry_calls_during_replay == 0`、`driver.model_calls == 0`；原始 Observer/重放禁止任何模型调用；重放禁止所有遥测查询，拒绝被吞掉也要留下计数。所有相关传输必须接入桩，不允许真实网络或旁路 fallback。

## 实现者可自定的清单与最小接口提议

产品的 sample_id/evidence_id 格式、signals 内部结构、run/replay 具体签名、
字段命名及存储格式可自定；通过测试适配器投影下面的规范化视图，不能改变冻结语义。
中间 `latest_sample_verdict` 取值与中间恢复 verdict 不冻结。产品接口不必与测试字典逐字相同。

- `run(IncidentScenario, profile, handled_at, observations, until)`：真实产品驱动接收原始遥测与人工登记，不接收预设结果。
- `IncidentOutcome` 视图增加 subject_id、incident_lifecycle、recovery_confirmed、latest_sample_verdict、终态 recovery_verdict/reasons、healthy_window_seconds、observation_ended、recovery_samples、recovery_profile、recovery_handled_at、model_requests、used_sample_count。现有 final_state 属于调查 Run，不能替代事故生命周期。
- 采样视图含 subject_id、sample_id、目标（含 integration_id）、两类 generation、profile revision、序号、绝对窗口、disposition/outcome；信号含 value/source/query/observed_at/evidence_id/raw_sha256。冻结 profile 包括必要信号、阈值、流量、新鲜度、覆盖/窗口/频率、期限与次数。
- `replay(profile, handled_at, samples, allow_telemetry=False, allow_model=False)`：返回重算结果或显式完整性不一致，以及 external_queries/model_requests 审计。
- fixture 的 `recovery_runtime(recovery_boundaries)` 将桩注入所有遥测/环境/模型传输；`GuardedRecoveryDriver` 拥有 replay 模式和桩计数，不能从产品 outcome 复制计数。
- `seed_incident` 只准备 OpsPilot 的业务记录，fixture 同时留存目标不可变副本；`snapshot_incident` 按 id 读取 target、生命周期、观察会话、采样，以及 adopted_sequence/adopted_window_end、healthy_window_seconds、used_sample_count、observation_authorization（session_id/主体及目标绑定/控制与观察 generation/profile revision/authorized）和 handling_audit；完整 observation_sessions 与暴露的 sample_jobs 快照须稳定投影，供提交前后比较。
- `continue_observation(scenario, observations, until)` 仅向现有会话提交，不登记处置或重新授权；用于隔离人工转态与错误样本效果。
- `read_raw_payload(evidence_id)` 是必须提供的接口，每个证据引用必须解析到与测试提供字节完全一致的已存原始 bytes 并校验实际摘要；缺接口、None 或非 bytes 都失败，不能编造 payload。

真实 runtime fixture 仍显式失败；解除 skip 前须接线。测试数字只用于合成边界，
不替代第 1 步真实环境 HealthProfile 校准，也没有新增最低样本数门槛。

## 执行证据

- 定向命令：`.venv/bin/python -m pytest tests/contracts/test_f6_observation.py tests/acceptance/test_f6_recovery.py -q`：53 passed（42 领域 + 11 夹具）、40 skipped、0 xfailed，0.35 秒。
- 本轮 `make check` 退出 0：锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed，45.57 秒；逐项处置见[任务记录](../tasks/2026-10-07-f6-acceptance-tests.md)。
- 未启动实验环境，未执行真实模型/遥测调用，未改产品或 passes。

机器人审查已按用户停机规则收口：第 4 轮三项在一个提交处置，此后仅处理能引用 C3/PRODUCT-CONSTRAINTS 原文的 P1；不再主动触发 review。
