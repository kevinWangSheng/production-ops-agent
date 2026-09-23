# PR 合并审阅摘要：#28 / #29 / #30

审阅日期：2026-09-17。只读判断，不构成合并授权。

**建议：#29 先修 deadline 红线；#28 先明确新 Run 接续的版本条件；#30 可以作为恢复协调模块评审，但不能据此批准完整自动恢复上线。三者不能凭各自 CLEAN 直接一批合入：真实合并冲突及跨 PR 行为组合需要复验。**

## 核查边界与原始验证

- 已读任务书、AGENTS Code Review Rules、PRODUCT-CONSTRAINTS、C3 §4–8、§13；读取 PR body、diff、分支源码和任务记录。行号均针对下列固定 HEAD，不能直接套用 main。
- #28：`49a51755a9baa7e3424e5ca8047c17c847727171`；#29：`26d0c70cd29e8a2cda64a50e4b9483f46e214966`；#30：`a8c28e5c54d977aeda37bed47de094c2334014d3`。本地 origin 引用与 GitHub 查询一致。
- GitHub 查询三者均为 OPEN、CLEAN；`gh pr checks` 三者都返回 `no checks reported`。手动 CI 另查：#28 run **35126487104** success，覆盖 `0aed9fb`，其后至 HEAD 只有任务记录 4 行；#29 run **35262241065** success，覆盖 `a378114`，其后至 HEAD 只有任务记录 1 行替换；#30 run **35265574868** success，精确覆盖 HEAD。因此 #28/#29 不能写成“最新 HEAD CI 已成功”，尽管源码与成功 CI 的版本相同。
- Code Review 汇总分别明确覆盖上述 HEAD，状态 Completed；GraphQL 查询 #28/#29/#30 的 thread 数为 **16/23/6**，全部 resolved。resolved 只代表讨论已关闭，不代表本报告指出的疑点不存在。安全审查不是交付门槛。
- 本轮实际运行：临时 detached worktree `/tmp/digest2-29`，使用既有该任务 `.venv/bin/python`，`PYTHONDONTWRITEBYTECODE=1 ... -m pytest -p no:cacheprovider tests/test_m1_investigation_loop.py tests/test_m1_investigation_pairing.py tests/test_m1_investigation_client.py -q` → **68 passed in 0.29s**；`/tmp/digest2-30` 同样运行 `tests/test_worker_recovery.py` → **9 passed in 0.11s**。测试的是 PR 单独分支，不是集成产物。
- 未启动 PostgreSQL、未写数据库、未调用真实模型/遥测/trace。PG 测试覆盖下文仅指已读测试代码，历史通过数不冒充本轮实跑。另作一次本机 socketpair 对抗验证，无外网请求，结果见 #29。
- 两个临时 worktree 已正常移除。主工作区仍为原 main，原有 `?? .playwright-mcp/` 未动。未修改仓库源码、任务记录、PR 或远程状态。GitHub 只读查询是任务书要求的联网例外。

## PR #28 — 人工控制持久结果与取消后接续

### 1. 一句话

让失效的模型/工具结果留作可追溯历史，并让取消的事故通过新 Run 接续；追问、纠正、取消本身的代际控制主要是已有能力，本 PR 不等于完整人工控制交付。

### 2. 接口形状

1. `DurableStore.new_run(incident_id, run_id, *, deadline, budget_limit, versions, actor) -> int`：从 cancelled 创建 queued Run，切换当前 Run，递增事故代际并写审计（`opspilot/persistence.py:222`）。
2. `commit_step()`：拒绝失效租约之前提交 `late_result:step:<logical_key>` 历史；业务步骤不得使用保留前缀（`:406`）。
3. `commit_tool()`：仅对确实存在的步骤保留迟到工具历史；不存在步骤返回 `UNKNOWN_IDENTITY`（`:459`）。
4. `publish()`：迟到发布统一使用稳定历史身份，而不是每次随机新建历史键。
5. `rebuild()`：待办只纳入当前代际且状态为 `response_committed/tool_result_committed` 的步骤，迟到行不再变成可执行工具（`:640`、`:668`）。
6. 既有 `opspilot_steps`：历史键、sequence、observed_at 承载迟到身份、顺序、时间；既有 `opspilot_controls` 记录 `new_run` 审计。没有新增 HTTP 端点。

### 3. 场景清单

