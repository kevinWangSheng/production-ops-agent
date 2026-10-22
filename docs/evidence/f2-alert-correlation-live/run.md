# F2 PR-C 有界真实 Run：告警关联（C4-A）

- 日期：2026-10-22 07:46–08:33Z；分支 `feature/F2-alert-correlation`，commit `91541db`（运行期间工作区干净，本目录为运行后新增）
- 合同：[PR-C 决定表](../../tasks/2026-10-21-f2-c-contract.md) C4-A，以及 [F2 任务记录](../../tasks/2026-10-17-f2.md)「PR-C 合同（r2）」
- 冻结摘要：[summary.json](summary.json)。原始 ledger 不入库（投递、identity、事故、Run、输入、subject events、页面 HTML、trace 回读、web/worker 日志），sha256 `bce1c2da…2cadd355`，440626 字节（ADR-0006）
- 类别：有界真实运行。告警由 kind lab 里真实的 Prometheus 和 Alertmanager 产生，不是脚本伪造的 webhook；模型是真实的 DeepSeek `deepseek-flash`。这不是生产证明
- 执行者：未参与 PR-C 实现的新上下文 Agent
- **结论：断言 1a、1b、1c 通过；断言 2 失败。**
  - 断言 2 失败的原因：C4-A 要求的人工 `follow_up` 落在一个已交接的 Run 上，被一个既有缺陷以 `INCOMPATIBLE_STATE` 挡下，一次模型都没调用，见「缺陷」一节
  - 补充证据：之后走了另一条路径，人工 `cancel` + `new_run`，Run 发布后再做一次 `follow_up`。这条路径证明附加事件进入了模型输入，但它不是 C4-A 规定的路径，不满足断言 2
  - 范围问题：后续 Run 沿用首个 Run 的授权窗口，所以模型看得到附加事件，却查不到附加 episode 的数据（见「观察到但不属本项判定」）

## 环境

- PostgreSQL 17.9：本 worktree 新建 `tmp/pg-f2c-live`，端口 55661，`make migrate` 升到 `0018_alert_correlation`，`postgres_lab runtime-roles` 建运行角色。日志开启 connections / disconnections。运行后 `pg_ctl stop`。其他 lane 的 PG 没有碰
- web：`python -m opspilot.web serve`，用户 `opspilot_web_lab`，绑定 `127.0.0.1:8080`，即 Alertmanager webhook 的目标 `192.168.5.2:8080`（colima 下指向宿主回环）。`OPSPILOT_EVENT_TOKENS` 来自 `workbench-alertmanager.env`，只有哈希。`OPSPILOT_TARGET_IDENTITIES` 有一条 `m0-otel-20260909`，`match.labels = {cluster, namespace, service}`；`cluster_uid` 与集群 kube-system UID 一致。`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`；实验 UI 账号 `f2clab`
- worker：`python -m opspilot.worker_main`，用户 `opspilot_worker_lab`，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号。版本 `prompt-replay-candidate-dd9018031487` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-4d7168180749`
- 两个进程都用最小环境（`env -i`）启动，都设 `OPSPILOT_TOOL_PROFILE=otel-demo`
- 实验环境：kind `opspilot-m1`。注入前检查（07:46Z）：
  - `kind_lab.py health` 为 ok
  - swap 剩 714 MiB（高于 400 MiB 的停止线）。运行中最低约 670 MiB
  - frontend 2 分钟 span rate 1.82/s，所以没有重启 otel-collector
  - checkout PlaceOrder p95 为 92.5 ms
  - `ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空
  - 占用登记：holder `lane-f2-c`，batch `f2-c-live`
