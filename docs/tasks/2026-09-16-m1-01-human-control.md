# M1-01 子任务：追问/纠正/取消（人工控制）

- 状态：PR 已就绪，待 #26 合并后 retarget 到 main，待用户审核合并
- 更新日期：2026-09-16
- PR：https://github.com/kevinWangSheng/production-ops-agent/pull/28
  （stacked，base = `chore/durable-store-hardening` / PR #26；待 #26 合并后 retarget 到 main）
- 依据：`SPEC.md` M1-01 bounded-open 门槛、Operating constraints、Verification and delivery；
  `PRODUCT-CONSTRAINTS.md` 人工控制优先级；
  C3 技术方案第 4 节「人工控制与结果所有权」「全局与目标级暂停」；
  现有 DurableStore 合同。
- 工作区：分支 `feature/m1-01-human-control`，
  worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-human-control`，
  起点 `origin/chore/durable-store-hardening`。与重启子任务共享 `persistence.py`，
  本任务未扩展其结构之外的改动。

## 目标与范围

在 DurableStore 控制代际基础上，验证并补齐 C3 人工控制中本子任务范围内的持久语义：
`follow_up` / `correct` / `cancel` 的代际条件更新、迟到结果只进历史、取消后新 Run 接续。
范围为领域、持久化和服务接口，不含 UI/SSE。

本 PR 定位是「验证 + 补缺」，不是从零实现人工控制。

## 本 PR 新增

- `DurableStore.commit_step()` / `commit_tool()`：代际或租约拒绝前把迟到结果写入 `late_result` 历史，并提交该历史事务。
- `DurableStore._late_result()`：`logical_key` 绑定原步骤/工具身份（保留前缀 `late_result:step:` / `late_result:tool:` / `late_result:publish:`）；写入单调 `sequence` 与 `observed_at`；同一身份重放 `ON CONFLICT DO NOTHING`。业务 `commit_step` 不得使用该前缀。
- `DurableStore.new_run()`：仅允许 cancelled 事故创建新 Run，要求匹配调用方观察到的 `expected_generation`，递增控制代际、替换 current_run、清除旧结论并写入 `new_run` 审计；同一 `run_id` 在已接续的 queued 事故上重试返回既有代际。
- PG 回归：`test_late_step_and_tool_results_are_recorded_as_history`、`test_expired_late_step_is_history_and_not_pending_work`、`test_live_steps_cannot_use_the_late_result_key_namespace`、`test_cancelled_incident_can_continue_with_a_new_run`、`test_new_run_is_refused_until_the_incident_is_cancelled`、`test_concurrent_follow_up_and_cancel_have_one_winner_generation`；纠正后迟到 `publish` 的身份/幂等断言。

## PR #19 既有并在本 PR 验证

以下行为在 DurableStore / 领域层已存在，本 PR 用 PG 集成或既有合同测试核对，未重写其结构：

- `DurableStore.control()` 的 `follow_up` / `correct` / `cancel` / `pause` / `resume`，含 `expected_generation` 条件更新与租约收回。
- `publish()` 拒绝过期代际结论（本 PR 把该迟到路径改走 `_late_result()`，使身份/时序与步骤/工具迟到结果一致）。
- 领域控制模型：`opspilot/domain/control.py` 的控制代际、全局/目标暂停、人工优先级和 scope 栅栏（内存对象，不是 DurableStore 行）。
- 领域确定性断言：`tests/test_domain_contracts.py`、`tests/test_domain_state_machines.py`。
- 既有 PG 合同：`tests/integration/test_m1_durable_state_postgres.py` 中的 pause/resume/claim 栅栏、代际拒绝、rebuild 快照。

## 未实现待决（超出本子任务，不要在本 PR 实现）

- 关闭事故后新异常默认新关联事故、显式 reopen；`DurableStore.control()` 不接受 `close`/`reopen`。领域层有 `test_a_closed_incident_stays_reopenable`、`test_a_new_anomaly_on_a_closed_incident_defaults_to_a_new_incident`，不是持久化路径。
- 目标重绑定、事故合并/拆分及授权撤销；无持久化操作，不得把 `test_an_adoption_rechecks_every_recorded_control_version` 记成该要求的证据。
- 全局/目标 suspension 持久化并接入 DurableStore `claim` / `reserve_budget` / 结果采纳。当前 schema 与租约只有事故代际；领域暂停状态只在 `opspilot/domain/control.py`。
- `follow_up` / `correct` 的输入内容（问题、事实、纠正）持久化。`control()` 只接受 incident、generation、action、actor，`opspilot_controls` 无载荷列。
- 持久化输入水位：`opspilot_runs.input_watermark` 初始化为 0，没有更新/纳入路径。领域层 `test_the_input_watermark_only_moves_forward_and_only_while_running` 不是 DurableStore 证据。
- 事故级历史视图：`rebuild()` 只重建 `current_run_id` 的断点。取消 Run 的步骤与 `late_result` 仍留在 `opspilot_steps`，但不经 `rebuild()` 返回。本子任务不把 `rebuild()` 扩成全量审计接口。

### 当前 HEAD 新增审查处置（2026-09-20）

- 采纳并修复：`new_run()` 增加 `expected_generation`，拒绝旧观察代际在更晚控制后重新创建 Run；重复确认仅在原代际关系仍匹配时幂等返回。
- 采纳并修复：失效 `publish()` 在写入 `late_result` 前验证 `step_id` 属于该 Run；随机或跨 Run 的 step 返回 `UNKNOWN_IDENTITY`，不制造伪历史。
- PG 定向测试：`40 passed`；全量 `make check`：`1051 passed, 111 skipped, 2 xfailed`。

## C3 第 4 节逐条映射

| C3 要求 | 分类 | 现有确定性证据 |
|---|---|---|
| 人工操作用 `expected_version` 条件更新并递增主体代际 | PR #19 既有，本 PR 验证 | 领域 `test_a_control_operation_requires_the_expected_version`、`test_each_accepted_control_increments_the_control_generation`；PG `test_concurrent_follow_up_and_cancel_have_one_winner_generation` |
| Run 绑定主体且单一当前 Run 可更新结论，跨主体不得修改 | PR #19 既有 | 领域 `test_a_release_run_may_not_write_an_incident_conclusion`、`test_a_revoked_session_and_a_suspended_scope_stop_adoption`；PG `test_rebuild_reads_a_consistent_snapshot`、`test_control_refuses_a_current_run_pointer_into_another_incident` |
| 旧 Run 迟到结果仅保存历史 | PR #19 有 `publish` 路径；本 PR 补步骤/工具并统一身份/时序 | PG `test_correction_rejects_late_publish_and_keeps_history_only`、`test_late_step_and_tool_results_are_recorded_as_history`。`test_old_generation_step_and_tool_writes_are_fenced` 只证明拒绝写入，不检查 `late_result` 行 |
| 暂停继续接收事件但不自动查询 | 领域既有；DurableStore 未持久化全局/目标 suspension | 领域 `test_suspension_outranks_resume_mode_and_observation_authorization`。M0 `test_m0_pause_observer_postgres.py` 是实验存储，不是本模块 DurableStore |
| 取消保持终态，新调查使用新 Run；取消不关闭事故 | 取消终态 PR #19 既有；新 Run 接续为本 PR 新增 | 领域终态 `test_terminal_states_reject_all_triggers`；PG `test_paused_run_reaches_terminal_state_on_cancel`、`test_cancelled_incident_can_continue_with_a_new_run` |
| 关闭事故新异常默认新关联事故，可显式重开 | 未实现待决 | 无 DurableStore 路径。领域 `test_a_new_anomaly_on_a_closed_incident_defaults_to_a_new_incident`、`test_a_closed_incident_stays_reopenable` 只覆盖内存状态机 |
| 重绑定/合并/拆分撤销旧授权并保留历史身份 | 未实现待决 | 无实现路径。不得引用 `test_an_adoption_rechecks_every_recorded_control_version` |
| 每轮固定输入水位，新输入不得显示已分析 | 领域既有；持久化水位未实现待决 | 领域 `test_the_input_watermark_only_moves_forward_and_only_while_running`。PG `test_rebuild_drops_pending_tools_from_a_superseded_generation` 只证明待办工具按代际过滤，不读写 `input_watermark` |
| 全局/目标暂停是确定性状态，目标只能由登记身份解析 | 领域既有；持久化未实现待决 | 领域 `test_target_scope_resolves_to_registered_identities_not_names` |
| 暂停优先于 resume/automatic/human-owned observation | 领域既有；持久化未实现待决 | 领域 `test_suspension_outranks_resume_mode_and_observation_authorization` |
| scope generation 递增；领取/预算/请求/采纳复核版本 | 领域既有；DurableStore 只复核事故代际 | 领域 `test_a_suspension_transaction_increments_its_scope_generation`；PG 事故级 `test_paused_incident_cannot_be_claimed_or_published`、`test_correction_rejects_late_publish_and_keeps_history_only`。全局/目标 generation 未接入 claim/budget/adoption |
| 解暂停只移除该层阻挡，不恢复旧任务/授权/采样窗口 | 领域既有 | 领域 `test_resume_does_not_restore_an_observation_authorization`、`test_a_paused_run_resumes_as_a_new_attempt`。PG `test_paused_run_reaches_terminal_state_on_cancel` 只覆盖暂停后取消，不是解暂停 |

## 验证

见「本轮审查修复与验证」。未修改 feature passes，未扩大产品权限。

## 历史过程（不是当前状态）

- 初稿曾把范围写成「落实 C3 人工控制、全局/目标级暂停，以及 follow-up/correction/cancel 的持久语义」，并引用不存在的测试名。机器人审查指出后，已改为上表分类，不再把待决项写成已交付。
- 初稿「验证」节写过 `make check` 1051 passed / 84 skipped，且因当时未设 `M1_DURABLE_POSTGRES=1` 跳过 PG。该条已过时。
- 初稿「未完成项」曾把「需要跑 PG 集成」列为当前待办，随后本 worktree 已实跑；不得与后文 PG 实跑并存为当前状态。
- 初稿「PG 实跑状态」曾把跳过原因归到 `docs/evidence/m0-b/results.md` 的 PID 45335。该文件记录的是另一次 lab 停止后的 PID 4391，不能作为本次跳过的出处。当时的端口占用是未落盘的本地观察；调度方释放 55431 后已改由本 worktree start/run/stop。

## PG 实跑（历史，2026-09-16 第一次）

调度方释放 55431 后，在本 worktree 实际执行：

- `.venv/bin/python -m scripts.m0.postgres_lab start`：成功初始化并启动专属 PostgreSQL 17.9，数据目录为 `tmp/m0-b/postgres`。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`：`32 passed, 54 skipped in 2.69s`。当时的 `test_correction_rejects_late_publish_and_keeps_history_only` 与 `test_concurrent_follow_up_and_cancel_have_one_winner_generation` 在通过集合中。
- `.venv/bin/python -m scripts.m0.postgres_lab stop`：成功停止；数据保留。