- 给定纠正使旧租约失效，当旧模型步骤或工具结果返回，则保存历史后拒绝业务采纳。覆盖：`test_late_step_and_tool_results_are_recorded_as_history`（PG；本轮未运行）。
- 给定旧结论晚于人工纠正，当再次 publish，则不覆盖当前结论，重复迟到发布按身份幂等。覆盖：`test_correction_rejects_late_publish_and_keeps_history_only`（PG）。
- 给定仅租约过期而代际未变，当迟到响应含工具计划，则不会在 rebuild 中重新成为待办。覆盖：`test_expired_late_step_is_history_and_not_pending_work`（PG）。
- 给定已取消事故，当用新 run_id 接续，则旧 Run 仍 cancelled，新 Run queued、代际递增；合法重试返回原代际。覆盖：`test_cancelled_incident_can_continue_with_a_new_run`（PG）。
- 给定事故尚未取消，当误调用 new_run，则拒绝，而不是把 accept/follow_up 后的 queued 误当接续重试。覆盖：`test_new_run_is_refused_until_the_incident_is_cancelled`（PG）。
- 给定同一预期代际，当 follow_up 与 cancel 并发，则只有一个成功。覆盖：`test_concurrent_follow_up_and_cancel_have_one_winner_generation`（PG；这是既有控制机制的补测）。

### 4. 红线核对

- **只读权限：PASS（本 PR 增量）**。仅修改 Agent 自有业务记录，不新增生产写操作或任意命令入口。
- **人工控制优先：具体疑点**。`new_run` 没有调用方提供的 `expected_generation/expected_version`，仅在事务里读“当前 cancelled”再推进（`opspilot/persistence.py:222–275`）。C3 §4 要求人工操作使用预期版本。锁能防并发写，不能辨认基于旧页面/旧取消状态发来的接续请求；而全局/目标 suspension 也不在本分支持久化路径中（任务记录 `:45`）。
- **业务记录恢复权威：PASS（局部）**。历史写入在报 `CONTROL_DENIED` 前显式提交（`:431`、`:489`），恢复读 PG，不用 checkpoint；旧 Run 历史不经当前 Run 的 rebuild 展示，属于可见性缺口。
- **秘密与数据出口：PASS（增量出口）**。没有新增网络出口；`response/result` 仍作为任意 dict 写入 Agent 数据库，不能据此认定未来 UI/trace 导出整列安全。
- **预算与 deadline：PASS（不重置旧 Run）**。旧 Run 保留；新 Run 使用新参数，不延长旧租约。疑点是 `new_run` 直接接受 deadline/budget_limit/versions，缺少与 `accept` 相当的输入校验及调用方授权证明（`:265–267`）；不能把这个 Python 接口直接视为可向用户开放的安全入口。

### 5. 疑点与验证办法

1. **新接续缺预期版本条件**：先读 cancelled generation N；另一操作者已接续并再次取消到 N+2；再提交旧意图的新 run_id。当前签名无处传 N，静态路径会接受当前 cancelled。验证：PG 增加这一顺序用例，要求旧请求拒绝且当前状态不变；若设计为内部可信命令，应提供上层同事务版本检查的调用证据。当前未看到该保证。
2. **新 Run 参数校验**：尝试 naive datetime、非正 budget_limit、畸形 versions、空 actor，检查是稳定 `INVALID_INPUT` 还是数据库/后续 claim 才失败。只读任务未执行写库复现。
3. **旧 Run 历史可见性**：取消后建立新 Run，再调用事故详情实际使用的查询，确认原迟到结果仍可查看；`rebuild()` 只读 current_run（`:653–657`），不能用“行仍存在”代替操作者可见。
4. **与 #29 的重复迟到实现**：#29 另写 `late-step:` 与随机 publish 历史，本 PR 用保留命名空间及 sequence。冲突解决应保留 #28 的身份/历史隔离，并重新跑迟到行不能成为 pending 的 PG 回归。

### 6. 合并依赖与冲突

- base：`chore/durable-store-hardening`，先合 **#26** 再 retarget main。与当前 #26 的 `merge-tree` 没有文本冲突。
- 与 **#29** 实测冲突：`ROADMAP.md`、`opspilot/persistence.py`、`tests/integration/test_m1_durable_state_postgres.py`。
- 与 **#30** 实测冲突：`opspilot/persistence.py`。该冲突关系到“排除迟到行”与“读取 assistant.tool_calls”同时保留，不能任选一边。
- 当前 HEAD 只有文档未被最新成功 CI 覆盖；整合后的新 HEAD 必须重新检查与审查。

