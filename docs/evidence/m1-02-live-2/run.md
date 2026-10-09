# M1-02 第 6 步（重跑）：F6 真实环境验收，健康窗从窗口末尾起算（#157，kind 实验环境）

- 日期：2026-10-09 00:18–01:49 UTC；issue #157（含 #88 第 6 步全部场景重跑）；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md)「第 6 步执行（重跑）」。
- 分支 `feature/F6-healthy-window-end`（worktree `../production-ops-agent-f6-window`，基于 main `fe62caa`），产品改动提交 `3bbad40`：`opspilot/observation/store.py` 折叠规则 `healthy_since = sample.window.end`（原 `window.start`），在线折叠、离线重放、验收投影共用；C3 §10、F6 第 1 步、F6 测试规范同步；测试由 Codex（测试作者）改写。
- 替代 [m1-02-live/run.md](../m1-02-live/run.md)（旧规则下的四场景）；驱动脚本沿用 [../m1-02-live/lab_run.py](../m1-02-live/lab_run.py)（本 PR 按 #157 ①–④ 修复：intake 幂等键取已保存的 experiment_id、失败不留旧 incident_id、复用仓库的不跟随重定向 opener、register 先持久化 expected_generation 再 POST）与 [../m1-02-live/probe_signals.py](../m1-02-live/probe_signals.py)。
- 费用：不调用模型（Observer 无模型客户端；调查 Run 保持 queued），供应商余额不变；无 LangSmith trace（观察路径无埋点，披露为缺口）。
- 实验环境写操作只有工程侧：`kind_lab.py up/stop`、`fault inject/restore`、`kubectl scale`（load-generator、kube-state-metrics）。产品进程只持有 PG 登录与 Prometheus 只读账号。

## 环境

| 层 | 实际 |
|---|---|
| 实验环境 | 主仓库 `scripts/kind_lab.py up`（00:18Z 恢复，`health` ok；起环境后先确认 frontend span 计数器在增长再开始，frontend span 计数器增长正常；00:37Z 起再次停滞，00:48:20Z `rollout restart deployment otel-collector` 后恢复，见场景 2 首次尝试）；用完 `stop` |
| PostgreSQL | 17.9 临时实例 127.0.0.1:55661，库 `f6live`（空库 migrate → 0005），Observer 登录 `obs_lab_login IN ROLE opspilot_observer`，`REVOKE TEMP`；数据目录 worktree `tmp/pg55661` |
| 工作台 | 两实例 `env -i`：8086 有界变体 profile（`otel-demo-checkout@8f91519db7fa`，[../m1-02-live/otel-demo-checkout-bounded.json](../m1-02-live/otel-demo-checkout-bounded.json)，`max_samples=11`），8087 shipped profile（`otel-demo-checkout@b72bbe2e30be`）；身份文件只列 `checkout-lab` |
| Observer | 一个进程 `env -i`（Observer DSN、Prometheus URL、observer 账号文件、`POLL_SECONDS=5`），00:18:41–01:48:58Z 服务全部五个会话；日志 [observer.log](observer.log) |

## 场景

顺序：场景 4 → 场景 2（首次尝试，部分覆盖）→ 场景 2（重做）→ 场景 3 → 场景 1。场景 2–4 用有界变体 profile（`max_samples=11`，加载器下限 `(max_samples−1)×60 ≥ 600`——在新规则下恰好是「11 个连续健康采样端到端 600 s」所需的数量），场景 1 用 shipped profile。每个场景目录：`summary.json`（intake/register 响应、工程侧实验环境动作 `lab_actions`、会话、逐采样逐读数、`replay_session`、`recovery_outcome` 投影、`observer_grants` 实测权限图）、`replay.json`（`python -m opspilot.observer.replay --incident <id>`，Observer 登录，退出码 0）、`incident-page.html`。所有采样 8 信号 × 3 条即时查询 = 24 请求；稳态每信号 5 个原始点；投影 `permissions=[read_only, human_control]`、`model_requests=[]`；重放 CLI 全部 `consistent=true`、`external_queries=[]`。驱动脚本退出码不是验收断言，结论来自冻结记录里的产品判定、投影与重放一致性。

### 场景 4：持续异常（F6 第 4 步）— `s4-continued-degradation/`

