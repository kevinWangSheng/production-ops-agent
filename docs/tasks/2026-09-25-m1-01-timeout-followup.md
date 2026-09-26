# M1-01 剩余工作 2b：超时 Run 上的追问/纠正自动开新 Run

- 状态：进行中（PR 待用户合并）
- 更新日期：2026-09-25
- 依据：用户决定 2026-09-25（经 lead 转达）：follow_up / correct 落在被清扫停放（`waiting_human`、`DEADLINE_EXCEEDED`、deadline 已过）的 Run 上时，记录输入并自动开一个带新 deadline 的 Run，与 `new_run` 一致，新 Run 看到全部既往输入；对标上游 HolmesGPT（TIMEOUT 后的新消息即新请求）。[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)；[#45 任务记录](2026-09-24-m1-01-deadline-sweep.md) 决定 4 / 审查 P2-3；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Runtime and human control requirements」（人工控制优先、不产生无界工作）。功能 ID：M1-01。
- 工作区：分支 `feature/m1-01-timeout-followup`，worktree `../production-ops-agent-timeout-followup`（起点 `origin/main` `7ce8404`，含 #45）。

## 目标与范围

`control()` 在 follow_up / correct 命中「deadline 已过」的当前 Run 时，不再把它重排为 `queued`（那一行没有任何 claim 能领，且每次轮询都会追加 `run_claim_refused`），而是在同一次人工动作里：记录输入、关闭旧 Run（`cancelled`）、按 `new_run` 的写法插入新 Run（新 deadline、沿用预算上限与版本、可带自己的输入快照）、事故指向新 Run；代际推进一步。未过期的交接 Run 保持原行为（重排队同一 Run）。cancel 不变。不留过期 `queued` 行，此情形下不再每轮询发 `run_claim_refused`。

不做：`new_run`/工作台路径自动构造续接输入（既有缺口，见决定 3）、resume 对过期 Run 的语义、投影修复。

## 决定与理由（Agent 自决，可逆）

1. **改在 `control()` 内，同事务完成，而不是工作台先 control 再 cancel + new_run。** 理由：判定条件与清扫同源（数据库时钟、行锁下的 state + deadline），与清扫并发时无论谁先到结果一致（PG 用例用双线程钉住）；一次人工动作一个代际、一条审计行，避免中间态留下过期 `queued` 行。
2. **触发条件取「当前 Run 处于开放态（queued/running/paused/waiting_human）且 deadline 已过」，不限于 `waiting_human`。** 理由：过期的 `running` Run 只是还没被清扫、过期的 `queued` Run 同样无人能领，追问落在它们上面的正确结果都是新 Run；这也是并发安全的来源。暂停的事故按既有 keep_paused 规则新 Run 以 `paused` 开出。
3. **`renew_run_id` / `renew_deadline` / `renew_input` 由调用方提供，缺省时拒绝（`ILLEGAL_TRANSITION`）。** 理由：`new_run` 的这三项也是调用方给的；持久层不能构造续接输入（C3 `continuation_context` 属调查层，旧输入快照绑定旧 run id 不能照抄——真实探针证实照抄会让报告引用校验失败）。工作台按 `new_run` 同一套派生 run id 与 wall，输入与 `new_run` 一样为空（真实驱动器跑工作台开出的新 Run 会 `INPUT_MISSING`，这是 ROADMAP 第 5 项的既有缺口，本 PR 不扩）；真实 Run 脚本与 PG 用例用 `continuation_context` 构造输入。
4. 工作台 `control_applied` 事件在指针变更时带 `run_id`（与 new_run 一致），由 `find_incident` 前后对比得出。

## 执行进展与证据

- 红/绿：`tests/integration/test_m1_timeout_followup_postgres.py`（7 例）与 `tests/test_m1_web_workbench.py` 新增 2 例在实现前全部失败（`AttributeError`/`ILLEGAL_TRANSITION`/状态断言），实现后通过。覆盖：follow_up / correct 超时后新 Run 被 claim 并调查到发布、轮次冻结输入含追问；未过期交接重排同一 Run；超时后 cancel、以及续开后 cancel；清扫与追问并发；续开 id 冲突拒绝且不动状态；工作台路径新 Run 显示、`control_applied` 带 run_id、无 `run_claim_refused`、同 key 重放不开第二个 Run。
- 收紧的既有用例：#45 的 `test_follow_up_cancel_and_new_run_work_after_a_timeout_handoff` 改为「无续开参数 → `ILLEGAL_TRANSITION`、状态不动」；`test_a_human_decision_that_landed_first_is_never_overwritten[follow_up]` 与工作台 `test_an_overdue_running_run_is_swept…` 改断新 Run。
- `make check`（dirty 工作区，实现提交前）：ruff / format / mypy 通过，pytest `1937 passed, 217 skipped, 2 xfailed`。
- PG 集成（本 worktree 自己的 55431 实例）：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q` → `179 passed, 38 skipped`（#45 为 172）。
- 真实 Run（DeepSeek Flash，8 秒 deadline → 清扫 → follow_up → 新 Run 12 分钟 wall）：新 Run 被 claim、引用 1 条携带证据 + 1 条新证据后发布；4 次 HTTP，0.047744 CNY 上界。见 [`docs/evidence/m1-01-timeout-followup/run.md`](../evidence/m1-01-timeout-followup/run.md)。
- 独立审查：待执行。

## 下一步与交接

- 独立审查 → 处置 P1/P2 → PR（用户门：`control()` 合同变更）→ CI → 一次 `@codex review`。
- 本 worktree 的 PostgreSQL 实例由本任务停止（`postgres_lab stop`，数据保留）；合并后按 AGENTS.md 清理 worktree。
- 用户可见的限制：工作台开出的新 Run（无论 new_run 还是本次续开）没有输入快照，真实驱动器尚不能跑它（ROADMAP 第 5 项）。
