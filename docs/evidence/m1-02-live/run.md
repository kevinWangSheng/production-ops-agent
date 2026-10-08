# M1-02 第 6 步：F6 真实环境验收（kind 实验环境，#88）

- 日期：2026-10-08 17:13–18:10 UTC；issue #88；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md)「第 6 步执行」。
- 分支 `feature/F6-live-acceptance`（worktree `../production-ops-agent-f6-live`，基于 main `510dcd3`）。
- 费用：本步按任务记录计划第 6 项**不调用模型**（Observer 进程无模型客户端，`tests/test_m1_observer.py` 的子进程 import 校验；调查 Run 保持 `queued`，未起 worker），供应商余额不变；费用只有实验环境资源。没有 LangSmith trace：观察路径按 D3 不经调查 worker，也没有 OTel 埋点（ADR-0006 的 trace 要求针对调查 loop）。
- 实验环境写操作只有工程侧：`kind_lab.py up/stop`、`kind_lab.py fault inject/restore`（flagd ConfigMap）、`kubectl scale`（load-generator、kube-state-metrics，用实验环境专用 kubeconfig）。产品进程（工作台、Observer）只持有 PG 登录与 Prometheus 只读账号。

## 环境

| 层 | 实际 |
|---|---|
| 实验环境 | 主仓库 `scripts/kind_lab.py up`（17:13Z 恢复，`health` 全部 ok；集群与 release 保留自第 0 步，chart 0.37.8 / kube-state-metrics 8.6.0）；用完 `stop` |
| 宿主 | 16 GB；`up` 前可回收约 3.1 GiB；运行期间 swap 10.6–11.2 / 12.3 GB（另有 4 条并行线在跑 PG/pytest）；Observer 进程 RSS 24 MB |
| PostgreSQL | 17.9 临时实例 127.0.0.1:55601，数据目录 worktree `tmp/pg55601`（**未碰** 55431 lab）；空库 `opspilot.schema migrate` → `0005_incident_mode`；Observer 登录 `obs_lab_login LOGIN IN ROLE opspilot_observer`，`REVOKE TEMP ON DATABASE m0_budget FROM PUBLIC` |
| 工作台 | `python -m opspilot.web serve`（`env -i`，127.0.0.1:8086，UI 用户 `demo` 的 pbkdf2 哈希；`OPSPILOT_TARGET_IDENTITIES` 只列 `checkout-lab` → `m0-otel-20260909 / kind-opspilot-m1 / otel-demo / checkout / otel-demo-checkout`）；无 worker、无模型 |
| Observer | `python -m opspilot.observer`（`env -i`，只带 `OPSPILOT_OBSERVER_DSN`、`OPSPILOT_OBSERVER_PROMETHEUS_URL=http://127.0.0.1:19090`、`OPSPILOT_OBSERVER_ENV_FILE`=observer 账号、`POLL_SECONDS=5`、`GRACE_SECONDS=30`），17:16:35Z 起一个进程服务全部场景；日志 [observer.log](observer.log) |
| Prometheus | 实验环境 NodePort，basic auth（第 4 步 D3 落地）：Observer 用 observer 账号，工程脚本用 lab 账号；口令在主仓库 `tmp/m1-kind-lab/`，不入库 |
| 驱动 | [lab_run.py](lab_run.py)（工程脚本）：`intake` → 工作台 `POST /intake/ui`；`register` → `POST /incidents/<id>/control action=register_remediation`（第 3 步人工动作，经产品路径）；`summarize` → `session_history` + `replay_session` + `recovery_outcome(IncidentScenario, RecoveryRecords)`（Observer 登录实测权限）；`page` → 事故页 HTML。离线重放另用 `python -m opspilot.observer.replay --incident <id>`（Observer DSN，`env -i`） |

## 校准（#88 评论的两项未校准信号）

脚本 [probe_signals.py](probe_signals.py)（lab 账号只读）在稳态（17:16:16Z，[calibration/probe-steady.json](calibration/probe-steady.json)）与注入故障后（17:18:50Z，[calibration/probe-fault.json](calibration/probe-fault.json)）各跑一次 shipped profile `otel-demo-checkout@b72bbe2e30be` 的全部 8 个信号（`query` / `coverage_query` / `freshness_query` 同一求值时刻）并拉取每个选择器的原始序列。

