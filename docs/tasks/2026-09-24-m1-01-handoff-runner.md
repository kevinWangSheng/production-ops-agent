# M1-01 剩余工作 1：runner 交接不发布 + 工作台实时进度接真实驱动

- 状态：进行中
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
- PG 集成、真实 Run、独立审查：待执行（见下一步）。

## 下一步与交接

- lab PostgreSQL 端口 55431 当前被 `production-ops-agent-m1-human-control`（已合并分支）的实例占用（pid 84545，0 个客户端）；已向 lead 请示是停掉该遗留实例、还是直接在其上跑集成套件。得到答复后：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`。
- 真实 Run：`M0_ENV_FILE=/Users/shenghuikevin/dev/AI/production-ops-agent/.env .venv/bin/python scripts/m1_live_runner.py`（一次正常上限 2）与 `--model-requests 1 --follow-up`（强制预算交接 + 追问后再 claim），证据写入 `docs/evidence/m1-01-handoff-runner/live-runs/<run_id>/`。
- 独立审查：全新上下文审查者，给 ADR、约束、diff 与原始证据。
- 用户裁定点：交接终态用 `waiting_human` 还是新增/改造 `failed`（见上「决定 1」）。