- 00:23:35Z `fault inject`（paymentFailure 100%，整个会话保持）+ intake（事故 `a6c68a1d…`）；00:25:36Z 错误率 0.68 可见 → **00:25:37Z `register_remediation`**（会话 `f0337ba0…`，期限 01:25:37Z）。
- 采样 1–2（00:26:37、00:27:37Z）：四个 span 信号 **`stale`**——VM 刚恢复后 PlaceOrder 选择器下有 3 条序列，其中 1 条在首批数据后不再更新（最新样本距采样 254 s），`freshness_query = min(timestamp(...))` 按最陈旧的序列判新鲜度（第 4 步 PR #119 P2-1 的 fail-closed 设计），直到该序列离开 5 min lookback；采样 3–11（00:28:37–00:36:39Z）全部 **`degraded`**：`error_ratio` 1.0、`dependency_error_ratio` 1.0（collector 刚重启、计数器从零起，窗内出站调用全部是失败的 Charge），deployment 三信号 1/1/1、依赖 8/8、率 0.013–0.038 /s、p95 48–85 ms。
- 第 11 个采样 `max_samples_exhausted` → `observation_ended_unconfirmed`，事故回 `open`（00:36:39Z）；`healthy_since` 始终为空。投影：`degraded`、不确认、`[DEGRADED_SIGNAL:error_ratio, DEGRADED_SIGNAL:dependency_error_ratio, CONTINUED_DEGRADATION, OBSERVATION_UNCONFIRMED]`、`handoff`、actions 279（record_handling 1 + advance 2 + read_only_query 264 + persist 11 + human_handoff 1）；没有部署/回滚/发布门动作。重放 11/11 一致。

### 场景 2（首次尝试，部分覆盖）：撤流量 — `s2b-traffic-removed-attempt1/`

- 00:37:35Z `fault restore` + intake（事故 `d30abd62…`）后发现 span 计数器自约 00:37Z 起再次停滞（frontend rate 0；与 2026-10-08 第二次会话相同的实验环境问题），00:48:20Z `rollout restart deployment otel-collector` 后恢复；00:48:21Z 为本事故重新 `fault inject`；00:51:24Z 错误率 0.67 可见即 `fault restore`，驱动的「等比率下降」条件写错（阈值 0.7 立刻满足），**同秒**撤流量并登记（会话 `c36471af…`）——与上一轮首次尝试同形：比率没有下降过程。
- 采样 1–3 `degraded`（比率 0.70 → 0.70 → 1.0，率 0.035 → 0.024）；采样 4 `no_data`（率 **0.0042 < 0.008** 流量门，错误率读数 1.0 仍在但不判定——「率在 0 与门槛之间」的单独 `INSUFFICIENT_TRAFFIC` 形态）；采样 5–11 `no_data`（率 0，空向量）。不确认 → 次数耗尽回 `open`（01:02:29Z）。投影 `unknown` / `INSUFFICIENT_TRAFFIC` + `REQUIRED_TELEMETRY_MISSING` + 三个 `MISSING_SIGNAL` / `handoff`；重放一致。保留为「撤流量后不确认」的证据，「错误率下降」见下一节。

### 场景 2（重做）：错误率下降时撤流量（F6 第 2 步）— `s2c-traffic-removed-while-errors-fall/`

- 01:02:43Z load-generator 回到 1 + `fault inject`（事故 `ae2cbd44…`）；01:03:32Z 比率 1.0 可见 → **`fault restore`（处置）**，流量保持；驱动等比率从峰值 1.0 下降 ≥ 0.2 → 01:05:33Z 比率 0.67、率 0.0125 /s → **撤流量（load-generator 0）→ 01:05:34Z `register_remediation`**（会话 `e493eca5…`）。
- 采样：

| 序号 | 窗口末尾 | outcome | 率 /s | 错误率 | 说明 |
|---|---|---|---|---|---|
| 1 | 01:06:34 | degraded | 0.0125 | 0.667 | 比率在下降途中仍 > 0.01 |
| 2 | 01:07:34 | degraded | 0.0083 | 0.50 | 继续下降；率刚好在门槛之上 |
| 3 | 01:08:35 | **no_data** | **0.0042** | **0.0** | 错误率已降到 0，但率 < 0.008 → 流量门：错误率不判定、不计健康（F6 第 2 步的核心形态） |
| 4–11 | 01:09:35–01:16:36 | no_data | 0 | 空向量 | 窗内无订单 |

- 全程不确认（`healthy_since` 为空），第 11 个采样 `max_samples_exhausted` → 回 `open`（01:16:36Z）。投影：`unknown`、`recovery_confirmed=false`、`[INSUFFICIENT_TRAFFIC, REQUIRED_TELEMETRY_MISSING, MISSING_SIGNAL:error_ratio, MISSING_SIGNAL:latency_p95_milliseconds, MISSING_SIGNAL:dependency_error_ratio, OBSERVATION_UNCONFIRMED]`、`handoff`、actions 279。重放 11/11 一致。

### 场景 3：去掉必要遥测（F6 第 3 步）— `s3-telemetry-removed/`

