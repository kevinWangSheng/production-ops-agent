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


## 接线（2026-10-08，lead 最终归类）

- 状态：31个第1–4步外部场景通过，5个跨目标产品缺口整体strict xfail、2个任务身份接口冲突strict xfail、5个重放skip。F6未完成。工作区 `production-ops-agent-f6-wiring`，分支 `test/f6-acceptance-wiring`；未改产品、迁移、profile、原M1-02任务、ROADMAP或passes。lead负责提交及停PG，本轮只改工作区。
- 接线：公开 register_remediation → 受限 Observer 领取 → sample 经测试-owned遥测桩 → submit_sample → PG session_history。调度输入不带信号值；原始证据读取持久bundle并双重核摘要，动作从真实审计投影。合成时钟/owner SQL只做时间、调度与故障准备，不替代生产时钟证明。
- #115：① 新ObservationStore按主体/会话取持久重放输入、② Guarded replay完整持久状态前后比较仅接好防护，产品replay仍NotImplementedError，待#87合并后验证，不能称完成；③ 独立核query/绝对窗/类型集合，④ 提交前撤销/到期状态与历史证据检查，⑤ 240秒耗尽4次预算早于600秒期限，均有真实接线；④完整旧任务清单仍缺接口。
- PR #137独立审查原文：`/Users/shenghuikevin/dev/AI/production-ops-agent/tmp/m1-02-review/137-review-final.md`。先前移走已知差异分支后，真实PG暴露预算1而非0、adopted而非history_only、水位推进和新增逻辑任务；lead确认这些均是提交未校验结果目标（C3 §10，[#138](https://github.com/kevinWangSheng/production-ops-agent/issues/138)）导致的错误采纳后果，不是新的独立缺口。
- lead最终决定：integration_id、cluster_uid、namespace、resource_uid、revision五个参数实例整体标 `pytest.mark.xfail(strict=True, raises=AssertionError, reason="产品缺口: 提交未校验结果目标（C3 §10），见 #138")`，只接受AssertionError。移除中途已知差异分支、临时InvariantChecks/ProductContractGap及其专用回归，恢复原始顺序断言。已用AST核对该函数正文与main `8aae433`完全一致，未放宽/删除原断言。#138修复PR须移除这五例xfail标记，以普通测试全部通过为门槛；保留标记而通过会触发strict XPASS失败。
- 撤销/到期两例函数正文保持原样：全部状态、权限、水位、预算、留底、证据先检查，最后匹配任务清单从空集变为原已领取旧job；xfail现限定 `raises=AssertionError`，函数正文和末尾冲突匹配未改。缺无样本结束任务的持久身份，不能推定产品安排新任务。位置 `opspilot/observation/revocation.py:53`、`opspilot/observation/store.py:1569`；外部lease witness不补入持久快照。
- 复验审查 `tmp/m1-02-review/137-recheck-final.md` P2 采纳：五个产品缺口实例及两例撤销/到期均加raises=AssertionError；非断言类异常照常失败，两种场景函数正文与HEAD的AST完全一致。新增夹具检查把实际七个参数实例导入隔离pytest子进程，用测试驱动注入RuntimeError，要求退出1，JUnit七例全为failure、无skipped/xfail和error；夹具定向9passed；独立复验也运行guards得到9passed（0.42秒），确认七例RuntimeError都正常failure。无关AssertionError仍可能被整体xfail接纳，本限制不替代无标记验收。#138修复PR必须移除五例标记，以普通测试全部通过为门槛。

- 分类：产品缺口类预期失败5例（#138），合同/接口冲突2例；不再声称产品缺口0或所有错误都会在这5个整体xfail中独立报错。其余通过场景按第1/2/3/4步为17/6/6/2，合同42pass/2xfail，重放5skip待#87。
- PG命令：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55651 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short` → **93 passed、5 skipped、7 xfailed**（4.45秒、退出0），日志 `tmp/f6-runtime-error-guard-pg.txt`。passed=31产品外部 +42领域 +11原夹具 +9适配器。
- `UV_CACHE_DIR=tmp/uv-cache make check` 退出0：锁/Ruff/mypy全过，**3145 passed、460 skipped、2个既有架构债 xfailed**（68.95秒），日志 `tmp/f6-runtime-error-guard-check.txt`。未initdb/停55651/碰其他端口，未调用真实模型/遥测或kind，费用0；无提交/push/PR。
