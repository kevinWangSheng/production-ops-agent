# F9 终轮第二次：第 2、4 步重做（2026-11-11）

类别：集成运行。租机 `opspilot-f9-1`（单机：kind lab 加 Compose 项目，`otel-demo` 工具档，真实 DeepSeek 模型），接着[上一轮干净主机终轮](../f9-final-clean-host-2026-11-11/README.md)的同一台机继续做，没有重装。这不是生产证明，也不是 soak；流量是 lab 的 load-generator，故障都是 flagd `paymentFailure` 注入，转人工是切断模型出口造成的。时间均为 UTC，2026-11-11（第 4 步从太平洋时间 11-11 05:15 开始，第 2 步到 11:22 结束）。冻结摘要见 [summary.json](summary.json)，主机原始文本在 [raw/](raw/)，事后补的只读查询在 [supplement/](supplement/)（见末节）。

上一轮第 2、4 步判为部分达成。独立审查后没有翻 `passes`，用户裁决：第 2 步在**一个** incident 上重做（先有证据，再切模型出口，转人工，用户接管，系统外恢复，`register_remediation`，`recovery_confirmed`，关闭）；第 4 步的 staging 是同一台租机上的第二个 Compose 项目 `opspilot-staging`，先升级 staging 再升级主项目；`v0.1.0` 与 `v0.1.1` 运行时代码相同这一点接受，证据照实写明。第 1、3、5 步本轮没有重做。

| 步 | 本轮事实状态 | 一句话 |
|---|---|---|
| 2 全流程 | **部分达成** | 七个要素都落在同一个 incident `aa6f1172` 上：告警开单 → Run 留下 4 行证据 → 切模型出口后转人工 → 用户接管 → `register_remediation` → 独立观察 `recovery_confirmed` → 关闭，复盘待审。与裁决顺序不同的是系统外恢复：用户在接管**之前**就恢复了故障，没有留下命令输出，确切时刻也没有记录 |
| 4 钉住版本的构建、升级与恢复 | **部分达成** | staging 项目先按 digest 从 `v0.1.0` 升到 `v0.1.1`，主项目随后升级再按 UPGRADE.md 回滚，命令都退出 0，每次切换后都有数据库快照，计数不变。缺：staging 上在途的恢复观察没有走到确认（40 个样本用完，`expired / max_samples_exhausted`），主项目切换时没有在途工作；staging 接入 lab 需要两处文档外的操作；两个版本运行时代码与 schema 相同；构建与 CI 是引用，没有重跑 |

## 第 4 步：staging 先升级，再升级并回滚主项目

### 版本（引用，本轮没有重跑构建或 CI）

