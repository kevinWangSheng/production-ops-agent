# M1-01 重启/不兼容处理

- 目标：实现 worker 侧按 PostgreSQL 业务记录重建 Run、续跑与租约 fencing；不兼容版本进入 `blocked(INCOMPATIBLE_STATE)`。
- 范围：`opspilot/recovery.py`、`opspilot/worker.py`、`opspilot/persistence.py` 及确定性测试；不改 UI。持久化最小改动：沿用原有事务与 fence，并在 `claim()` 的 SQL 写入边界把 `lease_until` 封顶到 Run `deadline`。
- 依据：SPEC.md、PRODUCT-CONSTRAINTS.md、C3 第 7 节、M0 recovery evidence、PR #28 diff（已检查 persistence.py，未重复其人工控制逻辑）。
- 完成条件：确定性测试、M1 PG 故障注入/重启覆盖、`make check`、独立审查、PR。
- 进展：新增不可变（深拷贝隔离）`RecoveryPlan` 与绑定 lease 的 `RecoverySession`；`Worker.resume()` 先从 PG 一致快照重建，再通过 `DurableStore.claim()` 获取新 epoch，待执行工具来自已提交业务记录。当前恢复执行器失败会 best-effort 释放同一 lease 并原样抛出原异常；`claim()` 使用数据库时钟将租约封顶到 Run `deadline`。
- 验证证据：`make setup` 成功；定向单元 3 passed；`make check` 通过（1054 passed, 84 skipped, 2 xfailed）；启动本 worktree 专属 PG 后 `M1_DURABLE_POSTGRES=1 ...test_m1_durable_state_postgres.py -q` 通过 31 passed；新增子进程真实 `Popen`/kill/restart 场景通过，确认 epoch 由 1 续为 2；PG 已由所属脚本停止。静态 `ruff`、`mypy` 与 `git diff --check` 通过。
- 独立审查：全新上下文第二轮已完成。审查指出恢复前代际重校验、畸形 tool call fail-closed、深层不可变三项问题；均已修复并补回归测试。原提交信息中的 `[ #M1-01 ]` 属拆分子任务标识，后续提交不再使用伪 feature ID，历史不改写。
- CI/审查收尾：修复 HEAD `2f31655` 的 workflow_dispatch run [35146834206](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35146834206) 已 success，`checks` 与 `m0-postgres` 均 success。三个机器人 thread 已逐条采纳修复并 resolve。当前 PR 仍以未合并的 #26 为 base；#26 合并后需 retarget `main`。本记录追加提交后需再以最终 HEAD 重跑 CI。
- 未运行真实模型调用；不声称产品验收或 feature passes 通过。
- 新一轮机器人 thread：lease epoch/owner/expiry 在工具 dispatch 前校验，并在代际不匹配时调用精确 lease relinquish；新增 PG fencing 回归。修复后本地 `ruff`、`mypy` 通过，M1 PG 集成 33 passed。待最终 HEAD CI。
- 最终收尾：HEAD `193539d` 的 workflow_dispatch run [35150133043](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35150133043) 中 `checks`、`m0-postgres` 均 success；本轮 2 个 thread 已逐条回复、修复并 resolve，当前 GraphQL 查询无 `isResolved=false` thread。PR 等待用户合并。

## 追加（2026-09-17）：`rebuild()["pending_tools"]` 恒空（ui-e2e 交接项 1/4 → storefix2 A）

- 缺陷：`rebuild()` 只从顶层 `response["tool_calls"]` 读工具计划，而 PR #29 的 loop 按 C3 §4/§7 提交完整模型响应
  `{"assistant": {...tool_calls}, finish_reason, usage, ...}`，于是产品 Run 的 `pending_tools` 恒空，
  `Worker.resume()/RecoverySession.execute_pending` 永远没有可续接的计划。
- 归属：当前 pending_tools 块（校验 `2f316553`、`operation_id`/`tool_call` `05b48721`）与 `Worker` 都在本 PR；
  读取路径与合同不符（#33 的 web 层已按 `assistant.tool_calls` 消费），故修读方、落本 PR；main 上的原始读取器被本 PR 的重写覆盖，不另开分支。
- 修复：`4273a38`（`_tool_plan()`：`assistant.tool_calls` 优先，否则顶层，兼容 M0 harness 形状）；
  `2fc706f`（审查 P3：`assistant` 非 dict 时返回非列表，`rebuild()` 校验 fail closed）。
- 测试（`tests/integration/test_m1_durable_state_postgres.py`）：`test_rebuild_lists_pending_tools_from_a_loop_shaped_step`
  （修复前 `[] == [(step, 1)]` 红）、`test_worker_resume_executes_only_the_pending_tools_of_a_loop_step`
  （修复前 `execute_pending` 返回 0 红；修复后只执行 ordinal 1、旧租约写入 `CONTROL_DENIED`）、
  `test_rebuild_rejects_a_malformed_loop_shaped_step`。
