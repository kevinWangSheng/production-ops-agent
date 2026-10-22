# #309 合同预审：压缩后重置去重 + 同轮工具有限并行

- 预审者：Claude（Opus 5.5，未参与实现），只读，基于 `origin/main` `0b4b820`（worktree `poa-309`），2026-10-22。
- 未启动 PG／kind，未调用模型，未跑测试。「已核对」= 读代码所得。上游 = HolmesGPT `a0009195`（经 `gh api contents` 取得）。

## 0. 现状要点（已核对）

**去重（B5）**
- `seen_tool_calls` 指纹 → 首次 evidence_id，只存在本 attempt 内存，resume 不重建（`loop.py:336-341`；B5 记录 `docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md:137-141`）。runner 每次领取都走 `resume()`（`runner.py:385-392`），所以每次新 attempt 已经等于清空。
- 只有产出 evidence_id 的派发才记入；纯网关拒绝不记，重复时照常再跑（`loop.py:834-845`）。重复调用不派发、不续租、不进 ledger（`loop.py:807-820`）。
- 压缩只有一个调用点 `_manage_context → _compact`（`loop.py:903`）。摘要未被接受时直接交接 `COMPACTION_FAILED`（`loop.py:1018-1021`）；接受时替换 messages、切 segment（`loop.py:1022-1028`），这里没有碰 `seen_tool_calls` → 正是 issue 说的缺口。
- 上游：只在 `conversation_history_compacted` 且 `RESET_REPEATED_TOOL_CALL_CHECK_AFTER_COMPACTION`（默认 True，`env_vars.py:156-158`）时重置，保留的是 `guarded_tool_call_ids`（会累加）里的调用（`tool_calling_llm.py:1221-1222,1279-1289`）。被保护的 id 来自 `overflowing_tool_call_ids`：**只有历史一次摘要请求放不下（`history_overflowed`）时**才填，取值为输出 ≥ 单工具上限一半的 tool 消息（`compaction.py:32-34,279-288,429-430,514-515`）。
- OpsPilot 走不到上游这条「溢出」路径：压缩只发一个请求，超字节就以 `REQUEST_TOO_LARGE` 停（`loop.py:952-953`），没有分块摘要；单条结果 >25k token 网关整条拒绝（审计 §三，`executor.py:1563-1577`）。→ 按上游语义，OpsPilot 的保护集合恒为空 = **全部清空**。

**派发**
- 串行：每个非重复调用先 `store.renew()` + `assert_current()`，再 `executor.execute`、`commit_tool(step_id, index)`，接着更新 `evidence_ids`/`delivered`/`results`/`capture_delivery`（`loop.py:799-871`）。
- `commit_tool` 允许任意顺序：按 ordinal 追加、幂等，租约失效就记 late_result 并抛 `CONTROL_DENIED`（`persistence/steps.py:183-240`）；rebuild 按 ordinal 排序，缺失的 ordinal 记为 pending（`context.py:792-813,1338-1347`；`steps.py:57-81`）。恢复回放在 `_dispatch_lock` 下串行，每项前后都续租（`worker.py:149-196`）。
- 模型可见顺序：`pair_tool_results` 按 tool_calls 的顺序重排 tool 消息（`messages.py:136-139`）。但 `state.delivered` 的追加顺序会进入 `run_coverage_message`（`loop.py:139`），这条消息模型可见。
- 上游：`ThreadPoolExecutor(max_workers=16)`（`tool_calling_llm.py:1469`），用 `as_completed` 按**完成顺序**把结果追加进 messages（`:1490,1580-1582`）；`previous_tool_calls` 只在所有任务提交之后才追加，所以同一批里的重复调用不一定能互相拦住（`:1474-1478,1546,1580`）。这两点 OpsPilot 都**不应照搬**。
- C3 §13 表里已有「工具并行数 2」（`technical-proposal-2026-09-07.md:462`），§5 写的是「工具采用有限并行」（`:219`）。PR #317（#313）保留了这句，PR 正文写明等 #309 来兑现。