### 7. 需要用户拍板的事项（原文）

PR 描述：

> 全局/目标 suspension 持久化接入 claim/budget/adoption、follow-up/correction 输入载荷持久化、close/reopen、目标重绑定、事故合并/拆分及授权撤销、持久化输入水位：不在本子任务范围，供用户决定是否另立任务。

任务记录 `docs/tasks/2026-09-16-m1-01-human-control.md:48`：

> 本子任务不把 `rebuild()` 扩成全量审计接口。

这里需决定的是合并边界和后续验收归属，不能批准取消 C3 已要求的能力；这段文字只代表 #28 分支当时的缺项，不能据此推断后续 #31/#33 仍未实现。

## PR #29 — Flash 调查循环

### 1. 一句话

加入“模型响应持久化 → 执行只读工具 → 观察配对 → 有证据绑定的结构化报告/明确交接”循环，但当前仍有可复现的请求总时限缺口。

### 2. 接口形状

1. `InvestigationRequest` / `LoopOutcome`：Run 范围、问题、工具 schema、证据 context、调用上限作为输入；执行状态、报告、交接原因、用量和 revision 作为输出。
2. `InvestigationLoop.run()`：每次调用代表一次 worker attempt；模型、执行器、提交器、时钟由调用方注入（`loop.py:195–207`）。
3. `DeepSeekClient.complete(ModelCall)`：一次非流式 HTTPS 请求，拒绝重定向，认证只在请求头（`client.py:63–139`）。
4. `StepCommitter` / `MemoryStepStore` / `DurableStepStore`：模型预算预留/结算、步骤与工具提交接缝；PG adapter 绑定租约。
5. `DurableStore.settle_budget(..., outcome)`：reserved 转 spent/unknown，未知费用继续占用；这是请求计数结算，不是 CNY 对账（`persistence.py:307`）。
6. `ReportV2` / `parse_report()` / `unsupported_citations()`：`m0-report-v2` 结构与证据、目标、时间引用检查。
7. `evidence_context_projection()` / `delivered_from_context()`：字段白名单投影与 Run 身份匹配的证据引用输入（`reports.py:278`、`:496`）。
8. `prompt_revision_versions()`：给恢复 versions 提供 L1+L2 内容 revision；L1 使用本分支引入的 #27 较旧版本，不是当前 #27 HEAD。

### 3. 场景清单

以下测试均包含在本轮 **68 passed** 中。

- 给定模型先要工具再给报告，当循环执行，则先提交完整响应及工具观察，再构造配对上下文；最后请求保留给报告且不再给 tools。覆盖：`test_committed_step_records_request_hash_and_response_id`、`test_last_request_is_reserved_for_the_report_and_sends_no_tools`。
- 给定响应截断却携带工具计划，当 finish_reason=length，则不执行工具。覆盖：`test_length_finish_with_tool_calls_does_not_execute`。
- 给定预算用尽或发起前已过 deadline，当请求下一轮，则明确 handoff 且不误报完成。覆盖：`test_budget_exhaustion_handoffs_and_is_not_completed`、`test_deadline_before_dispatch_handoffs_without_calling_the_model`。
- 给定网络失败并重试，当发生第二次物理请求，则重新预留并核对控制，失败记 unknown。覆盖：`test_retry_re_reserves_and_rechecks_control`、`test_every_physical_request_is_settled_as_spent_or_unknown`。
- 给定模型编造引用，或传入其他 Run 的引用 context，当验证报告，则不承认为本 Run 已交付证据。覆盖：`test_invented_evidence_id_is_not_published`、`test_invented_target_ref_is_not_published`、`test_invented_time_scope_is_not_published`、`test_evidence_context_from_a_different_run_is_not_trusted`。后者没有证明跨 Run 内容不进入 prompt，见下文。
- 给定有效但信息不足的报告，当完成解析，则执行可 completed，同时保留 `INCOMPLETE_INVESTIGATION` handoff。覆盖：`test_incomplete_valid_report_is_completed_execution_with_handoff`。

### 4. 红线核对