- 验证：`make check` → `All checks passed!` / `Success: no issues found in 15 source files` / `1056 passed, 90 skipped, 2 xfailed`；
  PG 定向 durable_state → `36 passed`。集成分支（main + 10 open PR）同一修复 `make check` → `1483 passed, 112 skipped, 2 xfailed`。
- 独立审查（全新上下文只读子代理）：修读方正确、Worker 测试非同义反复；P3「`assistant` 非 dict 静默为空」已采纳；
  另指出（范围外、先于本修复）`pending_tools[].operation_id` 为 `step:ordinal`，执行器 `ToolRequest.operation_id` 为 `step-t{index}`，
  接入真实执行器前需统一；以及当前产品 web 路径未使用 `Worker.resume`，本修复现阶段的可见效果是事故页「未提交工具操作数」与恢复接口本身。

## 追加（2026-09-17，历史记录）：租约续期接入 `RecoverySession`（lease-wire.md §7b）

- 依据：`/private/tmp/claude-501/-Users/shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/lease.md`（历史工作区路径，当前不作为验证入口）。该阶段记录的是 PR #35 尚未合并时的接线与临时合并验证；随后 PR #35 已合并到 `main`，因此本段关于「缺少 `renew_lease`」「行为不变」「需先合并 #35」的判断均为历史状态，不代表当前 tree。
- 历史做法：`RecoverySession.execute_pending` 在每次 `execute(item)` 之后、`commit_tool()` 之前调用 `_renew()`；当时通过可选能力兼容尚未合并的 `DurableStore.renew_lease`。历史单测覆盖无能力替身、`execute → renew → commit` 顺序和续租拒绝；历史 PG 临时合并验证覆盖短租约续期及人工 cancel 竞态。
- 当前树校准：`origin/main` 已包含 PR #35（merge commit `76293b0`，实现提交 `91451d9`），本分支基于该 main 合并结果，`opspilot.persistence.DurableStore.renew_lease` 已存在并由 `_renew()` 直接启用。`tests/integration/test_m1_lease_renewal_wiring_postgres.py` 仅在 `M1_DURABLE_POSTGRES!=1` 或能力确实不存在时跳过；因此在显式 PG 环境中它是可运行的当前验证，不应再写成恒跳过或未接线。
- 当前验证边界：本次修复会重新运行 wiring PG 测试并记录实际通过/跳过状态；没有真实模型调用、产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 P1——claim 后刷新恢复计划

- 依据：PR #30 最新 Code Review thread 指出，`Worker.resume()` 原先在 `claim()` 前生成 `RecoveryPlan`，claim 后只校验代际；同代际的另一 worker 可能先提交 pending tool 后失去 lease，导致旧计划重复外部查询或漏掉新状态。
- 修复：claim 成功后再次调用 `DurableStore.rebuild()`/`recover()`，再校验刷新计划的 `candidate`、`run_id` 与 lease 的 `control_generation`；任一不符即精确 `abandon()` 并返回 `CONTROL_DENIED`。通过 `RecoverySession` 绑定刷新后的计划，保留已有 fence 语义。
- 回归：`tests/test_worker_recovery.py::test_resume_refreshes_pending_plan_after_claim` 使用确定性双快照替身，断言 `rebuild → claim → rebuild` 顺序、claim 前后计划不同，以及 session 使用新 pending tool/run state。
- 工作区/版本：分支 `feature/m1-01-restart-recovery`，基线 `origin/main=a1eeadf88bc0e8c93e3f0ee223fae5bc6a339091`，修复开始前 HEAD `f9f936c4de61f2b0279b000a8f06085445254202`，修复代码提交 HEAD `e24b7325f304846499a7ad56aa54d8597e91c708`。
- 验证：定向 worker 单测 `10 passed`；`make check`（含 ruff、format、mypy）`1061 passed, 111 skipped, 2 xfailed`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q` `38 passed`。55431 已被其他 worktree 的 PostgreSQL 进程占用，本 worktree 未停止或接管该进程；定向 PG 测试在该现有实例上通过。无真实模型调用、无产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 最新 review findings 收尾

- **P1：迟到结果不得参与模型计划校验（`4057312360`）**：采纳并修复。`DurableStore.rebuild()` 对 `status='late_result'` 的不可变历史行跳过 `_tool_plan()` 校验；模型步骤仍保持原有 fail-closed 校验。新增 `test_rebuild_ignores_late_result_payload_shape`，覆盖 `{"tool_calls": 0}` 这类合法任意结果，避免后续重建被污染历史阻断。
- **P2：claim 后刷新失败必须释放租约（`4057312362`）**：采纳并修复。`Worker.resume()` 的第二次 `recover()` 抛出 `PersistenceError` 时，对同一 owner/epoch/generation 执行 best-effort `abandon()`，原始刷新错误原样抛出；释放失败不会覆盖原始错误。新增两个确定性单测，分别覆盖释放成功与释放自身失败。
- **P2：恢复被位移的人控 PG 覆盖（`4057312364`）**：采纳并修复。恢复原先被 worker 测试替换的 9 个 `DurableStore` 集成测试，覆盖取消后新 Run、迟到结果历史/命名空间、旧代际与幂等、并发 follow-up/cancel 等控制语义；另新增 1 个 late-result rebuild 回归。
- 工作区/验证：分支 `feature/m1-01-restart-recovery`，55431 由其他 worktree 持有，未停止或接管。`ruff check`、`ruff format --check`、`mypy` 全绿；worker 定向单测 `12 passed`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q` `48 passed`；最终 `make check` `1061 passed, 111 skipped, 2 xfailed`。无真实模型调用、无产品验收或 feature passes 结论。

