# M1-01 剩余工作 1：runner 交接不发布 + 工作台实时进度接真实驱动

- 状态：进行中（PR #44 就绪待用户合并）
- 更新日期：2026-09-24
- 依据：[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)（用户 2026-09-24 决定，PR #43 分支）第 1 条；ROADMAP「M1-01 剩余工作」第 1 项；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Runtime and human control requirements」（显式 handoff 结果、人工控制优先）。功能 ID：M1-01。
- 工作区：分支 `feature/m1-01-handoff-runner`，worktree `../production-ops-agent-handoff-runner`（起点 `origin/main` `1f030fc`）。

## 目标与范围

A. 真实驱动器 `InvestigationRunner` 只发布「合格报告且无 handoff」的结论；交接时 Run 落为持久交接态、租约释放、事故保持开放，`control()` 仍接受 follow_up / correct / cancel，之后可 `new_run`；已提交的结论步骤保留可读。与工作台路径统一为同一条规则。

B. 真实驱动器发出工作台已消费的同一套事件（`run_claimed` / `step_committed` / `tool_committed` / `run_completed` / `run_handoff` / `run_claim_refused`），页面能看到真实 Run 的实时步骤并以正确终态事件收尾。

不做：deadline 清扫（ADR-0005 第 2 条，单列）、网页 8192、依赖变更、close/reopen。

## 前提与完成条件

- 前提：lab PostgreSQL（`scripts/m0/postgres_lab`，端口 55431）；DeepSeek 常设授权（AGENTS.md「费用与真实调用」），凭据经 `M0_ENV_FILE`。
- 完成条件：新增/修改的行为测试先红后绿；`make check` 通过；`M1_DURABLE_POSTGRES=1` 与 `M0_B_POSTGRES=1` 集成套件通过；至少一次经真实驱动器的有界真实 Run 附 ledger；独立审查完成且 P1/P2 已处置；PR 就绪待用户合并（功能 PR，用户门）。

## 必要上下文

- `opspilot/investigation/runner.py`：唯一同时认识 Worker / 断点 / loop 的组合层；本次收敛为 `_settle`（发布或 park）+ `_hand_off`。
- `opspilot/investigation/context.py::conclusion_publishable`：两条驱动路径共用的唯一发布判定。
- `opspilot/investigation/progress.py`（新）：事件载荷的唯一定义（从 `web/service.py` 的 `_EmittingCommitter` 迁出）；`ProgressLog` / `EvidenceProjection` 是 `opspilot.web` 的结构切片，投资层不反向 import web。
- `opspilot/persistence.py::hand_off`：与 `block()` 同栅栏的写路径，`running -> waiting_human`。
- `opspilot/web/service.py`：`run_once` 改用共享模块；`_handoff(park=...)` 区分「loop 交接」与「崩溃/栅栏拒绝」。
- 上游对照：HolmesGPT `5e983c17` `conversations_worker/worker.py::_terminal_to_status`（仅真实回答 COMPLETED；失败后仍可追问）。

## 决定与理由（Agent 自决，可逆）

1. **交接终态取 `waiting_human`**（`RUN_EXECUTION` 的 `running --awaiting_human_input--> waiting_human`）。理由：它是领域状态机中唯一「由人工回复重新排队（`human_reply -> queued`）、可暂停、可取消」的状态；`control()` 的放行名单 `_CONTROL_OPEN_RUN_STATES` 已含它，`claim()` 不接受它（轮询 worker 不会自行重跑），`publish()` 要求 `running`（迟到发布落为历史）。备选 `failed`（上游 FAILED 的字面对应）在本仓库是无出边终态且 `control()` 注释明确将其排除，改用它要同时改领域状态机与 `control()`，属合同层变更，留给用户裁定；`blocked` 只接受 cancel，不满足「追问保持可用」。
2. **崩溃不 park。** `Workbench.run_once` 捕获到的意外异常 / 存储拒绝仍只释放租约（既有 #33 行为，测试 `test_an_unexpected_investigator_error_releases_the_lease_and_hands_off` 要求可立即再 claim）；runner 对意外异常保持「像被杀进程一样」向上抛、租约到期后续跑（C3 §7）。只有 loop 明确给出的交接结果才 park。
3. **runner 的 `evidence` 投影须与 executor 注册证据的 sink 是同一个存储**（工作台路径同样如此），否则 `commit()` 对未注册证据抛 `UNKNOWN_IDENTITY`；写在 runner 文档串与测试 Harness 里。
4. 断点 2 重放的工具结果由 `RecoverySession` 提交，不经 committer，因此不发 `tool_committed`；页面从行快照读它们。记为已知偏差。

## 执行进展与证据

