# M1-02 第 4 步：独立 Observer 进程在 kind 实验环境的有界真实采样

- 日期：2026-10-07 18:15–18:24 UTC；issue #86；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md)「第 4 步执行」。
- 分支 `feature/m1-02-observer`（worktree `../production-ops-agent-observer`，基于 main `924068d`）。
- 授权：本步不调用模型，费用只有实验环境资源（无供应商调用，余额不变）。实验环境写操作只有 `kind_lab.py up/stop`。
- 两次运行：第一次（PR 初版，revision `@039b9a9a508d`）与审查处置后的第二次（revision `@0ceb325af50f`，见文末「第二次运行」）。冻结摘要：[summary.json](summary.json)（第一次会话，含其后被 sweep 的结束记录）、[summary-run2.json](summary-run2.json)（由 [lab_run.py](lab_run.py) `summarize` 从数据库读出；**不含原始返回字节**，只有每条读数的状态、值、点数、`raw_sha256`、原始捆绑字节数与从捆绑里解出的最新原始样本时间）。Observer 进程日志：[observer.log](observer.log) / [observer-run2.log](observer-run2.log)；启动时宿主内存：[host-memory.txt](host-memory.txt) / [host-memory-run2.txt](host-memory-run2.txt)。

## 环境

| 层 | 实际 |
|---|---|
| 实验环境 | `scripts/kind_lab.py up`（主仓库目录执行，工件在 `tmp/m1-kind-lab/`）：colima `m1-kind` 恢复，`health` 全部 ok；用完 `stop`（18:56Z），集群与 release 保留 |
| 宿主 | 16 GB；`up` 前可回收 3.81 GiB，swap 8.4 → 10.3 GB（上限 11.3 GB），Observer 运行期间 10.2 GB；未触及 D4「无法稳定运行」边界但很紧 |
| PostgreSQL | 17.9 临时实例 127.0.0.1:55491，数据目录在会话临时目录（**未碰** 55431 lab）；库 `observer_lab` 空库 `make migrate` → `0003_observation_store`；Observer 登录角色 `obs_lab_login LOGIN IN ROLE opspilot_observer`，`REVOKE TEMP` |
| Prometheus | 实验环境 NodePort `http://127.0.0.1:19090`，无认证（Prometheus 子 chart 默认）；Observer 以 `OPSPILOT_OBSERVER_PROMETHEUS_URL` 读取 |
| profile | `otel-demo-checkout@039b9a9a508d`（本步加 `freshness_query` 后的 revision），内容经 `authorize_session` 存入 `opspilot_health_profiles` |

## 步骤

1. 18:15:45Z `lab_run.py prepare`（owner DSN，工程侧代替第 3 步的人工登记）：注册目标 `deployment/checkout-20261007T181545Z`、接收事故 `b2ec6977…`、授权会话 `420085de…`，参数取 profile 的 session（3600 s / 40 次 / 60 s / 600 s），首个任务立即到期。
2. 等到 18:21Z（让 5 分钟窗口全部落在授权之后并积累 ≥ 3 个抓取点）。
3. 18:21:02Z 以 `env -i` 只带 `OPSPILOT_OBSERVER_DSN`（obs_lab_login）、`OPSPILOT_OBSERVER_PROMETHEUS_URL`、`OPSPILOT_OBSERVER_POLL_SECONDS=5`、`OPSPILOT_OBSERVER_GRACE_SECONDS=30` 启动 `python -m opspilot.observer`，`timeout -s TERM 150` 有界结束（退出码 124 = timeout 发出 SIGTERM；进程自身日志 `observer stopped`，在途样本已提交）。
4. 18:56Z `summarize` 冻结摘要；`kind_lab.py stop`。

## 结果（`summary.json`）

| 序号 | 窗口（UTC） | 提交 | outcome | disposition | health_basis | 请求数 |
|---|---|---|---|---|---|---|
| 1 | 18:16:02.6 – 18:21:02.6 | 18:21:02.65 | healthy，必要信号齐 | adopted | confirmed | 24（8 信号 × 3） |
| 2 | 18:17:02.8 – 18:22:02.8 | 18:22:02.83 | healthy | adopted | confirmed | 24 |
| 3 | 18:18:03.0 – 18:23:03.0 | 18:23:03.00 | healthy | adopted | confirmed | 24 |

