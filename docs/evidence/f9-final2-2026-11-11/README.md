# F9 终轮第二次：第 2、4 步重做（2026-11-11）

类别：集成运行。租机 `opspilot-f9-1`（单机：kind lab 加 Compose 项目，`otel-demo` 工具档，真实 DeepSeek 模型），接着[上一轮干净主机终轮](../f9-final-clean-host-2026-11-11/README.md)的同一台机继续做，没有重装。这不是生产证明，也不是 soak；流量是 lab 的 load-generator，故障都是 flagd `paymentFailure` 注入，转人工是切断模型出口造成的。时间均为 UTC，2026-11-11（第 4 步从太平洋时间 11-11 05:15 开始，第 2 步到 11:22 结束）。冻结摘要见 [summary.json](summary.json)，主机原始文本在 [raw/](raw/)，事后补的只读查询在 [supplement/](supplement/)（见末节）。

上一轮第 2、4 步判为部分达成。独立审查后没有翻 `passes`，用户裁决：第 2 步在**一个** incident 上重做（先有证据，再切模型出口，转人工，用户接管，系统外恢复，`register_remediation`，`recovery_confirmed`，关闭）；第 4 步的 staging 是同一台租机上的第二个 Compose 项目 `opspilot-staging`，先升级 staging 再升级主项目；`v0.1.0` 与 `v0.1.1` 运行时代码相同这一点接受，证据照实写明。第 1、3、5 步本轮没有重做。

| 步 | 本轮事实状态 | 一句话 |
|---|---|---|
| 2 全流程 | **部分达成** | 七个要素都落在同一个 incident `aa6f1172` 上：告警开单 → Run 留下 4 行证据 → 切模型出口后转人工 → 用户接管 → `register_remediation` → 独立观察 `recovery_confirmed` → 关闭，复盘待审。与裁决顺序不同的是系统外恢复：用户在接管**之前**就恢复了故障，没有留下命令输出，确切时刻也没有记录 |
| 4 钉住版本的构建、升级与恢复 | **部分达成** | staging 项目先按 digest 从 `v0.1.0` 升到 `v0.1.1`，主项目随后升级再按 UPGRADE.md 回滚，命令都退出 0，每次切换后都有数据库快照，计数不变。缺：staging 上在途的恢复观察没有走到确认（40 个样本用完，`expired / max_samples_exhausted`），主项目切换时没有在途工作；staging 接入 lab 需要两处文档外的操作；两个版本运行时代码与 schema 相同；构建与 CI 是引用，没有重跑 |