| 信号 | 原始序列 | 标签形状 | 窗内原始点（5 min） | 读数（稳态 / 故障） | 结论 |
|---|---|---|---|---|---|
| `deployment_ready_replicas`（`kube_deployment_status_replicas_ready{namespace="otel-demo",deployment="checkout"}`） | 恰好 1 条 | `namespace`、`deployment` 直接作为标签（scope 选择器精确绑定主体），其余为 kube-state-metrics 的 `instance`/`job`/`service`/`node`/helm 标签，被 `min()` 聚合掉 | 3（VM 刚恢复 3 分钟）→ 5（60 s 抓取） | 1 / 1，新鲜度 27 s / 1 s | 阈值 `min 1` 与 `spec.replicas=1` 一致；`minimum_samples=3` 在 300 s 窗 5 点下成立；**不需校准，profile 不改** |
| `deployment_available_replicas_min_in_window`（`min(min_over_time(kube_deployment_status_replicas_available{…}[5m]))`） | 同上 1 条 | 同上 | 3 → 5 | 1 / 1 | 同上；本次没有制造 pod 重启，窗内下探到 0 的形态只在 PG 测试覆盖（限制） |
| 其余 6 个信号 | — | — | 5 | 稳态：副本 1、率 0.013/s、错误率 0、p95 84 ms、依赖 8/8、依赖错误率 0；故障：错误率 **0.586**、依赖错误率 0.057、率 0.020/s、p95 95 ms | 故障下 `error_ratio` 与 `dependency_error_ratio` 越过 0.01 上限，其余不变——与第 1 步校准一致 |

## 场景

四个场景顺序执行（同一目标 `checkout-lab`，每个场景一个新事故），一个 Observer 进程服务全部会话。场景 2–4 用**有界变体** profile `otel-demo-checkout@8f91519db7fa`（`tmp/f6-live/otel-demo-checkout-bounded.json`，不入库；与 shipped profile 逐字段相同，只有 `session.max_samples` 由 40 改为 11——加载器允许的最小值 `(max_samples-1)×60 ≥ 600`，让「未确认 → 次数耗尽交接」在 11 分钟内真实发生；`deadline_seconds` 等其余会话参数不变）。场景 1 用 **shipped profile** `otel-demo-checkout@b72bbe2e30be`。每个场景目录：`summary.json`（冻结摘要：intake/register 响应、工程侧实验环境动作、会话、逐采样逐读数、`replay_session`、`recovery_outcome` 投影）、`replay.json`（`python -m opspilot.observer.replay --incident <id>` 以 Observer 登录离线重放，退出码 0）、`incident-page.html`（事故页）。所有采样 8 信号 × 3 条即时查询 = 24 请求，每信号 `sample_count=5`（60 s 抓取）或缺测，原始捆绑 1.3–2.0 KiB 带 sha256（原始字节留在库里，不入库）。

### 场景 4：持续异常（F6 第 4 步）— `s4-continued-degradation/`