- `v0.1.0`：提交 `dd953431`，镜像 `ghcr.io/kevinwangsheng/opspilot@sha256:228df9f7…`；`v0.1.1`：提交 `9b208cd1`，镜像 `…@sha256:0af7b9ff…`。release 与 CI 运行见[上一轮「构建与 CI」](../f9-final-clean-host-2026-11-11/README.md#构建与-ci引用本轮没有重跑)。
- **两个版本运行时代码相同。** 在 `opspilot/` 下只差版本号和评测代码，schema head 都是 `0024_trace_outbox`（上一轮已核对）。本轮所有 `migrate` 都输出 `schema unchanged: 0024_trace_outbox`。所以这里换的是两个 digest 不同的发布镜像，验证的是按 digest 切换、数据保留和回滚流程，不是一次有代码或 schema 变化的升级。用户已裁决接受这一点。
- 容器内 `/healthz`、`/readyz` 返回的 `version` 与日志里的 `revision` 跟镜像对得上（`0.1.0` / `dd953431…`，`0.1.1` / `9b208cd1…`），见各快照。

### 过程

主项目 `opspilot`（工作台 18090）在 13:16 时跑的是 `v0.1.0` digest（上一轮回滚后的状态），没有进行中的 Run 或观察（[20-snap-main-before.txt](raw/20-snap-main-before.txt)）。

| 时间 | 做了什么 | 结果（原始记录） |
|---|---|---|
| 13:15:08 | `control-secrets --secrets-dir ~/.config/opspilot-staging` | 退出 0，五个文件写入（[10-staging-control-secrets.log](raw/10-staging-control-secrets.log)）。随后加 `DEEPSEEK_API_KEY` 的那一步没有日志 |
| 13:15:23–13:15:35 | `OPSPILOT_PROJECT=opspilot-staging OPSPILOT_SECRETS_DIR=… OPSPILOT_WEB_HOST_PORT=18190 OPSPILOT_IMAGE=<v0.1.0 digest> OPSPILOT_START_AGENT=1 install.sh install` | 退出 0：独立的网络、卷、口令文件，`schema upgraded: 0024_trace_outbox`，四个登录在 head，三个运行时 healthy（[11-staging-install.log](raw/11-staging-install.log)）。这几个变量在 README 设置表里有 |
| 13:15:35 | **文档外**：把 staging 三个容器接入 `kind` 网络，不带别名 | 三条 `rc=0`（[12-staging-network.log](raw/12-staging-network.log)）。没用 `connect-control`，因为它会给 staging 也挂上 `opspilot-*` 别名，和主项目冲突。日志末尾有一个读 `OPSPILOT_OTEL_PROMETHEUS_URL` 的探针报 `KeyError`；之后 staging observer 的样本 `present=True`，说明它查得到 lab Prometheus，探针失败的原因没有查 |
| 13:16:03 | staging 初始快照 | 三个运行时 `/readyz` 200，`0.1.0`，空库（[20-snap-staging-fresh.txt](raw/20-snap-staging-fresh.txt)）；`install.env` 两个项目的键名一致，值没有导出（[21-install-env-keys.txt](raw/21-install-env-keys.txt)） |
| 13:16:25 | `fault inject`（ConfigMap `e47d27c2fa01`） | [30-step4-fault-inject.log](raw/30-step4-fault-inject.log) |
| 13:24:04 | lab Alertmanager 把告警投给主项目 | 主项目没有开新单：投递 `attached` 到还没关的 `1fbdce34`（[supplement/s2-main-db.txt](supplement/s2-main-db.txt) 的 delivery 行） |
| 13:24:09 | **文档外**：[forward_alert.py](raw/forward_alert.py.txt) 从主项目库取这条 firing 告警，按 webhook v4 格式 POST 给 staging | http 200，`created`：incident `69778314-65d3-5e3c-90a1-2b2b58d9ca03`，Run `2dcc2d92-824e-53da-bed6-cc713124f068`（[31-forward-alert.log](raw/31-forward-alert.log)）。staging 不在 lab 告警路由上，只能这样送告警 |
| 约 13:27:41 | staging Run 完成 | worker 日志 `status=published`；Run `completed`，预算 14，工具操作 28，证据 28 行（[40-snap-staging-before-upgrade.txt](raw/40-snap-staging-before-upgrade.txt)）。报告内容没有导出 |
| 13:28:01 | 系统自动授权恢复观察 `23204a25-b054-5c2c-a18c-e559967af7cc` | `auto_authorize_observation`，actor `system`（[supplement/s3-staging-db.txt](supplement/s3-staging-db.txt)） |
| 13:28:09 | `fault restore`（ConfigMap `1ee5c0258e3e`） | [32-step4-fault-restore.log](raw/32-step4-fault-restore.log) |
| 13:28:54 | 两个项目升级前快照 | [40-snap-staging-before-upgrade.txt](raw/40-snap-staging-before-upgrade.txt)、[40-snap-main-before-upgrade.txt](raw/40-snap-main-before-upgrade.txt) |

之后按 [step4b.sh](raw/step4b.sh) 执行，输出 [50-step4.log](raw/50-step4.log)：

| 段 | 时间 | 命令 | 结果 |
|---|---|---|---|
| S1 | 13:29:28 | 保存 staging `v0.1.0` 三个容器日志 | [logs-51-…-staging-*.log](raw/logs-51-before-upgrade-v010-opspilot-staging-worker.log) |
| S2 staging 升级 | 13:29:28–13:29:39 | `env OPSPILOT_PROJECT=opspilot-staging OPSPILOT_SECRETS_DIR=… OPSPILOT_IMAGE=<v0.1.1 digest> install.sh upgrade` | 退出 0；stop → postgres → `schema unchanged` → init-roles → 三个运行时 healthy |
| S3 | 13:29:39 | 文档外：staging 重新接入 `kind` 网络（不带别名） | 三条 `rc=0` |
| S4 | 13:32:09 | 快照（升级后 150 s） | 镜像 `0af7b9ff…`，web、worker `/readyz` 200 `0.1.1`，**observer `/readyz` 503**；观察 `23204a25` 仍 `authorized`，2 个样本（[52-snap-staging-after-upgrade.txt](raw/52-snap-staging-after-upgrade.txt)） |
| M1 | 13:32:10 | 主项目升级前快照与日志 | [53-snap-main-before-upgrade.txt](raw/53-snap-main-before-upgrade.txt)、`logs-53-*` |
| M2 主项目升级 | 13:32:11–13:32:28 | `OPSPILOT_IMAGE=<v0.1.1 digest> install.sh upgrade`，再 `connect-control --project opspilot` | 两条都退出 0；`schema unchanged`；三个运行时带别名接入 |
| M3 | 13:33:28 | 快照 | 镜像 `0af7b9ff…`，三个 `/readyz` 200 `0.1.1`（[54-snap-main-after-upgrade.txt](raw/54-snap-main-after-upgrade.txt)）；M4 保存 `v0.1.1` 日志 `logs-55-*` |
| R1 回滚 | 13:33:29 | `install.sh compose stop web worker observer` | 退出 0 |
| R2 | 13:33:35 | 查 `alembic_version` | `0024_trace_outbox`。两版 head 相同，按 UPGRADE.md 跳过 downgrade，本轮没有跑（上一轮跑过一次，按文档退出 1） |
| R3 | 13:33:35–13:33:45 | `OPSPILOT_IMAGE=<v0.1.0 digest> install.sh upgrade`，再 `connect-control` | 两条都退出 0 |
| R4 | 13:34:46 | 快照 | 镜像 `228df9f7…`，三个 `/readyz` 200 `0.1.0`（[56-snap-main-after-rollback.txt](raw/56-snap-main-after-rollback.txt)） |
| S5 | 13:34:47 | staging 再快照 | 仍是 `0.1.1`；**observer `/readyz` 仍 503**；4 个样本，第 4 个 `healthy`（[57-snap-staging-later.txt](raw/57-snap-staging-later.txt)） |

数据在切换前后：主项目在 53、54、56 三份快照里计数完全相同（incident 2、controls 4、证据 17、样本 25、steps 12、告警投递 8、复盘版本 3，alembic `0024_trace_outbox`）。staging 在 40、52、57 三份快照里证据 28、steps 15、controls 1、投递 1 不变，只有样本数随观察增长。上一轮缺的「每次切换后的数据库快照」这次都有，且是同一个脚本 [snap.sh](raw/snap.sh) 的前后对比。

### 在途工作

- 主项目：切换时没有在途的 Run 或观察（三个观察都在 08:10 前结束），所以主项目的升级与回滚只验证了数据保留，没有验证在途工作的延续。
- staging：观察 `23204a25` 在升级前 33 秒开始。第 1 个样本（窗口止于 13:29:06）在升级前；升级后由 `v0.1.1` 的 observer 接着采，第 2 个起都 `adopted`，说明采样跨过了升级。但它**没有确认恢复**：19:22:55 的最终快照（[72-snap-staging-final.txt](raw/72-snap-staging-final.txt)）是 `expired / max_samples_exhausted`，40 个样本，incident 回到 `open`。

为什么没确认，补查的每个样本（[supplement/s3-staging-db.txt](supplement/s3-staging-db.txt)、[supplement/s1-staging-observer-v011.log](supplement/s1-staging-observer-v011.log)）显示：

| 样本 | 窗口止于 | 结果 |
|---|---|---|
| 1–3 | 13:29:06–13:31:51 | `degraded`（故障 13:28:09 才恢复，flagd 约 1 分钟后生效） |
| 4–7 | 13:33:13–13:37:19 | `healthy` |
| 8 | 13:38:41 | `no_data`（`present=False`），健康连续段断开 |
| 9–11 | 13:40:03–13:42:46 | `healthy` |
| 12–40 | 13:44:08–14:22:24 | 全部 `degraded` |

确认需要连续 600 s 健康（会话的 `sustained_window_seconds`），两段健康都不够长（约 4 分钟、约 3 分钟）。从样本 12 开始的 `degraded` 与第 2 步 13:42:05 在同一个 lab 上注入的故障时间吻合：staging 观察的是同一个 checkout，这次故障直到下午才由用户恢复，40 个样本（会话上限）在 14:22 用完。所以没确认的直接原因是本轮自己的第 2 步故障打断了观察，这是由时间对上的推断，样本的读数没有导出。样本 8 为什么是 `no_data` **未查明**。

### staging observer `/readyz` 503

S4（13:32:09）和 S5（13:34:47）两次探测，staging observer 都是 503，web 和 worker 都是 200；同一时刻没有观察任务的主项目 observer 是 200；观察结束后的 19:22:55 staging observer 回到 200。从补查的 observer 日志看，样本 3 窗口止于 13:31:51、日志落在 13:32:13，样本 5 窗口止于 13:34:35、日志落在 13:34:57，两次探测都在某个样本进行中。这和 [#522「Observer /readyz 在恢复样本进行中报 POLL_STALE（503）」](https://github.com/kevinWangSheng/production-ops-agent/issues/522)描述的现象一致。但 snap.sh 只记录了状态码，没有记录 503 的响应体，原因码是不是 `POLL_STALE` **未确认**。

### 对照验收步骤

| 子项 | 本机记录 | 是否覆盖 |
|---|---|---|
| build、CI validation | `v0.1.0`、`v0.1.1` 的 release 与 CI 运行都 success（上一轮引用） | 覆盖（引用，本轮没有重跑） |
| staging deployment | 同一台租机上的第二个 Compose 项目，用文档里的变量安装；接入 lab 网络与收告警各需要一处文档外操作 | 覆盖，staging 与主项目同机（已裁决接受）；两处文档外操作 |
| Agent upgrade | staging 先 `v0.1.0 → v0.1.1`，主项目随后同样升级 | 覆盖；两版运行时代码与 schema 相同 |
| recovery | 主项目按 UPGRADE.md 回滚到 `v0.1.0` digest，切换后数据不变 | 覆盖回滚；没有跨 schema 的 downgrade；在途观察没有走到确认，主项目切换时没有在途工作 |
| pinned artifacts | 每次都用 `@sha256:` digest，快照里 `docker inspect` 的 `config_image` 与 `image_id` 核对 | 覆盖 |
| explicit environment configuration | 每条命令行给出 `OPSPILOT_PROJECT`、`OPSPILOT_SECRETS_DIR`、`OPSPILOT_IMAGE` 等；两个项目 `install.env` 的键名已导出 | 部分：`install.env` 只导出了键名，没有值 |

## 第 2 步：一个 incident 走完全流程

主项目此时跑的是 `v0.1.0` digest（第 4 步回滚后）。13:41:26 `1fbdce34` 被关闭（`close`，actor `operator`，channel `web`，generation 0→1，[supplement/s2-main-db.txt](supplement/s2-main-db.txt)），之后的告警才会开新单；这一步是谁点的不在归档里。

| 时间 | 事件 | 原始记录 |
|---|---|---|
| 13:42:05 | `fault inject`（ConfigMap `e47d27c2fa01`） | [60-step2-fault-inject.log](raw/60-step2-fault-inject.log) |
| 13:48:04 | 告警 `CheckoutPlaceOrderErrorRatioHigh` firing（startsAt 13:47:54），outcome `created`，actor `alertmanager`：incident `aa6f1172-b014-56bc-8761-fd1883a52cd0`，Run `78419b8b-54a6-5bee-9e47-b915a83c0b9c`，`related_incident_id` 指向已关闭的 `1fbdce34` | [62-handoff-state.txt](raw/62-handoff-state.txt)、[70-step2-timeline.txt](raw/70-step2-timeline.txt) |
| 13:48:04–13:48:18 | Run 两轮：第 1 轮 `metrics_range_query` ok、`traces_search` 报 `RESULT_TOO_LARGE`（未采用）；第 2 轮两次 `metrics_range_query` ok；随后提交了一个 `conclusion:g0:ctx0:round-2` 步（`response_committed`） | [62-incident-page-at-handoff.html](raw/62-incident-page-at-handoff.html) 的 Steps 与 Live progress |
| 13:48:18.975 | 脚本看到 Run `running`，证据 4 行，触发 | [60-midrun-egress-cut.log](raw/60-midrun-egress-cut.log) |
| 13:48:18.996 | root 在 `DOCKER-USER` 链加两条规则，拒绝 worker 两个容器地址的 tcp/443 | 同上 |
| 13:48:23.956 | worker 日志 `status=handed_off reason=MODEL_UNAVAILABLE` | [61-worker-full-log-at-handoff.log](raw/61-worker-full-log-at-handoff.log)（这次是完整的容器日志，不是手抄的一行） |
| 13:48:25.336 | Run `waiting_human` | 60-midrun-egress-cut.log |
| 13:48:25.377 | 删掉两条规则，worker 到 `api.deepseek.com:443` tcp ok。出口断了约 6.4 秒 | 同上 |
| 13:49:05 | 状态：模型预算 2 `spent`、2 `unknown`；证据 4 行；工作台显示 `handoff MODEL_UNAVAILABLE`，没有发布结论；`run_handoff` 事件带 3 个已采用的证据 id | 62-handoff-state.txt、62-incident-page-at-handoff.html |
| 14:48:04 / 15:48 / 16:48 / 17:48 | 同一告警每小时一次 firing，outcome `replayed` | 70-step2-timeline.txt |
| 18:31:04 之前，确切时刻未记录 | 用户在自己的终端恢复故障（`paymentFailure` 回到 off） | [63-fault-restore-by-user.txt](raw/63-fault-restore-by-user.txt)：编排 Agent 在 18:49:50 写的说明，**不是命令日志**。旁证：约 18:35 读到 flagd-config `defaultVariant=off`；sshd 里用户机器的 root 登录持续到 18:27:48 |
| 18:31:04 | 告警 resolved，`resolved_attached` | 70-step2-timeline.txt |
| 18:48:52 | `takeover`，actor `operator`，channel `web`，generation 0→1 | 同上 |
| 18:49:12 | `register_remediation`，`operator`，`web`，1→2，revision `payment:fault-restored`；开出观察 `86636672-0174-5cb2-b21a-54e71065b999`，`authorization_source post_remediation`，`authorized_by operator`，HealthProfile `otel-demo-checkout@b72bbe2e30be` | 同上、[64-snap-after-register.txt](raw/64-snap-after-register.txt) |
| 18:50:17–19:05:18 | 12 个样本全部 `healthy`（间隔 60 s，持续窗口 600 s，上限 40）；`healthy_since` 18:54:23（第 4 个样本） | 70-step2-timeline.txt |
| 19:05:40 | `recovery_confirmed`，lifecycle `observing_recovery → resolved` | 同上、[65-snap-after-recovery.txt](raw/65-snap-after-recovery.txt) |
| 19:22:22 | `close`，`operator`，`web`，2→3；incident `closed`，mode `human_owned`，Run `cancelled` | 70-step2-timeline.txt、[71-snap-final.txt](raw/71-snap-final.txt) |
| 19:23:04 | 复盘 v1 生成，状态 `under_review`；知识库 0 条 | [supplement/s2-main-db.txt](supplement/s2-main-db.txt)；70-step2-timeline.txt 在 19:23:07 计到复盘版本 5（71 号快照在 19:22:55 是 4） |

### 对照验收步骤（按裁决的顺序）

| 要素 | 本机记录 | 是否覆盖 |
|---|---|---|
| 业务流量与故障 | load-generator 持续流量；13:42:05 flagd 注入 | 覆盖（流量没有单独的速率记录） |
| 触发 | Alertmanager webhook 开出 `aa6f1172` 和 Run | 覆盖 |
| 调查、证据 | Run 两轮四次工具调用，证据 4 行（3 行采用，`traces_search` 那行报错未采用）；没有发布报告 | 覆盖「先有证据再转人工」；调查在模型出口被切断时中止，没有结论 |
| 转人工 | 切断模型出口 6.4 秒 → `MODEL_UNAVAILABLE` → `waiting_human` | 覆盖，原因是人为切断（按裁决的做法） |
| 用户接管 | `takeover`，operator，web | 覆盖 |
| 系统外人工处理 | 用户在自己的终端恢复故障；之后在工作台 `register_remediation` | 覆盖，但顺序与裁决不同：恢复在接管之前；没有命令输出 |
| 恢复观察 | `86636672` 由人工登记开出，12 个健康样本后独立确认 | 覆盖 |
| 归档 | 关闭；复盘 v1 待审 | 覆盖到「关闭 + 复盘待审」；没有审核成知识 |

和上一轮相比：这次调查、证据、转人工、人工处理、恢复观察、关闭都在同一个 incident 上。

## 缺口

第 2 步：

- 系统外恢复发生在 `takeover` 之前，不在裁决列出的 `takeover` 与 `register_remediation` 之间。
- 恢复命令的输出没有保留，确切时刻没有记录；只能从 18:27:48 的最后一次 root 登录、18:31:04 的 resolved 投递和约 18:35 读到的 flagd 配置旁证它在 18:31:04 之前。
- 转人工是人为切断模型出口（`DOCKER-USER` REJECT，约 6.4 秒）造成的，不是自然发生的模型故障。
- 转人工（13:48:24）到接管（18:48:52）隔了约 5 小时，是等用户，不是系统延迟。这期间告警每小时重放一次。
- Run 在切断前提交了一个 `conclusion` 步，但没有发布报告；这一步的内容和两个 `unknown` 预算预留对应哪次调用，没有导出（工作台页面只显示状态）。
- 用户的操作（takeover、register_remediation、close）只有数据库审计行，没有工作台截图；13:41:26 关闭 `1fbdce34` 的是谁不在归档里。
- 复盘没有审核，知识库为 0。
- 没有 LangSmith trace（worker 日志 `TRACE_MODE_OFF`），没有记录 token，也没有按余额对账。

第 4 步：

- staging 上在途的恢复观察 `23204a25` 没有确认，以 `expired / max_samples_exhausted` 结束（40 个样本）；从样本 12 开始的 `degraded` 与第 2 步的故障注入时间吻合（推断），样本 8 的 `no_data` 原因未查明。
- 主项目升级与回滚时没有在途工作，只验证了数据保留。
- staging observer `/readyz` 在 S4、S5 两次都是 503，时间落在样本进行中，现象与 #522 一致；503 响应体没有记录，原因码未确认。
- staging 与主项目在同一台租机上（已裁决接受）。接入 lab 需要两处文档外操作：不带别名接入 `kind` 网络，以及用 `forward_alert.py` 转发告警；12 号日志里还有一个失败的环境变量探针，原因没有查。
- `v0.1.0` 与 `v0.1.1` 运行时代码与 schema head 相同（已裁决接受），没有跨 schema 的升级或 downgrade，也没有 `INCOMPATIBLE_STATE` 情形。跨 schema 的演练只在 [F8 第 3 步](../f8-step3-upgrade-rollback/README.md)做过（本机 colima，fixture 档）。
- staging 的 `DEEPSEEK_API_KEY` 放入没有日志；staging `v0.1.1` 期间的容器日志没有在当时保存（observer 日志是事后补的，见 supplement）。
- `install.env` 只导出了键名。
- 构建、CI、release 没有重跑，只引用上一轮记录的 GitHub 运行。

## 这份证据不证明的

- 不是生产观察，不是 soak。每项只跑了一次，单机，合成流量加注入故障，转人工靠切断出口。
- 不证明不同代码或不同 schema 的版本之间能升级与回滚。
- 不证明在途工作能跨升级走到确认恢复。
- 第 1、3、5 步本轮没有重做。
- 本 PR 不改 `feature_list.json`，F9 `passes` 仍为 false；是否达成由独立审查与用户判断。

## 原始记录、补查与脱敏

[raw/](raw/) 是主机 `/home/opspilot/f9-final2/` 下 41 个文件的原样副本（归档 `f9-final2-host-evidence.tgz`，sha256 `5588901ae13837dbabbe78416736780904fa900ff0a627307673c9ad3616fca0`，41 个文件加 1 个目录，存在仓库外）。其中 `60-midrun-egress-cut.log` 属主是 root，其余是 opspilot；`63-fault-restore-by-user.txt` 是编排 Agent 写的说明，不是命令日志。唯一的改动是文件名：`forward_alert.py` 存为 `forward_alert.py.txt`，内容不变（sha256 `0bf0dfe1…` 与归档一致），避免仓库的 ruff 检查把这份原样脚本当成待格式化的代码。

[supplement/](supplement/) 是写这份证据时（约 19:27Z）从主机只读取的三份补充：staging observer 的完整容器日志（`docker logs`），以及主项目与 staging 数据库的 `SELECT` 结果（controls、观察会话与每个样本、复盘版本、知识库计数、13:00 之后的告警投递）。没有在主机上写文件，也没有改动任何服务。

入库前逐个检查过：没有口令、token、API key、cookie 或 CSRF 值，也没有带凭据的 DSN（只有文件名，例如 `ui_password`、`alertmanager_token`，以及 `DEEPSEEK_API_KEY=<key>` 这种占位）；工作台页面 `62-incident-page-at-handoff.html` 没有 CSRF 或会话字段，只有每个表单的 `idempotency_key`（形如 `takeover-0-<32 位十六进制>`，是幂等键，不是凭据，和仓库里已有的工作台页面证据一样原样保留）；地址只有 127.0.0.1、0.0.0.0 和 Docker 内部网段 172.18.x、172.19.x（容器地址，不是主机地址）；主机名只出现别名 `opspilot-f9-1`。没有需要改写的内容，所以没有 `[REDACTED]` 标记，副本内容没有改动。gitleaks 8.30.1（与 CI 同一版本）扫描 raw 与 supplement 无发现。