**线程安全**
- 执行器：`_operations_used += 1`、`_tool_seconds_used += elapsed` 没有加锁（`executor.py:886,961`）；`tool_seconds_used` 是各调用 elapsed 的**总和**，`_remaining_timeout` 拿它算 active 预算（`loop.py:1180-1186`）。
- tracing：`_current = threading.local()`（`tracing.py:769`）；`tool_call` 在当前线程没有 run span 时返回 NullSpan（`:855-857`），所以**工作线程里的工具 span 会整个丢掉**；`parent.tool_calls += 1` 也没有加锁（`:858`）。span 带 `step_id` 和 `opspilot.tool.index`（`:505-507`）；`tool_call_seq` 只在一个测试里被断言（`tests/test_tracing.py:362`）。
- 传输层：OtelDemo 按线程记录 socket，有整请求期限（`http_deadline.py:84-93`，`otel_demo.py:1057-1076`）；fixture 的 `_CannedTransport` 和 `_EchoControl` 都无状态（`fixture.py:244-260`）；`MemoryEvidenceStore` 是普通 dict，没加锁（`web/evidence.py:131-154`）。
- `EmittingCommitter` 有计数器和事件追加（`progress.py:336-433`）→ 应只在主线程调用。
- 连接池：每进程默认 `max_size=4`，排队 5 s（`persistence/base.py:43-55`）。B1 推荐 `POOL_MAX ≥ capacity+2`（`f2-b1-contract.md` U-B1-3）。DurableControl 每次快照都开一个事务（`otel_demo.py:853-856`）。持有连接时是否会再开一个事务（嵌套）：**未确认**。
- 单次工具超时：otel 30 s，fixture 10 s（`otel_demo.py:532,627`；`fixture.py:157`）。租约 = 360+60 s（`web/service.py:107`，`limits.py:90`）。

**九类必答（简）**：1 时间——每次调用的超时与期限不变（`executor.py:740-745,928-940`）。2 计数——见 U-2。3 状态——见 U-3。4 权限——无变更。5 数据合同——无迁移，行结构不变。6 旧数据——不涉及（去重状态不入库）。7 并发——见 U-1、R-6～R-10。8 外部调用——100 次请求上限不受影响（并行不消耗模型请求）；压缩后的重查最多多花几轮，仍在 100 次之内。9 `passes`——不翻；cap=1 等价性写成验收步骤（U-4）。

## 决定表

### 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U-1 | 默认并行上限 | **A** 2：与 C3 §13:462 一致，不改 C3；B1 下每 worker 同时最多 2 Run×2 个工具线程，连接池、lab Prometheus/Jaeger 压力小。**B** 4：多工具轮次更快，但池子 4 时排队更多。**C** 16（上游）：要改 C3 §13（用户门），lab 与连接池负载风险最大（实验已出现被挤垮的情况）。 | **A**，env 可调 | `worker_main.py`、`docs/development.md` | 按推荐 A（lead 2026-10-22 依用户 2026-10-17 常设指示采纳） |
| U-2 | 并发时 `tool_seconds_used` 与 active 预算的口径 | **A** 保持各调用耗时总和（`executor.py:961`）：ledger 和 active 预算的消耗与串行时相同，并行只省墙钟，不放宽 7200 s 上限，C3 §13 不动。**B** 改为每批墙钟：并行真正省预算，但 C3 §13「活跃时间」和 ledger 审计口径都要变。 | **A** | `executor.py`（只加锁）、`loop.py:1180-1186` | 按推荐 A（lead 2026-10-22 依用户 2026-10-17 常设指示采纳） |
| U-3 | 提交顺序与批内栅栏失败/崩溃 | **A** 按 index 顺序提交（等 i-1 提交完再提交 i；issue 写「提交顺序不变」）；续租或 fence 一失败就停止派发新调用，等在途调用结束（受单次超时约束），按 index 顺序尝试提交，第一次 `CONTROL_DENIED` 后按现有规则停止（迟到结果由 `steps.py` 记 late_result）。崩溃时已提交的前缀是持久的；已完成但未提交的和在途的调用由恢复回放串行重发，重复外部查询最多 N−1 次（N = 本轮调用数），ledger 每次都计。**B** 按完成顺序提交：崩溃时丢的少，但事件和页面上的 `tool_committed` 顺序会变，与「提交顺序不变」冲突。**C** 栅栏一失败就丢弃在途结果、不提交：执行器已记的证据不再入库，审计上不一致。 | **A** | `loop.py:_run_tools`；验收的恢复步骤 | 按推荐 A（lead 2026-10-22 依用户 2026-10-17 常设指示采纳） |
| U-4 | cap=1 是否必须逐字节等价 | **A** 必须：同一确定性场景下 cap=1 与改动前的已提交行、发往模型的 messages、事件序列逐字节相同（耗时字段除外），并且 cap=1 与 cap=2 的 `IncidentOutcome` 和下一次模型请求相同。**B** 只要求结果语义等价：测试更宽松，但守不住模型可见面。 | **A** | 验收测试 | 按推荐 A（lead 2026-10-22 依用户 2026-10-17 常设指示采纳） |
| U-5 | 重置的范围 | **A** 只在摘要被接受的压缩之后**全部清空**（上游语义在 OpsPilot 中保护集合恒为空，见第 0 节），不加 env 开关。**B** 照搬「≥ 单工具上限一半的结果仍拦截」：上游只在走溢出分支时才这样做，照搬属于 OpsPilot 独有的偏离。**C** 加 `OPSPILOT_RESET_DEDUPE_AFTER_COMPACTION` 开关：多一个配置面，没有使用场景。 | **A** | `loop.py:1022-1028` | 按推荐 A（lead 2026-10-22 依用户 2026-10-17 常设指示采纳） |

