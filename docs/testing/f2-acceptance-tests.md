# F2 验收测试（第 1–5 步，确定性）与证据映射

验收测试由未参与 F2 任何实现的 Agent 编写，依据是 [F2 原文](../../feature_list.json) 五步、PR-E 合同预审（`~/.agent/handoffs/opspilot-prereview-f2-pre-2026-10-24.md` §6 确定性行为清单与 E-R8；E-R1–E-R10 已按推荐裁决）、[F2 合同](../tasks/2026-10-17-f2-contract.md)（U1–U9），以及它引用的子合同：PR-C 的 C1–C3、R-C4，#343 的 U1–U3，#305 的 W1-B 与 W2-A，B1/B2。实现方不改这些断言。

这些都是 PostgreSQL 上的确定性测试，不调用真实模型，也不是生产证明。F2 `passes` 的门槛（U9）是三项同时满足：本文件的五步测试全绿；一次 kind 串行会话，覆盖第 1、2、3、5 步；第 4 步的非 kind 真实风暴。`passes` 由 PR-E 翻转，按 [M2 记录 Q12](../tasks/2026-10-24-m2-remaining.md) 由 Agent 合并。

## 外部入口与投影

- 刺激：`POST /intake/alertmanager`、`POST /intake/version-changes`、`POST /incidents/{id}/control`（pause/resume/cancel/follow_up/close/new_run）、`Workbench.release_anomaly_pass`、`WorkerLoop.poll_once`、`InvestigationRunner.resume`（按 worker 自己的组合方式构建），以及第 4 步真实的 `python -m opspilot.worker_main` 子进程（B1 本地 HTTPS 模型替身）。还会读一次工作台事故页 `GET /incidents/{id}`。
- 投影：每个场景是一个 `IncidentScenario(feature_id="F2", acceptance_step="1".."5")`，用 `outcome_from_durable`（Run 结局、证据、决定、动作、人工交互、交接原因）或 `alert_intake_outcome`（投递、Run 列表、关联事故）。
- 模型都是脚本化的，第 4 步用模型替身。工具用产品 fixture 档，第 3 步用真实 otel-demo transport 对接一个本地卡住的 HTTP 源。没有模型 HTTP，不连 kind，不出网。
- 手动改动只有一处：第 4 步启动子 worker 前调用 `neutralize_claimable`，把库里其他测试遗留的可领取 Run 设为过期。这是既有 B1/B2 套件的做法。租约只靠等待过期，不改写。
- 不断言思维链或内部调用顺序。

## 场景（[tests/integration/test_f2_acceptance_postgres.py](../../tests/integration/test_f2_acceptance_postgres.py)，26 例，PG opt-in）

