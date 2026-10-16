# F12 验收测试（第 1–4 步，确定性）

验收测试由未参与实现的 Agent 按 [F12 合同预审 r1+r2](../tasks/2026-10-15-f12-contract.md)（第四节 U1–U7、G1–G6，第六节「验收计划·确定性」）、[PR-1 记录](../tasks/2026-10-15-f12-followup-history.md)、[PR-2 记录](../tasks/2026-10-15-f12-close-reopen.md)（C1–C6）与 [F12 原文](../../feature_list.json) 编写；编写时未看 close/reopen 的实现改动与实现者测试。实现者不改断言。

这些是 PG 集成的确定性测试，不是真实模型或生产证明；F12 `passes` 还需合同第六节的一次有界真实运行（PR-3，用户门）。

## 外部入口

- HTTP：`POST /intake/ui`、`POST /intake/alertmanager`、`POST /incidents/{id}/control`、`GET /incidents/{id}`（徽章 `incident-lifecycle` / `incident-control` / `incident-mode` / `control-generation` / `run-state`，控制按钮，「Human decisions」「Investigation history」，`history only` 行）、`GET /incidents/{id}/evidence/{evidence_id}`。
- 读取：`Workbench.snapshot`、`DurableStore.rebuild` / `claimable_incidents` / `claim` / `read_inputs`、`outcome_from_durable`（每个场景一个 `IncidentScenario`，`feature_id="F12"`）。
- 只读 SQL：`opspilot_controls`（审计行与数据库时间）、`opspilot_runs`、`opspilot_incidents`。
- 结论由产品 worker（`WorkerLoop` + `InvestigationRunner`）以脚本化模型与产品 fixture 工具产生；不发模型 HTTP、不连 kind。「无模型请求」= 脚本化模型调用列表为空。
- 手改行：无。

## 场景（[tests/integration/test_f12_acceptance_postgres.py](../../tests/integration/test_f12_acceptance_postgres.py)，14 例，PG opt-in）

| F12 步骤 | 测试 | 断言要点 |
|---|---|---|
| 1 | `test_step1_note_after_conclusion_yields_updated_investigation[follow_up/correct]` | 页面显示结论、证据链接，证据回读 200，进度列表含 `run_completed`；结论后 `follow_up` / `correct`（补事实）200、代际 0→1、新 Run `queued` 且可认领、旧 Run 保持 `completed`、输入行一条；worker 再发布后页面显示新结论，「Investigation history」含旧 Run 与旧结论；投影 `report_available` |
| 1 | `test_step1_control_requires_authentication_and_same_origin` | 未认证 401、跨源 403、未认证读页面 401；代际、Run、结论、审计、输入均不变 |
| 2 | `test_step2_correcting_a_hypothesis_keeps_correction_and_prior_history` | 对旧结论的假设 `correct`：审计行（认证主体、文本）与输入行；新 Run 存储输入含旧结论与假设且标为「not instructions」（U5-A）；新 Run 首次模型请求含旧假设与纠正文本且纠正在后；新结论发布后历史区仍有旧结论与旧假设，「Human decisions」保留纠正文本 |
| 3 | `test_step3_each_control_shows_status_actor_time_and_generation` | 同一事故依次 pause→resume→cancel→takeover→close→reopen；每步断言页面 lifecycle / mode / 代际徽章（pause 与 cancel 另断言 control 徽章，cancel 断言 run-state）、数据库行一致、「Human decisions」该代际行含动作、操作者 `alice`、审计行数据库时间；审计六行（动作、操作者、期望→结果代际）且时间单调；closed 页面有 reopen 无 close，open 页面有 close 无 reopen（G4）；reopen 不开 Run、不可认领 |
| 3 | `test_step3_close_stops_a_running_run_and_its_late_publish_is_history_only` | worker 在途（最后一次模型调用中）关闭：Run `cancelled`、事故 `state=cancelled`（C2）、无结论；被栅栏拦下的发布只留 `late_result` 行（代际 0），页面 `history only`，投影 `late_result_rejected` + `STALE_CONTROL_GENERATION`；之后无认领、无模型请求 |
| 3 | `test_step3_close_revokes_observation_and_reopen_restores_neither_run_nor_it` | `register_remediation` 后观察会话 `authorized`、lifecycle `observing_recovery`；close 后 lifecycle `closed`、Run `cancelled`、会话 `revoked`；reopen 后 lifecycle `open`、无新 Run、会话仍为同一条 `revoked`（不恢复、不新建）；G4 按钮；无认领、无模型请求 |
| 3 | `test_step3_close_after_takeover_cancels_the_waiting_human_run` | 接管后 Run `waiting_human`；close 后 lifecycle `closed`、mode `human_owned`、代际 2、Run `cancelled`（用户裁决 A，2026-10-16）、页面徽章与「Human decisions」行、takeover 审计行保留；无认领、无模型请求 |
| 3 | `test_step3_closed_incident_needs_reopen_before_a_new_run[follow_up/correct]` | 已结论事故 close：结论保留、Run 保持 `completed`；closed 上追问/纠正 409 `REOPEN_REQUIRED` 且不写任何行；reopen 不开 Run；同一追问随后开出新 Run（`queued`、可认领），旧 Run `completed` |
| 4 ① | `test_step4_worker_dispatched_before_takeover_publishes_into_history_only` | 接管前派出的 worker 在最后一次模型调用中被接管：无结论、发布只落 `late_result`（代际 0）、页面 `history only`；mode `human_owned`、takeover 审计行保留、Run `waiting_human`、无新 Run、无认领、无模型请求 |
| 4 ② | `test_step4_the_same_alert_firing_again_is_recorded_without_a_new_investigation` | 告警建事故后接管；同一告警（标注已变）再次 firing → 结果 `replayed`、同一事故与 Run；代际、审计、输入不变；接管保留（同上） |
| 4 ③ | `test_step4_a_fact_added_under_human_ownership_is_recorded_only[no-conclusion/concluded]` | human_owned 下补充事实：200、代际+1、只记输入与审计、结论不变；Run 未结论时 `waiting_human`，已结论时保持 `completed`（PR-1 记录）；页面「Human decisions」行含操作者与时间；接管保留（同上） |