## 追加（2026-09-20）：最终 review findings 收尾

- P1 executor 异常：`RecoverySession.execute_pending()` 在工具 callback 抛异常时 best-effort `abandon()` 当前 owner/epoch/generation lease，原始异常保持不变，释放失败不覆盖它；新增两条 worker 单测。
- P2 deadline：`DurableStore.claim()` 的初始 `lease_until` 使用数据库时钟与 Run deadline 的 `LEAST` 封顶，续租与初始领取保持同一绝对期限边界；更新 PG deadline 回归。
- P2 记录同步：当前 main 已含 PR #35，续租 wiring 在本分支可用；旧的“#35 未合并/能力缺失”文字保留为历史背景，不再作为当前状态。
- 本轮最终验证：worker 单测 `12 passed`；M1 DurableStore PG 定向 `48 passed`；全量 `make check` `1061 passed, 111 skipped, 2 xfailed`。55431 由其他 worktree 持有，未停止或接管。

## 追加（2026-09-20）：PR #30 P1/P2 最新 findings

- **P1：恢复工具 executor 异常的 lease 清理（`4057446902`）**：采纳并修复。`RecoverySession.execute_pending()` 只捕获 executor callback 的退出路径，best-effort 调用同一 `Lease` 的 `store.abandon()`，随后用裸 `raise` 保留原异常；abandon 失败也不覆盖原异常。未把外部异常伪装成成功或吞掉。新增 worker 单测覆盖释放成功及释放失败仍保留原异常，且断言没有 renew/commit。
- **P2：初始 claim 的 deadline 封顶（`4057446908`）**：采纳并修复。`DurableStore.claim()` 的持久 SQL 使用 PostgreSQL `LEAST(clock_timestamp()+lease_interval, deadline)`，避免应用时钟误差和 deadline 前的 420 秒超额租约；更新 PG 回归断言 claim 直接得到 `lease_until == deadline`，并保留 deadline 到期后的 `DEADLINE_EXCEEDED` 语义。
- **P2：续租任务记录状态（`4057446911`）**：采纳并修复。明确 `origin/main` 已含 PR #35（`76293b0`，实现 `91451d9`），当前 `renew_lease` 已存在且 wiring PG 测试在显式 `M1_DURABLE_POSTGRES=1` 下可运行；历史临时合并与恒跳过描述已标注为历史，不伪称当前 wiring 已运行。本次验证实际结果以命令输出为准。
- 本轮没有真实模型调用、产品验收或 feature passes 结论；55431 若由其他 worktree 占用，不停止或接管其 PostgreSQL 进程。
- 最终本地验证（当前工作树，2026-09-20）：`PYTHONPATH=. .venv/bin/pytest -q tests/test_worker_recovery.py` 为 `14 passed`；显式复用已由其他 worktree 持有的 55431 PostgreSQL（未停止/接管），`M1_DURABLE_POSTGRES=1 PYTHONPATH=. .venv/bin/pytest -q tests/integration/test_m1_durable_state_postgres.py tests/integration/test_m1_lease_renewal_postgres.py tests/integration/test_m1_lease_renewal_wiring_postgres.py` 为 `67 passed`，wiring 未跳过；`make check` 为 `1065 passed, 121 skipped, 2 xfailed`，其中默认未设置 PG opt-in，wiring 的 2 项按显式 PG 条件跳过。`ruff check`、`ruff format --check`、`mypy` 均由 `make check` 覆盖并通过。

## 追加（2026-09-20）：PR #30 最新 P2 持久化失败与工具结果校验

