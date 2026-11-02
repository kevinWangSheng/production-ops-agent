# #358：发布事故附加告警后，后继 Run 换框（#347 的真实运行证据）

- 日期：2026-11-01（UTC 23:37–23:58），kind `opspilot-m1`（colima `m1-kind`），OTel Demo 2.0.2，与 F14 第 3 步同一个 kind 窗口，排在 F14 之后。
- 依据：issue #358；裁决 Q11 与 #358 可逆默认（`~/.agent/handoffs/opspilot-rulings-2026-11-01-m3-optimization.md`）；PR-5c 窗口做法（[f11-acceptance-live/run.md](../f11-acceptance-live/run.md) 发布 B）。
- 代码：分支 `chore/358-release-successor-live`，窗口内运行 `55d8dbb6`（基于 main `0089197e`）；产品代码未改。收集前修正了 collector 的「覆盖 episode」判据，见下文「判定」。
- 冻结摘要：第 1 轮 [summary.json](summary.json)，第 2 轮（开 trace）[round-2/summary.json](round-2/summary.json)（都由 `scripts/r358_live.py collect` 生成）。原始 ledger 在主仓库 `tmp/lab-ledgers/358-live/`（drive 记录）与 `tmp/lab-ledgers/r358-1/`（harness 的 release/watch/webhook/fault），不入库；`358-live` 下各文件的 sha256 见摘要 `raw_ledger`。
- 性质：有界真实运行。发布是 F11 harness 的真实 `kubectl patch` 滚动；登记走 `POST /intake/version-changes`（release-deployer token）；授权与发布异常消费由 web 进程的自动 pass 完成；告警由真实 Alertmanager 投递；cancel 与 new_run 经工作台控制路由 `POST /incidents/{id}/control`；模型是真实 DeepSeek Flash。这不是生产证明。

## 第 2 轮（2026-11-02，开 LangSmith trace）

第 1 轮（下文各节）没有导出 trace。用户随后把 `LANGSMITH_API_KEY` 加进本窗口专用的 key 文件，lead 安排按同一做法重跑 1 轮，worker 开 `OPSPILOT_TRACE=lab`、`LANGSMITH_PROJECT=opspilot-lab-358`。key 只由进程自己读取（`worker_main._configure_tracing`，lab 摘要回读用 `scripts.lab_evidence.langsmith_client`），执行者没有读取或打印。worker 启动日志为 `trace export enabled mode=lab`，没有失败码。

- 确定性断言：`box.checks` 7 项全部为 true，**与第 1 轮相同，没有变化**。
  - 后继框 `[2026-11-01T04:50:06Z, 2026-11-02T04:50:06Z]`，终点等于创建时刻（deadline − 7200 s）。
  - 锚点 `released_at` `04:39:35Z`。
  - 首个 Run 的框终点等于 consumed_at `04:49:13Z`。
  - 默认窗正确，`release_observation` 事实不变。
- 附加 episode：delivery 1，`attached`、`firing`，告警 `startsAt` 04:49:54Z，收到时间 04:50:05Z，晚于首个框终点。
- 后继 Run 对附加区间的查询：`observed`。14 次查询越过首个框终点进入 episode，其中 12 次 ok，2 次 no_data（第 1 轮是 20 次，ok 18；查询数取决于模型，判定不变）。
- 离线网关判定：后继第一条覆盖查询窗口 `[03:39:35Z, 04:50:00Z]` 在首个 Run 输入上为 `denied / WINDOW_OUT_OF_SCOPE`，构造 0 个请求；在后继输入上放行。与第 1 轮结论相同。
- 网关记录：
  - 首个 Run 有 12 条视图（ok 9、RESULT_TOO_LARGE 2、no_data 1），窗口终点都不晚于 04:49:13Z。它的 `tool_operations_used` 为 14，比视图多 2 条，应是 cancel 时尚未提交的调用（未逐条核实）。
  - 后继 39 条：ok 33、no_data 4、RESULT_TOO_LARGE 1、INVALID_PARAMS 1，没有 WINDOW_OUT_OF_SCOPE。