- 01:17:12Z load-generator 回到 1 + `kubectl scale deployment kube-state-metrics --replicas=0`（整个会话保持）+ intake（事故 `cf0a71f8…`）；等 `kube_deployment_status_*` 最新样本距今 > 90 s（01:18:27Z 实测 105 s）→ **01:18:28Z `register_remediation`**（会话 `70ffe995…`）。
- 采样 1–3（01:19:32–01:21:32Z）：`deployment_ready_replicas` / `deployment_available_replicas` / `dependency_deployments_available` 即时查询空向量（staleness marker）→ `no_data`；`deployment_available_replicas_min_in_window`（`min_over_time[5m]`）仍从窗内旧点算出值但新鲜度 > 90 s → **`stale`**；样本 `stale`、`required_signals_present=false`。采样 4–11（01:22:32–01:29:34Z）四个信号全部 `no_data`。span 信号全程正常（率 0.004–0.042 /s，load-generator 刚恢复时流量薄；错误率 0；p95 正常）。
- 第 11 个采样 `max_samples_exhausted` → 回 `open`（01:29:34Z）。投影：`unknown`、不确认、`[REQUIRED_TELEMETRY_MISSING, MISSING_SIGNAL:deployment_available_replicas, MISSING_SIGNAL:deployment_ready_replicas, MISSING_SIGNAL:deployment_available_replicas_min_in_window, MISSING_SIGNAL:dependency_deployments_available, OBSERVATION_UNCONFIRMED]`、`handoff`、actions 279。重放 11/11 一致。

### 场景 1：处置后恢复（F6 第 1 步，shipped profile，新规则）— `s1-recovered/`

- 01:30:05Z kube-state-metrics 回到 1；01:30:06Z `fault inject` + intake（事故 `0834af7e…`，shipped-profile 工作台 8087）；01:33:22Z 错误率 0.71 可见 → **`fault restore`（系统外人工处置）→ 01:33:23Z `register_remediation`**（会话 `cfb90629…`，期限 02:33:23Z，40 次 / 60 s / 600 s）。
- 15 个采样（窗口末尾 01:34:25–01:48:27Z）：

| 序号 | 窗口（UTC） | outcome / basis | 率 /s | 错误率 | 说明 |
|---|---|---|---|---|---|
| 1–4 | …–01:37:25 | degraded | 0.025–0.046 | 0.82 / 1.0 / 0.875 / 0.667 | 窗内仍含故障；采样 4 是最后一个非健康采样，窗口末尾 **01:37:25Z** |
| 5 | 01:33:25–01:38:25 | **healthy / confirmed** | 0.0083 | 0 | `healthy_since = 01:38:25Z`（本窗口**末尾**；旧规则会取起点 01:33:25Z）；窗口起点 01:33:25 晚于登记 01:33:23，不是 `window_before_authorization` |
| 6–14 | …–01:47:27 | healthy / confirmed | 0.021–0.042 | 0 | 旧规则下第 10 个采样（01:43:26Z）已会确认；新规则此时健康窗 301 s，继续观察 |
| 15 | 01:43:27–01:48:27 | healthy / confirmed → **`recovery_confirmed`** | 0.033 | 0 | 健康窗 01:48:27 − 01:38:25 = **601 s ≥ 600**，事故 `resolved` |

- 距最后一个非健康采样的窗口末尾（01:37:25Z）到确认（01:48:27Z）**662 s**，健康窗 [01:38:25, 01:48:27] 与任何非健康采样的评估窗口不重叠——#157 用户决定的要求在真实环境成立。其余信号：deployment 三信号 1/1/1、依赖 8/8、p95 48–96 ms，每信号 5 点。
- 投影：`healthy`、`recovery_confirmed=true`、`healthy_window_seconds=601`、`recovery_reasons=[]`、无交接、`observation_ended_reason=recovery_confirmed`、`permissions=[read_only, human_control]`、actions 378 = record_handling 1 + advance 2 + read_only_query 360 + persist 15、`model_requests=[]`。重放 15/15 一致，CLI `healthy / recovery_confirmed=true / healthy_window_seconds=601 / expected_lifecycle=resolved`。



## 本次没有做 / 限制

- 模型与 trace：不调用模型，无 LangSmith trace（观察路径无埋点），如实披露为缺口；费用 0。
- 场景 2 首次尝试仍是「restore 与撤流量同秒」（驱动的等待条件写错），保留为部分覆盖记录；重做（`s2c-…`）覆盖「错误率下降时撤流量」，并出现率在 0 与门槛之间的单独 `INSUFFICIENT_TRAFFIC` 采样。
- 实验环境自身问题再次出现：VM 恢复后 span 指标计数器停滞（00:37Z 起），工程侧 `rollout restart deployment otel-collector` 后恢复；VM 刚恢复时 PlaceOrder 选择器下残留 1 条不再更新的序列，使场景 4 前两个采样按 `min(timestamp)` 判 stale（fail-closed，5 min 后自行消失）。原因未深究（未确认）。
- 真实 `deadline_expired` 未等；缺 span 信号单独场景、pod 重启形态、Playwright 截图未做；流量薄（2 locust 用户）。
- 场景 2–4 用只改 `max_samples=11` 的有界变体；shipped 的 40 次只在场景 1 用到 15 次。
- 实验环境状态在结束前恢复（四次 inject/restore 均 verified；load-generator、kube-state-metrics 1/1），`colima stop m1-kind`（集群保留）。