- **P2：executor 后续持久化失败释放租约（`4057494822`）**：采纳并修复。`RecoverySession.execute_pending()` 将续租和 `commit_tool()` 纳入同一 best-effort `abandon()` 清理路径；释放失败不会遮蔽原始 `TIMEOUT`/`RETRY`/`STORAGE_UNAVAILABLE` 等持久化错误。新增确定性 worker 回归，分别覆盖续租失败、提交失败及释放失败仍保留原异常。
- **P2：live step 的畸形 `tool_results` fail closed（`4057494824`）**：采纳并修复。`DurableStore.rebuild()` 在构造 `pending_tools` 前验证结果为 list、每项为 mapping、ordinal 为非 bool 的合法 int（范围内且无重复），并拒绝缺失/非 mapping result；`{}`、scalar、非 mapping 项统一抛出 `PersistenceError("INCONSISTENT_STATE")`。新增 PG 回归覆盖这些损坏形状，并保留现有工具计划/代际过滤语义。
- 当前验证：worker 定向 `16 passed`；复用其他 worktree 持有的 55431 PostgreSQL（未停止/接管），M1 durable + lease renewal + wiring 定向 `68 passed`；`make check` 为 `1067 passed, 122 skipped, 2 xfailed`，`ruff`、格式、`mypy` 均通过。无真实模型调用、产品验收或 feature passes 结论。

## 追加（2026-09-20）：当前 HEAD 新增 P2 findings

- **P2：续租/提交失败后的 lease 清理（`4057494822`）**：采纳并修复。executor 成功但 `_renew()` 或 `commit_tool()` 抛出持久化/控制异常时，`execute_pending()` 对同一 lease best-effort `abandon()`，原始异常原样抛出；新增 renew rejection 与 commit failure 回归断言，释放失败不覆盖原始错误。
- **P2：持久 `tool_results` fail-closed（`4057494824`）**：采纳并修复。`rebuild()` 对活模型步骤要求 `tool_results` 是列表，每项是带唯一、非负、范围内整数 ordinal 和 mapping result 的记录；malformed 值返回 `INCONSISTENT_STATE`，不把 falsey 值当空列表而重放外部查询。新增 PG 回归覆盖非列表、非法 ordinal、缺失/错误 result。
- 最新本地验证：worker 定向 `15 passed`；55431 复用的 M1 durable/renewal/wiring `68 passed`；`make check` `1066 passed, 122 skipped, 2 xfailed`；ruff/format/mypy 全绿。无真实模型调用、产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 最新两条 findings——恢复执行的在途租约与待办复核

- 起点：PR #30 在 HEAD `0c0325b` 上 CI 两项均 success、分支与 `main` 同步，`mergeStateStatus=BLOCKED` 的唯一原因是 `main` 分支保护的
  `required_conversation_resolution`，即下面两条未处理 thread（`required_approving_review_count` 为 0）。

- **P1：每个工具 dispatch 前续租（`4057662960`）**：采纳并修复。原实现只在 `execute()` 之后、`commit_tool()` 之前续租，
  进入时剩余租约短于 callback 时（如 `resume(lease_seconds=2)` 而工具耗时 2.5s），租约在外部调用在途期间就过期，别的 worker 可以
  claim 同一个 Run 并重复同一操作。`RecoverySession.execute_pending()` 现在在 dispatch 前也调用 `_renew()`；
  `renew_lease` 用 `LEAST(GREATEST(lease_until, now+extend), deadline)`（`persistence.py:482`）从不缩短租约，因此工具执行期间
  实际由 `renew_seconds` 覆盖，且不越过 Run deadline。
  - 部分拒绝并说明依据：同一条意见还要求「强制工具超时或心跳」。`execute` 是同步不透明 callable，本层无法取消已进入的调用，
    `join(timeout)` 只会让 session 提前放弃而 callback 仍在后台查询，重复查询风险不降反升。墙钟终止按 C3 §8 属于工具网关
    （`feature/m1-01-tool-executor` 的 `opspilot/tools/registry.py:63` `MAX_REQUEST_TIMEOUT_SECONDS = 30.0`，远小于 420s）。
    已知限制照实记录：callback 超过 `renew_seconds` 时不保证外部查询 exactly-once；此时本 session 的结果走 `_late_result`
    保留为历史行而非静默丢失，且工具为只读、受查询预算约束。
- **P2：跳过已不在持久待办集合中的工具（`4057662963`）**：采纳并修复。`RecoveryPlan` 是快照，提交后不变；对同一个仍然有效的
  session 再次调用 `execute_pending()` 会重复外部查询，之后才由 `commit_tool()` 按 ordinal 去重——持久状态正确，但真实花掉了
  查询范围/成本/速率预算。新增 `_still_pending()`：每个 item 用 `_assert_current()` 取回的当前快照按 `(step_id, ordinal)` 复核，
  已提交的跳过（`continue`）而不报错，因为人工控制变化已由 `_assert_current()` 抛 `CONTROL_DENIED` 拦截。