跳过项是其他 M0 集成测试的独立 opt-in（预算、live、restart 等），不影响当时 M1 durable 集合。

## PG 实跑（历史，2026-09-16 第二次，迟到结果与 new_run 落地后）

- `.venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`（专属 PG）：`34 passed`。
- `.venv/bin/python -m pytest tests/integration -q`（专属 PG）：`34 passed, 54 skipped in 2.49s`；随后 `postgres_lab stop` 成功。
- `make check`：`1051 passed, 88 skipped, 2 xfailed`；ruff、format、mypy 全通过。

当时尚未写入迟到结果的稳定身份、`sequence`/`observed_at`，以及 `new_run` 对同一 `run_id` 的重试幂等；这些是本轮审查修复的对象。

## 本轮审查修复与验证（2026-09-16）

针对 PR #28 当时 12 条未 resolve 的机器人审查 thread：

- 代码采纳：迟到结果保留稳定 `logical_key`（步骤/工具/publish 身份）并幂等写入；`late_result` 行写入单调 `sequence` 与 `observed_at`；`new_run` 对同一 `run_id` 仅在已有 `new_run` 审计且仍为当前 queued Run 时重试返回既有代际。未取消事故上的 `new_run` 仍是 `ILLEGAL_TRANSITION`（独立审查 P1）。租约过期写入的 `late_result` 不进入 `pending_tools`，也不得被 `commit_tool` 改写成活步骤（HEAD 机器人 P1）。
- 任务记录采纳：分清本 PR 新增 / PR #19 既有并验证 / 未实现待决；删除不存在的测试名；PG 阻塞出处改为未落盘本地观察并标为历史；未完成 PG 实跑的旧待办移入历史。
- 未实现、按审查意见标为待决而不在本 PR 实现：close/reopen、重绑定/合并/拆分、全局/目标 suspension 持久化接入 claim/budget/adoption、follow-up/correction 输入内容、持久化输入水位。