- **只读权限：PASS（本 PR 调用形状）**。工具经 `ReadOnlyToolExecutor`，target/window 从可信 scope 构造而不是模型参数选择（`loop.py:413–425`）；没有生产 mutation 或 release gate。
- **人工控制优先：PASS（持久写入栅栏局部）**。每个物理请求 reserve，围栏后的结算保持占用，步骤提交走持久栅栏（`loop.py:474–514`、`:533–561`）。没有完成全局/目标暂停的完整服务接线证明。
- **业务记录恢复权威：具体疑点**。步骤响应已提交后才执行工具（`:352`、`:396`）；但 `run()` 从空 messages/round-1 起步（`:239`、`:258`），不是从已有步骤重建整个调查。恢复与 #30 的上层组合尚需验证；此外 discipline revision 仍漏掉非 L1a segment 变化（`instructions/discipline.py:220–235`）。
- **秘密与数据出口：具体疑点**。Authorization 只用于头、无代理自动继承和重定向；嵌套白名单过滤有测试。但 context 的 Run 检查只发生在 `delivered_from_context()`，此前 `loop.py:243–248` 已把投影后的其他 Run context 放进 prompt；“不采纳引用”并不等于“没有跨 Run 出口”。`reasoning_content` 会存入步骤 assistant（`:347–352`、`:500–508`），未来整列导出必须单独过滤。
- **预算与 deadline：存在已复现红线缺口**。`client.py:127–129` 将小于 100ms 的剩余时间扩为 100ms；`:156–168` 在一次 `read(65536)` 前设置 socket timeout，不能在 BufferedReader 内部每次 raw read 后递减总剩余时间。HTTPError 的正文读取 `:132–137` 也绕过 `_read_capped`。现有通过测试不覆盖真实缓冲层慢滴流。

### 5. 疑点与验证办法

1. **已复现：wall deadline 不是硬上限，建议合并前修复。** 本轮调用 PR 原函数 `_read_capped`，使用本机 `socket.socketpair()` + `http.client.HTTPResponse`，响应 `Content-Length: 10`，另一端每 20ms 写一个字节；传入 `start+0.06` deadline。输出：`MODEL_UNAVAILABLE budget_seconds 0.06 elapsed_seconds 0.221`。持续有字节到达会重置单次 socket 等待，外层直到 read 返回才有机会检查。验证修复应把真实缓冲层 slow-trickle、响应头等待和 HTTPError 路径都纳入有界终止回归，不能仅断言 settimeout 数列递减。
2. **跨 Run context 仍进入模型输入。** `reports.py:510` 只返回空 delivered views，`loop.py:243–248` 却无身份门槛地发送 context。验证：用 Run A 的 context 调用 Run B，捕获 fake ModelClient 的 ModelCall.messages，断言 A 的内容完全不存在或调用在发送前被拒。现有测试仅断言引用不能支持报告，结论应收窄。
3. **证据 current 时间策略仍只校验单点 freshness。** `reports.py:415–426` 没有 source 起止区间核验；PR 自己明确缺 transport 数据基础。验证：工具输出同时提供 source_start/end、observed_at，加入“末点新鲜但区间覆盖不合要求”的拒绝测试；是否扩大 #20 schema 是独立设计事项。
4. **恢复/版本和上下文上限仍未完整接通。** `MAX_CONTEXT_TOKENS=131072` 是常量，目前执行的是 512KiB HTTP 字节上限；二者不等价。恢复还需用真实 PG 验证 round 已提交时不重复调用、未知费用不释放、tool_schema_revision 与 prompt_revision 同时守门。不能把 fixture + MemoryStepStore 的 2 HTTP 历史 Run 称为 PG 产品端到端验收。

### 6. 合并依赖与冲突

- base：`feature/m1-01-tool-executor`，先合 **#20**。与当前该 base 的 `merge-tree` 无文本冲突。
- **#27 是内容/版本正确性依赖**：PR 已拷入旧版 L1，不能再称无差异复用当前单一来源。与当前 #27 实测冲突：`opspilot/instructions/__init__.py`、`opspilot/instructions/discipline.py`、`tests/integration/test_m1_durable_state_postgres.py`、`tests/test_instruction_discipline.py`。以批准的当前 #27 合同消解并重新核 revision。
- 与 **#28**：`ROADMAP.md`、`opspilot/persistence.py`、`tests/integration/test_m1_durable_state_postgres.py`。
- 与 **#30**：`opspilot/persistence.py`、`tests/integration/test_m1_durable_state_postgres.py`。
- 上述为 `git merge-tree $(git merge-base A B) A B` 输出实际冲突标记所在文件；同文件双方修改但无冲突的 `tests/test_architecture.py` 没有误列为冲突。没有修改/合并工作分支。