- **独立审查（全新上下文只读子代理，未参与实现）发现并处置**：
  - F1 文案：原注释/测试名称把跳过场景写成「另一个 worker 在同代际提交」。核对 `_lease_revoked`（`persistence.py:157-163`）后确认
    不可达——`commit_tool` 以 owner+epoch 设栅栏，别的 worker 换了 epoch 只会写 `_late_result`；本方租约若已失效则 `lease_current`
    先失败。已改写为「同一租约的先前一轮或并行调用者」，PG 测试同步改名。
  - F2 代码（本次一并修复）：`_assert_current()` 内 `rebuild()` 抛瞬时 `PersistenceError` 时不释放租约，会把一次短暂读失败变成
    最长 `renew_seconds` 的恢复停摆，与 `Worker.resume()` claim 后刷新失败的处理不对称。已按同一 best-effort `abandon()` 模式补上，
    原始读错误原样抛出。栅栏拒绝（`lease_current` 为 False 或代际/run 不匹配）仍不 abandon，并已加注释说明依据：
    `abandon()` 的 WHERE 要求 owner+epoch+generation 全匹配，这些情形要么不命中，要么只清掉一个 `claim()` 本就视为可领取的过期租约。
  - F3 文案：`DEFAULT_LEASE_SECONDS` 注释与默认值一致性测试的归因不准——「工具不会在更短租约下执行」现在由 pre-dispatch 续租本身
    保证，与两个默认值是否相等无关。已改写为：相等的实际意义是覆盖 claim→首次续租之间的窗口，以及无 `renew_lease` 的最小替身。
- 红绿对照（按约定用 `git diff` 补丁 + `git apply -R`，未使用 checkout/stash）：单独撤销 `opspilot/worker.py` 后
  6 个单测 + 3 个 PG 测试红；其中 pre-dispatch 续租的 PG 测试红因正是竞争 `claim()` 未抛 `LEASE_ACTIVE`（"DID NOT RAISE"），
  即改动前另一 worker 确实能在工具在途时夺走 Run。单独移除 F2 的 try/except 后 `test_a_transient_rebuild_failure_releases_the_live_lease` 红。
- 新增测试：单测 `tests/test_worker_recovery.py` 共 `21 passed`（含 dispatch 前后两次续租顺序、续租在 dispatch 前被拒时工具不执行、
  第二轮不重复已提交工具、只执行仍未完成的 ordinal、瞬时 rebuild 失败释放租约、栅栏拒绝不释放）；PG 新增 3 条
  （`test_the_lease_is_renewed_before_each_tool_is_dispatched`、`test_a_second_execute_pending_pass_does_not_repeat_a_committed_query`、
  `test_resume_skips_a_call_already_committed_under_this_lease`）。
- 验证：`make check` → `1072 passed, 126 skipped, 2 xfailed`，`ruff check`/`ruff format --check`/`mypy` 全绿；
  `M1_DURABLE_POSTGRES=1` 下 durable_state + lease_renewal + wiring → `72 passed`。
  复用其他 worktree 持有的 55431 PostgreSQL，未停止或接管该进程。
- 边界：无真实模型调用，无产品验收或 feature passes 结论；本次只改恢复执行的租约时机与待办复核，不触碰冻结上限与验收步骤。

## 追加（2026-09-20）：PR #30 第二轮 3 条 P2（HEAD `9cfed14` 上的新 review）

`9cfed14` 的 CI 两项均 success，但机器人在该 HEAD 上开出 3 条新 P2，`mergeStateStatus` 仍因
`required_conversation_resolution` 为 BLOCKED。三条均采纳并修复：

- **P2：同一 session 的并发 dispatch 需串行化（`4057730348`）**：采纳并修复。上一轮的待办复核在「复核 → 续租 → 回调」之间是多步非原子操作，
  两个线程共用同一个 `RecoverySession` 时可能都判定同一 ordinal 仍 pending 并各发一次外部查询；`commit_tool` 事后去重救不回已花掉的查询成本与速率。
  `RecoverySession` 新增 `_dispatch_lock`（`field(default_factory=threading.Lock, repr=False, compare=False)`），`execute_pending()` 整轮持锁，
  实际执行移入 `_execute_pending_locked()`。语义依据 C3 §6「一个租约就是一次执行尝试」。
  说明边界：锁只覆盖**进程内**并发；跨 worker 的重复本来就由 `commit_tool` 的 owner+epoch 栅栏挡住，锁补的正是该栅栏看不见的那一段。
  机器人建议的另一方案「durably claim each ordinal」需要新增每 ordinal 的持久认领行，属 schema 变更，超出本 PR 范围，未采用。
- **P2：`publish()` 失败需释放租约（`4057730354`）**：采纳并修复。`RecoverySession.publish()` 原先直接透传，`store.publish()` 抛
  `TIMEOUT`/`RETRY`/`STORAGE_UNAVAILABLE` 时租约会留到期（最长 `renew_seconds` 或 Run deadline），替补 worker 拿到 `LEASE_ACTIVE`。
  现按本模块既有模式 best-effort `abandon()` 后原样抛出原异常。注意租约被撤销时 `store.publish()` 是**返回 False 而不是抛异常**，
  因此该路径不会误释放；已加回归测试固定这一点。全仓没有任何调用方按 `PersistenceError` 的错误码分支，故与 `execute_pending` 一致地对所有
  `PersistenceError` 释放，不引入新的错误码判别。
