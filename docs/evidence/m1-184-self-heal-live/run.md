# #184 第 3 步：自动恢复观察 kind 真实运行（两场景）

- 日期：2026-10-13 05:46–06:45Z；分支 `feature/F6-184-self-heal-step3`，产品代码 = `main` `df3863e`（第 1、2 步已合并，本分支不改 `opspilot/`）
- 合同：[任务记录](../../tasks/2026-10-12-184-self-healing-recovery.md) r6 + 第 3 步决定（[预审](../../tasks/2026-10-12-184-step3-prereview.md) D3-01–D3-15）
- 冻结摘要：[s1/summary.json](s1/summary.json)、[s2/summary.json](s2/summary.json)（含取证/判据脚本与实验 profile 的 sha256，`artifacts`）；原始 ledger（库记录、F6 投影全量、页面 HTML、完整判据、watch 时间线）不入库，本机 `tmp/184-step3/ledgers/`，哈希见各摘要 `raw_ledger`（ADR-0006）
- 判据：[verify.py](verify.py) 由独立 Codex 按合同与公开接口编写（实现者未改）；取证：[collect.py](collect.py)（lead）
- 类别：有界真实软件环境运行 + 真实模型调用。故障只由 kind lab 的 flagd 钩子注入与恢复，告警由真实 Prometheus/Alertmanager 产生；不是生产证明

## 环境

- kind `opspilot-m1`（OTel Demo chart 0.37.8、Alertmanager v0.28.1）；运行前 `kind_lab.py health` 全部 ok、所有 flag 为 off；与另一会话（production-ops-agent-00）确认实验环境空闲后才注入。
- PostgreSQL 17.9 专用空库（127.0.0.1:55497，`make migrate` → `0013_self_heal_authorization`）；运行角色 `opspilot_web_lab` / `opspilot_worker_lab` / `opspilot_observer_lab`（组角色 0003/0011 授权，未改）。
- 三个产品进程：`opspilot.web serve`（自动观察循环用默认 30 s，未设 `OPSPILOT_SELF_HEAL_INTERVAL_SECONDS`；身份文件沿用 M1-04 的 `m0-otel-20260909` 条目，`cluster_uid` 与集群 `kube-system` UID 核对一致）、`opspilot.worker_main`（`otel-demo` profile，`OPSPILOT_TRACE=lab`）、`opspilot.observer`（`env -i` 启动，只带 Observer 自己的 DSN 与 Prometheus 账号）。
- HealthProfile：S1 用随产品发布的 `otel-demo-checkout.json`（revision `otel-demo-checkout@b72bbe2e30be`）；S2 用只把 `session.deadline_seconds` 3600 → 1200 的 [实验变体](otel-demo-checkout-deadline-1200.json)（`@dc7d44caa04d`，D3-07：保证期限先于 40 次采样耗尽），web 换 profile 重启后才注入 S2。
- trace：round `184-step3-20261012`，project `opspilot-lab-184-step3-20261012`，运行前经 `lab_evidence.prepare_lab_project` 建立并回读 `trace_tier=longlived`。

## S1：harness 恢复，无人点击 → `resolved`

| 时间（UTC） | 事件 |
|---|---|
| 05:46:54 | `fault inject --experiment-id m184-s1-20261012`（paymentFailure 100%，`verified: true`） |
| 05:54:53 | 工作台收到 firing（`startsAt` 05:53:54），建事故 `cb7ef8c8-…` 与 Run `86c9c662-…` |
| 05:54:55–05:56:14 | worker 调查：4 次模型请求、9 次工具调用，`report_available`，结论 `supported`（payment `Charge` 「Invalid token」导致 checkout PlaceOrder 失败） |
| 05:56:36 | web 循环自动授权：`auto_authorize_observation` / `system`，代际 0 → 0，会话 `authorization_source=automatic`、`target.revision=null`；事故 `observing_recovery` |
| 05:56:38 | 看到自动会话后 `fault restore`（ConfigMap 回到 `1ee5c025…`，`verified: true`） |
| 05:57–06:00 | 样本 1–3 `degraded`；样本 4 `healthy` 但窗口起点早于授权（`window_before_authorization`），不计入 |
| 06:00:53 | 告警 resolved → `resolved_attached`（不改生命周期，M1-04 决定 C） |
| 06:01:38–06:11:39 | 样本 5–15 连续 `healthy`/`confirmed` |
| 06:11:40 | ending `recovery_confirmed`，`observing_recovery → resolved` |