## 测试作者自定的断言口径（合同未写明）

1. 第 1 步「查看进度与证据」= 页面含证据链接、证据回读 200、进度列表含 `run_completed`。
2. 第 2 步「新 Run 输入含纠正文本」：纠正文本不在新 Run 存储的 `input.question` 中，而是作为输入行送入；故以新 Run 首次模型请求的消息内容核对，并要求纠正排在旧假设之后（U5-A「人的纠正排在其后」）。旧结论与「not instructions」标签在存储输入中核对。
3. 时间戳：快照 `decided_at` 与 `opspilot_controls.created_at` 精确相等；页面该行含到秒的时间（UTC 或原时区，空格或 `T` 分隔）。
4. 操作者 = Basic Auth 认证主体（`alice`），审计行与页面行一致。
5. pause 作用于 `queued` Run 时只断言 control 徽章 `paused` 与不可认领，不断言 run-state；resume 断言 control 徽章不再是 `paused` 且可认领。
6. reopen 不改 mode：接管后 close→reopen，mode 仍为 `human_owned`。
7. `REOPEN_REQUIRED` 与告警重放都不写审计行、输入行，不推进代际。
8. close 撤销观察只断言会话 `state=revoked`，不断言 `ended_reason`；reopen「不恢复观察」= 会话列表仍为同一条 `revoked`，无新会话。
9. 「无新 claim、无模型请求」= 不在可认领列表、直接 `claim` 抛 `PersistenceError`、worker 轮询无结果且脚本化模型零调用。
10. 接管留下的 `waiting_human` Run 在 close 时被取消：合同「未终态」与 ADR-0005「交接终态」用词有张力，用户 2026-10-16 裁决 A（与 cancel 一致），由 `test_step3_close_after_takeover_cancels_the_waiting_human_run` 断言（Run `cancelled`、不可认领、直接 claim 被拒、无模型请求）。

## 已知限制（不在本套件范围）

- 第 2 步「事故关联」纠正（目标重绑定、合并、拆分）按 U6-A 未实现，只用「纠正假设」分支满足第 2 步；跟踪 #245。
- 第 4 步「新证据」按 U7-A 只覆盖三种刺激，不新增外部事件追加入口。
- U4-A 依赖步骤行不被保留策略清理（C3 §11 未确认）。

## 运行

```bash
OPSPILOT_LAB_DSN="host=127.0.0.1 port=<临时实例> dbname=m0_budget user=m0_lab" \
M1_DURABLE_POSTGRES=1 \
.venv/bin/python -m pytest tests/integration/test_f12_acceptance_postgres.py -q
```