- 每条读数 `status=ok`、`sample_count=5`（300 s 窗 5 个原始点：kube-state-metrics 与 span 指标的实际抓取/推送间隔 **60 s**，证实第 1 步 `minimum_samples=3` 的估计），`raw` 捆绑 1.4–1.8 KiB 并带 sha256；最新原始样本距窗口末尾 39–57 s（< `freshness_seconds` 90）。
- 值：副本 1、pods 1、重启 0、依赖 Deployment 8/8、错误率 0、依赖错误率 0、p95 95.8–97.5 ms、PlaceOrder 率 0.0208 / 0.0125 / **0.0083** /s（第 3 样本仅略高于门槛 0.008，是第 1 步记录的「流量薄」限制的实例）。
- 会话：`authorized`，`adopted_count=3`，`healthy_since=18:16:02.6`（第 1 样本窗口起点，晚于授权 18:15:45），连续健康覆盖 7 分钟 < 600 s 持续窗，事故仍 `observing_recovery`——按合同未到确认时刻；有界运行结束后无进程继续采样，会话将在 deadline（19:15:45Z）由下次 sweep 以 `deadline_expired` 交接回 `open`（本次未等到，临时库之后销毁）。
- 重放：`replay_session` 三条样本 `matches=True`，`session_consistent`、`lifecycle_consistent`、`consistent` 全为 True（原始捆绑 sha256 复核通过）。

## 本次没有做 / 限制

- 只跑了「健康」路径的真实采样；撤流量、去遥测、持续异常、挂起中途的真实场景只在 PG 端到端测试（HTTP 桩）里覆盖，真实环境验收属第 6 步 #88。
- 没有等到 600 s 持续窗与 `resolved`（有界 150 s），所以真实环境的 `recovery_confirmed` 转换未实测，只有 PG 测试覆盖。
- 网络可达性：Observer 与调查侧读的是同一个无认证 NodePort，隔离只到独立进程与独立变量；端点/令牌真正分开要在实验环境上另配，不在本步。
- 授权动作是工程脚本直接调存储原语，不是第 3 步的人工登记（#85 并行开发）。

## 第二次运行（2026-10-07 19:19–19:28 UTC，审查处置后，revision `otel-demo-checkout@0ceb325af50f`）

采样行为因审查处置改变（`freshness_query` 改为 `min(timestamp(...))`；读数 raw 捆绑升为 `opspilot.observer.reading/2`，带 `sample_time` 与 `body_complete`；单响应上限 30 KiB；HTTP 绝对 deadline），按 lead 要求重跑一次 ≤150 s。

- 环境：`kind_lab.py up` 第一次因 `helm repo update` 到 GitHub Pages 的瞬时 EOF 失败（exit 1；宿主 curl 同地址 200），VM 已起、集群与 release 已装，`health` 显示 Prometheus / Deployment 全就绪（Jaeger 因刚重启尚无 checkout 服务，Observer 不用 Jaeger），未再 helm。宿主 swap 10.5/11.3 GB。用完 `stop`（19:28:46Z）。
- 19:19:50Z `prepare`：事故 `a6716edc…`、会话 `1c51e888…`，参数同第一次。19:25:31Z 以 `env -i` 只带 Observer 自身变量启动，`timeout 150`。
- 第一轮 `sweep_expired_sessions` 先把**第一次运行的会话** `420085de…` 以 `deadline_expired` 结束（其 1 小时期限 19:15:45Z 已到）：结束记录 `('deadline_expired', 'observation_ended_unconfirmed', 'observing_recovery', 'open')`，事故回到 `open`，重放一致（`summary.json` 已更新为这个终态）——这是「未确认到期交接」路径在真实库上的一次实例。
- 新会话 3 个样本（`summary-run2.json`）：

| 序号 | 窗口（UTC） | outcome / basis | 最新原始样本距窗口末尾 | PlaceOrder 率 /s | p95 ms |
|---|---|---|---|---|---|
| 1 | 19:20:32.4 – 19:25:32.4 | healthy / confirmed | 5.2–36.2 s | 0.0208 | 90.5 |
| 2 | 19:21:32.6 – 19:26:32.6 | healthy / confirmed | 5.4–36.3 s | 0.0167 | 90.0 |
| 3 | 19:22:32.7 – 19:27:32.7 | healthy / confirmed | 5.5–36.5 s | 0.0208 | 93.8 |

  每样本 24 条即时查询、8 条 ok 读数、`sample_count=5`；raw 捆绑 1526–1880 B（format `/2`）。`min(timestamp(...))` 下依赖信号的新鲜度取 8 个 Deployment 中最旧的最新样本，仍在 90 s 内。
- 重放：`replay_session` 一致；另外用 `sampler.replay_readings(profile, 读数行)` 只从已存捆绑（exact bytes + sha256 + 捆绑内 `sample_time`）重判三条样本，结果 `healthy / required_signals_present=True`，与已存判定一致；捆绑 `sample_time` 早于 `submitted_at` 2–5 ms（判定时刻与提交时刻分开记录）。
- 会话仍 `authorized`、事故 `observing_recovery`（未到 600 s 持续窗）；临时库随后销毁，不再有进程采样。
