# F2 PR-E kind 串行会话（K1–K5）

- 日期：2026-10-24 14:23–16:13Z；kind-opspilot-m1 / otel-demo；代码 = `test/F2-acceptance` HEAD `b5276a9`（main `036fb10` + 独立验收测试，产品代码与 main 相同；运行期间未改代码，本目录为运行中与运行后新增）
- 合同：PR-E 预审 `~/.agent/handoffs/opspilot-prereview-f2-pre-2026-10-24.md` §2 K1–K5（E-R1–E-R10 按推荐）；[F2 合同](../../tasks/2026-10-17-f2-contract.md) U7、U9；[#305 合同](../../tasks/2026-10-24-305-successor-window-contract.md) §3；[#343 合同](../../tasks/2026-10-24-343-alert-joins-release-incident-contract.md) U3-A；[M2 授权](../../tasks/2026-10-24-m2-remaining.md) Q6、Q10、runbook
- 冻结摘要：[k1](k1-refire-followup/summary.json)、[k2](k2-query-stall/summary.json)、[k3](k3-pause-inflight/summary.json)、[k4](k4-cancel-inflight/summary.json)、[k5 第 1 轮](k5-release-then-alert/r1-alert-first/summary.json)、[k5 第 2 轮](k5-release-then-alert/r2-alert-first/summary.json)。判据由 [collect.py](collect.py) 从产品行只读计算；操作经 [ctl.py](ctl.py)。原始 ledger（控制请求前后状态、页面 HTML、代理日志、线程采样、harness 发布与故障记录）在主仓库 `tmp/lab-ledgers/f2-live/` 与 `tmp/lab-ledgers/f2-live-k5-r{1,2}/`，不入库；摘要里有逐文件 sha256
- 性质：有界真实运行。告警来自实验环境真实的 Prometheus 与 Alertmanager；发布是 F11 harness 的真实 `kubectl patch` 滚动；故障是 flagd 真实注入；模型是真实 DeepSeek `deepseek-flash`。不是生产证明
- 执行者：未参与 F2 任何实现的 Agent，没有改产品代码与测试断言
- **结论：五步确定性测试 26/26 通过；K1、K2、K3、K4 PASS；K5 两轮都是告警先开，按 #343 U3-A 与 Q6 停线，判 FAIL（未取到「发布先开、告警附加」的真实证据）。F2 `passes` 保持 `false`。**
- 2026-10-25 补记：K5 按裁决 A 接受，F2 `passes` 翻为 `true`，见 [F2 任务记录 PR-E 节](../../tasks/2026-10-17-f2.md)。本文件其余内容为当时的原始记录，不改。

## 系统做了什么

一套产品进程跑完五个场景：工作台 web（产品默认 30 s 自动 pass，既做自动恢复观察授权，也消费发布异常）、常驻 worker（容量 2、工具并行 2、积压上限 8，全部默认）、独立 Observer。worker 的 Prometheus 地址指向宿主卡顿代理（`scripts/f2_stall_proxy.py`），K2 之外代理全部直通。

- K1：故障两次注入。第一次 firing 开事故和 Run；恢复后 resolved 挂回；第二次 firing（新 `startsAt`）只附加一条事件，不开 Run；人工追问开出后继 Run，它的时间框按 #305 换到新 episode，查询到了新 episode 的数据。
- K2：代理卡住一条 `query_range`。网关 30 s 关连接、记 `timeout`，同轮另两条查询照常提交，Run 继续并发布。
- K3、K4：模型请求在途时事故级 pause / cancel。迟到的回复只落为 `late_result` 历史。pause 之后 resume，同一 Run 继续并发布；cancel 是终态。
- K5：真实发布滚动后 R+300 s 注入故障。两轮都是告警先建事故和 Run，发布异常随后附加到该事故（F11 U8 路径），全库因每次故障只有 1 个事故、1 个 Run。#343 要的反向顺序没有出现。

## 环境

- PostgreSQL 17.9：本 worktree 新建两个一次性实例。确定性测试用 56470；会话用 56471（`initdb`，trust，开连接日志），`python -m opspilot.schema migrate` 升到 `0021_release_incident_links`，`postgres_lab runtime-roles` 建 web、worker 登录角色，另建 `opspilot_observer` 的登录角色。会话后两个都已 `pg_ctl stop`。共享的 55561、55431 没有碰
- web：`python -m opspilot.web serve`，`127.0.0.1:8080`（Alertmanager webhook 目标），`env -i` 最小环境；`OPSPILOT_EVENT_TOKENS` 只含 alertmanager 与 release-deployer 两个哈希（K5 前为加载 release-deployer 哈希重启一次，见时间线）；`OPSPILOT_TARGET_IDENTITIES` 为已核实的 `m0-otel-20260909` 条目；`OPSPILOT_TOOL_PROFILE=otel-demo`；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS` 未设（产品默认 30）；实验 UI 账号 `f2live`（口令只以哈希进 web 环境）
- worker：`python -m opspilot.worker_main`，最小环境，`OPSPILOT_TRACE=lab`，Prometheus 经代理 `127.0.0.1:19091`、investigator 账号；版本 `prompt-replay-candidate-bef70649a3a8` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-68ed2ca8f5d6`；每 Run 100 次模型请求上限（产品默认）
- Observer：`python -m opspilot.observer`，observer 账号直连 Prometheus（不经代理）
- 端口转发：flagd 18013（K5 harness 用），会话后已停
- trace：LangSmith 项目 `opspilot-lab-f2-live-20261024`，运行前建立，回读 `trace_tier=longlived`
- 占用：开跑前 `occupation show` 为 null；14:25:08 claim（holder `lane-f2-e`，batch `f2-live`，至 18:15Z）；16:12 release，回读 null
- 开跑前读数（14:24:57Z）：`kind_lab.py health` ok；flagd 11 个 flag 全 off（ConfigMap `1ee5c025…`），无 `fault-active.json`，`ALERTS` 为空；frontend 2 分钟 span 速率 2.50/s（> 0，未重启 collector）；checkout PlaceOrder p95 170 ms；swap 余量 1347 MiB（低于 runbook 的 2048 MiB，如实记录），内存空闲 36%。各场景前读数在摘要 `lab_readings_before`：p95 在 K2 前 96.5 ms、K3 前 320 ms、K4 前 96.5 ms、K5 前 95 ms，没有进入 runbook 的 0.5–1.8 s 区间；swap 余量全程 1280–1347 MiB

## 确定性测试（步骤 1–5）

`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN=<56470> .venv/bin/python -m pytest tests/integration/test_f2_acceptance_postgres.py`，HEAD `b5276a9`：**26 passed，56.67 s**（开 kind 之前跑，未改断言）。

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:24:57 | 开跑前读数；14:25:02 DeepSeek 余额 94.77 CNY；14:25:08 claim |
| 14:25:39 | 起代理（count 0）、web、worker、Observer。web 首次因 UI 哈希里的 `$` 被 shell 展开没起来，改单引号后重启（只改启动脚本） |
| 14:26:22 | K1 第 1 次 `kind_lab.py fault inject`（`f2-live-k1-a`，校验通过） |
| 14:32:40 | 投递 1：firing，startsAt 14:31:54 → `created`，事故 `9373ca99`，Run A `5598fc8e` |
| 14:33:19 | 第 1 次 restore |
| 14:35:31 | Run A 发布（11 次模型请求、21 次工具）；随后 web 自动授权恢复观察，lifecycle `observing_recovery` |
| 14:37:40 | 投递 2：resolved → `resolved_attached` |
| 14:38:32 | 第 2 次 inject（`f2-live-k1-b`） |
| 14:45:41 | 投递 3：firing，新 startsAt 14:44:54 → `attached`，同一事故，1 条 event 输入，Run 仍 1 个 |
| 14:46:46 | 第 2 次 restore |
| 14:46:56 | 人工 `follow_up` → 200，代际 1，后继 Run B `9b8f040f`；自动观察会话被撤销 |
| 14:49:22 | Run B 发布 |
| 14:50:41 | 投递 4：resolved（14:44:54 那个 episode）→ `resolved_attached` |
| 14:51:2x | 代理按 PID 停掉，以 `--count 1` 重启（无在跑 Run） |
| 14:51:33 | K2 `POST /intake/ui` → 事故 `4a35618c`，Run `67d8ee31` |
| 14:51:58.577 | 第 3 轮的 `query_range` t0 被代理卡住；同一毫秒 t1 直通，14:51:59.79 t2 直通 |
| 14:52:28.583 | 代理记到客户端断开（距请求 30.0 s） |
| 14:57:59 | K2 Run 发布（22 次模型请求、54 次工具） |
| 14:59:38 | K3 intake → 事故 `d32dfce1`，Run `d55f42e7` |
| 14:59:40.80 | 第 1 次模型请求在途 1.06 s（恰一条未结算预约），事故级 `pause` → 200，代际 1，Run `paused` |
| 14:59:41.51 | 迟到回复写为 `late_result:step:ctx0:round-1:e1`（代际 0）；worker `control_denied` |
| 15:04:02 | `resume` → 200，代际 2，同一 Run 重新排队；15:08:29 epoch 2 发布 |
| 15:11:15 | K4 intake → 事故 `ed2f705e`，Run `21808ee2` |
| 15:11:17.66 | 模型请求在途 1.05 s，事故级 `cancel` → 200，Run 与事故 `cancelled` |
| 15:11:18.38 | 迟到回复写为 `late_result`（代际 0）；worker `control_denied` |
| 15:12:25 | 人工 close K1–K4 四个事故（为 K5 腾出目标，撤销观察授权） |
| 15:16:17 | web 重启，加载 release-deployer token 哈希（无在跑 Run，代码未变） |
| 15:17:10 | K5 第 1 轮发布 R（`f2-live-k5-r1`），watch，登记 201（观察 `8723c157`），15:17:33 授权 applied |
| 15:22:10 / 15:22:37 | paymentFailure 注入校验 / flagd 生效 |
| 15:26:10 | 发布观察结束 anomalous；15:27:30 前后对比 complete |
| 15:27:41 | 告警 firing → `created`，事故 `96d75ae1`（`related_incident_id` = K1 已关闭事故），Run `bf7befd1` |
| 15:27:51 | 发布消费 pass：link `attached` 到 `96d75ae1`，无新 Run |
| 15:30:37 | 该 Run 发布；15:37:38 harness 恢复故障并校验；15:43:39 resolved；15:43:49 人工 close |
| 15:49:20 | K5 第 2 轮发布 R（`f2-live-k5-r2`），登记（观察 `d6a362fb`），15:49:24 授权 applied |
| 15:54:20 / 15:55:10 | 注入校验 / flagd 生效 |
| 16:00:12 | 发布观察结束 anomalous |
| 16:00:42 | 告警 firing → `created`，事故 `d3696bb8`，Run `9749686d` |
| 16:01:35 / 16:01:52 | 对比 complete / 发布消费 link `attached` 到 `d3696bb8`，无新 Run |
| 16:03:55 | 该 Run 发布；16:10:10 harness 恢复故障并校验 |
| 16:11:48 | 按核实过的 PID 用 SIGTERM 停 worker、web、Observer、代理、flagd 转发，全部退出 |
| 16:12:24 | DeepSeek 余额 92.59 CNY；随后 `occupation release`，回读 null |

## 逐场景断言

### K1 告警再 firing → attached → 人工 follow_up（含 #305）— PASS

| 断言 | 实际 | 判定 |
|---|---|---|
| 投递顺序 | 全库 4 条投递，fingerprint 都是 `83f7541ae0105d42`：firing `created` → resolved `resolved_attached` → firing（startsAt 14:44:54，不同于 14:31:54）`attached` → resolved `resolved_attached` | PASS |
| 附加不开 Run | follow_up 前该事故只有 Run A；附加到 follow_up 之间没有 `intake_accepted`/`run_claimed` 事件；恰 1 条 `kind='event'` 输入，键集合在 R-C4 白名单内，text "Alertmanager alert 83f7541ae0105d42 fired again with startsAt 2026-10-24T14:44:54Z (status firing) on this incident's target; attached to this incident without a new Run."；identity 表 1 行（附加 episode 不写 identity） | PASS |
| C4-A 断言 2（follow_up 路径真实跑通） | Run A 已结论，走 E-R4 的「已结论」分支：`follow_up` 返回 200 并开后继 Run B。Run B `completed`，库内 8 次模型请求、18 次工具；无 `INCOMPATIBLE_STATE`、无交接事件。trace 读回 7 个 llm span（导出时丢 1 个 span，worker 日志 14:47:13），**7/7 的模型输入含该事件 text** | PASS |
| #305 W1-B 换框 | Run A 框 `[10-23 14:32:40, 10-24 14:32:40]`，锚点 14:31:54。Run B 框 `[10-23 14:46:56, 10-24 14:46:56]`：终点 = 创建时 DB 时刻截秒（deadline 16:46:56.06 − 7200 s），长 24 h；锚点 14:44:54 = 附加 episode startsAt；默认窗 `[13:44:54, 14:46:56]` | PASS |
| #305 覆盖附加 episode 的查询 `ok` | Run B 共 12 次 `metrics_range_query` 的窗口覆盖 14:44:54 且 `ok`、已采纳；第一条是第 1 轮的 checkout PlaceOrder 按 status_code 的 rate，`[13:44:54Z, 14:46:56Z]`。trace 中对应 tool span 的输出为已采纳、有数据 | PASS |
| 页面 | 附加后的事故页投递表标出 `re-fired episode` | PASS |

- E-R4 的另一分支（首个 Run 以 `REPORT_INVALID` 交接后 follow_up 重新排队，即 #304 原失败路径）本次没有自然出现，真实环境里没有覆盖；确定性测试 `test_step1_follow_up_after_an_attachment_to_a_handed_off_run_requeues_it` 覆盖
- E-R5 的 reopen 分支没有出现：follow_up 时事故是 `observing_recovery`，follow_up 直接成功并撤销了自动观察会话
- trace：[Run A](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-6457-16a6aa0cf3c6?poll=true)、[Run B（后继）](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-d5a9-cca45c511ec1?poll=true)

### K2 查询卡住（当前 main、并行 2）— PASS

| 断言 | 实际 | 判定 |
|---|---|---|
| 卡住那条按时落为 timeout | `6b2250f5-…-t0`：`timeout` / `TOOL_TIMEOUT` / `source_contact=possible`，未采纳；网关计时 30.010 s；请求到代理记到断开 30.015 s | PASS |
| 同轮兄弟保留 | 同一步 t1、t2 都是 `metrics_range_query` `ok`，证据 `committed=true`、已采纳 | PASS |
| 不自动重试 | 卡住的 operation 只有 1 条计费；Run 期间代理收到 41 次 `query_range`，等于 Run 的 41 次 `metrics_range_query` | PASS |
| 不静默挂起 | Run `completed`，发布，无交接 | PASS |
| 线程回收（E-R2） | `ps -M` 每秒采样：Run 前 8；本 Run 卡住前 10；卡住期间 12–13；超时后 5 s 回到 10；Run 结束后回到 8 | PASS |
| 工作台文案 | 事故页恰 1 处 "source may have received the request" | PASS |

- 报告 `gaps` 没有提到超时的 operation id（PR-A 那次提到了）。gaps 由模型撰写，预审 K2 断言没有要求，记为观察
- trace：[K2 Run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-05f0-6b7760ac4e69?poll=true)（23 个 llm span，库内 22 次模型请求；口径差未核查）

### K3 事故级 pause 在途 — PASS

| 断言 | 实际 | 判定 |
|---|---|---|
| 在途定位（E-R7） | 恰一条未结算模型预约 `8dd38d98` 持续 1.06 s 后 POST pause，200 | PASS |
| pause 生效、迟到写回只入历史 | 事故与 Run `paused`，代际 1；0.7 s 后回复写为 `late_result:step:ctx0:round-1:e1`，代际 0；该 Run 在 pause 之前没有任何推进的 step；无结论 | PASS |
| resume 后继续 | `resume` 200，代际 2，同一 Run 重新排队，epoch 2，19 次模型请求后发布 | PASS |
| 验收投影 | `outcome_from_durable` 含 `late_result_rejected` 与 `STALE_CONTROL_GENERATION`。投影在会话收尾 close 之后取，`human_interaction` 反映最后一次控制，不是 pause | PASS |

- 被拒那次请求的预约在 resume 后结算为 `unknown`（360 s），记为观察
- trace：[epoch 2 根](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-4ac3-c61dbe575dd6?poll=true)。epoch 1 的根 span 没有读回（worker 14:59:53 记一次导出失败、丢 2 个 span）；pause 判定只用库内行

### K4 事故级 cancel 在途 — PASS

| 断言 | 实际 | 判定 |
|---|---|---|
| 在途定位 | 预约 `d8db6feb` 在途 1.05 s 后 POST cancel，200 | PASS |
| cancel 生效、迟到写回只入历史 | Run 与事故 state `cancelled`（lifecycle 仍 open），代际 1；0.7 s 后回复写为 `late_result`（代际 0）；无结论 | PASS |
| 终态、无新 Run | 收尾 close 之前该事故仍只有这 1 个 Run，之后没有 `run_claimed` | PASS |
| 验收投影 | 含 `late_result_rejected` 与 `STALE_CONTROL_GENERATION`（取于 close 之后，同 K3） | PASS |

- 被取消那次请求的预约保持 `reserved`（`budget_reserved=1`），没有人结算，记为观察
- 无模型完成，未取 trace

### K5 #343 发布先开、告警附加 — FAIL（两轮都是告警先开，停线）

两轮条件相同：真实滚动 + 登记 + 授权（`otel-demo-checkout-release@e2ed7b69b96b`，M=1500），paymentFailure R+300 s，持续 900 s；web pass 产品默认 30 s，不控序。

| 轮次 | 发布结束 anomalous | 对比 complete | 告警 firing 投递 | 发布消费 | 结果 |
|---|---|---|---|---|---|
| r1 | 15:26:10 | 15:27:30 | 15:27:41 `created`（事故 `96d75ae1`、Run `bf7befd1`） | 15:27:51 link `attached` 到同一事故，无新 Run | 告警先开 |
| r2 | 16:00:12 | 16:01:35 | 16:00:42 `created`（事故 `d3696bb8`、Run `9749686d`） | 16:01:52 link `attached` 到同一事故，无新 Run | 告警先开 |

- 判定：K5 的三条断言（link `created`、告警首投 `attached` 到发布事故、发布事故上一条告警事件）都没有满足，因为前提「发布先开」没有出现。#343 U3-A 要求第一轮反序时重跑一轮，第二轮仍反序，按 Q6 停线，交 lead 判断
- 分诊：不是产品缺陷，也不是测试或脚本错误。两轮里发布消费都要等 Observer 的前后对比完成（结束后 76–83 s），再等下一次 30 s pass，所以结束后约 100 s 才消费；告警在结束后 30–91 s 到达。默认参数下这个实验环境里告警通常先到，与 #343 预审「按 U2 时间线大概率发布先」的推断不同
- 两轮都观察到：每次故障全库只有 1 个事故、1 个 Run，发布异常作为事件附加到告警事故（F11 U8 / PR-4b S4 路径），没有出现 #343 想消除的「同因两事故两 Run」
- #343 的发布先开路径只有确定性测试覆盖：`test_step1_alert_after_a_release_incident_joins_it_and_claims_it`（本次 26 例全过）
- trace（告警事故的 Run，非发布事故）：[r1](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-2095-be8a309742de?poll=true)、[r2](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dab51aa2-332e-4335-b385-98ef9c9fb91d/r/00000000-0000-0000-caee-9dc3d8cedb3f?poll=true)
- 待决（lead / 用户）：K5 是否接受「两种顺序都只有一个事故一个 Run」作为步骤 1 部署部分的真实证据，#343 反向顺序只靠确定性测试；或另排一次控序取证（#343 合同把控序列为不推荐的选项 C）；或其他做法。在此之前 F2 `passes` 不翻

## 观察到但不属本项判定

- trace 导出：worker 4 次 OTLP 导出失败、共丢 6 个 span（14:47:13、14:56:27、14:59:53、15:04:56），停机时又报丢 6 个。库内行完整，所有判定都以库内行为准，trace 只用来证明模型输入
- 停 Observer 时有一个恢复观察采样仍在途（K5 r2 告警事故的自动观察），按 grace 30 s 退出
- 停机时 Prometheus 里 `CheckoutPlaceOrderErrorRatioHigh` 仍在 firing（5 分钟 rate 窗口的尾巴，flagd 已 off），Alertmanager 之后的 resolved 投递会因 8080 已关闭而失败重试，不影响任何库
- K5 r1 告警事故的 `related_incident_id` 指向已关闭的 K1 事故（同 fingerprint，C3 关联语义）
- 关闭的事故触发了复盘生成（worker 日志有 postmortem 行），费用计入余额差

## 费用

- DeepSeek 余额 94.77 → 92.59 CNY（14:25:02 → 16:12:24），差 2.18 CNY。同一账户期间可能有其他会话调用，这只是上界
- 调查 Run 库内共 82 次已结算模型请求（K1 11+8、K2 22、K3 19、K5 r1 9、r2 13），另有 K3、K4 各 1 次被控制拒绝的在途请求，外加复盘生成

## 失败轮与中途处理

- K5 r1、r2：告警先开，保留为 FAIL 轮（见上）
- web 第一次启动失败（UI 哈希被 shell 展开），14:26 前修启动脚本后重启；当时没有任何事故或请求
- K5 第一次调用 harness 时 zsh 没有拆分参数，argparse 直接报错，没有写开始记录、没有外呼；同名轮次重新执行
- K5 r1 授权首次因 `PYTHONPATH` 缺失报 `ModuleNotFoundError`（未连库），5 s 后重试 applied（R+23 s，故障排在 R+300 s）
- 收集器初版有三处读数错误（K1 trace 匹配方式、K2 代理计数没有限定 Run 时间段、K3/K4 投影字段取错时点），修正后重算；摘要是修正后的版本。K5 两份摘要事后去掉了 `incidents_since_release[].intake_key`（告警 delivery 哈希，避免密钥扫描误报），collect.py 已同步不再读取

## 实验环境释放

- K1 两次 `kind_lab.py fault restore` 都校验通过；K5 两轮由 harness `finally` 恢复并校验（`restore_config_verified`）。结束时 flagd 11 个 flag 全 off，ConfigMap `1ee5c025…`；`tmp/lab-ledgers/` 下没有 `fault-active.json` 与 `.active-*`
- `occupation show` 回读为 null
- 本次启动的 web、worker、Observer、卡顿代理、flagd 转发都按核实过的 PID 与命令行用 SIGTERM 停止，8080、19091、18013 不再监听；两个临时 PG 已停，数据留在本 worktree 的 `tmp/f2-e/`
- checkout 保留最后一次发布的环境变量值，与 U2、PR-3、PR-4b 相同

## 凭据

- 证据里没有 token、口令或 API key。UI 口令、事件 token 只以哈希进 web 环境；Prometheus 账号、模型与 LangSmith key 只进进程环境或由进程从私有 env 文件读取，没有打印
- 控制与授权请求在摘要里只写 `idempotency_label`；事件内容去掉了 `delivery_key`