### 7. 需要用户拍板的事项（原文）

PR 描述：

> 条目 3 的「完整 source 区间校验」子问题需要 PR #20（`opspilot/tools/`）先补 `source_start_at`/`source_end_at` 字段，属另一 PR 的设计评审范围，本任务不能替它做决定。

> `discipline.py` 的 `template_projection` 覆盖面缺口留给 #27 合并时整体替换；`tool_schema_revision`（`versions` 比对另一半）与产品代码里真正创建 Run 的 `DurableStore.accept`/`claim` 调用点不在本任务范围（worker/service 层尚未实现）。

任务记录原文：

> 用户审核合并 PR #29。

本报告建议把可复现 deadline 缺口交回修复，并在合并授权中明确上述跨 PR 集成条件；不把关闭的 thread 或历史成功 CI 视为接受红线降级。

## PR #30 — Worker 重启恢复协调

### 1. 一句话

让新 Worker 从 PG 已提交记录找到剩余工具操作、领取新 epoch 再继续，并在版本/身份不一致时拒绝恢复；它尚不是接上真实执行器和完整调查上下文的自动恢复服务。

### 2. 接口形状

1. `RecoveryPlan`：递归不可变的业务快照和 pending_tools；`candidate` 区分可尝试恢复状态（`opspilot/recovery.py`）。
2. `rebuild_plan(snapshot)` / `recover(store, incident_id)`：从 PG 快照生成计划，不读取旧 graph checkpoint。
3. `Worker.create(store, versions)`：建立独立 owner；`claim()` 领取当前版本的租约。
4. `Worker.resume(..., lease_seconds=420, renew_seconds=420)`：读计划、领取新 epoch，代际不一致则 abandon（`worker.py:94–108`）。
5. `RecoverySession.execute_pending(execute)`：每个工具前核 current lease/代际，执行后可选续租，再提交（`:31–64`）。
6. `RecoverySession.publish()`：仍委托 DurableStore 的发布条件。
7. `DurableStore.lease_current()` / `abandon()`：核租约与撤销本 attempt 所有权。
8. `DurableStore.rebuild().pending_tools[]`：新增稳定 `operation_id=step:ordinal` 与 `tool_call`；支持 `assistant.tool_calls` 和旧顶层形状，损坏形状拒绝（`persistence.py:585–638`）。没有新增产品 HTTP 端点。

### 3. 场景清单

- 给定 worker 进程死掉而业务记录仍在，当新 Worker resume，则新 owner/epoch 继续；覆盖：`test_worker_subprocess_kill_then_resume_from_business_rows`（PG；本轮未跑）。
- 给定 loop 形状的两项工具中一项已提交，当恢复，则只执行未完成 ordinal。覆盖：`test_rebuild_lists_pending_tools_from_a_loop_shaped_step`、`test_worker_resume_executes_only_the_pending_tools_of_a_loop_step`（PG）。
- 给定持久工具计划类型损坏，包括 falsey 的 `{}`/`""`/0，当 rebuild，则 `INCONSISTENT_STATE`。覆盖：`test_rebuild_rejects_a_falsey_malformed_tool_calls_value`、`test_rebuild_rejects_a_malformed_loop_shaped_step`（PG）。
- 给定旧 session 被新 epoch/人工代际取代，当派发待办，则拒绝旧计划。覆盖：`test_recovered_session_checks_epoch_before_dispatch`（PG）。
- 给定调用方试图修改嵌套恢复参数，当改 RecoveryPlan，则失败。覆盖：`test_recovery_plan_nested_values_are_immutable`（本轮通过）。
- 给定 store 提供续租能力，当工具完成，则续租后提交；续租被拒时不提交。覆盖：`test_execute_pending_renews_between_execute_and_commit_when_available`、`test_renewal_rejection_stops_before_the_tool_result_is_committed`（本轮通过；测试替身，不是本分支 PG 续租证明）。

### 4. 红线核对