- trace：project `opspilot-lab-f2-alert-correlation-live-20261022`，运行前建立，回读 `trace_tier=longlived`
- 防循环上限：沿用产品默认，每个 Run 最多 100 次模型请求

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 07:46:40 | 登记占用；PG、web、worker 就绪（worker `trace export enabled mode=lab`） |
| 07:47:44 | 第 1 次 `fault inject`（paymentFailure 100%，已校验） |
| 07:55:48 | 投递 1：firing，startsAt 07:54:54Z → `created`，事故 `e4527304`，Run `1b21375d` |
| 07:56:04 | 第 1 次 `fault restore` |
| 07:58:04 | Run `1b21375d` 在第 9 轮（最终报告轮）交接：`REPORT_INVALID`，进入 `waiting_human` |
| 08:00:48 | 投递 2：resolved（同 startsAt）→ `resolved_attached` |
| 08:01:06 | 第 2 次 `fault inject` |
| 08:06:48 | 投递 3：firing，新 startsAt 08:05:54Z → `attached`，事故 `e4527304`；写入一条 `kind='event'` 输入 |
| 08:07:12 | 第 2 次 `fault restore` |
| 08:07:21 | 人工 `follow_up`（control seq 30）把 `1b21375d` 重新排队 |
| 08:07:22 | worker 领取（epoch 2），随即以 `INCOMPATIBLE_STATE` 阻塞，0 次模型调用 |
| 08:12:47 | 投递 4：resolved（08:05:54Z 那个 episode）→ `resolved_attached` |
| 08:14:49 | 人工 `cancel` + `new_run`，开 Run `d1cc2750` |
| 08:16:37 | `d1cc2750` 发布报告 |
| 08:16:52 | 人工 `follow_up`（control seq 63），开新 Run `8d7772e9` |
| 08:17:33 | `8d7772e9` 发布报告 |
| 08:17:53 | 人工 `close`，事故 `e4527304` 变为 `closed` |
| 08:17:53 | 第 3 次 `fault inject` |
| 08:25:47 | 投递 5：firing，startsAt 08:24:54Z → `created`，新事故 `9fc2c586`，`related_incident_id = e4527304`，Run `b52f01bd` |
| 08:26:03 | 第 3 次 `fault restore` |
| 08:27:39 | `b52f01bd` 发布报告 |
| 08:30:47 | 投递 6：resolved → `resolved_attached`，挂在 `9fc2c586` 上 |
| 08:31:01 | 按 PID 用 SIGTERM 停 worker 与 web，两者都正常退出 |
| 08:31:13 | `occupation release`；回读 `show` 为 `null` |
| 08:33:23 | 冻结摘要后 `pg_ctl stop` |

六次投递的 fingerprint 都是 `83f7541ae0105d42`，actor 都是 `alertmanager`，都返回 200。

## 结果

| 断言 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1a 首次 firing 建事故 | 新事故，并开 Run | 投递 1 为 `firing` / `created`，事故 `e4527304-0561-…`，Run `1b21375d-607b-…` | 通过 |
| 1b 同 fingerprint、同目标、新 startsAt、旧事故未关闭 → `attached`，不开第二个 Run，追加一条 event | 结果为 `attached`，同一事故；Run 数不变；`kind='event'` 输入恰好一条 | 投递 3：`firing` / `attached` / 事故 `e4527304`，当时 lifecycle 为 `open`（08:17:53 才关闭）。全库只有 1 条 `kind='event'` 输入，actor `alertmanager`，text 为 "Alertmanager alert 83f7541ae0105d42 fired again with startsAt 2026-10-22T08:05:54Z (status firing) on this incident's target; attached to this incident without a new Run."。08:07:01 的快照里该事故只有 1 个 Run。subject events 在 seq 29（07:58:04）和 seq 30（08:07:21 的人工 follow_up）之间没有任何事件，所以附加没有开 Run，也没有发 announce（R-C10）。identity 表共 2 行，分别对应投递 1 和投递 5，附加 episode 没有写 identity（C1-A）。之后该事故上的 Run 都能追溯到人工控制：`d1cc2750` 来自 new_run seq 34，`8d7772e9` 来自 follow_up seq 63 | 通过 |
| 1c 关闭后再 firing → 新事故，并关联到已关闭的事故 | 新事故，`related_incident_id` 指向已关闭的事故 | 08:17:53 关闭 `e4527304`。投递 5 为 `firing` / `created`，新事故 `9fc2c586-a82b-…`，`related_incident_id = e4527304-…`，被关联的事故 lifecycle 为 `closed` | 通过 |
| 2 人工 follow_up Run 的 trace 证明附加事件进入了模型输入 | 附加之后，人工 `follow_up` 让 Run 继续跑，其 LLM 输入里有事件 text | 附加后的人工 `follow_up`（control seq 30，08:07:21Z）把已交接的 Run `1b21375d` 重新排队。worker 在 epoch 2 领取后，以 `blocked(INCOMPATIBLE_STATE)` 结束，0 次模型调用、0 次工具调用（[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b51d14e7-6668-44bd-b02c-ae9ded1e21a5/r/00000000-0000-0000-d469-fdabe54dc407?poll=true)）。没有任何 LLM 输入可以检查 | **失败** |
| 页面 | 附加可见；关联可见；列表一事故一行 | 事故 `e4527304` 页的投递表有 starts_at 列，投递 3 行标 `re-fired episode`。事故 `9fc2c586` 页有 `<a id="related-incident" href="/incidents/e4527304-…">`。列表页两个事故各出现一次 | 通过 |
| PG 日志 | web 和 worker 没有错误 | 1025 行。ERROR 1、FATAL 1，都是执行者自己造成的：第一次 `pg_ctl start` 没设 `LC_ALL`（55000），以及只读查询写错列名（42703）。42501 为 0 | 通过 |

