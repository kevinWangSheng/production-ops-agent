# F11 PR-4b 发布异常消费：kind 四场景有界真实运行（批次 `f11-pr4b-live`）

- 日期：2026-10-24（UTC 09:16–11:21），kind-opspilot-m1 / otel-demo；代码 `5943b0f`（`feature/F11-pr4b-release-consume`，运行期间工作区无代码改动）
- 合同：[PR-4 合同](../../tasks/2026-10-21-f11-pr4-contract.md) §7、P4-07、§9 r2/r3；[任务记录 PR-4b](../../tasks/2026-10-12-f11-post-release.md#pr-4b-消费关联v6-与续开)；[M2 授权](../../tasks/2026-10-24-m2-remaining.md)（Q6、runbook）
- 冻结摘要：每个场景一份，[s1](s1-healthy/summary.json)、[s2](s2-healthy-adfailure/summary.json)、[s3](s3-regression-alert-silenced/summary.json)、[s4](s4-regression-alert-on/summary.json)。原始 ledger 在主仓库 `tmp/lab-ledgers/pr4b-*`（不入库），各文件 sha256 见摘要 `raw_ledger`
- 性质：有界真实运行。发布是 harness 的真实 `kubectl patch` 滚动；登记走真实 `POST /intake/version-changes`；Observer、web 消费 pass、worker 都是本分支产品进程；故障是 flagd 真实注入；告警来自实验环境真实的 Prometheus 与 Alertmanager；模型是真实 DeepSeek `deepseek-flash`。不是生产证明；F11 `passes` 不变
- 执行者：未参与 PR-4b 实现的 Agent（只跑运行、写证据，没有改产品代码）
- **结论：四个场景全部 PASS，没有失败轮，没有用到 Q6 重跑。** 另发现一个不属本 PR 判定的既有缺陷（自动恢复观察在发布观察过的目标上报错），见「观察到但不属本项判定」

## 系统做了什么

每个场景都是同一条链：harness 改 checkout 的一个环境变量触发真实滚动（R = patch 回执）→ harness 用 release-deployer token 调工作台的发布登记接口 → 以 web 登录角色调用 `authorize_release`（正式清单 `otel-demo-checkout-release@e2ed7b69b96b`，M=1500；PR-5 之前没有授权页面）→ Observer 按 cadence 采样、判定，异常时写 anomaly ending 并做 before/after 对比 → web 后台 pass 每 30 s 消费 anomalous ending：目标上没有开放事故就新建事故和 Run，有就只追加一条事件 → worker 用 v6 输入跑调查。

## 环境

- PostgreSQL 17.9：本 worktree 新建临时实例（initdb，端口 55741，trust，开启连接日志），`python -m opspilot.schema migrate` 升到 `0021_release_incident_links`；`postgres_lab runtime-roles` 建 web、worker 登录角色，另建 `opspilot_observer` 的登录角色。运行后 `pg_ctl stop`。共享的 55561/55431 未碰
- web：`python -m opspilot.web serve`，`127.0.0.1:8080`（Alertmanager webhook 目标），最小环境；`OPSPILOT_EVENT_TOKENS` 只含 alertmanager 与 release-deployer 两个哈希；`OPSPILOT_TARGET_IDENTITIES` 为 lead 核实的 `m0-otel-20260909`；`OPSPILOT_TOOL_PROFILE=otel-demo`；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=30`（S4 的顺序控制见下）
- worker：`python -m opspilot.worker_main`，最小环境，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号；版本 `prompt-replay-candidate-bef70649a3a8` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-68ed2ca8f5d6`（v6 对应的 prompt_revision）；防循环上限为产品默认每 Run 100 次模型请求
- Observer：`python -m opspilot.observer`，observer 账号直连 Prometheus
- 端口转发：flagd 18013、Alertmanager 19093，均为本次启动，运行后停止
- trace：LangSmith 项目 `opspilot-lab-f11-pr4b-live-20261024`，运行前建立，回读 `trace_tier=longlived`
- 占用：`occupation show` 为 null 后 claim（holder `lane-f11-pr4b`，09:24:38Z），11:20Z release，回读 null
- 开跑前发现 flagd `paymentFailure` 自 2026-10-23T08:43Z 起一直是 100%（`f2-kill-resume-332` 注入后未恢复，fault-log 只有 inject），`CheckoutPlaceOrderErrorRatioHigh` 在 firing。用 `kind_lab.py fault restore --experiment-id f2-kill-resume-332` 恢复并校验（09:16:16Z，ConfigMap 回到 `1ee5c025…`），已告知 lead；等告警清除、p95 回落后才 claim
- 宿主读数：开跑前 checkout PlaceOrder p95 94–97 ms、frontend span 速率 2.15–3.0/s（> 0，未重启 collector）、内存空闲 50%、swap 余量 1.5 GiB（低于 runbook 的 2048 MiB，如实记录）；S4 前 swap 余量降到 367 MiB，内存空闲 39–40%，p95 48–97 ms，未见 runbook 所述 0.5–1.8 s 的升高，照常开跑；结束后 p95 90 ms、swap 余量 1.4 GiB、内存空闲 63%

## 时间线（UTC）

| 时间 | 动作 | 结果 |
|---|---|---|
| 09:24:58 | 失败轮 `pr4b-s1-healthy`（保留，不算通过）：harness 写入开始记录（`run-starts.jsonl`，record `03a8605b…`）后，`release` 读 checkout 时报 `EXTERNAL_CALL_FAILED`（本 worktree 缺 `tmp/m1-kind-lab/kubeconfig`），`watch` 报 `FileNotFoundError` | 没有发生发布（Deployment generation 仍为 17）、没有注入、没有登记或授权，未生成 ledger 目录；属环境前提，补齐 kubeconfig 后按规则换新轮次名 `pr4b-s1-healthy-b` |
| 09:25:21 | S1 发布（R，轮次 `pr4b-s1-healthy-b`），watch、登记 201、授权 applied | 观察 `f0b94758` |
| 09:54:06 | S1 观察结束 | healthy（`healthy_confirmed`，12 个样本） |
| 09:55:41 | S2 发布，排 adFailure R+300 | 观察 `0f692cbd` |
| 10:01:01–10:16:11 | adFailure 生效至恢复（flagd 确认 variant） | ad 与 frontend 出现错误 span，checkout 错误为 0 |
| 10:23:42 | S2 观察结束 | healthy |
| 10:25:38 | 建 Alertmanager silence（`alertname=CheckoutPlaceOrderErrorRatioHigh`，2 h） | active |
| 10:25:42 | S3 发布，排 paymentFailure R+300 | 观察 `7bcaa45b` |
| 10:30:44 | paymentFailure 生效 | — |
| 10:37:09 | S3 观察结束 | anomalous（`anomaly_detected`） |
| 10:38:4x | 告警 firing，被 silence 抑制（Alertmanager 状态 `suppressed`） | 告警投递 0 |
| 10:38:44 | 消费 pass：对比 `complete` 已在 → 新事故 `eaef0fb4` + Run `0c3d6233` | link `created` |
| 10:44:05 | Run 发布报告 | `completed`，18 次模型请求、37 次工具调用 |
| 10:45:45 | 故障恢复；10:46:01 人工 close `eaef0fb4`（UI control，generation 0→1） | 为 S4 腾出目标 |
| 10:48:59 | 告警清除；10:49:06 删除 silence | — |
| 10:49:20 | web 以 `OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0` 重启（消费 pass 关） | 见下「S4 顺序控制」 |
| 10:53:34 | S4 发布，排 paymentFailure R+300 | 观察 `96e375ef` |
| 10:59:23 | paymentFailure 生效 | — |
| 11:02:35 | S4 观察结束 | anomalous；对比 11:03:43 `complete` |
| 11:04:41 | Alertmanager 投递 firing → `created`，告警事故 `60eff8f1` + Run `2398b632` | — |
| 11:04:55 | web 以间隔 30 重启 | — |
| 11:05:25 | 消费 pass：追加到 `60eff8f1` | link `attached`，1 条事件，无新 Run |
| 11:08:30 | 告警事故 Run 发布 | `input_watermark=1`（事件已在安全边界纳入） |
| 11:14:23 | 故障恢复；11:16:41 resolved 投递 → `resolved_attached` | — |
| 11:20 | occupation release，停全部本次进程 | flagd 全部 off（`1ee5c025…`），无 `fault-active.json` |

S4 顺序控制：合同要求「告警事故先开出」。按 U2 时间线，发布判 anomalous（注入后约 6 分钟）会早于告警投递（约 8 分钟），实测也是如此（11:02:35 对 11:04:41）。所以 S4 期间先把 web 的后台 pass 关掉（间隔 0），等告警事故建出后再以间隔 30 重启。ending 持久保存，关掉 pass 只推迟消费（`__main__.py` 文档写明的行为），没有改任何产品代码或判据。

局限（独立审查 2026-10-24 指出）：S4 只证明「告警事故先开」前提下的追加路径。按实测时序，默认 30 s 间隔下发布消费会先于告警完成，发布新建事故与 Run；告警接入只挂到同 fingerprint 的告警事故（`opspilot/persistence/alerts.py:13-18`），会再建一个告警事故和第二个 Run（推断，未运行）。反向顺序没有合同条款，见 [#343](https://github.com/kevinWangSheng/production-ops-agent/issues/343)。

## 断言

| 场景 | 断言 | 实际 | 判定 |
|---|---|---|---|
| S1 健康发布 | 发布 healthy；零事故、零 Run、零 link | `healthy_confirmed`，样本 2 个 no_data（预热）+ 10 个 healthy。结束后再过两个消费周期（65 s）：事故 0、Run 0、link 0、事件 0、告警投递 0 | PASS |
| S2 健康发布 + adFailure | 发布 healthy；ad 方面若开了事故，不与该发布关联 | adFailure 真实生效（Prometheus：ad 错误 span 0.0083/s，frontend 0.0125/s，checkout 0），发布仍 `healthy_confirmed`。实验环境没有 ad 的告警规则，ad 方面没有开出任何事故；事故 0、link 0 | PASS |
| S3 回归、告警静默 | 发布 anomalous → 新建事故与 Run（v6），真实模型 Run 附 trace 与冻结摘要，报告引用发布前后与新旧实例证据 | 发布 anomalous（第 5 个样本 degraded，error_ratio 1.0）。对比 `complete`：before 窗 10:20:42–10:25:42、旧实例 `checkout-f8956c665-4cks7`、新实例 `checkout-659b547476-vswj4`；before_service/before_old error_ratio 0，after_service/after_new 1.0。link `created`：事故 `eaef0fb4`（`intake:release-anomaly:<observation>`，优先级 1，mode automatic），Run `0c3d6233` 输入 `opspilot-investigation-input-v6`，scope fact `release_observation` 含前后 revision、RS、hash、ending、anomaly 样本与对比。报告（`partial`）引用两个 Pod 名与 UID、两个 pod-template-hash、同一镜像 digest、发布时刻 10:25:42，以及 payment 的 `Invalid token` 错误与 flagd `paymentFailure`。告警投递 0（silence 生效） | PASS |
| S4 回归、告警开启 | 告警事故先开出；发布异常追加 1 条事件；没有第二个 Run | 告警投递 `created`（11:04:41，事故 `60eff8f1`、Run `2398b632`）早于消费（11:05:25）。link `attached`，`run_id` 为空，`input_id` 指向唯一一条 `kind='event'` 输入（actor `release_observation`，channel `release_observation`，text 只含系统生成的版本、ending、原因码与对比状态）。该事故 control_generation 仍为 0，优先级未变，Run 仍只有 `2398b632` 一个；全库 Run 共 2 个（S3 一个、告警一个）。resolved 投递 `resolved_attached` 到同一事故 | PASS |
| 每轮 | Deployment、ReplicaSet、flagd 以外的门禁与流量对象前后不变 | 23 类策略面对象（与 PR-3 证据同一脚本），每轮 308 个。S2–S4 的基线在 harness 发布与 watch 之后取，结束时只有 `flagd-config` 变化（harness 故障注入与恢复）。S1 的基线在发布前取，变化只有 checkout Deployment（`kubectl-patch` 09:25:21Z、generation 17→18）和 RS 的滚动（新增 `checkout-f96c97d6`，旧 RS 被历史上限清理），都来自 harness 的发布。四轮在排除范围外的变化均为 0 | PASS |

补充核对：

- 重放：以 web 登录角色对四个发布会话做 `replay_session`，全部 `consistent=true`、`integrity=[]`（含 PR-4b 的 link 核对）
- trace：[S3 Run 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8a425aef-71dc-4a29-8ac3-63dcd516f10c/r/00000000-0000-0000-3322-a454dfc71354?poll=true)（OTel `8b46dcb02687c0b0c69c537a52422008`），18 个 llm、37 个 tool 子 run，与库中 18 次模型请求、37 次工具调用一致；1114793 输入 / 52852 输出 token。前 14 个 llm 输入里能看到 `RELEASE_CONTEXT_BOUNDARY` 消息及新旧 hash；后 4 个看不到，是 tracer 每个 span 只保留 48 条提示消息（头 2 条 + 最后 46 条，`opspilot/tracing.py` `_MAX_PROMPT_MESSAGES`）造成的，Run 本身 `compactions=0`，模型输入快照为 v6
- PG 日志：ERROR 1 条，是执行者自己的只读查询写错列名；无 42501、无 FATAL
- web 日志：`release anomaly pass failed` 0 次

## 观察到但不属本项判定

- **自动恢复观察 pass 在发布观察过的目标上报错（既有缺陷，非 PR-4b 引入）**：S3、S4 的 Run 发布后，web 的 `self_heal_pass` 每 30 s 失败一次，共 28 次，`release_anomaly_pass` 不受影响（各自捕获异常）。位置：`opspilot/observation/store.py:1243-1246` 的 `authorize_session_in` 把同一目标上的全部旧会话的 `target` 解析为 `Target(**earlier["target"])`，发布观察会话存的目标快照没有 `revision` 字段，pydantic 报 `revision Field required`。这段代码来自 #120（`e7a0837`），发布观察会话来自 PR-2a，PR-4b 没有改动这两处。影响：一个目标一旦做过发布观察，其上任何事故的自动恢复观察（#184）都无法授权；人工登记处置是否同样受影响未核对。复现：任一目标先有一个 `purpose=release_observation` 的会话，再让该目标上某事故的结论发布并运行 `self_heal_pass`
- S4 告警事故 Run 的根 span 没有进 LangSmith：worker 在 11:08:41Z 记录一次 OTLP 导出读超时，丢 2 个 span，停机时又丢 2 个。合同不要求 S4 的 trace；该 Run 的库内记录完整（`completed`，16 次模型请求、32 次工具调用）
- S2 的「ad 方面若开了事故」在本环境不会发生：实验环境只有 checkout 的告警规则

## 费用

- DeepSeek 余额 95.34 → 94.79 CNY（09:18Z → 11:21Z），差 0.55 CNY。同一账户期间可能有其他会话调用，只是上界
- 两个 Run 共 34 次模型请求；S3 的 Run 1114793 输入 / 52852 输出 token

## 实验环境释放

- 三次故障都由 harness 的 `finally` 恢复并校验；结束时 flagd 11 个 flag 全部 `off`，ConfigMap sha256 `1ee5c025…`；`tmp/lab-ledgers/` 下没有 `.active-*` 与 `fault-active.json`
- silence 已删除（状态 expired）
- 本次启动的 web、worker、Observer、两个端口转发都按核实过的 PID 用 SIGTERM 停止，8080 不再监听；临时 PG 已停，数据留在本 worktree 的 `tmp/f11-pr4b-live/pg`
- `occupation show` 回读为 null
- checkout 保留最后一次发布的环境变量值，与 U2、PR-3 的做法相同

## 凭据

- 证据里没有 token、口令或 API key；UI 口令、事件 token 只以哈希进入 web 环境，Prometheus 账号与模型、LangSmith key 只进进程环境或私有 env 文件
- 授权与控制请求在摘要里只写 `idempotency_label`
