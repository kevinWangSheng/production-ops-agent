# M1-01 剩余工作 2：过期 Run 由清扫收尾（超时清扫）

- 状态：进行中（PR 待用户合并）
- 更新日期：2026-09-24
- 依据：[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md) 第 2 条；[ADR-0003](../adr/0003-business-state-recovery-authority.md)（行是权威、事件是投影）；ROADMAP「M1-01 剩余工作」第 2 项；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Runtime and human control requirements」（显式 handoff 结果、迟到完成不得抹掉更新的人工决定）。功能 ID：M1-01。上游对照：HolmesGPT `5e983c17` `conversations_worker/models.py` `ConversationStatus.TIMEOUT`（pg_cron 清扫 + 停机钩子）。
- 工作区：分支 `feature/m1-01-deadline-sweep`，worktree `../production-ops-agent-deadline-sweep`（起点 `origin/main` `2a918ab`，含 #44）。

## 目标与范围

Run 过 deadline 后，租约栅栏拒绝 worker 的一切写入，没有写入者能把它落为终态，行永远停在 `running`、页面永远「调查中」。本次按数据库时钟找出 `state='running'` 且 `deadline` 已过的 Run，在事务内行锁下复核后，用 #44 `hand_off` 同一条写语句停放为 `waiting_human`（owner/lease 清空、事故开放、结论为空），原因 `DEADLINE_EXCEEDED`，并经既有进度助手幂等地发出 `run_handoff`（`parked: true`）。不覆盖先落下的人工决定；停放后 follow_up / correct / cancel / new_run 与任何 `waiting_human` Run 一样可用。

不做：投影补写修复（#44 机器人发现，后续项）、网页 8192、依赖变更、重构、新终态。

## 决定与理由（Agent 自决，可逆）

1. **触发点：runner 的 `resume()` 入口与工作台的 `reconcile()`（每次快照/页面加载），不加 pg_cron 或守护进程。** 理由：仓库没有调度器，这两处是「有人轮询它」和「有人看它」的唯一入口，代码量最小；两处都按 `incident_id` 收窄扫描（一条索引外的小表查询）。`DurableStore.sweep_expired_runs()` 不带 `incident_id` 即为全表清扫，供将来的定时任务或脚本直接调用。
2. **每个 Run 一个事务，先锁事故行再锁 run 行**，与 `hand_off` / `renew_lease` / `commit_*` 同序，避免与 `control()` 交叉死锁；候选先按快照读出，再逐个在锁下复核 `running` 且 `deadline <= clock_timestamp()`。并发两次清扫只会有一次停放（PG 用例用 4 线程钉住）。
3. **事件幂等靠 `append_once` 按 `{run_id, parked, reasons}` 键控**，与 `run_completed` 的补写方式相同；清扫者在停放后、追加前死掉时，再次调用 `sweep_expired` 不会重复停放，但也不会补事件（行是权威，见 ADR-0003；补投影属 `reconcile()` 扩展，归入 #44 的后续项）。
4. **follow_up 停放后的 Run 会重新排队，但 deadline 不变**，因此 `claim()` 仍以 `DEADLINE_EXCEEDED` 拒绝，页面看到 `run_claim_refused`；清扫只看 `running`，不会再碰 `queued` 行。真正的继续路径是 cancel + `new_run`（新 deadline）。是否让 follow_up 对已超时 Run 直接拒绝、或让 `new_run` 成为唯一动作，属产品/合同层取舍，列入给用户的决策点，本 PR 不改。

## 执行进展与证据

- 红/绿：`tests/test_m1_web_workbench.py::test_an_overdue_running_run_is_swept_into_a_timeout_handoff_on_the_page` 实现前失败（`'running' != 'waiting_human'`）；`tests/integration/test_m1_deadline_sweep_postgres.py` 实现前 `ImportError`（`sweep_expired`）；实现后分别 2 passed、11 passed。PG 用例覆盖：过期 running 停放 + 迟到 commit/hand_off/publish 落 `late_result` 或被拒；未到期不动；cancel/pause/follow_up 先落下不动；新代际 Run 不动；幂等与 4 线程并发恰一次；全表清扫只动过期行；停放后 follow_up 重排队、cancel + new_run 可 claim；runner 入口清扫并只发一次 `run_handoff`；工作台 `snapshot()` 清扫并显示 `DEADLINE_EXCEEDED`。
- `make check`（实现提交 `eafdad4` 前的 dirty 工作区）：ruff / format / mypy 通过，pytest `1934 passed, 210 skipped, 2 xfailed`。
- PG 集成（本 worktree 自己的 55431 实例，`postgres_lab start`）：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q` → `172 passed, 38 skipped`（#44 为 161）。
- 真实 Run（DeepSeek Flash，8 秒 deadline，3 次 HTTP，0.012809 CNY 上界）：首次尝试跑过 deadline，结论步骤被栅栏落为 `late_result`，runner 返回 `control_denied`，行仍 `running`；过期后再轮询一次，入口清扫停放为 `waiting_human` 并发唯一 `run_handoff`（`DEADLINE_EXCEEDED`）。结论与账本见 [`docs/evidence/m1-01-deadline-sweep/run.md`](../evidence/m1-01-deadline-sweep/run.md)。
- 独立审查（全新上下文 Agent，拿 ADR/约束/diff/原始账本，不拿结论；自行跑 PG 与工作台用例并写探针）：无 P1。P2-1 并发用例断言全表清扫为空，在共用 lab 库上被其他集成用例遗留的 85 行过期 running 打红并顺带停放了它们 → 删除该断言（全表行为由专门用例以 `<=` 钉住）；P2-2 `announce_deadline_exceeded` 文档串与用例注释声称「清扫者停放后死掉，后来者会补发事件」而代码没有 → 改为如实写 at-most-once 且不补：行不记录停放原因，补写会在 loop 交接与超时之间猜，归入 ROADMAP 已列的投影补写后续项；P2-3 follow_up 停放后的 Run 变成永远 claim 不到的 `queued` 行，每次轮询追加一条 `run_claim_refused`（探针 3 次轮询 3 条）→ 与决定 4 同一问题，属 `control()` 合同层取舍（拒绝 follow_up/correct/resume 于已超时 Run，或让清扫也停放过期 `queued` 行），本 PR 不改，列为用户决策点；P3-4 清扫的 `PersistenceError`（如 4 秒锁超时）会从 `resume()` 无类型抛出、把页面 GET 变 503 → 两处调用改为捕获后跳过（下次轮询/加载再扫），新增单测；P3-5 清扫事件 `evidence_ids` 为空而 loop 结果有 2 条（模板只渲染 execution/reasons，证据经 `tool_committed` 仍可见）、P3-6 单语句 `FOR UPDATE OF i,r` 与两语句锁序的 ABBA 窗口为既有且受 `lock_timeout` 封顶、P3-7 内存替身与 DB 语义一致 → 记录不改。复验：修复后 PG 清扫用例 11 passed、工作台用例 48 passed。

## 下一步与交接

- 独立审查 → 处置 P1/P2 → PR（用户门，功能 PR）→ CI → 一次 `@codex review` 分诊。
- 本 worktree 的 PostgreSQL 实例由本任务停止（`postgres_lab stop`，数据保留）；合并后按 AGENTS.md 清理 worktree。
- 用户决策点：决定 4 / 审查 P2-3（follow_up 对已超时 Run 的语义：保持现状、`control()` 拒绝、或清扫也停放过期 `queued` 行）。