上表是 13:15–14:22 那一轮的记录，保持原样。独立审查认为第 4 步只差「在途观察跨 digest 切换后走到 `recovery_confirmed`」。20:01–20:36 只重做了 staging 的恢复/升级这一段，观察窗口内没有再注入故障：同一个观察会话 `27c61712` 在 `v0.1.0` 上取了第 1 个样本，切到 `v0.1.1` 后继续取样，以 `completed / recovery_confirmed` 结束。见下文[第 4 步重做](#第-4-步重做staging-在途观察跨-digest-切换2001z2036z)。

独立审查认为第 2 步未达成，唯一原因是系统外恢复在接管之前、没有命令输出和确切时刻。21:44–22:28 在主项目上按裁决顺序重做了一个 incident `65b0490a`，见下文[第 2 步重做](#第-2-步重做一个-incident-按裁决顺序走完2144z2228z)。

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

## 第 4 步重做：staging 在途观察跨 digest 切换（20:01Z–20:36Z）

只重做 staging 的恢复/升级这一段，为的是补上面 `23204a25` 没有走到确认的缺口。主项目没有升级。原始记录在 [raw-step4-rerun/](raw-step4-rerun/)，是主机 `/home/opspilot/f9-final3/` 的副本。命令沿用上一轮：[snap.sh](raw-step4-rerun/snap.sh) 与上一轮相同，[forward_alert.py](raw-step4-rerun/forward_alert.py.txt) 与上一轮逐字节相同（sha256 `0bf0dfe1…`），接入 `kind` 网络仍然不带别名。新写的脚本只有：回滚 [step1.sh](raw-step4-rerun/step1.sh)、升级 [step4.sh](raw-step4-rerun/step4.sh)、观察授权后立即恢复故障的 [restore_when_authorized.sh](raw-step4-rerun/restore_when_authorized.sh)、关闭旧 incident 的 [close_staging.py](raw-step4-rerun/close_staging.py.txt)、导出 [final.sh](raw-step4-rerun/final.sh)，以及只读的 [q.sh](raw-step4-rerun/q.sh)、[state.sh](raw-step4-rerun/state.sh)、[flag.sh](raw-step4-rerun/flag.sh)。

| 时间 | 做了什么 | 结果（原始记录） |
|---|---|---|
| 20:01:22 | 预检，只读 | `kind_lab.py health` 为 ok；`paymentFailure` 为 `off`；两个项目三个运行时 `/readyz` 都是 200，主项目在 `0.1.0`、staging 在 `0.1.1`；主项目 incident 全部 `closed`；staging 的 `69778314` 仍是 `open`，它唯一的观察已经 `expired`（[00-preflight.log](raw-step4-rerun/00-preflight.log)、[01-snap-main-preflight.txt](raw-step4-rerun/01-snap-main-preflight.txt)、[01-snap-staging-preflight.txt](raw-step4-rerun/01-snap-staging-preflight.txt)） |
| 20:03:34 | **本轮决定**：在 staging 上关闭 `69778314`（operator 通过工作台控制接口 `POST /incidents/<id>/control action=close`，Basic Auth 加 staging 工作台的 Origin，和点 Close 按钮走的是同一条路径） | http 200，generation 0→1，`closed`（[02-staging-close-old-incident.log](raw-step4-rerun/02-staging-close-old-incident.log)）。原因：incident 没关时，同指纹的新告警只会 `attached`，不开新 Run（`opspilot/persistence/alerts.py`）；自动观察对每个 `(incident, control generation)` 只决定一次（`opspilot/observation/store.py` `auto_observation_candidates`）。不关这个单就得不到新的 Run 和新的自动观察。主项目 13:41:26 关 `1fbdce34` 用的也是 close。没有手写数据库 |
| 20:03:57–20:05:07 | 按 UPGRADE.md「Rolling back」把 staging 回滚到 `v0.1.0` digest | 先保存 `v0.1.1` 日志（`logs-10-*`）；`compose stop web worker observer` 退出 0；`alembic_version` 为 `0024_trace_outbox`，与旧版 head 相同，按文档跳过第 2 步 downgrade；`OPSPILOT_IMAGE=<v0.1.0 digest> install.sh upgrade` 退出 0，`schema unchanged`；不带别名重新接入 `kind`，三条 `rc=0`；快照：三个运行时 `0.1.0`、`/readyz` 200（[10-step1-staging-rollback.log](raw-step4-rerun/10-step1-staging-rollback.log)、[11-snap-staging-after-rollback.txt](raw-step4-rerun/11-snap-staging-after-rollback.txt)） |
| 20:05:38 | `fault inject --experiment-id f9-final3-step4`（ConfigMap `e47d27c2fa01`） | rc 0（[20-fault-inject.log](raw-step4-rerun/20-fault-inject.log)）。这是本轮唯一一次注入 |
| 20:10:04 | lab Alertmanager 把告警投给主项目 | 主项目 `created`：incident `c030a3a3-0369-5ba3-a7fd-d44066a31286`，Run `0a2f20e4-7fc1-57f1-9fbb-479bba283922` |
| 20:10:10 | **文档外**（同上一轮）：`forward_alert.py` 转发给 staging | http 200，`created`：incident、Run 的 id 和主项目相同（`c030a3a3`、`0a2f20e4`，同一条告警推出同一个 id），`related_incident_id` 指向刚关闭的 `69778314`（[21-forward-alert.log](raw-step4-rerun/21-forward-alert.log)） |
| 20:12:58 | staging Run 发布 | worker 日志 `status=published`（`v0.1.0`）；Run `completed`，预算 12，工具操作 21（[logs-41-…-staging-worker.log](raw-step4-rerun/logs-41-before-upgrade-v010-opspilot-staging-worker.log)） |
| 20:13:03 | 系统自动授权恢复观察 `27c61712-da7f-5afb-a127-56e64eab168c` | `auto_authorize_observation`，actor `system`，`authorization_source automatic`；间隔 60 s，持续窗口 600 s，上限 40，截止 21:13:03 |
| 20:13:31 | `fault restore`（ConfigMap `1ee5c0258e3e`），由 [restore_when_authorized.sh](raw-step4-rerun/restore_when_authorized.sh) 在看到 staging 授权后立即执行 | rc 0，之后 `paymentFailure` 为 `off`（[30-fault-restore.log](raw-step4-rerun/30-fault-restore.log)）。从这一刻到 20:36 收尾，lab 上没有再注入任何东西 |
| 20:14:31 | staging `v0.1.0` observer 写入第 1 个样本 | `degraded`，窗口止于 20:14:08，`adopted` |
| 20:14:47 | 升级前检查与快照 | 会话 `authorized`，1 个样本；三个运行时 `0.1.0`、`/readyz` 200（[40-snap-staging-before-upgrade.txt](raw-step4-rerun/40-snap-staging-before-upgrade.txt)），保存 `v0.1.0` 日志 `logs-41-*` |
| **20:14:48–20:14:59** | **切换**：`OPSPILOT_IMAGE=<v0.1.1 digest> install.sh upgrade` | 退出 0；stop → postgres → `schema unchanged: 0024_trace_outbox` → init-roles → 三个运行时 healthy；不带别名重新接入 `kind`，三条 `rc=0`（[42-step4-staging-upgrade.log](raw-step4-rerun/42-step4-staging-upgrade.log)） |
| 20:14:59 | 切换后立即查 | 同一会话 `27c61712` 仍是 `authorized`，1 个样本 |
| 20:15:56 | `v0.1.1` observer 写入第 2 个样本 | `degraded`，`adopted`（[logs-52-…-staging-observer.log](raw-step4-rerun/logs-52-on-v011-opspilot-staging-observer.log)） |
| 20:16:30 | 升级后 90 s 快照 | 三个运行时 `0.1.1`，**observer `/readyz` 这次是 200**；会话 `authorized`，2 个样本；证据 48、steps 28、controls 3、投递 2、复盘版本 2，与升级前的 40 号快照相同，样本 41→42（[43-snap-staging-after-upgrade.txt](raw-step4-rerun/43-snap-staging-after-upgrade.txt)） |
| 20:35:03 | 会话结束 | **`completed / recovery_confirmed`**，16 个样本，`healthy_since` 20:23:46；incident `c030a3a3` 的 lifecycle 变为 `resolved`，mode `automatic`（[50-staging-session-samples.txt](raw-step4-rerun/50-staging-session-samples.txt)、[51-snap-staging-final.txt](raw-step4-rerun/51-snap-staging-final.txt)） |

staging 会话 `27c61712` 的 16 个样本（每个窗口 5 分钟，全部 `adopted`）：

| 样本 | 窗口止于 | 结果 | 由谁采 |
|---|---|---|---|
| 1 | 20:14:08 | `degraded` | `v0.1.0` observer（日志 `logs-41`，20:14:31 写入） |
| 2–3 | 20:15:35、20:16:56 | `degraded` | `v0.1.1` observer（日志 `logs-52`，下同） |
| 4–5 | 20:18:18、20:19:40 | `healthy` | |
| 6–7 | 20:21:02、20:22:24 | `no_data`（日志 `present=True`） | |
| 8–16 | 20:23:46–20:34:42 | `healthy` | |

第 8 个样本起连续健康，第 16 个样本（窗口止于 20:34:42）满 600 s，20:35:03 确认。切换前后是同一个 session id `27c61712-da7f-5afb-a127-56e64eab168c`，1 个样本在旧 digest 上，15 个样本在新 digest 上。

**主项目对同一条告警做了什么**（没有升级，一直在 `v0.1.0`，没有操作工作台）：20:10:04 开出 `c030a3a3`，Run `0a2f20e4` 在 20:12:40 发布（预算 9，工具操作 17），20:12:43 自动授权观察，session id 同样是 `27c61712`；20:17:04 收到 resolved 投递（`resolved_attached`）；观察 16 个样本（1–3 `degraded`，4–6 `healthy`，7 `no_data`，8–16 `healthy`），20:34:39 `completed / recovery_confirmed`，lifecycle `resolved`；20:36:12 复盘 v1 生成。最终状态：incident `c030a3a3` 为 `resolved`、mode `automatic`、**没有关闭**，没有在途的 Run 或观察；其他 incident 都已 `closed`（[50-main-session-samples.txt](raw-step4-rerun/50-main-session-samples.txt)、[51-snap-main-final.txt](raw-step4-rerun/51-snap-main-final.txt)、[logs-53-main-*](raw-step4-rerun/logs-53-main-since-2000Z-opspilot-worker.log)）。是否关闭这个单留给编排者决定。

**两个项目的最终状态**（20:36:12，[54-final-flag-and-state.txt](raw-step4-rerun/54-final-flag-and-state.txt)）：`paymentFailure` 为 `off`。staging 在 `v0.1.1` digest，`69778314` 为 `closed`，`c030a3a3` 为 `resolved`（没有关闭），没有在途的 Run 或观察。主项目在 `v0.1.0` digest，情况见上段。

### 这次重做的缺口

- 关闭 staging 旧单 `69778314` 是本轮为得到新观察而做的一次人工控制（operator，通过工作台控制接口，不是浏览器点击，没有截图）。它触发了 staging 对 `69778314` 的复盘：20:10:03 第一次 `failed code=MODEL_UNAVAILABLE`，20:11:42 第二次成功（v2）；第一次失败的原因没有查。
- 这次切换是 `v0.1.0 → v0.1.1`，两版运行时代码与 schema head 相同（已裁决接受），所以验证的是按 digest 切换时在途观察的延续，不是跨代码或跨 schema 的延续。staging 回滚时跳过了 downgrade（同 head，按文档）。
- staging 仍靠两处文档外操作接入 lab：不带别名接入 `kind` 网络，`forward_alert.py` 只转发 firing，所以 staging 没有收到 resolved 投递；它的 lifecycle 是观察确认后变成 `resolved` 的。
- 样本 6–7（staging）和样本 7（主项目）是 `no_data`，日志里却是 `present=True`；两个项目在相邻时间都出现，像是 lab 侧的现象，原因**未查**。上一轮样本 8 的 `no_data` 也未查。
- 配置的采样间隔是 60 s，实际相邻样本约 82 s；本轮没有查原因。
- 只有一次切换、一个会话，单机，合成流量加注入故障；不是 soak，也不是生产。
- 没有 LangSmith trace，没有记录 token，也没有按余额对账（两个 Run 加一次复盘重试的 DeepSeek 调用）。
- 主项目这次没有升级，主项目的升级与回滚仍只有上面 13:32–13:34 的数据保留记录，没有在途工作。

### 原始记录

[raw-step4-rerun/](raw-step4-rerun/) 是主机 `/home/opspilot/f9-final3/` 下 39 个文件的原样副本（归档 `f9-final3-host-evidence.tgz`，sha256 `6584e7a849935926729f307cd18b85ca61ecf8eef3556f3e854f579cf1a3dbd8`，39 个文件加 1 个目录，存在仓库外）。改动有两处。一是两个文件名：`forward_alert.py` 存为 `forward_alert.py.txt`（sha256 `0bf0dfe1…`），`close_staging.py` 存为 `close_staging.py.txt`（sha256 `a011b742…`），内容不变，避免 ruff 把原样脚本当成待格式化的代码。二是 [02-staging-close-old-incident.log](raw-step4-rerun/02-staging-close-old-incident.log) 第 3 行：`close_staging.py` 发出的 `idempotency_key` 是 `close-0-<32 位十六进制>`，这是幂等键，不是凭据，但 gitleaks 的 `generic-api-key` 规则会把它当成密钥，所以仓库副本里把这 32 位换成了 `<32-hex nonce omitted in the repo copy>`；归档里是原值。入库前逐个检查过：只出现凭据文件名（`ui_password`、`alertmanager_token`）和读取它们的代码，没有值；另外把主机上两个项目的 `ui_password`、`alertmanager_token` 和 `DEEPSEEK_API_KEY` 的实际值与全部 39 个文件逐字比对，没有命中。gitleaks 8.30.1 扫描整个证据目录无发现。

## 第 2 步：一个 incident 走完全流程

本节是 13:41–19:23 那次的记录，保持原样；按裁决顺序的重做见[第 2 步重做](#第-2-步重做一个-incident-按裁决顺序走完2144z2228z)。

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

## 第 2 步重做：一个 incident 按裁决顺序走完（21:44Z–22:28Z）

为补独立审查指出的缺口（系统外恢复在接管之前，没有命令输出和确切时刻），在主项目 `opspilot`（`v0.1.0` digest，三个运行时 13:33:40 起没有重启）上重做一个 incident，按裁决顺序：先有证据 → 切模型出口 → 转人工 → 用户接管 → 系统外恢复 → `register_remediation` → `recovery_confirmed` → 关闭。原始记录在 [raw-step2-redo/](raw-step2-redo/)，是主机 `/home/opspilot/f9-final4/` 的副本；事后补的只读查询是 [supplement/s4-step2-redo-db.txt](supplement/s4-step2-redo-db.txt)（SQL 在 [s4-step2-redo-db.sql](supplement/s4-step2-redo-db.sql)）和 [supplement/s5-step2-redo-host.txt](supplement/s5-step2-redo-host.txt)。[snap.sh](raw-step2-redo/snap.sh) 与前两次逐字节相同；切出口的 [block_model_midrun4.sh](raw-step2-redo/block_model_midrun4.sh) 是新写的，按「本脚本启动后出现的新 Run 有 N 行证据」触发（第一次用的脚本没有入库，两者差别无法对照）。

| 时间 | 事件 | 原始记录 |
|---|---|---|
| 21:43:04 | 预检快照：三个运行时 `/readyz` 200，`0.1.0` / `dd953431…`；`c030a3a3`（第 4 步重做时的告警开出的单）`resolved`、没有关闭，没有在途的 Run 或观察 | [00-snap-main-preflight.txt](raw-step2-redo/00-snap-main-preflight.txt) |
| 21:44:02 | **准备**：用户在工作台关闭 `c030a3a3`（`close`，operator，web）。不关的话同指纹的新告警只会 `attached`。副作用：21:45:38 生成了 `c030a3a3` 的复盘 v2 | [01-prior-incident-closed-by-user.txt](raw-step2-redo/01-prior-incident-closed-by-user.txt)、[61-worker-full-log-at-handoff.log](raw-step2-redo/61-worker-full-log-at-handoff.log) |
| 21:44:12 | 编排 Agent 以 root 启动切出口脚本，记下已有的 4 个 Run | [60-midrun-egress-cut.log](raw-step2-redo/60-midrun-egress-cut.log) |
| 21:44:14 | 编排 Agent `fault inject --experiment-id f9-final4-step2`（ConfigMap `e47d27c2fa01`），rc 0 | [60-step2-fault-inject.log](raw-step2-redo/60-step2-fault-inject.log) |
| 21:51:04.374 | **触发**：告警 firing（startsAt 21:50:54），outcome `created`，actor `alertmanager`：incident `65b0490a-df80-51b2-adfa-698c53a9ee7b`，Run `50936b1d-2e18-5f18-8dd1-caa0afcd84f9`，`related_incident_id` 指向 `c030a3a3`；21:51:04.475 `run_claimed` | [70-step2-timeline.txt](raw-step2-redo/70-step2-timeline.txt)、s4 的 `delivery` 与 `subject_event` 1–2 |
| 21:51:06.766–21:51:07.686 | **调查**第 1 轮（`ctx0:round-1`，`deepseek-flash`，`tool_calls`）：`traces_search` 报错、未采用；`metrics_range_query` ok、采用 | s4 的 `step` 0、`evidence`、`subject_event` 3–5 |
| 21:51:11.680–21:51:11.775 | 第 2 轮（`ctx0:round-2`）提交，`read_evidence` ok、采用，此时证据 3 行 | s4 的 `step` 1、`subject_event` 6–7 |
| 21:51:11.792 | 脚本看到 Run `running`、证据 3 行，触发 | 60-midrun-egress-cut.log |
| 21:51:11.815 | **切出口**：`DOCKER-USER` 两条规则拒绝 worker 两个容器地址的 tcp/443 | 同上 |
| 21:51:12.589 | 第 2 轮第二个工具 `metrics_range_query` ok、采用，证据第 4 行。它落在切断之后（工具走 lab 内网的 Prometheus，不经 443） | s4 的 `subject_event` 8 |
| 21:51:12.613 / 21:51:12.617 | **转人工**：`conclusion:g0:ctx0:round-2` 步，`execution failed`、`handoff_reasons ["MODEL_UNAVAILABLE"]`、`report_content null`；`run_handoff` 事件 `published false`、`report_sha256 null`，引用 3 个已采用证据 id（报错的 `traces_search` 不在内）；worker 日志 `status=handed_off reason=MODEL_UNAVAILABLE` | s4 的 `step` 2、`subject_event` 9；61-worker-full-log-at-handoff.log |
| 21:51:13.999 | Run `waiting_human` | 60-midrun-egress-cut.log |
| 21:51:14.044 | 删掉两条规则，worker 到 `api.deepseek.com:443` tcp ok。出口断了约 2.2 秒 | 同上 |
| 21:51:35 | 快照：incident `open`、mode `automatic`、generation 0；Run `waiting_human`，预算 2，工具操作 4 | [62-snap-at-handoff.txt](raw-step2-redo/62-snap-at-handoff.txt) |
| 21:59:44.932 | **用户接管**：`takeover`，operator，web，generation 0→1 | 70-step2-timeline.txt、s4 的 `control` 与 `subject_event` 10 |
| 21:59:59 | **系统外恢复**：用户从自己的机器经 Claude Code `!` 前缀执行 `ssh … kind_lab.py fault restore --experiment-id f9-final4-step2`；输出：ConfigMap `1ee5c0258e3e`，`paymentFailure -> off`，rc 0，前后时间戳都是 21:59:59Z | [63-fault-restore-by-user.log](raw-step2-redo/63-fault-restore-by-user.log)：**编排会话里捕获的输出，由 Agent 在 22:00:11 存到主机，不是主机上写出的日志**（见下）。主机侧旁证见 s5 |
| 22:00:16.917 | **`register_remediation`**：operator，web，1→2，revision `payment:fault-restored`；开出观察 `b4367964-62b9-5889-a6cb-29c9fce8f318`，`authorization_source post_remediation`，`authorized_by operator`，HealthProfile `otel-demo-checkout@b72bbe2e30be`，间隔 60 s，持续窗口 600 s，上限 40 | 70-step2-timeline.txt、s4 的 `control` |
| 22:03:04 | 告警 resolved，`resolved_attached` | 70-step2-timeline.txt |
| 22:01:18–22:16:18 | 12 个样本（窗口止于）：1–3 `degraded`（22:01:18、22:02:40、22:04:01），4–12 `healthy`（22:05:23–22:16:18），全部 `adopted`；`healthy_since` 22:05:23 | 70-step2-timeline.txt、[73-observer-log.log](raw-step2-redo/73-observer-log.log) |
| 22:16:40.583 | **恢复确认**：`completed / recovery_confirmed`，lifecycle `observing_recovery → resolved` | 70-step2-timeline.txt、s4 的 `observation_ending` |
| 22:27:58.085 | **关闭**：`close`，operator，web，2→3；incident `closed`、mode `human_owned`、state `cancelled`，Run `cancelled` | 70-step2-timeline.txt、[71-snap-final.txt](raw-step2-redo/71-snap-final.txt) |
| 22:28:50 | **归档**：复盘 `bedcab61-a4e2-4d0c-98ae-087e01b6b03c` v1，`under_review`，`last_run_id` 为本 Run，`observation_session_id` 为 `b4367964`；生成用 `deepseek-flash` 1 次请求（22:28:08–22:28:50）；知识库 0 条 | s4 的 `postmortem*`、`subject_event` 15。70 号时间线（22:28:24）和 71 号快照（22:28:14）早于它，所以里面的复盘计数 7 不含这一条 |

### 系统外恢复的记录从哪来

主机上的 [63-fault-restore-by-user.log](raw-step2-redo/63-fault-restore-by-user.log) 不是 `tee` 写出的：用户粘贴的命令折成了两行，`tee` 没拿到文件参数，第二行把路径当命令执行，报 `No such file or directory`。文件内容是编排会话里捕获的命令和输出，由 Agent 在 22:00:11 存到主机，文件头写明了这一点。所以**这份文件是 Agent 保存的转录，不是主机写出的日志**。

主机侧能独立核对的（[s5-step2-redo-host.txt](supplement/s5-step2-redo-host.txt)，事后只读取）：

- 系统日志：21:59:58.950 一次 root ssh 登录（session 2808），21:59:59.317–21:59:59.540 `su` 到 `opspilot`，21:59:59.722 断开，与命令形状 `ssh … "su - opspilot -c '…'"` 和输出里的 21:59:59Z 一致。22:00:11.336–22:00:12.167 的 session 2809 与 Agent 存文件的时间一致。
- lab ConfigMap `flagd-config` 的 `managedFields`：最后一次 `kubectl-patch Update` 在 21:59:59Z；现在 `paymentFailure` 为 `off`。
- 这两条只能证明恢复发生在 21:59:59（秒级），在接管（21:59:44.932）之后约 14 秒、`register_remediation`（22:00:16.917）之前约 17 秒，不依赖转录。**不能证明是谁执行的**：Agent 和用户的 ssh 来自同一台机器、同一把钥匙。「用户执行」只有转录文件头和编排者的陈述。

### 对照验收步骤与裁决顺序

F9 第 2 步原文的七个要素：

| 要素 | 本次记录 | 原始文件 |
|---|---|---|
| business traffic and a fault | load-generator 持续流量（没有单独的速率记录，观察样本每个窗口 `requests=24`）；21:44:14 flagd 注入 | 60-step2-fault-inject.log、73-observer-log.log |
| trigger | Alertmanager webhook `created` → `65b0490a` 与 Run `50936b1d` | 70-step2-timeline.txt、s4 |
| investigation | 两轮成功的模型调用，四次工具调用；切断后的模型调用失败 | s4 的 `step`、`subject_event` 3–9 |
| evidence | 4 行，3 行采用（两个 `metrics_range_query`、一个 `read_evidence`），`traces_search` 报错未采用；转人工事件引用 3 个采用的 id；证据内容没有导出 | s4 的 `evidence`、`subject_event` 9 |
| human handoff | `MODEL_UNAVAILABLE` → `waiting_human`，没有发布报告 | 60-midrun-egress-cut.log、61-worker-full-log-at-handoff.log、s4 |
| external human handling | 接管后用户在系统外恢复故障，再在工作台 `register_remediation` | 70-step2-timeline.txt、63-fault-restore-by-user.log、s5 |
| recovery observation | `b4367964` 由人工登记开出，3 个 `degraded` 后 9 个 `healthy`，独立确认 | 70-step2-timeline.txt、73-observer-log.log |
| incident archive | 关闭；复盘 v1 `under_review` | 70-step2-timeline.txt、71-snap-final.txt、s4 |

裁决顺序逐项对照（都在 `65b0490a` 上）：

| # | 裁决 | 时刻 | 依据 |
|---|---|---|---|
| 1 | 先有证据 | 21:51:11.775 证据第 3 行 | s4 `subject_event` 7；脚本 21:51:11.792 看到 3 行 |
| 2 | 切模型出口 | 21:51:11.815 | 60-midrun-egress-cut.log |
| 3 | 转人工 | 21:51:12.617 | s4 `subject_event` 9、worker 日志 |
| 4 | 用户接管 | 21:59:44.932 | `opspilot_controls` |
| 5 | 系统外恢复 | 21:59:59 | 63（转录）、s5（系统日志、`managedFields`） |
| 6 | `register_remediation` | 22:00:16.917 | `opspilot_controls` |
| 7 | `recovery_confirmed` | 22:16:40.583 | 观察会话、`observation_ending` |
| 8 | 关闭 | 22:27:58.085 | `opspilot_controls` |

按这些原始记录，八个时刻严格递增，没有颠倒。唯一的细节：第 4 行证据（21:51:12.589）是在切断之后、转人工之前写入的，转人工引用的 3 个 id 里有它。

### 这次重做的缺口

- 转人工是人为切断模型出口（`DOCKER-USER` REJECT，约 2.2 秒）造成的，不是自然发生的模型故障。
- 系统外恢复的命令记录是 Agent 保存的转录，不是主机上写出的日志；主机侧只有时间旁证（系统日志的 `su`、ConfigMap 的 `managedFields`），不能区分执行者是用户还是 Agent。
- Run 留下了证据，但转人工前没有发布报告（`published false`、`report_content null`）；证据内容和两次失败调用的细节没有导出。
- 模型预算 4 个预留：2 个 `spent`（2.27 s、3.98 s），2 个 `unknown`（各计 360 s）。conclusion 步记 `model_requests_used 4`，成功的只有 2 步，所以推断这两个 `unknown` 是切断后失败的两次调用；预留行没有时间戳，无法直接核对。
- 用户的操作（关闭 `c030a3a3`、接管、`register_remediation`、关闭）只有数据库审计行，没有工作台截图。
- 复盘 v1 没有审核，知识库为 0。
- 没有 LangSmith trace：worker 日志 `trace outbox delivery off: TRACE_MODE_OFF`，这个 incident 的 11 行 trace outbox 全部 `pending`。没有按余额对账；token 只有库里的记录：两次成功的模型调用共 14701（5545 + 9156），复盘生成 prompt 17179、completion 9173；两次失败调用的 token 未知。
- 关闭前一个 incident `c030a3a3` 是为这次重做做的准备，顺带生成了它的复盘 v2。
- 每个环节只跑了一次，单机，合成流量加注入故障；不是 soak，也不是生产。

### 原始记录与脱敏

[raw-step2-redo/](raw-step2-redo/) 是归档 `f9-final4-host-evidence.tgz`（sha256 `3816ed0251248956b5901b80d76b3c5027c0a90e1c22104c36a289bcf4f1f02e`，14 个文件加 1 个目录，存在仓库外）的原样副本，没有改名也没有改内容（没有 `.py` 文件）。`61-worker-full-log-at-handoff.log` 与 `72-worker-log-final.log` 内容相同：handoff 之后 worker 到 72 号导出（约 22:28）为止没有再写日志；复盘生成于 22:28:50，推断那一行日志晚于导出，没有补取。`60-midrun-egress-cut.log` 是 root 脚本的输出，`63-fault-restore-by-user.log` 是 Agent 保存的转录（见上）。

supplement 的 s4、s5 是写这份证据时（约 22:31Z）从主机只读取的：s4 是主项目数据库的 `SELECT`（意图写在文件头，SQL 原文同目录）；s5 是 `iptables -S DOCKER-USER`（现在只剩空链）、21:59:50–22:00:15 的系统日志（只留 root 登录、`su`、logind 行，过滤掉互联网扫描的失败登录）和 `flagd-config` 的 `managedFields`。主机上没有写文件，也没有改动任何服务。s4 里有三处是查询时主动省略的：intake 事件的 `question` 只给 sha256，三条 control 的 `idempotency_key` 不导出（形如 `…:takeover-0-<32 位十六进制>`，是幂等键，不是凭据；省略是为了不再碰上 gitleaks `generic-api-key` 的误报）。s5 的 sshd 行里，客户端地址、端口和密钥指纹在本地保存前换成 `<client omitted>`。

入库前逐个检查过：没有口令、token、API key、cookie 或带凭据的 DSN；地址只有 127.0.0.1、0.0.0.0 和 Docker 内部网段 172.18.0.4、172.19.0.3。另外把主机两个项目凭据目录里所有长度不小于 12 的值（`ui_password`、`alertmanager_token`、`postgres_password`、各 `*_pgpass`、`web_event_tokens`、`web_ui_users`、`worker_env` 里的 `DEEPSEEK_API_KEY` 等，共 35 个）与 raw-step2-redo 和 s4、s5 逐字比对（比对在主机上进行，值没有离开主机），只命中 `alertmanager` 这个词本身（`web_event_tokens` 里的调用方名称，不是密钥）。gitleaks 8.30.1 扫描 raw-step2-redo 与 supplement 无发现。

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
- 不证明在途工作能跨升级走到确认恢复。（13:15–14:22 那一轮如此；20:01–20:36 的重做里，staging 上的一个观察会话跨同代码的 digest 切换走到了 `recovery_confirmed`，见上文重做一节。）
- 第 1、3、5 步本轮没有重做。
- 本 PR 不改 `feature_list.json`，F9 `passes` 仍为 false；是否达成由独立审查与用户判断。

## 原始记录、补查与脱敏

[raw/](raw/) 是主机 `/home/opspilot/f9-final2/` 下 41 个文件的原样副本（归档 `f9-final2-host-evidence.tgz`，sha256 `5588901ae13837dbabbe78416736780904fa900ff0a627307673c9ad3616fca0`，41 个文件加 1 个目录，存在仓库外）。其中 `60-midrun-egress-cut.log` 属主是 root，其余是 opspilot；`63-fault-restore-by-user.txt` 是编排 Agent 写的说明，不是命令日志。唯一的改动是文件名：`forward_alert.py` 存为 `forward_alert.py.txt`，内容不变（sha256 `0bf0dfe1…` 与归档一致），避免仓库的 ruff 检查把这份原样脚本当成待格式化的代码。

[supplement/](supplement/) 是写这份证据时（约 19:27Z）从主机只读取的三份补充：staging observer 的完整容器日志（`docker logs`），以及主项目与 staging 数据库的 `SELECT` 结果（controls、观察会话与每个样本、复盘版本、知识库计数、13:00 之后的告警投递）。没有在主机上写文件，也没有改动任何服务。

入库前逐个检查过：没有口令、token、API key、cookie 或 CSRF 值，也没有带凭据的 DSN（只有文件名，例如 `ui_password`、`alertmanager_token`，以及 `DEEPSEEK_API_KEY=<key>` 这种占位）；工作台页面 `62-incident-page-at-handoff.html` 没有 CSRF 或会话字段，只有每个表单的 `idempotency_key`（形如 `takeover-0-<32 位十六进制>`，是幂等键，不是凭据，和仓库里已有的工作台页面证据一样原样保留）；地址只有 127.0.0.1、0.0.0.0 和 Docker 内部网段 172.18.x、172.19.x（容器地址，不是主机地址）；主机名只出现别名 `opspilot-f9-1`。没有需要改写的内容，所以没有 `[REDACTED]` 标记，副本内容没有改动。gitleaks 8.30.1（与 CI 同一版本）扫描 raw 与 supplement 无发现。