- 17:16:35Z `fault inject`（paymentFailure 100%，整个会话保持注入）；17:16:47Z 工作台 intake（201，事故 `12e4a6f2…`，调查 Run 保持 queued）；17:18:39Z 错误率 0.55 可见；**17:19:00Z 工作台 `register_remediation`**（200，generation 0→1，事故 `open → observing_recovery`，会话 `d2760213…`，期限 18:19:00Z，11 次）。
- 11 个采样（窗口末尾 17:20:00–17:30:02Z，每分钟一次）全部 `degraded / adopted / outcome_not_healthy`：`error_ratio` 0.83 → 1.0，`dependency_error_ratio` 0.09–0.15（均 > 0.01 上限），其余 6 个信号正常（副本 1/1/1、依赖 8/8、率 0.022–0.046 /s、p95 48–93 ms）。`healthy_since` 始终为空。
- 第 11 个采样：`max_samples_exhausted` → `observation_ended_unconfirmed`，事故 `observing_recovery → open`（结束记录 17:30:02Z）。
- 投影：`recovery_verdict=degraded`、`recovery_confirmed=false`、`recovery_reasons=[DEGRADED_SIGNAL:error_ratio, DEGRADED_SIGNAL:dependency_error_ratio, CONTINUED_DEGRADATION, OBSERVATION_UNCONFIRMED]`、`human_interaction=handoff`（`MAX_SAMPLES_EXHAUSTED` + 上述原因）、`permissions=[read_only, human_control]`、`actions` 279 条 = record_handling 1 + advance_incident_lifecycle 2 + read_only_query 264（11×24）+ persist_observation 11 + human_handoff 1，`model_requests=[]`；没有部署/回滚/发布门动作（实验环境里唯一的写是工程脚本的 flagd patch，产品进程不持有 kubeconfig）。
- 重放：`replay_session` 11/11 `matches`，CLI `consistent=true`、`recovery_verdict=degraded`、`recomputed_verdict=degraded`、`expected_lifecycle=open`、`external_queries=[]`、`model_requests=[]`。
- 观察：第 1 个采样的窗口（17:15:00–17:20:00Z）起点早于登记时刻 17:19:00Z——产品按「窗口末尾 = 采样时刻、跨度 = 300 s」取窗，首个窗口必然含处置前数据；对本场景无影响（持续异常），对场景 1 的意义见下。

### 场景 2：撤流量时错误率下降（F6 第 2 步）— `s2-traffic-removed/`

- 17:30:36Z `fault restore`（paymentFailure 恢复）+ 17:30:38Z `kubectl scale deployment load-generator --replicas=0`（撤流量）；17:30:39Z intake（事故 `1a83ff1b…`）；**17:30:55Z `register_remediation`**（会话 `2e9f6315…`）。
- 采样 1–3（窗口末尾 17:31:57–17:33:58Z）：窗内只剩故障期的 PlaceOrder span，`error_ratio=1.0`、率 0.033 → 0.017 /s（绝对错误率在下降、比率不降）→ `degraded`。采样 4–11（17:34:58–17:41:59Z）：窗口不再含任何新订单，`request_rate_per_second=0.0`（序列仍在，5 个点，计数不增）< 门槛 0.008 → 流量门生效，`error_ratio` / `latency_p95` / `dependency_error_ratio` 读数 `no_data`（空向量），样本 `no_data`、`required_signals_present=false`、`adopted`，**不确认恢复**；deployment 四信号与依赖 8/8 全程正常。
- 第 11 个采样 `max_samples_exhausted` → 事故回 `open`（17:41:59Z）。投影：`recovery_verdict=unknown`、`recovery_confirmed=false`、`latest_sample_verdict=no_data`、`healthy_window_seconds=0`、原因 `[INSUFFICIENT_TRAFFIC, REQUIRED_TELEMETRY_MISSING, MISSING_SIGNAL:error_ratio, MISSING_SIGNAL:latency_p95_milliseconds, MISSING_SIGNAL:dependency_error_ratio, OBSERVATION_UNCONFIRMED]`、`handoff`、`permissions=[read_only, human_control]`、actions 279、`model_requests=[]`。
- 重放：11/11 一致，CLI `consistent=true`、`unknown/unknown`、`expected_lifecycle=open`、无外部查询。

### 场景 3：去掉必要遥测（F6 第 3 步）— `s3-telemetry-removed/`