- LangSmith trace（`trace.read_back = found`，项目 `opspilot-lab-358`，保留期 longlived）：
  - 首个 Run `ace08c55`：[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/da173c3c-d762-4647-be2a-807d57884622/r/00000000-0000-0000-f50b-ce1f9324f50b?poll=true)，6 个 llm、14 个 tool 子 span，与库里 6 次模型请求、14 次工具调用一致。
  - 后继 Run `a1c283b9`：[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/da173c3c-d762-4647-be2a-807d57884622/r/00000000-0000-0000-f48a-899c59041f31?poll=true)，21 个 llm、39 个 tool 子 span，与库里 21 次模型请求、39 次工具调用一致。
  - issue 验收的 trace 链接一项由本轮满足。
- 时间线（UTC，2026-11-02）：
  - 04:38 持 `lockf ~/.agent/locks/kind-lab` 开窗；frontend span 速率 2.29/s，checkout error ratio 0，`ALERTS` 为空，flag 全部 off。
  - 04:38:52 读开窗余额；04:39 起一次性 PG（端口 56386，新库 `r358b`）、web、Observer、worker 和两个端口转发。
  - 04:39:34 建 silence；04:39:35 发布 R（`r358-2-20261102T043934Z`）；04:39:46 webhook 201（观察 `1e8f631a`）。
  - 04:44:55 flagd 确认 paymentFailure 100%。
  - 04:49:13 首个 Run `ace08c55` 出现在事故 `99899a8e` 上，同一秒删除 silence。
  - 04:50:05 告警 attached；04:50:06 cancel（代际 0→1）和 new_run（1→2），后继 `a1c283b9`。
  - 约 04:54:45 后继 Run 完成（published，21 次模型请求、39 次工具调用）；首个 Run `cancelled`（6 次、14 次）。
  - 05:00:08 恢复故障。第一次 SIGINT 误发给了后台 zsh 包装进程，约 4 分钟后才发现，改发给 harness 的 python 进程（PID 57795），由 `finally` 恢复。所以 fault 比计划多开了约 5 分钟，此时两个 Run 都已结束。恢复后 flag 全部 off，没有 `fault-active.json`，silence expired。
  - 05:00:20 SIGTERM 停 worker（trace 已 flush），随后停 web、Observer 和端口转发，都按 PID 核对过。
- 代码：窗口内运行 `0fc0f8d8`，加上本提交里 `scripts/r358_live.py` 的 `trace_links` 改动（按 metadata `run_id` 回读两个 Run 的根 run，并统计子 span）。round-2 摘要里的 `code_revision` 记的是 `0fc0f8d8`。
- 环境与第 1 轮相同（owner DSN、同一注册表、同一事件 token 哈希），只是数据库换成新库 `r358b`（一次性 PG，端口 56386，在 poa-358 的 tmp 下）。
- swap 空闲量：开窗 683/561 MiB，Run 期间 322–330 MiB，收尾时 291 MiB，低于 lead 设的约 300 MiB 线，已当即报告。当时两个 Run 都已结束，剩下的收集很轻，没有停其他应用。

## 结论（第 1 轮，2026-11-01）

1 轮即满足 Q11；为补 trace，又跑了上面的第 2 轮。

- 主证据（确定性）：`box.checks` 7 项全部为 true。
  - 后继框终点 `2026-11-01T23:50:06Z` 等于后继创建时刻（deadline − 7200 s，取整到秒），框长 24h。
  - 锚点仍为 `released_at` `23:38:22Z`（`release_released_at`），默认窗 `[22:38:22Z, 23:50:06Z]`。
  - 首个 Run 的框终点等于 consumed_at `23:49:29Z`。
  - `release_observation` 事实逐字节不变。
  - 期望值用产品自己的 `ReleaseAnchor` 重算。