### 可逆默认（Agent 自定，写一行理由）

| 编号 | 问题 | 推荐与理由 | 影响面 |
|---|---|---|---|
| R-1 | 重置的位置 | 放在 `_compact` 中被接受的分支末尾（`loop.py:1028` 之后）`state.seen_tool_calls.clear()`；被拒的压缩已经交接，不用处理；resume 本来就是空的。只有一个调用点（`:903`），不存在其他压缩路径 | `loop.py` |
| R-2 | 同一批内的重复 | 按 index 先算指纹：每个指纹的第一个调用作为「首个」并行派发；同一批的后续相同调用和已见过的调用，在首个完成后按 index 用现有串行规则处理（首个有 evidence_id → 返回重复视图；首个是纯拒绝 → 按串行再跑）。这样每个后续调用的结果与串行完全相同 | `loop.py` |
| R-3 | 配置 | `InvestigationLoop`/`InvestigationRunner` 增加 `tool_parallelism: int = 1`；worker 从 `OPSPILOT_TOOL_PARALLELISM`（默认 2，取值 1–16，非法值时启动 `SystemExit`）读入。参数默认 1 → 现有测试、评测录制/回放和其他脚本行为不变（录制钩子 `evaluation/recording.py`、`replay.py:118-140` 的线程安全**未确认**，评测线路显式传 1） | `runner.py`、`worker_main.py`、`development.md` |
| R-4 | cap=1 的实现 | cap=1 不建线程池，在调用线程内执行：thread-local、trace 和异常路径与现在完全一样 | `loop.py` |
| R-5 | 续租/fence | 只在主线程做，每次派发前一次 `renew()`+`assert_current()`（与现在次数相同），然后提交任务；`commit_tool`、`delivered`/`evidence_ids`/`results`/`capture_delivery` 都在主线程按 index 处理 → `StepCommitter`/`EmittingCommitter` 保持单线程使用，`run_coverage_message` 顺序不变 | `loop.py` |
| R-6 | 执行器计数 | 用一把锁保护 `_operations_used`/`_tool_seconds_used` 的增量；每次调用内部的控制、期限、超时检查不变 | `executor.py` |
| R-7 | trace | 提交任务时把主线程的 run span 带进工作线程（`tracing` 增加一个上下文助手：进入线程设置 `_current.run`，结束时恢复）；`tool_calls` 计数加锁；`seq` = 开 span 的顺序，与 index 不同不算缺陷（index 和 step_id 已经在 span 上） | `tracing.py` |
| R-8 | 单个调用出现非预期异常 | `execute` 按设计不抛异常；如果抛了：先提交 index 小于它的结果，等在途调用结束，丢弃 index 更大的结果（由恢复重发），原样重新抛出——与串行时「后面的调用没派发」的持久状态一致 | `loop.py` |
| R-9 | 恢复回放 | `worker.execute_pending` 保持串行，不在本 PR 范围 | — |
| R-10 | 版本闸 | 不改 `CONTEXT_POLICY`、`prompt_revision`、tool schema：已提交行的形状和 rebuild 结果不变，升级时正在跑的 Run 照常续跑，不会被 `blocked` | `context.py` 不动 |
| R-11 | 连接池 | 不自动扩池，也不改 B1 的校验；工具线程只在短事务里占连接。真实 Run 里观察不到 `TIMEOUT` 即可；嵌套事务的风险与 B1 同为**未确认**，如果 B1 先合并，可以把校验收紧为 `capacity×(1+P)`（另提 issue） | 文档 |