本 worktree 专属 PostgreSQL：

- `.venv/bin/python -m scripts.m0.postgres_lab start`：成功（既有 `tmp/m0-b/postgres`，PostgreSQL 17.9，端口 55431）。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`：命名空间修复后 `36 passed in 2.24s`。
- `.venv/bin/python -m scripts.m0.postgres_lab stop`：成功停止；数据保留。
- `make check`：ruff / format / mypy 通过；`1051 passed, 90 skipped, 2 xfailed`（M1 PG 用例默认跳过）。

独立审查（全新上下文，仅 `9c9b6dd..` 本轮改动）：P1 为 `new_run` 把 `accept()` 后的 queued 当前 Run 误当成丢失确认重试；已用 `new_run` 审计收窄幂等并补 `test_new_run_is_refused_until_the_incident_is_cancelled`。P3 两条为 C3 映射引用过宽，已收紧。随后机器人 P2 要求保留迟到 `logical_key` 命名空间：已改为 `late_result:` 前缀，业务 `commit_step` 拒绝该前缀。`rebuild` 不展示旧 Run 迟到历史仍为残留，不在本 PR 扩大范围。

CI：仓库 workflow 仅对 base 为 `main` 或 `chore/m0-*` 的 PR 自动触发。代码 HEAD `ec567e3` 的 `workflow_dispatch` 已 success：`checks` 与 `m0-postgres`（[run 35124652364](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35124652364)）。待 #26 合并后 retarget 到 main。

## 下一步与交接

- PR #28 审查 thread 已逐条回复并 resolve；`workflow_dispatch` CI 在当前 HEAD 上跑。
- 用户只审核最终可合并 PR。合并及 retarget 仍待 #26 与用户审核。
- 待决（超出本子任务）：close/reopen、重绑定/合并/拆分、全局/目标 suspension 持久化接入、follow-up/correction 输入内容、持久化输入水位。


## 接手记录（2026-09-16）

执行者更换为 Codex。当前 HEAD：`0aed9fb4cce9081294bbf4f12d64d2ffa735543a`（`0aed9fb`）。PR #28 仍为 OPEN，base=`chore/durable-store-hardening`，head=`feature/m1-01-human-control`，当前 merge state 为 `CLEAN`；GraphQL reviewThreads 检查结果为全部已 resolve，未发现最后一次推送后新增的未解决机器人 thread。既有 workflow_dispatch CI（run `35124652364`）在当时 HEAD `ec567e3` 上 `checks` 与 `m0-postgres` 均 success；本次无代码变更，未重新触发 CI。待 #26 合并后按交接要求 rebase 到 `main`、retarget PR、确认自动 CI 并处理新增 review thread。