运行方式：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN=<临时库> .venv/bin/python -m pytest tests/integration/test_f2_acceptance_postgres.py`

| 步骤 | 测试 | 断言要点（预审 §6 条目） |
|---|---|---|
| 1 | `test_step1_duplicate_alert_delivery_is_replayed_into_the_same_incident` | 同 fingerprint 同 `startsAt` 重投返回 `replayed`，事故、Run、identity 行都不增加（1.1） |
| 1 | `test_step1_refired_episode_joins_the_open_incident_without_a_new_run` | 新 `startsAt` 返回 `attached`、同一事故、`run_id` 为空、Run 不增加；事故行不变；只有一条事件，键集合在 R-C4 白名单内；重复 firing 不再加事件；resolved 返回 `resolved_attached`（1.2） |
| 1 | `test_step1_after_close_the_same_fingerprint_opens_a_related_incident` | 关闭后同 fingerprint 返回 `created`，`related_incident_id` 指向已关闭事故，旧事故不被重开（1.3） |
| 1 | `test_step1_distinct_concurrent_alerts_on_one_target_stay_distinct_incidents` | 同一目标两个不同 fingerprint 并发到达：两个事故，各一个 Run（1.4） |
| 1 | `test_step1_concurrent_first_deliveries_of_one_alert_share_one_incident` | 同一告警并发首投只开一个事故、一个 Run（1.1 的并发形态） |
| 1 | `test_step1_release_anomaly_attaches_to_the_open_alert_incident_on_its_target` | 告警先开事故，发布异常消费后 link 为 `attached`；只加一条 `release_observation` 事件，无新 Run，控制列不变；重复 pass 不再增加（1.5，U2-A / F11 U8） |
| 1 | `test_step1_alert_after_a_release_incident_joins_it_and_claims_it` | 反向顺序：发布事故先开，告警 X 首投 `attached`、`run_id` 为空，Run 仍为 1，控制列不变，priority 取 min；随后 Y 另开事故；X resolved 后仍算认领，Z 也另开事故（1.6，#343 U3-A） |
| 1 | `test_step1_duplicate_deployment_event_is_replayed_and_a_changed_one_refused` | 发布 webhook 重投返回 `replayed`，观察主体不增加；同 id 内容不同返回 409 `RELEASE_IDENTITY_CONFLICT`（1.7） |
| 1 | `test_step1_follow_up_after_an_attachment_to_a_concluded_incident_sees_the_event` | 已结论事故收到附加后 follow_up：开出新 Run，模型首个请求含该事件 text 与追问，发布结论（1.8） |
| 1 | `test_step1_follow_up_after_an_attachment_to_a_handed_off_run_requeues_it` | 强制最终轮 `REPORT_INVALID` 交接后附加，再 follow_up：同一 Run 重新排队，输入逐字节不变（W2-A）；worker 领取后不报 `INCOMPATIBLE_STATE`，有模型调用且含事件 text，最后发布（1.8，C4-A 断言 2，#304） |
| 1（可选） | `test_step1_successor_run_after_attached_episode_reframes_on_new_episode` | #305 W1-B：cancel+new_run 后继 Run 的锚点是附加 episode 的 `startsAt`，框终点是创建时的 DB 时刻（±1 s），框长 24 h，默认窗 `[锚点−1h, 框终点]`（1.9，不作门槛） |
| 2 | `test_step2_kill_while_the_model_request_is_in_flight_then_resume` | 断点①：同一事故同一 Run；租约未过期时领取被拒（`LEASE_ACTIVE`），过期后 epoch +1；丢失的模型请求只重发 1 次，被杀的预约仍计入用量；工具不重查；已提交的证据保留并被结论引用；`worker_resumed`（2.1–2.6） |
| 2 | `test_step2_kill_after_the_reply_commits_before_its_tool_runs_then_resume` | 断点②：已提交的计划在恢复后只派发 1 次（`replayed_tools == 1`），模型请求多 1 次（2.1–2.3） |
| 2 | `test_step2_kill_while_a_tool_is_in_flight_keeps_the_settled_sibling` | 断点③（并行 2）：已结算的兄弟不重派，只有在途那条重派 1 次；工具次数含被卡的那次；兄弟证据被结论引用；被弃 attempt 的写回只作为 `late_result` 历史（2.1–2.4） |
| 3 | `test_step3_stalled_query_times_out_keeps_the_sibling_and_the_run_completes` | 真实 otel-demo transport 加本地滴流源：被卡查询在注册超时（30 s）+1 s 余量内落为 `timeout`/`TOOL_TIMEOUT`/`possible`，不采纳；网关不自动重试；连接被关闭；线程回到基线；兄弟证据保留并被引用；Run `completed`；事故页显示 "source may have received the request"（3.1–3.6、3.8） |
| 3 | `test_step3_a_stall_past_the_run_deadline_ends_in_an_explicit_handoff` | 超时被剪到 Run 剩余期限（4 s）；attempt 不发布；worker 下一次轮询的清扫把 Run 交接为 `waiting_human`，原因 `DEADLINE_EXCEEDED`，不会静默挂起（3.6，ADR-0005） |
| 4 | `test_step4_alert_storm_is_kept_whole_and_bounded_by_priority_capacity_and_backlog` | 50 条真实 webhook：每条一个事故、一个 Run；子 worker（容量 2、积压 8）运行时采样到的 running 峰值 ≤2，queued ≤8，三类状态合计 50；running 都是 critical；停放的优先级不高于未停放的；每个停放 Run 只有一条 `BACKLOG_FULL`/`not_started` 交接事件，投影为 handoff；follow_up 重新排队后不再被停放（4.1–4.5） |
| 4 | `test_step4_a_higher_severity_attachment_only_raises_the_priority` | warning→critical 附加把 priority 从 1 升到 0；之后 info 附加不降（4.2） |
| 4 | `test_step4_an_unbounded_or_unsafe_configuration_refuses_to_start[3 例]` | `BACKLOG_MAX` 为 `0` 或非数字，或 `POOL_MAX_SIZE < CAPACITY+2` 时，worker 非零退出，且没有发出模型请求（4.3、4.4） |
| 4 | `test_step4_one_incident_never_runs_two_attempts_at_once` | 一个 attempt 在途时，第二个执行者领取同一事故被拒（`LEASE_ACTIVE`），且没有模型调用；最后该事故只有 1 个 Run、epoch 1（4.6） |
| 4 | `test_step4_the_per_run_model_budget_bounds_the_work` | 默认 Run 的 `budget_limit` 和输入里的 `model_requests` 都是 100；预算 3 时总共恰好 3 次模型请求，之后交接；在最后一次请求时被杀，重启后没有可用预算：不发模型请求，结局 `budget_exhausted`/`BUDGET_EXHAUSTED`（4.7） |
| 5 | `test_step5_pause_while_the_model_reply_is_in_flight_then_resume` | 模型回复在途时事故级 pause：Run 与事故 `paused`，代际 1，无结论；迟到的回复成为旧代际的 `late_result`；投影为 `human_control`/`pause`，带 `late_result_rejected` 与 `STALE_CONTROL_GENERATION`；已提交证据保留；暂停期间 worker 不运行；resume 后同一 Run 继续并发布（5.1、5.4、5.5） |
| 5 | `test_step5_cancel_while_the_model_reply_is_in_flight_is_final` | 在途时 cancel：Run 与事故 `cancelled`，无结论；迟到写回为 `late_result`，不开新 Run；之后轮询与 resume 都不再运行它（5.3、5.4） |
| 5 | `test_step5_pause_while_a_query_is_stalled_lets_the_late_result_be_history_only` | 查询卡住期间 pause：有界返回；Run `paused`，无结论，无新证据，结果只作为 denied/timeout 历史；投影为 `pause`（5.2；同时覆盖第 3 步 3.7） |

## 测试作者自定的口径与合同出入

1. 名称假设（A1–A6）写在测试文件头部。
2. **第 5 步 5.5「cancel 后 follow_up 需先 reopen」没有断言。** `opspilot/persistence/controls.py:446-452` 只在 lifecycle 为 `resolved`/`closed` 时返回 `REOPEN_REQUIRED`，而 cancel 不改 lifecycle。预审这条与代码不一致，需要合同方确认。当前测试只断言 cancel 对该 Run 是终态。
3. **第 4 步 4.7「100 次上限 → `budget_exhausted`」**：一直要工具的模型会在最后一次请求时被强制进入最终轮，结局是交接（`TOOL_PLAN_ON_FINAL`，投影为 `failed`），不是 `budget_exhausted`。`budget_exhausted` 出现在「预算已用完时重启」这条路径上。两条路径都断言了上限与交接，只对后者要求 `budget_exhausted`。
4. **第 3 步 3.6「报告 gaps 含超时 operation」**：gaps 由模型撰写，脚本化模型下断言没有意义，所以不测；改为断言工具行状态与工作台文案。在 Run 期限之前被卡的 attempt，runner 返回 `control_denied`，明确的交接状态由清扫写出。
5. 第 3 步 3.5 的线程回基线是进程内的 `threading` 基线，不是 kind 会话里的 `ps -M`（E-R2）。「连续 5 次卡住不累积」引用既有的 [`tests/test_f2_query_deadline_acceptance.py`](../../tests/test_f2_query_deadline_acceptance.py) `test_repeated_stalls_do_not_accumulate_threads_or_connections`，这里不重复。3.2「卡住期间的字节不形成证据」在这里表现为 timeout 结果未被采纳，字节层面由同一文件的 `test_stalled_response_bytes_are_discarded_not_returned` 覆盖。
6. 第 2 步 2.5 的「租约到期 + 一个轮询周期」以直接 `resume` 表达：到期前被拒，到期后第一次领取即 epoch +1。

## 在 `main`（2cc2fcd）上的结果

2026-10-24 在临时 PG 17.9（端口 56450）上运行：**25 通过，1 失败**。失败的是 `test_step1_successor_run_after_attached_episode_reframes_on_new_episode`，因为 #305（PR #346）尚未合并，后继 Run 的锚点与框仍沿用首个 Run（J5）。把 PR #346 head `ce66135` 的 `opspilot/` 改动临时应用后，26 例全部通过（随后已撤销）。

## 证据映射（预审 §1 对照表）

| 步骤 | 确定性测试（本文件） | 已有真实证据（复用，按当时 commit 解读，非 PR-E HEAD） | PR-E 会话（kind，[run.md](../evidence/f2-live/run.md)） |
|---|---|---|---|
| 1 | 上表第 1 步各例 | 重复告警 `replayed`：[m1-04-intake-live](../evidence/m1-04-intake-live/run.md)、[f12-live/run.md](../evidence/f12-live/run.md)；再 firing 三段 1a/1b/1c：[f2-alert-correlation-live](../evidence/f2-alert-correlation-live/run.md)（`91541db`）；部署事件附加到开放告警事故：[f11-pr4b-live](../evidence/f11-pr4b-live/run.md) S4（`5943b0f`）；重复发布 webhook：[f11-pr3-release-intake-live](../evidence/f11-pr3-release-intake-live/run.md)；不同并发事故分开：[f2-b2-live](../evidence/f2-b2-live/README.md) | **K1 PASS** [k1-refire-followup](../evidence/f2-live/k1-refire-followup/summary.json)：再 firing `attached`，人工 follow_up 开后继 Run，7/7 模型输入含事件，#305 换框且覆盖新 episode 的查询 `ok`；**K5 FAIL** [r1](../evidence/f2-live/k5-release-then-alert/r1-alert-first/summary.json)、[r2](../evidence/f2-live/k5-release-then-alert/r2-alert-first/summary.json)：两轮都是告警先开（发布附加到告警事故，每次故障 1 个事故 1 个 Run），按 U3-A/Q6 停线；2026-10-25 按裁决 A 接受（发布先开顺序只由 #343 确定性测试覆盖，见 [F2 任务记录](../tasks/2026-10-17-f2.md)） |
| 2 | 上表第 2 步三例 | [f2-kill-resume-live](../evidence/f2-kill-resume-live/README.md)：①`model_in_flight`（`424a6bf`）、②`response_committed`（`264a271`）、③ 修复后复跑 `tool_in_flight-332/`（`a41622b`），原轮 `tool_in_flight/` FAIL 与 `-r1-invalid` 无效轮均保留；#242 压缩 [242-compaction-live](../evidence/242-compaction-live/README.md)（单独断言，非门槛） | 不需要（E-R1） |
| 3 | 上表第 3 步两例，以及第 5 步卡住期间 pause 一例 | [f2-query-deadline-live](../evidence/f2-query-deadline-live/run.md)（`ab34df1`，schema 0017；早于 #309 并行与 #337） | **K2 PASS** [k2-query-stall](../evidence/f2-live/k2-query-stall/summary.json)：30.01 s `timeout`/`TOOL_TIMEOUT`/`possible`，同轮两条兄弟提交，无重试，线程 12→10→8 回基线 |
| 4 | 上表第 4 步各例 | [f2-b2-live](../evidence/f2-b2-live/README.md)（`f98e9aa`，非 kind 真实 DeepSeek，12 条、容量 2、上限 6）；[f2-b1-live](../evidence/f2-b1-live/README.md)（容量 2 峰值、critical 先领） | 不需要（U9：第 4 步走非 kind 真实风暴，已完成） |
| 5 | 上表第 5 步三例 | [f7-step4-live](../evidence/f7-step4-live/run.md)（全局与按目标暂停时在途轮 `late_result`）；[f12-live](../evidence/f12-live/run.md)（接管后、运行中 close 后 `late_result`）。事故级 pause/cancel 的在途迟到写回**尚无真实记录** | **K3 PASS** [k3-pause-inflight](../evidence/f2-live/k3-pause-inflight/summary.json)、**K4 PASS** [k4-cancel-inflight](../evidence/f2-live/k4-cancel-inflight/summary.json)：在途回复写为 `late_result`（旧代际），pause 后 resume 继续发布，cancel 终态 |

PR-E 会话的证据按 E-R9 放在 `docs/evidence/f2-live/`：run.md、各场景的 summary.json、references.md。复用证据的 commit 与 prompt/tool_schema revision 写进 references.md。