- **P2：非 mapping 的持久模型响应需 fail closed（`4057730358`）**：采纳并修复。`_tool_plan()` 对非 dict 的 `response` 原先返回 `[]`，
  于是 JSON scalar/list/null 会被读成「该步骤没有工具」的合法空计划，绕过 `rebuild()` 的既有 fail-closed 校验。改为返回 `None`
  这个既有的非 list 哨兵（与 `assistant` 损坏时同一处置），由 `rebuild()` 拒为 `INCONSISTENT_STATE`。
  `commit_tool()` 也调用 `_tool_plan()`，改动后该路径仍是 `UNKNOWN_IDENTITY`，结果不变。

- 红绿对照（补丁 + `git apply -R`/定点移除，未用 checkout/stash）：移除串行化与 publish 清理后
  `test_concurrent_passes_do_not_dispatch_the_same_call_twice`、`test_publish_failure_releases_the_lease_and_preserves_the_error` 红；
  还原 `_tool_plan` 后 `test_rebuild_rejects_a_malformed_loop_shaped_step` 红因 `DID NOT RAISE PersistenceError`。
- 新增测试：并发双线程 dispatch（用 `Event` 让第二个调用者确实在第一个回调在途时进入）、publish 失败释放且不遮蔽原异常、
  publish 被拒（返回 False）不释放；PG `test_rebuild_rejects_a_malformed_loop_shaped_step` 扩展 `"oops"`、`[{...}]`、`7` 三种形状。
- 验证：`make check` → `1075 passed, 126 skipped, 2 xfailed`，`ruff check`/`ruff format --check`/`mypy` 全绿；
  `M1_DURABLE_POSTGRES=1` 下 durable_state + lease_renewal + wiring → `72 passed`；worker 定向 `24 passed`。
  复用其他 worktree 持有的 55431 PostgreSQL，未停止或接管。
- 边界不变：无真实模型调用，无产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 第三轮 2 条 P2（HEAD `fa6683f`）

`fa6683f` 的 CI 两项均 success，机器人在该 HEAD 上开出 2 条新 P2，均已采纳并修复，其中一条的**实施位置**与建议不同。

- **P2：恢复调用应使用规范 operation id（`4057748291`）**：采纳（修复位置调整）。
  缺陷属实：`rebuild()` 手写 `f"{step_id}:{ordinal}"`，而仓库规范来源 `opspilot/domain/runs.py:272` 的
  `tool_operation_id()` 产出 `f"{step}#{tool_index}"`。按规范 id 去重的网关或证据库会把重启重放当成新操作，
  重复外部查询并拆散证据历史。该不一致先于本 PR 存在，之前作为「已知项」记在 PR 与任务记录里，本轮予以消除。
  **不能按字面建议在 `opspilot/persistence.py` 里调用该 helper**：持久化层是否建立在 domain 之上，是本仓库
  **尚未做出的架构决定**，由 `tests/test_architecture.py::test_persistence_builds_on_domain` 以
  `xfail(strict=True)` 记录。实测在 `persistence.py` 加 `from opspilot.domain import tool_operation_id` 后，
  该测试变成 `XPASS(strict)`，`make check` 直接红——即按字面修会以「顺手改一行」的方式替一个开放架构问题作出决定。
  **实际做法**：`rebuild()` 不再产出 `operation_id`（附注释说明原因与去处）；由 `opspilot/recovery.py` 的
  `rebuild_plan()` 经新增 `_identified()` 用 `tool_operation_id()` 打戳。`recovery` 依赖 `domain` 不受上述架构欠债约束。
  这样格式只有一个定义，且架构决定仍然保持开放（两条 xfail 仍为 xfail）。
- **P2：版本复核需与被解码的快照绑定（`4057748294`）**：采纳并修复。`recovery_metadata()` 与随后的 `recover()`
  是两个事务；其间若发生 cancel -> new_run 且新 Run 的步骤已由另一版本的 worker 写入新 schema，旧校验器会先抛
  `INCONSISTENT_STATE`，`claim()` 来不及持久化 `blocked`/`INCOMPATIBLE_STATE` 交接。
  抽出 `Worker._gate_versions()`，解码失败时再跑一次该门：当前 Run 已不兼容则走 blocked 交接，否则原异常原样抛出。
  **残余与诚实边界**：这是「复核」而非机器人建议的「与同一快照原子绑定」。真正的原子绑定需要把版本判定下沉进
  `rebuild()` 的快照事务，即改动被多处调用的持久化 API，超出本 PR 范围；本实现在该窄窗口后收敛（重试时正常门先跑），
  已加回归固定「兼容 Run 的解码失败不得被改写成 INCOMPATIBLE_STATE」。

- 红绿对照：单独撤销本轮 `opspilot/` 改动后 `test_pending_tool_carries_the_canonical_operation_id`、
  `test_resume_blocks_a_run_replaced_by_an_incompatible_one_mid_decode` 红；还原后全绿。
