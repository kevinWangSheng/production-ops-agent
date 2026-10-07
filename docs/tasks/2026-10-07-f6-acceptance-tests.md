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

## 下一步与交接

检查和独立审查完成，提交本分支 PR，等待接线；不合并。解除 skip 时提供真实外部驱动；原断言只按合同或验收变更处理。
F6 第 6 步真实环境、持久化原子边界与角色隔离验证留在原 M1-02 任务范围。