- 附加 episode：delivery 1，`attached`、`firing`。告警 `startsAt` 为 23:48:54Z，收到时间 23:50:04Z，晚于首个 Run 的框终点。
- 后继 Run 对附加区间的查询：`observed`。20 次 metrics_range_query 的窗口越过首个 Run 的框终点、伸进 episode（终点最晚 23:50:00Z），其中 18 次 status `ok`，2 次 `no_data`。
- 「首个 Run 上会被拒」：在首个 Run 存储的输入上，用后继的第一条覆盖查询窗口 `[23:40:00Z, 23:50:00Z]` 离线判定，产品网关给出 `denied / WINDOW_OUT_OF_SCOPE`，构造的请求数为 0。同一窗口在后继输入上放行（本地替身回 `no_data`）。全程不联网、不调模型。
- 网关记录佐证：首个 Run 11 次工具调用，没有一次的窗口越过 23:49:29Z；后继 38 次，全部按状态、原因、窗口列在 `gateway_records`。
- LangSmith trace：第 1 轮**不可用**（见下文「trace」），第 2 轮补上了，链接见上文。

## 时间线（UTC）

| 时间 | 动作 | 结果 |
|---|---|---|
| 23:37:07 | `occupation claim`（holder r358） | — |
| 23:37:38–23:41 | 启动 Observer、worker、web；转发 Alertmanager 19093、flagd 18013 | web 第一次启动时 UI 账号哈希含 `$`，被 shell 展开成空，登录 401。改为不经 shell 读取，按 PID 停掉后重启（只影响登录，没有产品动作） |
| 23:38:22 | 建 silence（只匹配 `alertname=CheckoutPlaceOrderErrorRatioHigh`，2h）；harness 发布 R（`r358-1-20261101T233822Z`） | — |
| 23:38:35 | watch 完成 | 新旧 RS/Pod 身份齐 |
| 23:38:47 | webhook | 201 created，观察 `4b828252` |
| 23:43:22 | 计划注入 paymentFailure 100%（R+300，`--method Float`） | ConfigMap 已写；flagd 在 kubelet 同步后确认 |
| 23:49:31 | drive 看到发布异常开出的事故 `3effeccc` 与首个 Run `91d073b8`，同一秒删除 silence | HTTP 200 |
| 23:50:04 | 告警投递，附加到该事故 | delivery 1 `attached` |
| 23:50:06 | cancel（代际 0→1），new_run（1→2） | 后继 Run `bab51630` |
| 约 23:55:34 | 后继 Run 完成 | `completed`，15 次模型请求、38 次工具调用；首个 Run `cancelled`，3 次模型请求、11 次工具调用 |
| 23:56:00 | web 自动为该事故授权恢复观察（#184） | 审计 `auto_authorize_observation` |
| 23:56:58 | 对 harness fault 进程（PID 16577，本任务自起）发 SIGINT，`finally` 恢复 | flagd 回到 `off`；lab 11 个 flag 全部 off；没有 `fault-active.json` |
| 23:57:27 | 按 PID 与 cwd 核对后 SIGTERM 停 web、worker、Observer 与两个端口转发；`occupation release` | 回读 null；silence 状态 expired |

## 环境