- 受影响的既有断言已更新：2 条 PG 断言改为断言 `rebuild()` 不再产出 `operation_id`；单测 fixture 补 `step_id`
  （规范 id 由它派生）。
- 验证：`make check` → `1077 passed, 126 skipped, 2 xfailed`（两条架构 xfail 仍为 xfail，欠债未被本次顺手改掉），
  `ruff check`/`ruff format --check`/`mypy` 全绿；`M1_DURABLE_POSTGRES=1` 下 durable_state + lease_renewal + wiring → `72 passed`；
  worker 定向 `26 passed`。复用其他 worktree 持有的 55431 PostgreSQL，未停止或接管。
- 边界不变：无真实模型调用，无产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 第四轮 1 条 P2——栅栏读失败的租约清理，并把该缺陷类结构化

- **P2：`lease_current()` 读失败需释放租约（`4057763665`）**：采纳并修复。`_assert_current()` 原先只把 `rebuild()`
  包进 best-effort `abandon()`，漏了它上一行的 `lease_current()`。该调用**抛异常**（TIMEOUT/STORAGE_UNAVAILABLE）
  与**返回 False**语义相反：前者是读失败，本会话可能仍持有活跃租约，直接上抛会让替补 worker 在存储恢复后仍拿到
  `LEASE_ACTIVE` 直到租约到期；后者是栅栏拒绝，租约本就不在手上，无需释放。现在两次读各自包在 try 内，
  拒绝路径保持不释放。
- **止损：把这一缺陷类变成结构断言。** executor 失败、续租/提交失败、`publish()`、`rebuild()`、`lease_current()`
  是**分四轮**被逐个发现的同一类缺陷（持有租约期间某个 store 调用抛错 → 租约留到期 → 恢复停摆）。继续逐点补只会
  再来一轮，因此新增 `tests/test_architecture.py::test_recovery_session_releases_its_lease_when_a_store_call_fails`：
  用 AST 检查 `RecoverySession` 内所有直接的 `self.store.<方法>` 调用是否都位于「except 分支调用
  `_abandon_best_effort()`」的 try 内，`_abandon_best_effort` 自身除外。撤销本轮修复后该结构测试与行为测试同时红。
  覆盖边界已写入 docstring：只覆盖直接 store 调用；`_renew()` 经 `getattr` 间接调用 `renew_lease`，由其调用点的 try 覆盖。
  该测试放在 `tests/test_architecture.py`，符合该文件「每条断言对应一个已经发生过的真实缺陷」的既有定位。
- 当前审计结论（本轮逐条核对 `worker.py` 全部 store 调用）：`lease_current`、`rebuild`、`commit_tool`、`publish`
  均在释放路径内；`abandon` 自身是终点；`Worker.claim`/首次 `recover()`/`_gate_versions()` 发生在取得租约之前，
  无租约可释放；`resume()` claim 后的第二次 `recover()` 已有精确 `abandon()`。持有租约期间的 store 调用已无遗漏。
- 验证：`make check` → `1079 passed, 126 skipped, 2 xfailed`，`ruff check`/`ruff format --check`/`mypy` 全绿；
  `M1_DURABLE_POSTGRES=1` 下 durable_state + lease_renewal + wiring → `72 passed`；worker 定向 `27 passed`。
  复用其他 worktree 持有的 55431 PostgreSQL，未停止或接管。
- 边界不变：无真实模型调用，无产品验收或 feature passes 结论。

## 追加（2026-09-20）：第二次独立审查（覆盖 `9cfed14..8616ea7`）与两处更正

第二位独立审查者（全新上下文、只读、未参与实现）审查了第二至四轮的全部改动，结论为**无阻塞**，
三个修复方向均正确、未引入新缺陷；另有 2 条 P2 属「陈述与代码不符」，已按下述更正。

- **更正 1（我此前的公开理由不准确）**：第三轮我在 PR thread 与任务记录里写「在 `persistence.py` 加
  `from opspilot.domain import tool_operation_id` 会让 `test_persistence_builds_on_domain` 变成
  `XPASS(strict)`，`make check` 直接红」。该结论**只对绝对 import 成立**。
  `tests/test_architecture.py` 的 `_imported_modules()` 只收集 `node.module`：
  `from opspilot.domain import x` → `'opspilot.domain'`（命中）；
  `from .domain import x` → `'domain'`、`level=1`（**不命中**）。
  而 `opspilot/` 内部一律使用相对 import。已实测确认两种形式的差异。
  因此正确的理由是：**持久化层是否建立在 domain 之上是未决架构决定，不应由一行顺手 import 替它作出**——
  而不是「测试会红」。修复位置（放在 `recovery.py`）不变，审查者也认可该位置。
  代码注释已相应改写为「reaching for the helper here would settle that decision in passing」。