## 允许改动的文件

`opspilot/investigation/loop.py`、`opspilot/investigation/runner.py`（透传参数）、`opspilot/tools/executor.py`（只加锁）、`opspilot/tracing.py`（线程传递和计数锁）、`opspilot/worker_main.py`（env）、`scripts/m1_live_runner.py`（新增 `--tool-parallelism`、`--context-tokens` 两个参数，用于真实 Run）、`docs/development.md`、任务记录。**不改**：`messages.py`、`context.py`、`reports.py`、提示词/registry、persistence/迁移、`worker.py` 回放、C3。C3 §5:219 的措辞合并后就与实现一致；§13:462 的「2」与默认值一致 → 本 PR 不改 C3，#313/PR #317 已注明依赖 #309，不需要另外引用本 PR。

## 独立验收测试作者须覆盖（公开接口 / IncidentScenario→IncidentOutcome，fixture 档）

1. 压缩被接受后，用与压缩前完全相同的调用再查一次：真实派发，得到新 evidence_id，不是 `DUPLICATE_TOOL_CALL`；压缩前的同一轮内重复仍被拒。
2. 摘要被拒的压缩 → `COMPACTION_FAILED` 交接（回归）。
3. 一轮 ≥2 个调用、cap=2：用带延迟的传输证明各调用的时间区间有重叠；已提交的 ordinal 完整；下一次模型请求里的 tool 消息按 tool_calls 顺序排列；`delivered` 和覆盖消息的顺序与 cap=1 相同。
4. U-4：cap=1 与改动前逐字节相同（确定性夹具）；cap=1 与 cap=2 的 Outcome 相同。
5. 同一批内的重复：首个产出证据 → 后续的返回重复视图且不派发；首个是纯拒绝 → 后续照常派发（与串行相同）。
6. 批中 fence 失败：不再派发新调用；在途结果尝试提交并按 late_result/交接处理；已提交的前缀保留。
7. 批中崩溃后 resume：已提交的 ordinal 不重发，只有 pending 的被重发，最终结果完整。
8. 并发下 ledger 的 `tool_operations_used` 和 `tool_seconds_used`（总和）与串行相同；100 次请求上限的计数不变。
9. 不合法的 `OPSPILOT_TOOL_PARALLELISM` 在启动时被拒。

## 真实 Run 证据（ADR-0006：LangSmith trace + `summary.json`）

- Run A（otel-demo 档，cap=2，真实 DeepSeek）：trace 里至少一轮 ≥2 个工具 span 挂在 run span 下，时间区间有重叠；summary 记录每轮的工具数、并行峰值、ledger 计数，以及没有 `TIMEOUT`/`CONTROL_DENIED`。
- Run B（压缩）：生产窗口是 1M token（`limits.py:63`），实际触发不到 95% 阈值 → 用缩窄的 `RunLimits(context_tokens≈120k)` 强制压缩（`within()` 允许缩窄，`limits.py:131-135`），必须出现 ≥1 次被接受的压缩。压缩后模型是否会重查同一调用无法强制：如果出现，summary 记下它被真实派发（新 evidence_id）；如果没出现，照实写「未发生」，该行为以第 1 条确定性测试为准。
- 每轮跑前查 swap 和 lab 负载（有记录：并行线路会挤垮 kind 实验）。