- **远程审查状态：未完成**。GitHub 汇总显示 Code Review 已覆盖 HEAD，但 Security Review 仍为 `running`（查询时）。按项目交付规则不能把 #30 标为审查闭环。
- **只读权限：具体集成疑点**。模块自身没有系统写操作，但 `execute_pending` 接受任意 Python callback（`worker.py:52–58`），自身不限制工具种类、target、预算。它是可信工程接缝，只有接入只读网关后才可给产品路径判 PASS。
- **人工控制优先：PASS（结果采纳局部）**。执行前重读当前 lease/Run/代际，执行后持久提交仍有 fence（`:31–39`、`:57–62`）。在途回调的停止/清理由调用方承担；不能称本模块已实现有界取消。
- **业务记录恢复权威：PASS（来源）／具体组合疑点**。只读 PG 快照，新 claim 新 epoch。当前 `rebuild` 对所有步骤按代际找 pending，未像 #28 一样过滤 status（`persistence.py:629–631`）；与 #28/#29 的 late_result 行组合必须复验，避免迟到历史重新作为执行计划。
- **秘密与数据出口：PASS（本模块增量）**。没有模型/trace 网络出口；恢复计划包含原 payload，传给真实执行器/后续模型时仍需遵守原 Run/provider、撤权和秘密字段规则，当前模块不自行证明这些上层出口。
- **预算与 deadline：具体疑点**。领取/提交仍检查持久租约与 deadline，但 `execute(item)` 自身无超时（`worker.py:58`）。420 秒租约只是占有权期限，不会终止卡住的回调；续租缺 #35 时是 no-op（`:48–50`），且有 #35 也要等 execute 返回才调用。

### 5. 疑点与验证办法

1. **续租依赖尚未在分支落地**：本分支 DurableStore 无 renew_lease，`test_m1_lease_renewal_wiring_postgres.py` 的 hasattr 条件使测试跳过。验证：合入 #35 后在组合 HEAD 实跑 PG 续租、取消、deadline 用例，不能引用单分支 9 passed 证明真实续租。
2. **工具 operation_id 不一致**：`persistence.py:626` 为 `step:ordinal`，#29 经 ToolRequest 使用 `step-t{index}`。验证：恢复与正常执行使用同一操作，把去重、预算扣费、Evidence 身份逐字段对齐；不要只看工具返回内容一致。
3. **迟到计划与合法计划必须分离**：把 #28 迟到 step + #30 assistant.tool_calls 重建组合起来，要求迟到行保留但 pending 为空。此处有真实文本冲突，必须有组合 PG 回归，不能通过取 #30 一侧解决。
4. **420 秒和有界停止**：用会超过 deadline/卡住的执行回调，观察是否在合同清理时限内退出，并确认后续工具不发起、旧结果不采纳。现在只有后两项 fencing 的局部证据，缺终止回调机制。
5. **完整调查恢复未证明**：本模块恢复已提交工具，未重建 #29 的下一轮模型 messages/round 计数。验证需是完整断点场景，而不是单独 `execute_pending` 的成功。

### 6. 合并依赖与冲突

- base：`chore/durable-store-hardening`，先合 **#26**。当前 base 合并无文本冲突。
- 续租能力还依赖 **#35 `fix/lease-renewal`**；与当前 #35 `merge-tree` 无文本冲突，但无冲突不是运行验证。
- 与 **#28**：`opspilot/persistence.py` 实际冲突。
- 与 **#29**：`opspilot/persistence.py`、`tests/integration/test_m1_durable_state_postgres.py` 实际冲突。
- #30 当前 HEAD 有精确 CI 成功和 Code Review 完成证据；以上集成问题仍需新组合 HEAD 的检查与审查。

### 7. 需要用户拍板的事项（原文）

PR 描述：

> **#35 需先合并**，本分支的续期接入才会真正生效；#35 合并前，本改动对生产行为无影响（`getattr` 找不到方法，等价于未改动）。

这句话只适用于“新增续租调用”的可选行为；本 PR 还改了 claim 默认租约为 420 秒，不能泛化成整份 PR 无行为变化。

> 420 这个数字借自 #33/#35 的「模型请求超时+余量」推导，但 `RecoverySession` 本身不调模型，本分支代码里也没有强制的单工具执行时长上限，因此该值对本条路径是保守估计而非独立推导的结果，已在代码注释与任务记录中如实标注，留给后续复核。

> `pending_tools[].operation_id`（`step:ordinal`）与执行器 `ToolRequest.operation_id`（`step-t{index}`）格式不一致，先于本变更、接入真实执行器前需统一。

需要的决定是是否按“恢复协调模块、带明确后续集成条件”合入，以及将操作身份统一/有界终止复验归入哪个现有集成任务；没有依据批准其已具备完整自动恢复能力。