判据（verify.py，11/11 通过；冻结摘要中的判据于 06:50:37 重读，与 06:12 首次结果一致）：C1 发布前提、C2 唯一授权决定与会话、C3 system 审计不推进代际、C4 无任何人工 control、C5 automatic + revision null、C6 F6 来源 automatic 且 `permissions` 无 `human_control`、`actions` 无人工动作、C7 离线重放一致（`healthy`，健康窗 601 s ≥ 600 s）、S1-1 会话 `completed`/`recovery_confirmed`、S1-2 事故 `resolved` 与 F6 一致、S1-3 F6 `recovery_confirmed`、S1-4 事故页显示 "recovered, no remediation recorded" 与来源 automatic。trace：[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f23200d9-3795-484f-9aae-415924c67a36/r/00000000-0000-0000-6ffd-ae52ba7bfed9?poll=true)，`read_back = found`，子 run llm 4、tool 9，与库中 `budget_spent = 4`、`tool_operations_used = 9` 一致。

## S2：故障持续 → 到期保持 `open` 并交接

| 时间（UTC） | 事件 |
|---|---|
| 06:12:26 | `fault inject --experiment-id m184-s2-20261012`（`verified: true`） |
| 06:17:53 | 新 firing（`startsAt` 06:16:54，同 fingerprint、新身份），建事故 `6c915943-…` |
| 06:17:55–06:20:34 | 调查：8 次模型请求、16 次工具调用，`report_available`，结论 `partial` |
| 06:20:46 | 自动授权（同 S1 形态），deadline 06:40:46（1200 s） |
| 06:21:46–06:39:51 | 样本 1–19 全部 `degraded`（故障与流量一直存在） |
| 06:40:46 | ending `deadline_expired`，transition `observation_ended_unconfirmed`，`observing_recovery → open` |
| 06:41:09 | 首次 verify（16/16）与冻结后执行 restore |
| 06:41:30 | `fault restore`（fault-log `at` 06:41:30.153Z，`verified: true`） |
| 06:44:51 | 恢复后约 3 分钟（web 循环多轮）只读复核：两条决定各一个会话，S2 事故 `open`，未补开观察；`kind_lab.py health` ok，所有 flag off（原始输出 `tmp/184-step3/s2-post-restore-check.txt`，sha256 `7f2ba1d4…d82d`，不入库） |
| 06:50:43 | 独立审查后重新 verify 并重新冻结（web 以同一 profile 只读重启，worker/Observer 未启动）：S2 仍 16/16，其中 S2-9（同代际无第二个自动会话）此时读于 restore 之后；摘要的 `verdicts.checked_at` 即此次 |

判据（verify.py，16/16 通过）：C1–C7 同上（重放一致，`degraded`）；S2-1 会话 `expired`/`deadline_expired`；S2-2 事故 `open` 与 F6 一致；S2-3 恰好一条未确认 ending、不早于冻结 deadline；S2-4 F6 `recovery_confirmed=false`、`observation_ended=true`、`human_interaction=handoff`，`handoff_reasons` = `DEADLINE_EXPIRED`、`DEGRADED_SIGNAL:error_ratio`、`DEGRADED_SIGNAL:dependency_error_ratio`、`CONTINUED_DEGRADATION`、`OBSERVATION_UNCONFIRMED`；S2-5 不是次数耗尽（19 < 40）；S2-6 自动授权决定仍是 `authorized`（不是授权失败交接）；S2-7 采样期间信号与流量可用、无 unknown；S2-8 页面无恢复成功文案；S2-9 同代际无第二个自动会话。trace：[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f23200d9-3795-484f-9aae-415924c67a36/r/00000000-0000-0000-8ef0-8d0f9164cbf8?poll=true)，`read_back = found`，子 run llm 8、tool 16，与库一致。

## 费用

同一 DeepSeek key 余额三次读数：202.15（05:48:53）→ 202.06（06:13:02）→ 201.07（06:49 后），本轮合计 1.08 CNY。窗口内 worker 的模型调用：S1、S2 两次调查 Run（4 + 8 次请求）与两份自动复盘草稿（结论发布后由 worker 生成，各 1 次请求，见摘要 `postmortem_attempts`）。S1 复盘 06:12:07 开始、06:13:07 结束，跨过中间读数，所以不按场景拆分，只报合计。窗口内同一 key 无其他已知调用（未核查其他会话）。

## 观察到但不属本步判定

- Observer 权限实测为 `read_only` + `database_temp`：后者是 PostgreSQL 对 PUBLIC 默认授予的 `TEMP`，由部署决定是否收回（docs/development.md 0011 段已写明），不是本切片引入。
- 自动授权与人工登记一样不写 subject event（D2-11，按设计）；事故页靠刷新快照看到生命周期与来源。
- S1 样本 4 在恢复约 4 分钟后即判 `healthy`（5 分钟评估窗仍含部分故障时段），但因窗口早于授权未计入；健康连续期从样本 5 起算，持续 601 s 后确认。这是 HealthProfile 阈值的既有行为，不属本步。
