# F6 独立验收与合同测试作者

- 状态：最后一轮修订完成、机器人审查按停机规则收口（外部场景尚未接线，F6 未完成）
- 更新日期：2026-10-07
- 依据：[M1-02](2026-10-03-m1-02-recovery-observation.md)；[测试映射与接口提议](../testing/f6-acceptance-tests.md)
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-f6-tests`；`feature/m1-02-f6-acceptance-tests`；基线 `0c97377`

## 目标与范围

按 C3 与 F6 五步独立编写纯函数采纳合同测试，以及接线前 skip 的外部场景。
不读并行实现、不改产品、依赖、迁移或验收原文。提交 PR，不合并。
接口格式选用显式字段与原始信号字典，便于只断言可观察结果；属于合同提议。

## 执行进展与证据

- 已确认工作区干净、分支与用户给定一致；fetch 后 origin/main 仍为 `0c97377`。
- 用户限定只能操作本 worktree，所以未同步主 worktree 或清理其他会话 worktree。
- 定向 pytest：54 passed、26 skipped、0 xfailed，0.29 秒；命令见测试说明。
- 独立审查首轮发现 4 项外部场景缺口，已补新鲜度/覆盖/处置前数据、信号阈值与低流量、零模型调用审计、重放独立重算；全新上下文审查者已静态复验受影响项，无未处置合同发现。
- 最终 `make check` 退出 0：锁检查、Ruff lint/format、mypy 通过；pytest 2654 passed、337 skipped、2 既有 xfailed，44.76 秒。两个 xfail 是既有 persistence/domain 架构债，不是 F6 缺陷。
- 独立审查仅做合同/断言静态审查；pytest 执行数字由作者本地运行记录，不混称独立运行验收。
- 未运行模型、trace、实验环境或遥测调用；费用 0，无供应商调用需对账。
- 首版纯函数合同未发现可复现不一致；当时 26 个外部场景尚未接线，不能认定 F6 完成。

## 审查处置（PR #112 追加修订）

- Bot P1：所有 run outcome 与请求 subject_id 绑定，采样也绑定主体；同 target 双事故交换被处置身份，分别核对观察中/确认后状态，另一事故的生命周期、会话、采样完全不变。
- P2-1：补控制 generation=4、观察 generation=3，大于当前值同样 history_only。P2-2：补 H,H,D 与 H,H,D,H,H，分别重置为 0、后续连续健康最多 120 秒；期限前不结束/不交接，期限后回 open 交接。
- P2-3：拆出 latest_sample_verdict 与 recovery_confirmed，不冻结窗未满 verdict；P2-4：测试-owned 环境/遥测桩记录并拒绝写请求和重放查询，driver 持有计数，另加 5 个可执行夹具负向检查；P2-5：Target 补 integration_id。
- O1：文档列冻结语义与实现者可自定接口两清单，原因码为 harness 规范化码；O2：逐信号 evidence_id 唯一，可读原始 bytes 时核验 SHA-256；O3：篡改重放接受重算还原或显式完整性不一致，不能复述错误判定；O4：补先健康后撤流量且错误下降序列。
- O5：文档说明运行时 reopen/新阶段未覆盖，因为移出 M1-02；O6：skip 补 #86/#87 和持久采样 #84；O7：删除非生命周期取值/触发器的空转参数；O8 按用户要求不处理，最低样本由合同/profile 决定。
- 全新上下文静态复验额外发现 H,H,D 可提前结束的缺口，已收紧，静态复验无未处置合同发现；不是产品运行验证。定向 pytest：47 passed（42 领域 + 5 夹具）、33 skipped、0 xfailed，0.30 秒；最终 `make check` 退出 0，锁/Ruff/mypy 通过；2647 passed、344 skipped、2 既有架构债 xfailed，44.59 秒。
- 本次仅追加提交和普通 push；不再触发机器人、不回复/resolve thread、不合并，lead 接手审查处置。

### 第二轮机器人 P2（943724a 之后）

- 模型边界：测试-owned `model_request` 先计数再抛错；原始与重放均断言 driver.model_calls 为 0；补两例驱动吞掉拒绝仍可检出的夹具检查，产品自报审计保留但不再单独充当证明。
- unknown 留底：合法 stale/coverage-gap 调用 assert_saved_basis，逐样本 adopted、原信号和来源保留，used_sample_count=3、恢复 unknown 且未确认、全 stale 健康时长为 0；处置前数据拆成独立场景，不冻结采纳方式。
- 窗口重建：H,H,D,H,H 恰为 120 秒；新增原始期限 420 秒/次数 7 的冻结 profile，H,H,D,H,H 仍观察、H,H,D,H,H,H 达 180 秒并 resolved，不在观察途中延长预算。
- 定向 pytest：49 passed（42 领域 + 7 夹具）、35 skipped、0 xfailed，0.34 秒；全新上下文静态复验指出 stale 健康窗仍可增长，已补等于 0 并复验，本轮无未处置合同发现；最终 `make check` 退出 0：锁/Ruff/mypy 通过，2649 passed、346 skipped、2 既有架构债 xfailed，45.83 秒。35 个外部实例未接线，不是 F6 验收通过。
- 仅追加提交、普通 push；不触发 @codex review、不回复或 resolve thread、不合并，仍由 lead 接手审查。

### 第三轮机器人 P1/P2（442c7f3 之后）

- P1 跨目标：fixture 预种事故并保留不可变目标；assert_saved_basis 同时核对预种目标、事故快照目标、profile 和已存采样目标。新增 5 个目标身份字段变体，先空采样 run 授权，再 continue_observation 提交错误目标，核对 history_only 留底、无恢复确认、生命周期/采纳水位/健康窗/预算未推进；原授权身份、绑定与版本、处置审计不变，历史采样仍指向原会话。
- P2 证据解析：read_raw_payload 是必需接口，缺失/None/非 bytes 即失败；每个证据引用均核验原始字节 SHA-256，包括错误目标历史样本；补 3 个可执行 reader 拒绝检查，文档删除可选读取表述。
- 定向 pytest：52 passed（42 领域 + 10 夹具）、40 skipped、0 xfailed，0.27 秒；最终 make check 退出 0：锁/Ruff/mypy 通过，2652 passed、351 skipped、2 既有架构债 xfailed，45.70 秒。全新上下文静态复验补充要求核对原授权及处置审计，已修复并复验，无未处置合同发现。外部场景仍未接线，未改产品代码或 passes。
- 仅追加提交与普通 push；不触发机器人、不回复/resolve thread、不合并。

### 第 4 轮：最后一轮修复与停机收口（f3a9bad 之后）

- P2 信号绑定：每个场景修改刺激后生成测试-owned raw_payload bytes，桩按查询/绝对窗口返回这些字节；证据读取必须逐字节等于刺激，随后核 SHA-256；补可运行桩检查，不再只验证产品字节与产品摘要自洽。
- P2 有界继续：低流量及依赖退化 count=3（180 秒，原预算 300 秒/5 次）均要求 observation_ended=False、无交接/交接原因。
- P1 会话/任务：错误目标提交前后完整 observation_sessions 不变；若暴露 sample_jobs，完整集合也必须不变，不限于当前授权。
- 定向 pytest：53 passed（42 领域 + 11 夹具）、40 skipped、0 xfailed，0.35 秒；最终 make check 退出 0：锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed，45.57 秒；限定静态复验确认三项已处置，无 C3/PRODUCT-CONSTRAINTS 原文支撑的新 P1。
- 机器人审查已按用户停机规则收口：本轮三项合在一个追加提交，此后仅修能引用 C3/PRODUCT-CONSTRAINTS 原文的 P1；不继续扩展 P2/可选项。普通 push，不触发 @codex review、不回复/resolve thread、不合并。

- 停机后 C3 §10 P1（7a992ad 之后）：依据「失效结果只保留历史……也不安排后续任务」，删除 sample_jobs 条件守卫，完整任务集合必须提供且前后相等；文档明确任务五字段及会话合同字段的稳定投影，排除租约重试字段和辅助计数/时间戳；定向 pytest 53 passed、40 skipped、0 xfailed，0.35 秒，make check 退出 0（锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed，45.04 秒），一个追加提交普通推送，不触发审查、不操作 thread、不合并。

- 用户修正（da9f04a 之后）：sample_jobs 比较收窄为 job_id/session_id/sequence 集合，允许同一任务释放租约或推迟，不把 due_at/state 变化误判为安排后续任务；已用源文件实际断言验证重试字段变化通过、任务新增/身份与序号变化拒绝。定向 pytest 53 passed、40 skipped、0 xfailed（0.35 秒）；make check 退出 0，锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed（45.97 秒）；普通追加提交，不触发 review、不操作 thread、不合并。

## 下一步与交接

检查和独立审查完成，提交本分支 PR，等待接线；不合并。解除 skip 时提供真实外部驱动；原断言只按合同或验收变更处理。
F6 第 6 步真实环境、持久化原子边界与角色隔离验证留在原 M1-02 任务范围。


## 接线（2026-10-08）

- 状态：真实 PG 接线与运行完成，31 个第 1–4 步外部场景通过；7 个合同/接口冲突 strict xfail，5 个第 5 步重放 skip，F6 尚未完成。worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-f6-wiring`，分支 `test/f6-acceptance-wiring`，起点 `main 8aae433`。未改 ROADMAP、原 M1-02 任务、产品、迁移、profile 文件或 passes。
- 公开接线：真实 `register_remediation` → 受限 Observer login 领取 → `ObserverLoop.sample` 经测试-owned InstantSource 查询 value/coverage/freshness → `submit_sample` 原子采纳 → `incident_sessions/session_history` 与主体/控制快照。只传调度元数据；输入 bytes 不直接进入采样/提交。source/查询/profile revision 做校验后的逆映射，原始 evidence 从 PG bundle 解码并双重核摘要；未知动作不得从审计投影中消失。
- 时间选择：合成窗尾注入 Observer 的公共时钟；owner SQL 只做授权日期、任务 due、到期 fault 设置，数据库租约仍用真实 DB clock；不修改次数、水位、判定或生命周期。属于测试调度设施，不能替代真实时钟/生产观察证明。session.authorized 是原始授权记录，当前有效授权由 authorized 与 state=authorized 共同投影；原始行完整留在 authority_history。
- #115：① 全新 ObservationStore 按主体/会话取 `session_history`，② Guarded replay 比较完整原始持久 history、主体及控制快照（抛错时也检查）；①②随第 5 步 skip「待 #87 合并」；③ query/绝对窗/查询类型由桩独立记录并按集合比较，在真实外部场景执行；④ 撤销/到期两例在采样完成、提交前使持久授权失效，状态/水位/控制/历史及证据检查通过，完整任务清单缺接口；⑤ max_samples=4、deadline=600 秒，240 秒次数耗尽后交接且无可领取任务，真实 PG 通过。
- 31 个通过场景：持续健康确认/窗未满、四种单信号异常、陈旧/时间缺口/处置前数据、同目标双事故隔离、退化重置与重新累计、无/低流量与撤流量、六种缺必要信号、持续依赖异常，以及独立次数耗尽。按第 1/2/3/4 步分别 **17/6/6/2 passed**。
- 冲突 1：integration_id、cluster_uid、namespace、resource_uid、revision 五个参数分别要求错误目标只作历史且健康窗 0；实际健康窗 60 秒。公开 `HealthSample/SignalReading` 和 `submit_sample` 没有目标输入，无法保义表达异目标提交。位置 `opspilot/domain/observation.py:82`、`opspilot/observation/store.py:1287`；五例保留全部断言并标 strict xfail「合同/接口冲突」。
- 冲突 2：revoke/expire 两例要求迟到结果只作历史且完整旧任务集合不变；状态、权限、水位、预算、历史和原始证据断言通过，最后集合断言实际从空集变为同一个旧 job。结束会话清除活动 job，结束记录不保存无样本 job 身份。复核 ObservationStore `incident_sessions/session/session_history`、工作台 `observation_sessions` 与 Observer `sample/submit_sample`，无其它公开已发放任务清单；位置 `opspilot/observation/revocation.py:53`、`opspilot/observation/store.py:1569`。两例 strict xfail「合同/接口冲突」，不以内存领取账本冒充持久状态，不据此认定产品新安排任务。
- 独立审查：全新上下文 Agent 查出信号与完整动作投影问题，已修复并静态复验；本轮复核公开接口，并由独立审查者真实 PG 复跑得到 92 passed、5 skipped、7 xfailed（3.48 秒、退出 0）；审查指出整用例 xfail 可能吞掉其他失败，已加 `raises=ContractInterfaceConflict`，只允许两类确证形态触发；其他权限/状态/证据/fixture 失败正常失败。旧 lease witness 单独作为外部证据，绝不补入持久快照。产品缺口类 xfail **0**，合同/接口冲突 **7**，无断言删除或放宽；任务集合断言仅移到末尾以先执行其余历史与状态检查。
- 环境：默认 `make setup` 因 uv cache 权限失败，`UV_CACHE_DIR=tmp/uv-cache make setup` 退出 0。沙箱 initdb 曾失败；lead 随后在沙箱外启动指定 55651 PG17，本轮直接使用，没有再 initdb，也没有停止该实例。未碰其他端口、未启动 kind、模型或真实遥测；费用 0。
- PG 定向原始首轮：92 passed、7 failed、5 skipped（跨目标 5、授权标记映射 2）；修正有效授权投影后两例明确失败于任务集合。加 strict xfail 后命令：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55651 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short` → **92 passed、5 skipped、7 xfailed**，退出 0，3.52 秒。passed=31 产品外部 + 42 领域 + 11 原夹具 + 8 适配器检查；原始日志 `tmp/f6-pg-live.txt`、`tmp/f6-authority-live.txt`。
- `UV_CACHE_DIR=tmp/uv-cache make check` 退出 0：锁/Ruff/mypy 全过，3144 passed、460 skipped、2 个既有架构债 xfailed（68.45 秒）；原始日志 `tmp/f6-make-check.txt`；覆盖、门控与冲突详见 [测试映射](../testing/f6-acceptance-tests.md)。
- 交接：lead 负责 Git 提交、push/PR 和停 PG；本会话只改工作区。接口冲突须合同/接口决策，第 5 步待 #87 合并。本轮没有再尝试提交；历史 git 写权限失败已记录，提交列表为空。
