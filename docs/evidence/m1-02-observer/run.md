# M1-02 第 4 步：独立 Observer 进程在 kind 实验环境的有界真实采样

- 日期：2026-10-07 18:15–18:24 UTC；issue #86；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md)「第 4 步执行」。
- 分支 `feature/m1-02-observer`（worktree `../production-ops-agent-observer`，基于 main `924068d`）。
- 授权：本步不调用模型，费用只有实验环境资源（无供应商调用，余额不变）。实验环境写操作只有 `kind_lab.py up/stop`。
- 冻结摘要：[summary.json](summary.json)（由 [lab_run.py](lab_run.py) `summarize` 从数据库读出；**不含原始返回字节**，只有每条读数的状态、值、点数、`raw_sha256`、原始捆绑字节数与从捆绑里解出的最新原始样本时间）。Observer 进程日志：[observer.log](observer.log)；启动时宿主内存：[host-memory.txt](host-memory.txt)。

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