- 2026-09-24 实现提交 `707c7b9`（本分支）。
- 红/绿：新增单测 `tests/test_m1_web_workbench.py::test_a_handoff_parks_the_run_for_a_human_and_keeps_control_open`、`::test_cancel_and_new_run_are_accepted_after_a_handoff` 在实现前失败（`'running' != 'waiting_human'`），实现后通过；`tests/test_m1_web_workbench.py` 44 passed。
- `make check`（dirty 工作区，实现提交前）：ruff / format / mypy 通过，pytest `1927 passed, 197 skipped, 2 xfailed`。
- 修改的既有断言（按 ADR-0005 收紧，不是放宽）：`tests/integration/test_m1_loop_resume_postgres.py` 中 `test_an_exhausted_budget_after_restart_is_a_handoff_not_a_completion` 与 `test_a_rejected_plan_is_never_replayed_by_recovery` 原断言 handoff 也 `published`，现断言 `handed_off`、结论为空、run `waiting_human`。
- PG 集成（2026-09-24，在共用的 55431 实例上，未启停）：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q` → `160 passed, 38 skipped`（跳过为 `M0_B_RESTART` 等另行 opt-in 的用例）。新增 PG 用例：交接后 `waiting_human` + follow_up/correct 重新排队、cancel + new_run、断点 4 的交接行不发布、停放后迟到发布落 `late_result`、runner 事件顺序（完成 / 交接 / 拒绝 claim）、页面快照回读、证据投影错配交接。既有 `test_a_late_commit_from_a_fenced_attempt_is_history_not_transcript` 由 `unpublished` 改断 `control_denied`（被围栏的尝试既不能发布也不能停放它已不持有的 Run）。
- `make check`（HEAD `744a084`）：`1927 passed, 198 skipped, 2 xfailed`。
- 独立审查第 1 轮（全新上下文 Agent，拿 diff/ADR/约束，不拿结论）：P1-1 runner 路径尚无执行证据 → PG 套件已跑，见上；P2-1 `test_a_stale_publish_cannot_land_on_a_parked_run` 断言了存储没有的行为（随机 step_id 抛 `UNKNOWN_IDENTITY`）→ 改为先提交真实结论步骤再停放，并断言 `late_result` 行；P2-2 runner 的 `evidence` 投影与 executor sink 不一致时抛 `PersistenceError` 被记成 `control_denied` 且无终态事件、每租约周期重复 → `EmittingCommitter.commit_tool` 把投影失败转成 `StepStoreError("EVIDENCE_PROJECTION_FAILED")`，loop 以该原因交接（PG 用例覆盖），runner 字段加说明；P3-1 已停放/暂停的 Run 每次轮询都追加 `run_claim_refused` → runner 只在快照看起来可运行（事故非 paused/cancelled/completed 且 run 为 queued/running）时才发；P3-2 文档串改为「每个已结算的尝试恰一个终态事件，崩溃不发」；P3-3/P3-4/P3-5 记为观察（loop 级 `STORAGE_UNAVAILABLE` 会停放；claim 时版本不兼容只发 `run_claim_refused`；`publish()` 本身不强制发布规则）。第 2 轮复验（同一审查者，自行重跑 PG 套件 160 passed）：P1-1/P2-1/P2-2/P3-1/P3-2 全部确认已处置，5 处 `TOOL_PLAN_ON_FINAL` 与 `control_denied` 的断言调整判定为收紧而非放宽；新 P3：非 `UNKNOWN_IDENTITY` 的投影失败原因为复合串 `EVIDENCE_PROJECTION_FAILED:<code>`，与工具提交本身的 `STORAGE_UNAVAILABLE`（`_STORE_REFUSALS`，无结论行）不对称——记录，不改。审查时真实 Run 尚未落库，之后未改产品代码，只改了证据脚本的 fixture 覆盖。
- lab PostgreSQL：端口 55431 原被 `production-ops-agent-m1-human-control`（已合并分支）遗留实例占用；lead 会话确认无客户端后在该 worktree 用 `postgres_lab stop` 停止（数据目录保留，worktree 未删）。上面的首轮集成结果（160 passed）跑在该遗留实例上；随后本 worktree 用 `postgres_lab start` 起自己的实例（`tmp/m0-b/postgres`），重跑 `tests/integration`：第 1 次 `2 failed, 158 passed`（失败用例名未捕获，仅有汇总行），第 2、3 次均 `160 passed, 38 skipped`；记为未定位的偶发，待 CI 的 m0-postgres 作业对照。
- 真实 Run（2026-09-24，DeepSeek Flash，经 `InvestigationRunner` + 本 worktree PG）：4 次，合计 0.080403 CNY 上界。两次合格报告发布（`3a7dee13`、`325642ca`）、两次交接不发布（`b127b23a` 含 follow_up 后再 claim、`f987b4b6` 为脚本配置错误导致工具被拒）。结论与账本见 [`docs/evidence/m1-01-handoff-runner/run.md`](../evidence/m1-01-handoff-runner/run.md)。

## 下一步与交接

- PR [#44](https://github.com/kevinWangSheng/production-ops-agent/pull/44)：CI 成功后已做一次 `@codex review` 分诊，2 个 P2 均采纳并修复（`ba9a306` 断点 2 重放的工具结果发 `tool_committed` 并钉证据；`a811b9e` `conclusion_publishable` 对恢复行 fail-closed：版本为非空字符串、正文为字符串且摘要一致），thread 已回复并 resolve；lead 合入 main（`0b304a3`，含 #43 ADR）后机器人第 3 个 P2「hand_off 被拒仍发 run_handoff」：可复现（人工控制在尝试中推进代际），但 #33 的三条既有用例要求页面把被围栏尝试的结果记为历史，故采「区分事件」方案——`run_handoff` 载荷新增 `parked`（真实停放为 true，被拒/崩溃为 false），runner 被拒时仍不发；修复后 `make check` 1931 passed、PG 161 passed。集成套件在本实例上此后连续 6 次全过，首跑的 2 个失败未复现、用例名未捕获，不断言无害。用户门，待用户合并。
- 本 worktree 的 PostgreSQL 实例由本任务停止（`postgres_lab stop`，数据保留）；合并后按 AGENTS.md 清理 worktree。
- 用户已裁定（2026-09-24，经 lead 转达）：交接终态保持 `waiting_human`，不改为 `failed`。