- 17:43:07Z `kubectl scale deployment load-generator --replicas=1`（流量回来）+ `kubectl scale deployment kube-state-metrics --replicas=0`（deployment 遥测消失，整个会话保持）；17:43:09Z intake（事故 `300c5951…`）；等 `kube_deployment_status_*` 最新样本距今 > 90 s（17:44:20Z 实测 92.9 s）后 **17:44:21Z `register_remediation`**（会话 `7b2e0af8…`）。
- 采样 1–3（17:45:25–17:47:25Z）：`deployment_ready_replicas` / `deployment_available_replicas` / `dependency_deployments_available` 即时查询返回空向量（Prometheus 对消失的抓取目标写入 staleness marker）→ `no_data`；`deployment_available_replicas_min_in_window`（`min_over_time[5m]`）仍能从窗内旧点算出值，但 freshness（最新原始样本 17:43:38Z）距采样时刻 > 90 s → **`stale`**（值置空、原始返回保留）；样本 `stale`、`required_signals_present=false`。采样 4–11（17:48:25–17:55:27Z）：窗内再无旧点，四个信号全部 `no_data`，样本 `no_data`。span 信号全程正常（率 0.004 → 0.058 /s——load-generator 刚恢复时流量薄，采样 1–2 的率 0.0042 低于门槛，但样本已先按缺测判 unknown；错误率 0；p95 75–150 ms）。
- 第 11 个采样 `max_samples_exhausted` → 事故回 `open`（17:55:27Z）。投影：`unknown`、不确认、原因 `[REQUIRED_TELEMETRY_MISSING, MISSING_SIGNAL:deployment_available_replicas, MISSING_SIGNAL:deployment_ready_replicas, MISSING_SIGNAL:deployment_available_replicas_min_in_window, MISSING_SIGNAL:dependency_deployments_available, OBSERVATION_UNCONFIRMED]`、`human_interaction=handoff`、`permissions=[read_only, human_control]`、actions 279、`model_requests=[]`。
- 重放：11/11 一致，CLI `consistent=true`、`unknown/unknown`、`expected_lifecycle=open`。

### 场景 1：处置后恢复（F6 第 1 步，shipped profile）— `s1-recovered/`

- 17:55:50Z `kubectl scale deployment kube-state-metrics --replicas=1`（场景 3 后恢复遥测）；17:56:15Z `fault inject`（事故）；17:56:16Z intake（事故 `ae235cca…`，shipped-profile 工作台 127.0.0.1:8087）；17:57:44Z 错误率 0.5 可见；**17:57:45Z `fault restore`（系统外的人工处置）→ 同秒 `register_remediation`**（200，generation 0→1，会话 `28a4b4c6…`，期限 18:57:45Z，40 次 / 60 s / 600 s 持续窗，`recovery_handled_at=17:57:45.574Z`）。
- 10 个采样（窗口末尾 17:58:47–18:07:49Z）：

| 序号 | 窗口（UTC） | outcome / basis | 率 /s | 错误率 | 依赖错误率 | ready / avail / min窗 / 依赖 | p95 ms | 备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | 17:53:47–17:58:47 | degraded / outcome_not_healthy | 0.0167 | 0.75 | 0.091 | no_data ×4 | 90 | 窗内 kube-state-metrics 点 < 3（17:55:50 才恢复），`required_signals_present=false`；错误率已越界所以判 degraded 而非 unknown |
| 2 | 17:54:47–17:59:47 | degraded | 0.0208 | 0.60 | 0.063 | 1 / 1 / 1 / 8（3–5 点） | 93.75 | |
| 3 | 17:55:48–18:00:48 | degraded | 0.0167 | 0.75 | 0.081 | 1 / 1 / 1 / 8 | 90 | |
| 4 | 17:56:48–18:01:48 | degraded | 0.0167 | 0.50 | 0.056 | 1 / 1 / 1 / 8 | 95 | 最后一个含故障样本的窗口 |
| 5 | 17:57:48–18:02:48 | **healthy / confirmed** | 0.0125 | 0 | 0 | 1 / 1 / 1 / 8 | 96.25 | `healthy_since = 17:57:48Z`（本窗口起点） |
| 6–9 | …–18:06:49 | healthy / confirmed | 0.0125–0.0208 | 0 | 0 | 1 / 1 / 1 / 8 | 48–92.5 | 率始终 ≥ 门槛 0.008 |
| 10 | 18:02:49–18:07:49 | healthy / confirmed → **`recovery_confirmed`** | 0.0292 | 0 | 0 | 1 / 1 / 1 / 8 | 91.25 | 健康窗 18:07:49 − 17:57:48 = **601 s ≥ 600** |