## 缺陷：最终报告被拒而交接的 Run，人工 follow_up 后被阻塞（`INCOMPATIBLE_STATE`）

本节不含凭据或非公开数据，可直接整理成 issue。

- 触发：
  - Run `1b21375d-607b-51d9-9527-51992cde88c9` 在 `ctx0:round-9` 提交最终报告，这一步的 `context.final=true`、`input_watermark=0`
  - 报告被拒，Run 交接：subject event `run_handoff`，`reasons: ["REPORT_INVALID"]`，`parked: true`，Run 进入 `waiting_human`
  - 该 Run 的 step 依次是 `ctx0:round-1` … `ctx0:round-9`，再加一行 `conclusion:g0:ctx0:round-9`
- 操作：人工 `follow_up`（`expected_generation=0`）。它把同一个 Run 重新排队，没有开新 Run
- 结果：
  - worker 领取（`run_claimed` epoch 2，control_generation 1）后立即交接
  - subject event 为 `run_handoff`，内容是 `{"parked": false, "handoff": true, "reasons": ["INCOMPATIBLE_STATE"], "execution": "blocked", "published": false}`
  - worker 日志：`attempt incident=e4527304-0561-5feb-81a3-b49a16eb9d2f status=blocked reason=INCOMPATIBLE_STATE`
  - 0 次模型调用、0 次工具调用。这个 Run 之后只能 cancel，页面上不再出现 follow_up
- 失败位置：
  - `rebuild_transcript` 在 `opspilot/investigation/context.py:960` 调用 `_check_snapshot_hash`
  - `_check_snapshot_hash` 在 `opspilot/investigation/context.py:1229` 抛出 `ContextError("INCOMPATIBLE_STATE")`
  - 也就是 `messages_hash(sent) != recorded`：重建出来的 `sent` 是第 9 轮之前的消息加 `FINAL_REPORT_INSTRUCTION` 加 `run_coverage_message(delivered)`（同文件 1213–1228），它的哈希与该步记录的 `input_snapshot_hash` 不一致
- 复现：
  - 执行者在一份库副本里做了只读重建。副本里事故的 `current_run_id` 指回 `1b21375d`，用完即删
  - `ctx0:round-1` 到 `ctx0:round-8`（`final=false`、`input_watermark=0`）的哈希全部一致，只有 `ctx0:round-9`（`final=true`、`input_watermark=0`）不一致
  - 把 `kind='event'` 输入从快照里去掉再重建，在同一位置同样失败
  - 所以与 PR-C 的附加事件无关。PR-C 也没有改 `opspilot/investigation/`
  - 根因（例如 `delivered` 是否与该步发送时一致）没有追查，本 PR 不修
- 影响：一旦最终报告被拒，C4-A 规定的「附加后人工 follow_up」这条路径就走不通，人只能 cancel + new_run

## 补充证据（另一条路径，不满足 C4-A 断言 2）