- PostgreSQL 17：F14 窗口里起的一次性实例（端口 56385，trust），在其中新建数据库 `r358`，`opspilot.schema migrate` 升到 `0024_trace_outbox`。与 PR-5c 不同，web、worker、Observer 都用 owner DSN，没有建 runtime 登录角色。这只影响数据库权限的真实性，与本项的框和锚点判据无关。
- web：`env -i`，变量名见下；`OPSPILOT_EVENT_TOKENS` 只有哈希，来自 lab 的 `workbench-alertmanager.env` 与 `workbench-release-deployer.env`；目标身份文件是 lead 核实过的 `m0-otel-20260909` 注册表（含 alert match labels）；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=30`；`OPSPILOT_RUN_SECONDS` 不设，即 7200。
- worker：investigator 账号；`OPSPILOT_ENV_FILE` 指向用户为本窗口专设的模型 key 文件，只由进程自己读取，执行者没有读取或打印；不设 `OPSPILOT_TRACE`。版本 `prompt-replay-candidate-bef70649a3a8` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-a3a95a0f888e`。
- 进程启动时记下的环境变量名（不含值）：
  - web：`HOME, LC_CTYPE, OPSPILOT_EVENT_TOKENS, OPSPILOT_HEALTH_BIND, OPSPILOT_SELF_HEAL_INTERVAL_SECONDS, OPSPILOT_TARGET_IDENTITIES, OPSPILOT_TOOL_PROFILE, OPSPILOT_UI_USERS, OPSPILOT_WEB_DSN, PATH, PYTHONPATH`
  - worker：`HOME, LC_CTYPE, OPSPILOT_ENV_FILE, OPSPILOT_HEALTH_BIND, OPSPILOT_OTEL_PROMETHEUS_PASSWORD, OPSPILOT_OTEL_PROMETHEUS_USERNAME, OPSPILOT_TOOL_PROFILE, OPSPILOT_WORKER_DSN, PATH, PYTHONPATH`
  - Observer：`HOME, LC_CTYPE, OPSPILOT_HEALTH_BIND, OPSPILOT_OBSERVER_DSN, OPSPILOT_OBSERVER_ENV_FILE, OPSPILOT_OBSERVER_PROMETHEUS_URL, PATH, PYTHONPATH`
- 实验目录：worktree 的 `tmp/m1-kind-lab` 以符号链接指向主仓库同名目录，收窗后删除。
- 宿主读数：开跑前 frontend span 速率 1.55/s，checkout p95 48 ms，error ratio 0，`ALERTS` 为空。swap 空闲量 1416–1456 MiB。窗口内没有跑全量测试，也没有起 Codex。

## trace

worker 在 `OPSPILOT_TRACE=lab` 时，会从 `OPSPILOT_ENV_FILE` 读 `LANGSMITH_API_KEY`（`opspilot/worker_main.py:716-721`）。本窗口的 key 文件规则是只允许进程读模型 key，lead 要求凡是要从 env 文件取 LangSmith key 就先停下，不读。所以第 1 轮的 worker 没有开 trace，摘要 `trace.read_back` 为 `not_exported`。用户之后把 LangSmith key 加进同一个专用文件，第 2 轮开了 trace，`round-2/summary.json` 的 `trace.read_back` 为 `found`，两个 Run 都有根 run 和子 span（见上文第 2 轮）。

## 费用（E1）

每轮约 2 个真实 Run：首个 Run 被中途 cancel，加上后继 Run。预算与 F14 第 3 步的 12 次分开计（lead 已接受）。第 1 轮实际 18 次模型请求（3 + 15），余额差见任务记录。第 2 轮 27 次模型请求（6 + 21），执行者经 `scripts.holmes_baseline.run.model_key()` 读余额（只打印 balance_infos）：137.13 CNY（2026-11-02 04:38:52Z）→ 136.28 CNY（05:01:11Z），差 0.85 CNY。余额可能延迟入账，这是收窗后约 6 分钟的读数，同账号同期如有其他调用，这个差额只是上界。

## 判定

- collector 的「覆盖 episode」判据在收集时修正过：最初要求查询终点不早于告警的收到时刻（23:50:04Z），但后继的框在 23:50:06Z 就结束了，模型的查询终点最晚是 23:50:00Z，按这个判据会得出「未观察到」。现在的判据是：查询终点越过首个 Run 的框终点（首个 Run 因此会拒），并晚于告警 `startsAt`（伸进 episode）。离线测试 `tests/test_358_release_successor_live_impl.py` 仍全部通过。
- 摘要里的 `successor_created_at` 带数据库会话时区偏移（`-08:00`），时刻本身与框终点一致。