- **更正 2（`publish()` 注释与实际错误面不符）**：原注释称该清理「only covers real publication failures」。
  实际上 `store.publish()` 还会抛确定性的 `UNKNOWN_IDENTITY` 与 `FINAL_STEP_REQUIRED`（后者租约仍有效，
  只是结论与已提交步骤不匹配），改动后这两种也会 `abandon()`。注释已改为如实说明：
  对所有 `PersistenceError` 释放（本包任何地方都不按错误码分支），此后该 session 作废，
  调用方需重新 `resume()` 取得新 epoch；并标注「`FINAL_STEP_REQUIRED` 是否应保留租约，待投资循环真正调用本方法时再定」。
  行为未改：审查者指出 `RecoverySession.publish` 目前无产品调用方，且 `execute_pending` 对 `commit_tool` 的
  `UNKNOWN_IDENTITY` 早就是同样处置，与既有模式一致。

- 审查者记录的后续项（先于本 PR、超出本次范围，**未修**）：
  1. `tests/test_architecture.py::_imported_modules()` 不收集相对 import，使该架构欠债守卫存在漏报；
     修好后 `persistence.py` 仍不依赖 domain，故不影响当前结论。
  2. 结构测试 `test_recovery_session_releases_its_lease_when_a_store_call_fails` 不校验 handler 捕获的异常类型，
     也不强制 `_renew()` 必须在受保护 try 内（当前代码满足，但将来可能漏报）。
  3. `operation_id` 在本分支无消费者；执行器分支的 `ToolRequest.operation_id` 若未改用 `tool_operation_id()`，
     接入时须核对，否则不一致只是从 persistence 挪到了接缝处。**未确认**。
  4. `copy.deepcopy(RecoverySession)` 会因 `threading.Lock` 不可 pickle 而 TypeError（当前无调用方）。
- 审查者复核证据：`make check` `1079 passed, 126 skipped, 2 xfailed`（两条架构 xfail 仍为 XFAIL）；
  PG durable_state + lease_renewal + wiring `72 passed`；并发测试连跑 5 次全过；ruff/format/mypy 全绿。

- **更正 3（同一类：注释比代码乐观）**：`Worker.resume()` 里版本 re-gate 的注释原写
  「Any other failure ... still surfaces as itself」。实际上 re-gate **自身**失败时（`recovery_metadata()`
  再抛 TIMEOUT，或 `claim()` 抛 `LEASE_ACTIVE`），它的异常会替换原解码错误，原错误仅保留在 `__context__`。
  注释已改为如实说明这一点，并指出下次重试会先跑正常版本门，因此该行为收敛而不会锁死。
  仅改注释，行为未变。

## 追加（2026-09-20）：第六轮 1 条 P2——claim 后刷新失败也要重跑版本门

- **P2：post-claim refresh 失败时重跑版本门（`4057830925`）**：采纳并修复。第三轮我只给 claim **之前**的
  `recover()` 加了版本 re-gate，claim **之后**的第二次 `recover()` 用的是同一段竞态逻辑却没有同样处理——
  这是我自己留下的不对称，不是新竞态。并发 cancel -> new_run 若把已 claim 的兼容 Run 换成不兼容版本的 Run，
  旧校验器拒绝其 payload 后，原实现只 abandon 旧租约并上抛 `INCONSISTENT_STATE`，新 Run 仍停在 queued，
  正常的升级交接被错误地走进「损坏状态」这条不可重试路径。
- 修复：在释放旧租约**之后**调用 `self._gate_versions()`，不兼容则走 blocked/`INCOMPATIBLE_STATE` 交接；
  兼容 Run 的解码错误仍原样上抛。顺序重要：先 abandon 旧 Run 上的陈旧租约，再对新 Run 做版本门。
  注释与第一处 re-gate 保持同一口径，并同样如实写明「re-gate 自身失败时其异常会替换原错误（原错误保留在
  `__context__`），下次重试先跑正常门因而收敛」。
- 回归：`test_resume_blocks_an_incompatible_run_that_replaces_the_claimed_one`（断言先释放旧租约、
  再对新 run_id claim、抛 `INCOMPATIBLE_STATE`）、`test_a_post_claim_decode_failure_on_a_compatible_run_surfaces_as_itself`
  （兼容 Run 的解码失败必须原样上抛且不得触发第二次 claim）。撤销本轮改动后前者红。
- 验证：`make check` → `1081 passed, 126 skipped, 2 xfailed`，ruff/format/mypy 全绿；
  `M1_DURABLE_POSTGRES=1` 下 durable_state + lease_renewal + wiring → `72 passed`；worker 定向 `29 passed`。
  复用其他 worktree 持有的 55431 PostgreSQL，未停止或接管。
- 本轮 code review 已覆盖当时的 HEAD `e6fefb8`（codex 19:20:24 的 review），不再是「审查只覆盖旧提交」的状态；
  本次修复后 HEAD 变更，仍需新一轮覆盖当前 HEAD 的复审。
