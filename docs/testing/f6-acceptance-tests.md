# F6 验收与采纳合同测试

验收测试由未参与实现的 Agent（Codex）按合同编写，依据为
[F6 五步原文](../../feature_list.json)、[Recovery observations](../../PRODUCT-CONSTRAINTS.md#recovery-observations)、
[C3 §4、§10、§13](../design/technical-proposal-2026-09-07.md) 与
[M1-02 计划](../tasks/2026-10-03-m1-02-recovery-observation.md)。
仅查看公开领域接口、外部入口及测试，不读并行实现。
实现者不能为通过测试修改断言；下面区分冻结语义与可由适配器自定的产品接口。

## 当前覆盖（#164 已结束任务身份断言）

[外部场景](../../tests/acceptance/test_f6_recovery.py) 42 个实例全部启用 PG opt-in：
第 1 步23、第 2 步6、第 3 步6、第 4 步2、第 5 步5，**42 passed、0 xfailed、0 skipped**。
纯领域合同42 passed；持久撤销/到期2例已移除 strict xfail，改为真实身份断言；原边界夹具11 passed；驱动检查52 passed（权限实测1例、13项绑定负向检查及18项重放/动作检查需PG）。先交接再重新授权恢复的事故按完整审计合同通过；本轮仅证明指定 PG 套件，不能替代真实生产验收。

- 第1步：持续窗口、四个信号阈值、陈旧/时间缺口/处置前数据、同目标双事故隔离、退化重置与重新累计；目标四身份与revision共5例按下述批准合同改写。
- 第2步：零/低流量、期限前后、先健康后撤流量；独立次数预算（4次、240秒，早于600秒期限）通过。
- 第3步：分别缺 deployment、请求量、错误、延迟、pods、dependencies，到期 unknown 与交接通过。
- 第4步：持续依赖退化，有界观察后交接，环境写入为零。
- 第5步：恢复、撤流量、缺测、异常四种存储重放，以及只篡改判定的完整性重放全部通过。输入由全新 ObservationStore 的 `incident_records` 在一个快照中按主体/会话取回；调用产品 `replay_history` 与 `recovery_outcome`，原始证据映射也只读捕获快照；无模型/遥测查询，重放前后完整业务快照相等。不一致分支保留产品 `unverified`，不复述已存 `resolved`。

此前各轮数字为历史证据，包含的2个任务身份 xfail 不代表当前状态；#164 最新验证见文末。

## 产品入口与字段映射

驱动只有刺激、时间/环境准备、身份/表示转换；判定、健康窗、原因、交接、动作、权限来自
`recovery_outcome(IncidentScenario, RecoveryRecords)`。Records 来自 `incident_records`，grants 来自 Observer 登录的 `table_privileges()`；不再调用 evaluate_readings 或按计数拼动作/交接。

| 产品字段/表示 | Harness 保义映射 |
|---|---|
| scenario/subject 的持久 UUID | fixture 别名与 UUID 双向表；转换前逐项核验顶层、采样、会话 subject.id、授权 subject.id/subject_id 等于登记的事故 UUID，且 subject.kind=incident，错绑直接 AssertionError |
| verdict、confirmed、healthy_window、used_count、结束/交接、actions、permissions | 原样复制；不重算、不补造、不筛权限 |
| `DEGRADED_SIGNAL:dependencies`，该信号 required | `DEPENDENCY_UNHEALTHY`；其余原因码原样保留，handoff_reasons 使用同一命名表 |
| `incident_lifecycle="unverified"`，`recorded_lifecycle` | 两者原样保留，满足完整性场景 `!= resolved` |
| RecoverySample.target / 顶层 target | 产品会话的不可变绑定原样复制；不读取遥测 JSON.target 决定目标 |
| RecoverySample / RecoverySignal DTO | 转换前按 sample_id 核对同一持久快照中所属会话，sample.session_id 必须等于该会话，revision 必须等于该会话绑定的 revision；未知采样或错绑直接 AssertionError。序号、绝对窗、两类 generation、disposition/outcome 原样保留 |
| native profile 内容 / 内容 hash revision | 逐项核验 revision 存在、SHA-256 与持久内容一致、内容规范化 hash/revision 一致、且符合对应持久会话绑定；不回退当前 profile。顶层缺失或错绑同样直接 AssertionError，再从冻结 calibration 元数据恢复 synthetic 表示 |
| scoped `synthetic_<name>{namespace="demo",service="checkout"}` 与 source `synthetic-lab` | 校验与所属冻结 profile 一致后，仅表示为原 query 名及 `prometheus:synthetic-lab`；真实查询仍是 scoped 表达式 |
| Signal.value 为 None（如 stale） | 原始值从已存、双重验证的 query body 取回，保留 status/verdict 不变；正常 value 必须与该 body 一致 |
| signal.observed_at | 产品投影的 freshness 原始样本时间原样复制，不用窗尾代替 |
| raw_sha256 / body_sha256 | 验证完整 bundle hash 后解出原始 query body，场景 raw_sha256 为产品 body_sha256；逐字节等于测试刺激 |
| 缺信号的 no_data 空向量占位 | 仅在真实 query body 为空向量且status=no_data时映射为稀疏字典中的缺项，保留原始产品记录与原因 |

测试刺激改为真实 Prometheus vector JSON（保留fixture附带元数据）；value/coverage/freshness分别提供bytes。
Observer 的 `PrometheusReadOnlySource` 使用注入 opener，经 telemetry_query 桩取数；无真实网络fallback。
Guarded驱动独立比较 query/绝对窗/类型调用集合，不冻结内部顺序；模型/环境传输由桩拒绝且先计数。

### TEMP 与 CI

PUBLIC 默认 TEMP 会被产品如实报告为 `database_temp`。夹具仅对自己创建的 `f6_acceptance_*` 测试库执行
`REVOKE TEMP ON DATABASE <fixture_db> FROM PUBLIC`，之后使用受限 Observer 登录；不改 m0_budget 或其他库。
这是 PostgreSQL17 通用SQL，CI m0-postgres 服务使用同一准备逻辑。CI m0-postgres 入口现在包含 F6 验收、合同和驱动检查；新增绑定负向检查需要 PG，不能只依赖 checks 作业的默认非PG make check。
新增PG检查临时授 TEMP 后确认产品与适配器都报告 database_temp，且冻结只读断言拒绝，finally收回，证明没有过滤多余权限。

## 合同变更（用户 2026-10-08）

独立核查结论：不可变会话绑定、绑定目标的查询和提交版本栅栏满足 C3 §10；公开提交接口不接受caller提供目标。
#138已关闭。用户批准原跨目标5例改为公开接口可表达的等价合同，移除原5个xfail；其余验收场景断言未放宽。

| 原断言/刺激 | 新断言（对应删除理由） |
|---|---|
| integration_id / cluster_uid / namespace / resource_uid 随采样payload改变，必须history_only | 四例分别改变登记身份，再登记同一目标必须TARGET_MISMATCH拒绝；拒绝前后整个持久快照相等。身份在登记与查询授权处校验，不构造不存在的目标提交参数 |
| 错误payload目标采样不得耗预算、推进健康窗或任务 | 拒绝登记时预算/水位/任务/生命周期/授权/会话/审计都不变；随后旧有效会话正常采样可adopted、用1次预算并有0秒健康窗（#157：首个健康采样从窗口末尾起算），其目标仍是登记目标，遥测元数据不能改变绑定 |
| 保存history.target等于payload的foreign target | 现有会话样本的产品target恒等于原登记目标；原始payload仍逐字节留存及核hash，JSON.target无授权权威 |
| 错误payload提交后全部授权/会话/任务集合不变 | 拒绝再次登记时完整集合不变；正常旧会话采样后授权身份/版本、原目标、处置审计不变，允许合法水位与下一任务推进 |
| revision当作其他身份字段拒绝/旧采样不改变目标 | 新revision再次登记形成新会话，控制/观察generation推进，旧会话撤销、原target不变；已领取旧结果仅history_only，新会话生命周期/水位/预算/授权/审计不变 |
| revision旧提交前后所有job集合必须完全相等 | 旧无样本job仅在历史落库后重新可见；最新活动任务槽不变，完整集合只允许新增原已领取旧job，不得新增其他逻辑任务；与下面已知投影限制区分 |
| 旧身份历史判定、健康窗0、无恢复确认等 | 拒绝登记阶段保留无推进；正常采样阶段以有效会话判断。revision旧结果仍不推进健康窗或预算、不确认恢复；主体/会话绑定、序号、窗口、原始证据、只读边界继续检查 |

## 最后两条 P1（PR #137）

- **PRRT_kwDOUSm_486qdOfc：选择按时间边界裁剪的事故级重放。** 新存储快照选中会话后，仅保留截至该会话的 sessions 前缀（保持每次登记与对应会话的产品关联），控制行截止该会话最后结束记录的 recorded_at；尚未结束则截止最后采样 submitted_at，没有采样则截止授权审计 created_at。排除之后的新登记/控制行。事故生命周期复制所选会话结束记录 lifecycle_after；无结束记录按登记授权的 observing_recovery 合同值；代际/目标引用取范围内记录，历史 mode 无持久值则为None，不沿用当前模式。再交给产品 recovery_outcome，驱动不重新判定。新增真实PG检查：第一会话2样本仍观察，第二会话4样本resolved（#157：180秒需3个健康间隔）后重放第一会话，必须仍为observing_recovery，仅1次登记、2次持久化、36次查询，不携带第二次登记或当前resolved。重放前后当前业务快照仍相等。
- **PRRT_kwDOUSm_486qdOfm：冻结动作合同仅收紧。** 保留 actions ⊆ READONLY_ACTIONS，同时要求 record_handling 次数等于范围内登记审计且大于0，persist_observation 次数等于已提交样本数，read_only_query 次数等于测试桩独立见证的实际发出查询数；有采样必须有查询，交接必须有 human_handoff。#145 第2条按用户2026-10-08裁定限定为最新会话/当前范围：最新会话非交接不新增 human_handoff，但 actions 是完整审计，历史真实交接必须保留。所有结果的 human_handoff 次数精确等于同一持久快照范围内各会话 deadline_expired / max_samples_exhausted 结束记录数，不从最新 human_interaction 或产品动作自报推出；单会话非交接仍为0，交接结果还须至少1。较早会话重放按裁剪快照计数；所有结果的 advance_incident_lifecycle 次数精确等于范围内持久登记和结束记录所见的生命周期变化数。夹具初始 open，登记进入 observing_recovery；按事件时间核对结束记录 before/after，重复授权及生命周期未改变的结束记录不多计。完整性重放按持久变化计数，不能把 unverified 当作一次持久转态；较早会话重放仅用裁剪快照。查询见证由真实采样调用前后桩差量关联到实际receipt.sample_id，不从产品动作自报计数。原8类空/缺/截断动作审计负向检查保留，新增7类虚假交接/重复或缺少生命周期动作检查。

## 已解除的任务身份 xfail 与验证边界

#154 已按用户2026-10-08裁定落地。同事故第一会话零流量5次采样后交接回 open，
再登记新会话，4次健康采样后 resolved（#157：180秒需3个健康间隔）。最新 `human_interaction=None`，
全历史 `actions` 中 `human_handoff` 必须恰为1，直接通过 `assert_readonly`。
该场景另验证移除历史交接或多加一次均被拒绝；交接结果重复交接动作同样被拒绝。

#164 已移除 revoke/expire 两例的 strict xfail 与异常转换，删除不再使用的
`ContractInterfaceConflict`。结束记录现保留旧任务身份：迟到结果提交前的
`before["sample_jobs"]` 必须恰为 `(submitted_job_witness(),)`，见证取自提交前持有的 lease，
独立核对 session_id 等于该会话、sequence 等于测试刺激；提交后集合必须完全相等。
不得缺失旧任务，也不得混入其它任务。原状态/水位/预算/授权/会话/审计/历史证据/只读断言均保留。
新增 revoke/expire × 缺失、额外、错误 job/session/sequence 共10个负向检查：即使提交前后
集合相等，伪造投影也必须被真实断言拒绝。当前指定 F6 PG 套件无 xfail。

时间准备保持合成窗尾与人工处置日期；只有due/授权日期和成对的created_at/deadline_at用于模拟已过原duration，
由产品 sweep_expired_sessions 写结束状态，夹具不写判定、生命周期、水位或计数。
旧单独把deadline移到created之前会被新重放报 SESSION_PARAMETER_MISMATCH，现保持冻结正duration。
本运行证明到期边界与离线一致性，不证明真实时间流逝、调度延迟或生产时钟；未启动kind或调用真实模型/遥测。

## 必须冻结的验收清单

以下是作者维护的测试规范；实现者不能改断言来迁就实现：

- `incident_lifecycle` 正常沿用 subjects.py 的 open/observing_recovery/resolved/closed；完整性不一致时产品投影为 unverified，已存生命周期另列 recorded_lifecycle，不改写业务状态；到期未确认回 open。合法 unknown 采样的预算消耗必须持久记录（`used_sample_count`），不能丢采样而凭空交接。恢复确认与当前采样分开；窗未满、异常、缺测或无流量均不能确认恢复，H,H,D 重置窗口，不能跨 D 累计。持续健康窗从连续健康采样中第一个采样的评估窗口**末尾**起算（2026-10-08 用户决定，#157；C3 §10 同步）：确认时距最后一次非健康采样至少一个完整持续窗，不与其评估窗口重叠；单个健康采样的健康窗为 0。
- 当前场景的终态恢复 verdict、交接语义与原因码：`INSUFFICIENT_TRAFFIC`、`REQUIRED_TELEMETRY_MISSING`、`MISSING_SIGNAL:<name>`、`DEPENDENCY_UNHEALTHY`、`CONTINUED_DEGRADATION`；篡改拒绝分支 `STORED_OBSERVATION_INTEGRITY_MISMATCH`。这些是 harness 规范化码，产品原始码可不同，由适配器做保义映射。
- 所有 outcome 的 `subject_id` 等于请求主体，已存采样绑定同一主体及所属会话不可变目标；目标合同按用户2026-10-08批准变更：异身份再次登记被拒前后完整快照不变，旧有效会话采样目标不受遥测元数据影响，revision新登记使旧结果仅历史。具体替换见上表；同目标另一事故的持久状态、会话、采样不变。
- 合法样本 `disposition == "adopted"`；无效身份/授权/水位只作历史。每个信号 value/source/query/observed_at 原样保存；样本与逐信号 evidence_id 唯一；每个信号的原始 bytes 由测试在修改刺激后冻结，遥测桩按 query/绝对窗口返回。每个 evidence_id 必须能取回与测试刺激逐字节相同的 bytes，实际 SHA-256 必须与记录一致，取不回或字节不同即失败。历史采样同样校验证据绑定。
- 原始 Observer 与重放 `model_requests == ()`；重放 `external_queries == ()`。完整产品 actions/permissions 直接来自产品投影与实测授权；基线词汇仍限read_only/human_control，环境写入由独立桩禁止。
- 除产品审计外，必须由测试拥有的环境/遥测/模型桩证明 `driver.environment_writes == ()`、`driver.telemetry_calls_during_replay == 0`、`driver.model_calls == 0`；原始 Observer/重放禁止任何模型调用；重放禁止所有遥测查询，拒绝被吞掉也要留下计数。所有相关传输必须接入桩，不允许真实网络或旁路 fallback。

## 实现者可自定的清单与最小接口提议

产品的 sample_id/evidence_id 格式、signals 内部结构、run/replay 具体签名、
字段命名及存储格式可自定；通过测试适配器投影下面的规范化视图，不能改变冻结语义。
中间 `latest_sample_verdict` 取值与中间恢复 verdict 不冻结。产品接口不必与测试字典逐字相同。

- `run(IncidentScenario, profile, handled_at, observations, until)`：真实产品驱动接收原始遥测与人工登记，不接收预设结果。
- `IncidentOutcome` 视图增加 subject_id、incident_lifecycle、recovery_confirmed、latest_sample_verdict、终态 recovery_verdict/reasons、healthy_window_seconds、observation_ended、recovery_samples、recovery_profile、recovery_handled_at、model_requests、used_sample_count。现有 final_state 属于调查 Run，不能替代事故生命周期。
- 采样视图含 subject_id、sample_id、目标（含 integration_id）、两类 generation、profile revision、序号、绝对窗口、disposition/outcome；信号含 value/source/query/observed_at/evidence_id/raw_sha256。冻结 profile 包括必要信号、阈值、流量、新鲜度、覆盖/窗口/频率、期限与次数。
- `replay(persisted, allow_telemetry=False, allow_model=False)`：产品 replay_history 从新存储快照重放，返回产品 recovery_outcome 表示及 external_queries/model_requests 审计。
- fixture 的 `recovery_runtime(recovery_boundaries)` 将桩注入所有遥测/环境/模型传输；`GuardedRecoveryDriver` 拥有 replay 模式和桩计数，不能从产品 outcome 复制计数。
- `seed_incident` 只准备 OpsPilot 的业务记录，fixture 同时留存目标不可变副本；`snapshot_incident` 按 id 读取 target、生命周期、观察会话、采样，以及 adopted_sequence/adopted_window_end、healthy_window_seconds、used_sample_count、observation_authorization（session_id/主体及目标绑定/控制与观察 generation/profile revision/authorized）和 handling_audit；完整 observation_sessions 与必须提供的 sample_jobs 快照须稳定投影，供提交前后比较。
- `sample_jobs` 必须列出该事故全部采样任务（包括非当前会话的任务），比较投影只含 job_id、session_id、sequence（逻辑序号），提交前后按集合比较，保证不新增任务、不改变归属或逻辑序号。缺少集合即验收失败。不比较 due_at、state 或租约字段：拒绝提交后释放租约或推迟同一任务属于处置该提交，不是 C3 §10 的「安排后续任务」，不能因这些合理变化判失败。
- `observation_sessions` 只投影合同字段：session_id、purpose、subject、target、subject_control_generation、observation_generation、state、authorized、health_profile_revision、adopted_sequence、adopted_window_end、active_sample_job_id；除合同水位外不含额外计数、创建/更新时间戳等辅助字段。
- `continue_observation(scenario, observations, until)` 仅向现有会话提交，不登记处置或重新授权；用于隔离人工转态与错误样本效果。
- `read_raw_payload(evidence_id)` 是必须提供的接口，每个证据引用必须解析到与测试提供字节完全一致的已存原始 bytes 并校验实际摘要；缺接口、None 或非 bytes 都失败，不能编造 payload。

## 执行证据（main 280e550 改接）

- `137-recheck2-final.md` P2 已处置：映射先核主体 UUID、每个 revision 的存在/持久绑定及内容 SHA，一律不修正错绑。新增10类实际产品投影篡改负向检查（主体/subject_id、未知revision、已存在但属于另一会话的revision、持久digest、投影content、顶层profile缺失），全部直接AssertionError，不带xfail。该轮独立复验驱动检查21 passed，并确认原缺失profile复现现直接报错、不会清空样本。
- 指定PG定向：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55651 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short` → **124 passed、2 xfailed、0 skipped**（9.66秒，退出0）。其中41产品外部、42领域、11原夹具、30驱动检查通过。
- 改接阶段全新上下文独立审查者曾同命令复跑 **105 passed、2 xfailed**（8.27秒），未发现新增合同/正确性缺口。
- `UV_CACHE_DIR=tmp/uv-cache make check` → **3192 passed、492 skipped、2个既有架构债xfailed**（72.18秒，退出0）；锁/Ruff/mypy全过。默认未启用PG，不能代替上条；另执行Ruff与git diff --check通过。
- 最后两条P1独立复验：同一PG命令 **124 passed、2 xfailed**（9.62秒），较早会话隔离与8类动作审计负向检查通过，无未处理正确性缺口。
- #115①持久快照取回与②重放不写业务状态现已在5个产品重放场景实际验证；③遥测边界集合、④撤销/到期持久提交、⑤次数先于期限耗尽已运行，④当时完整任务身份清单为2个xfail，现已由 #164 真实断言覆盖。
- 4条机器人线程：结果拼装改为产品投影；权限硬编码改为实际grants/TEMP准备；payload目标改为产品绑定；原5个目标xfail随批准合同改写移除。当时保留2个任务身份xfail；现标记已移除，RuntimeError回归继续覆盖（实际7个身份/授权场景全为普通RuntimeError failure，不被xfail吞掉）。
- 未改产品/迁移/profile或passes，未initdb/停PG/提交/push/PR；PG仍由lead管理，费用0。

### #145 收紧（main 510dcd3，2026-10-08）

- 红：先仅加入3项逐样本会话/revision错绑、7项动作精确性检查，PG `-k 'sample_session_or_its_profile or surplus_handoff_or_inexact'` 得 **8 failed、2 passed、30 deselected**（2.18秒）。三种错绑、两种虚假交接和三种重复生命周期动作均未被旧断言拒绝；终态少一次生命周期动作已被拒绝。
- 收紧后上述10项与原套件共 **134 passed、2 xfailed**（12.18秒）。追加独立审查指出的“先交接再恢复”可达场景，保留真实产品失败；两会话观察→恢复与较早会话重放均核对精确生命周期动作次数。
- 最终 `UV_CACHE_DIR=tmp/uv-cache make check` → **3192 passed、503 skipped、2个既有架构债xfailed、0 failed**（74.13秒，退出0），锁/Ruff/mypy通过；默认非PG，503个skip包含该轮新增PG检查，不能替代PG证据。`git diff --check`通过；仅改测试及本文档，无提交，无产品修改。
- PR #155 的独立审查已完成（另一 Codex 全新会话，非测试作者，审查输出保存在 lead 本地 tmp/parallel-2026-10-08/f6-tests-review.md，原始 ledger 不入库）：未发现违反用户 #145 冻结要求的测试缺口；指出当时最新会话交接/全历史动作范围冲突，作者运行连续场景保留失败；本次按用户 #154 裁定修改测试合同。静态审查不代替PG证据。

### #154 完整审计范围（2026-10-08）

- 工作区 `production-ops-agent-f6-154`，分支 `test/f6-154-handoff-scope`，起点 main `1004a86`。仅改测试与本文档/任务记录，不改产品或 `passes`，不提交。
- 红：先直接断言先交接后恢复场景，并加入交接结果重复动作拒绝检查；旧断言得到 **2 failed、7 passed、33 deselected**（1.86秒）。恢复后真实历史交接被误要求为0，重复交接未被拒绝。
- 绿：驱动从与投影相同的持久快照保留全部会话结束原因（较早会话重放沿用裁剪范围）；`assert_readonly` 按结束记录精确核对交接数，其它断言不放宽。
- PG端口55633：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55633 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short` → **136 passed、2 xfailed、0 skipped、0 failed**（11.22秒，退出0）。41外部场景、42领域合同、11原夹具、42驱动检查通过；该轮2个xfail是当时的持久任务身份缺口，现已由 #164 解除。
- `UV_CACHE_DIR=tmp/uv-cache make check` 最终退出0：锁检查、Ruff lint/format、mypy（75源文件）通过；pytest **3386 passed、512 skipped、2个既有架构债xfailed、0 failed**（88.92秒）。首轮因新增测试格式不符退出2，定向格式化后重跑通过。默认未启用PG，不能代替上条PG证据；`git diff --check`通过。
- 全新上下文独立审查完成：未发现正确性/合同缺口，独立抽取 `assert_readonly` 的17个合成计数案例通过，3个测试文件语法解析通过；文档旧口径已复验消除。审查者未操作PG，此项不替代作者PG运行。

## #164 已结束任务身份（2026-10-08）

- 工作区 `production-ops-agent-f6-jobs`，分支 `feature/F6-ended-job-identity`，起点 `3c7f044`；只改测试与本次指定文档，不提交、不改产品/迁移/ROADMAP/passes，不操作其它端口或停库。
- 移除 revoke/expire 两个 strict xfail、捕获/转换与无其它使用处的 `ContractInterfaceConflict`。提交前快照必须恰含 lease 独立见证的旧 job，session_id 等于原会话、sequence 等于测试刺激；history_only 入库后任务集合不增不减。其它断言保留。另加10个投影篡改负向检查，覆盖缺旧任务、多任务及错误 job/session/sequence，前后同错也必须拒绝。
- 红/基线按实记录：仅去 xfail、不改断言时 **2 passed、42 deselected**（0.64秒，退出0），当前产品已补表示，未出现断言红。临时恢复原 strict/raises 标记核对旧行为，得到 **2 failed、42 deselected**（0.62秒，退出1），均为 `XPASS(strict)`；随后移除标记并收紧。这是旧标记失效证据，不是产品缺陷复现。
- 绿：下列指定 PG 套件 **149 passed、0 xfailed、0 skipped、0 failed**（12.51秒，退出0）：42外部场景、42领域合同、2持久撤销/到期合同、11原夹具、52驱动检查（含新增10例）。收紧后、增加负向检查前的同套件为139 passed（11.63秒）。

```sh
M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55671 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short
```

- `UV_CACHE_DIR=tmp/uv-cache make check` 退出0：锁检查、Ruff lint/format、mypy（77源文件）通过；pytest **3389 passed、524 skipped、2既有架构债xfailed、0 failed**（88.76秒）。默认非PG，新增10例在此skip，不能替代上面的PG证据。检查日志 `/private/tmp/f6-164-check.log`；`git diff --check` 通过。
- 旧说明核查：允许范围内的当前口径已更正；历史运行数字保持原值并标明已被本轮取代。范围外仍有 `ROADMAP.md` 的2例接口冲突/验收阻塞口径、`docs/tasks/2026-10-03-m1-02-recovery-observation.md` 第177行当前状态与第186行历史记录/第312行待作者改标记描述，以及 `docs/evidence/m1-02-recovery-outcome/run.md` 第26行旧投影来源表。依本轮只改指定路径限制未改这些文件，交由 lead 收口。
- 全新上下文独立 Agent 静态审查与最终文档复核完成，无未处置合同/正确性缺口；确认 witness 来自 fault 前的 held lease。审查者未运行PG，运行数字来自作者实际命令。

## #156 查询实际发出时间（sent_at）

本轮把 F6 的时间证据收紧到请求实际交给传输层的时刻。`BoundarySource` 现在接收与场景窗口末尾同步的合成 `clock`，因此注入 opener 的 `sent_at` 不会被开发机墙钟污染，重放的 `sample_time` 约束仍由产品验证。验收驱动逐 bundle 检查 `query`、`coverage`、`freshness` 子对象：每个已见证的请求必须有带时区的 RFC3339 `sent_at`，`LEASE_BUDGET` 必须没有该字段，`evaluated_at` 仍等于 `window_end`，并且见证请求数等于带时间戳的子对象数。

动作顺序断言改用 `RecoveryOutcome.action_events`。每个 `read_only_query` 事件按自己的 `sent_at` 排入控制、持久化和结束记录；数据库时间与发送时间相差不超过 60 秒时保持快照顺序并标记 `order_uncertain`。没有 `sent_at` 的旧 bundle 回退 `evaluated_at`、再回退 `submitted_at`，事件标记 `approximate`；`actions` 必须恒等于事件动作元组。

测试驱动在转换产品投影时保留 `RecoveryAction` 事件对象，并把产品 `RecoverySignal.sent_at` 映射到测试信号视图；这两项是表示转换，不能改变合同语义。

第 5 步新增四类只改 `sent_at` 并重算 raw hash 的重放负向：非法格式/时区、`LEASE_BUDGET` 却带时间、同一读数递减顺序、晚于 `sample_time + 60s`。四类都要求 `unknown`、`incident_lifecycle=unverified`、`STORED_OBSERVATION_INTEGRITY_MISMATCH` 及对应完整性码；合法时间戳的重放判定保持一致，旧形状仍可重放但查询动作标记近似。

本轮实际运行：

```sh
.venv/bin/python -m pytest tests/test_f6_recovery_outcome.py tests/test_f6_driver_guards.py -q --tb=short
```

在 issue #156 实现尚未合入的 `main`（072bf78）上，该命令为 **35 passed、9 failed、42 skipped**；9 个失败分别是缺少 `PrometheusReadOnlySource.clock`、`RecoveryOutcome.action_events` 和 `sent_at` 完整性校验，均对应本合同新增接口/行为。PG 定向命令按本文件约定在实现分支提供 PostgreSQL 后运行；本工作区没有把预期红改成绿，也未修改既有五步判定或 `read_only_query` 次数合同。

按用户提供的 PG 命令实际运行结果为 **73 passed、77 failed、0 skipped**。其中大部分场景在驱动注入 `clock` 处因 main 缺少 `PrometheusReadOnlySource.clock` 提前失败，合同导出测试因缺少 `RecoveryAction` 失败；其余新断言失败来自 `action_events`、`sent_at` 和重放完整性行为尚未实现，未发现与测试改写自身有关的失败。

```sh
M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55711 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short
```