- 人工 `cancel` + `new_run`（control seq 33、34）开了 Run `d1cc2750`，它的 7 次 LLM 输入都含该事件（[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b51d14e7-6668-44bd-b02c-ae9ded1e21a5/r/00000000-0000-0000-6220-00a25906efbc?poll=true)）
- 它发布后，人工 `follow_up`（seq 63）开出新 Run `8d7772e9`（[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b51d14e7-6668-44bd-b02c-ae9ded1e21a5/r/00000000-0000-0000-c56a-3225eb317c51?poll=true)，OTel `b59f2bb14ea57ff8edb0c91709590c51`）
  - 5 个 llm 子 run 的输入都含 `{"investigation_inputs":[{"content":{"channel":"alertmanager","text":"Alertmanager alert 83f7541ae0105d42 fired again with startsAt 2026-10-22T08:05:54Z …"},"kind":"event",…`
  - llm 5、tool 7，与库中 `budget_spent=5`、`tool_operations_used=7` 一致
- 这说明附加事件能进入后续 Run 的模型输入，但路径多了一次人工 cancel + new_run，所以不算断言 2 通过

## 观察到但不属本项判定

- **附加 episode 的数据不在授权窗口内**：
  - 事故 `e4527304` 上后续的 Run（`d1cc2750`、`8d7772e9`）沿用首个 Run 的窗口 `2026-10-21T07:55:48Z`–`2026-10-22T07:55:48Z`，这是开发指南写明的「沿用前一 Run 的历史窗口」
  - 附加 episode 的 startsAt 是 08:05:54Z，在窗口之外
  - `8d7772e9` 的报告原文："Whether the 08:05:54Z re-fire has the same cause … are unobserved: a metric query for 08:00–08:10Z was refused as outside the authorized window"
  - 结果是附加事件能进入模型输入，但模型查不到它的遥测。这是否该由 F2 后续步骤处理（例如附加时扩窗），属合同层取舍，未决
- 附加 episode 的投递 3 的 `annotation_revision` 是 1，不是延续投递 2 的 2。看起来 revision 按 episode（delivery_key）计数，与 resolved 投递 4 的 revision 2 一致。合同没有写明这一点，未核对实现意图
- 事故行的 `state` 列在 Run 完成后仍是 `queued` / `running`（已关闭的事故也一样）。这个现象 M1-04 已有记录，本次没有核查
- Run `1b21375d` 的 `REPORT_INVALID` 是第 9 轮最终报告被拒造成的，与 M1-01 的已知问题同类，本次没有分析

## 失败与未覆盖

- 断言 2 失败，原因见「缺陷」一节。没有改代码或测试断言，也没有在本 PR 里修 investigation 代码
- 没有在真实环境里覆盖：多个开放候选时取最新者、交接事故不参与关联、并发投递的锁顺序、`resolved` 事故上的附加标记（列表页）。这些只由 PG 测试覆盖

## 费用

- DeepSeek 余额从 18.92 降到 7.90 CNY（07:46:33Z → 08:31:01Z），差 11.02 CNY
- 同一账户在此期间有其他会话在调用，所以 11.02 只是上界，不能全部算在本次头上
- 本次 4 个有模型调用的根 run 共 817019 输入 / 66752 输出 token。按 PR-A 的单价量级（195677 输入 token 约 0.04 CNY）推算，本次约 0.2 CNY，未核对

## 实验环境释放

- 三次 `fault restore` 都已校验（ConfigMap 回到 `1ee5c025…`）。读回 flagd ConfigMap：11 个 flag 的 `defaultVariant` 都是 `off`
- `occupation show` 回读为 `null`；`tmp/lab-ledgers/` 下没有 `.active-*` 和 `fault-active.json`
- 执行者启动的 web、worker 已按 PID 用 SIGTERM 停止，8080 不再监听；自建 PG 已停。这次没有用 port-forward

## 凭据

- 证据里没有 token、口令或 API key
- Prometheus 账号文件、webhook token 哈希、DeepSeek / LangSmith key 和 UI 口令只进了进程环境，没有打印，也没有写进日志
- summary.json 里的控制请求只写 `idempotency_label`，事件内容去掉了 `delivery_key`