- 结束记录 18:07:49Z：`recovery_confirmed`，事故 `observing_recovery → resolved`；会话 `completed`，`adopted_count=10`（40 次预算未用完，期限未到）。
- 投影（`recovery_outcome`）：`recovery_verdict=healthy`、`recovery_confirmed=true`、`healthy_window_seconds=601`、`recovery_reasons=[]`、`human_interaction=null`、`handoff_reasons=[]`、`observation_ended_reason=recovery_confirmed`、`permissions=[read_only, human_control]`、`actions` 253 = record_handling 1 + advance_incident_lifecycle 2 + read_only_query 240 + persist_observation 10、`model_requests=[]`、`handling_audit=[register_remediation by demo]`、`target` = 会话绑定的登记身份（`m0-otel-20260909 / kind-opspilot-m1 / otel-demo / checkout-lab / otel-demo-0.37.8`）、`recovery_profile_revision=otel-demo-checkout@b72bbe2e30be`。F6 第 1 步要求的 deployment 状态（可用/就绪/窗内最小副本）、请求量（门槛）、错误、延迟、依赖（8 个 Deployment 可用 + 出站错误率）与持续窗口在第 10 个采样同时满足才报告恢复；第 1–4 个采样有任一越界即不确认。
- 重放：`replay_session` 10/10 一致；CLI `consistent=true`、`recovery_verdict=healthy`、`recovery_confirmed=true`、`recomputed_verdict=healthy`、`healthy_window_seconds=601`、`expected_lifecycle=recorded_lifecycle=resolved`、`external_queries=[]`、`model_requests=[]`。
- **发现（合同层，转 #157）**：`healthy_since` 取首个健康采样的窗口**起点**（17:57:48Z），该起点早于最后一个 degraded 采样的窗口末尾（18:01:48Z）240 s；从最后一个异常窗口末尾算起到确认只有 361 s。原因是 `rate()[5m]` 以窗内首个样本为基线，故障停止后一个抓取间隔内新窗口就读为 0。这是第 2 步折叠规则（`store.py` `healthy_since = sample.window.start`）的既定语义，重放与投影用同一规则，本步不改；是否改为按窗口末尾计时由用户决定。

## 本次没有做 / 限制

- 模型与 trace：按任务记录计划第 6 项不调用模型，没有调查 Run 执行（Run 行保持 queued）、没有 LangSmith trace；Observer 路径没有 OTel 埋点。费用 0（无供应商调用；未另抓余额快照）。
- 场景 2–4 用有界变体 profile（`max_samples=11`，其余与 shipped 相同）而不是 shipped 的 40 次；真实「次数耗尽交接」三次，真实 `deadline_expired`（1 小时）本次未等到（第 4 步 run.md 第二/三次运行已有两例真实到期交接）。
- 场景 2「撤流量时错误率下降」的实际轨迹：前 3 个采样比率仍为 1.0（窗内只剩故障期 span）判 degraded，之后率为 0 才进流量门；没有出现「率在 0 与门槛之间」的 `INSUFFICIENT_TRAFFIC` 单独判定（流量薄，2 locust 用户，与第 1 步限制一致）。
- 场景 3 只去掉了 kube-state-metrics 这一类遥测（四个 deployment 信号）；缺 span 信号（请求量/错误/延迟/依赖错误率）的真实场景没有另做（在场景 2 后段以空向量形态出现）。
- `deployment_available_replicas_min_in_window` 窗内下探到 0 的形态没有制造（没有重启 checkout pod）；pod 更换导致的 stale 判定未出现。
- 首个采样的窗口含处置前数据（窗口末尾 = 采样时刻），产品不裁剪；场景 1 的首个采样因此判 degraded，不影响结论。
- 事故页 HTML 保存自工程脚本的 GET（带 UI 账号），未做 Playwright 截图。
- 实验环境状态在结束前恢复（flagd 两次 inject/restore 均 verified；load-generator、kube-state-metrics 回到 1/1），`colima stop m1-kind`（集群与 release 保留）。
