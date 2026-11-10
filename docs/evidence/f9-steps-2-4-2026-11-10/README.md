# F9 第 2–4 步：全流程、杀 worker、钉住版本的升级与回滚（2026-11-10）

类别：集成运行。主机是 F9 第 1 步的同一台租机 `opspilot-f9-1`（单机，kind lab + Compose 项目 `opspilot`，`otel-demo` 工具档，真实 DeepSeek `deepseek-flash`），状态见 [F9 第 1 步证据](../f9-clean-install-2026-11-09/README.md)。这不是生产证明，也不是 soak；故障都是 flagd `paymentFailure` 注入，流量是 lab 的 load-generator。冻结摘要见 [summary.json](summary.json)。时间为 UTC。

| 步 | 结论 | 一句话 |
|---|---|---|
| 2 全流程 | **部分通过** | 触发、调查、证据、恢复观察、关闭与复盘都有记录，关闭是用户本人在工作台做的。本次 Run 没有转人工（`handoff: false`），也没有「系统外人工处理」的登记（`register_remediation`），所以 human handoff 与 external human handling 两项在本机没有覆盖 |
| 3 杀 worker | **通过** | Run 进行中 `docker kill` worker，租约到期后新 worker 以 epoch 2 从下一轮续跑，同一个 incident，没有重复 incident，报告发布，恢复观察照常走到 `recovery_confirmed` |
| 4 钉住版本 | **部分通过** | 本机构建 → `v0.1.0` 发布镜像（按 digest）→ 按 UPGRADE.md 回滚到本机构建 → 再升回 `v0.1.0`，三次切换都带着在途工作，数据都在，升级后新 Run 正常完成。两个版本的 schema head 相同、运行时代码相同，没有覆盖跨 schema 的真实版本变化 |

## 第 2 步：一次事故从告警到归档

Incident `3c6deab1-96cf-549a-859c-466f4a385848`，Run `97890bd2-ffe6-520c-8689-ed8d32b52b37`（第 1 步开出的那一次）。下表每一项都是从 PG 读的（owner 只读查询），工作台页面同时可见（operator 200，匿名 401）。

| 要素 | 记录（表 / id / 时间） |
|---|---|
| 业务流量与故障 | lab load-generator 持续流量；07:07:39 注入 `paymentFailure`，07:19:37 恢复（第 1 步证据） |
| 触发 | `opspilot_alert_deliveries` #1：`CheckoutPlaceOrderErrorRatioHigh` firing，startsAt 07:12:54，07:13:04 收到，outcome `created`，actor `alertmanager`；`opspilot_subject_events` seq 1 `intake_accepted` 07:13:04 |
| 调查 | seq 2 `run_claimed` 07:13:04.9（epoch 1）；`opspilot_steps` 13 行，round-1 07:13:07 到 round-12 07:16:17；20 次工具调用（`tool_committed`），工具耗时 27.1 s；seq 35 `run_completed` 07:16:17，`execution completed`，`published true` |
| 证据 | `opspilot_evidence` 20 行（采纳 19，状态 ok / no_data / error），observed 07:13:09–07:15:16；报告 `supported`，根因与注入一致（第 1 步证据） |
| 人工交接 | **本 Run 没有**。conclusion `handoff: false`，`handoff_reasons` 为空，`opspilot_inputs` 0 行。下面「没有覆盖的」说明替代证据 |
| 系统外人工处理 | **没有登记**。`opspilot_controls` 只有两行：07:16:26 `auto_authorize_observation`（actor `system`）和 09:39:14 `close`（见下）。没有 `register_remediation`；故障是由第 1 步的执行 Agent 用脚本恢复的，不是人在系统外处理 |
| 恢复观察 | `opspilot_observation_sessions` `7cc8ee35-e9e2-5d59-b0f2-ee2d6afad039`：07:16:26 由系统自动授权（`authorization_source automatic`）；`opspilot_observation_samples` 20 个（07:17:53–07:43:50，degraded / no_data / healthy，14 个确认健康）；healthy_since 07:32:32；`opspilot_observation_endings` `0b6a15e3`：07:43:50 `recovery_confirmed`，lifecycle `observing_recovery → resolved` |
| 复盘 | `opspilot_postmortems` `c6f4ef12-7189-4ee5-b32e-7428997860e0`：v1 07:45:14 由 worker 生成（`under_review`） |
| 人工关闭 | `opspilot_controls` `2de6586e-537e-49a0-847e-90d14efab888`：09:39:14 `close`，actor `operator`，channel `web`，generation 0→1；incident `lifecycle closed`，`closed_at` 09:39:14。用户本人在工作台操作 |
| 归档 | 关闭后 v1 变 `stale`（`control_generation_changed`），09:40:21 生成 v2 `under_review`（18 条结论，3 条 uncertain）。v2 还没有人审核，所以 `opspilot_knowledge_entries` 为 0：复盘进入了待审，还没有成为知识 |

用量：12 次模型调用（`opspilot_budget_reservations` 12 行全部 `spent`），缓存命中 324608、未命中 39866、输出 26185 token；全部在北京时间高峰（15:13–15:16），按 [soak 费用页](../m3-soak-2026-11-07/README.md#费用) 同一价目约 CNY 0.30。

### 人工交接这一项靠什么证明

- 本机没有。`otel-demo` 档下告警匹配到目标就自动调查，调查成功完成就不转人工。
- 已有的转人工记录来自 72h soak（[m3-soak-2026-11-07](../m3-soak-2026-11-07/README.md)）：ep03 的 Run `MODEL_UNAVAILABLE` 后转人工；A3 切断模型出口，Run `2c1120e2` 进入 `waiting_human`。但两次都是脚本操作员按 B1 规则关闭的，不是真人，所以「真人接手并在系统外处理」也没有在 soak 覆盖。
- 本机可以不需要用户就造出一次真实的转人工：用 `deploy/compose/compose.drill-egress.yaml` 切断 worker 的模型出口，再注入故障，Run 第一次模型调用失败后进入 `waiting_human`（与 soak A3 同法）。缺的是后半段：真人在工作台看到交接、在系统外处理（例如恢复故障）、用 `register_remediation` 登记、等独立恢复观察确认，再关闭。这一段需要用户，本次没有做，留给协调者决定是否安排。

## 第 3 步：Run 进行中杀掉 worker

| 时刻 | 事件 |
|---|---|
| 09:48:49 | `kind_lab.py fault inject --experiment-id f9-step3-worker-kill-20261110`（已校验，ConfigMap sha256 `e47d27c2fa01`） |
| 09:54:04 | 告警 delivery #3 firing（startsAt 09:53:54），outcome `created`：新 incident `487fac98-a430-5635-a8f7-b4ebee87de2c`，`related_incident_id` 指向已关闭的 `3c6deab1`（工作台显示「Opened after closed incident … the closed incident was not reopened」） |
| 09:54:05 | Run `f3dfb53c-4ac0-5177-a7d9-302d42b86e34` `run_claimed` epoch 1（owner `5d1a0dce`） |
| 09:54:08–09:54:09 | round-1 step 提交，两个工具结果提交（09:54:08.85、09:54:09.71） |
| 09:54:10.088 | `docker kill opspilot-worker-1`。此时 run `running`、epoch 1，租约到 10:01:08；1 个预留 `spent`、1 个 `reserved`：round-2 的模型调用在进行中 |
| 09:54:40 | 30 s 内重启策略没有拉起容器（状态 `exited`，restarts 0，与 soak A1 相同：`unless-stopped` 不对 `docker kill` 生效） |
| 09:54:40–43 | 按 soak A1 的做法 `install.sh compose start worker`，退出码 0；新 worker 09:54:45 `worker started` |
| 10:01:11 | 旧租约到期后，新 worker 的 slot `36a85781` 领取 Run：`run_claimed` epoch 2，从 round-2 续跑 |
| 10:05:21 | round-18 `stop`，`run_completed`（`published true`，`handoff false`）；worker 日志 `attempt … status=published` |
| 10:05:27 | 系统自动授权恢复观察 `12259232-acab-54a5-a275-ee561b41a17d` |
| 10:07:03 | `fault restore`（已校验，`1ee5c0258e3e`） |
| 10:23:37 | `recovery_confirmed`，lifecycle `observing_recovery → resolved`（13 个样本，healthy_since 10:12:12）。这期间经历了第 4 步的三次版本切换 |
| 10:25:14 | 复盘 v1 `under_review`（在 `v0.1.0` 的 worker 上生成） |

判定：

- 同一个 incident：整个过程 `opspilot_incidents` 只多了 `487fac98` 一行（之前 1 行，之后 2 行），Run 也只有这一个，没有重复 incident 或重复 Run。
- 可见的恢复：`opspilot_subject_events` 里 `run_claimed` 出现两次（epoch 1、epoch 2）；工作台显示 `run f3dfb53c… completed · epoch 2 · budget 18 spent / 1 reserved / 0 unknown`。
- 不重复已提交的工作：round-1 的 2 个工具结果在 kill 前已提交，没有重派；`opspilot_tool_charges` epoch 1 2 次、epoch 2 28 次，同一 step/ordinal 的 `tool_committed` 没有重复。被打断的是 round-2 的模型调用，续跑时重新请求。
- 报告：`completed`，结论 `partial`。报告复现了告警（PlaceOrder 100% 错误），追到 payment 的 `PaymentService/Charge` 抛 `Payment request failed. Invalid token`；flagd 只作为时间上的相关，没有当成已证事实。根因方向与注入一致，但比第 1 步那次（`supported`）弱。19 轮，16 条结论，5 个缺口。
- 已知问题 #527：被杀时进行中的那次模型调用，预留 `42e2677f-429f-5e12-9051-7706ee2ede84` 一直停在 `reserved`。按要求只记录，不修。这次调用的 token 没有记入任何 step，费用里没有它。
- 已有覆盖：soak A1（ep06 的 Run `d687184a`，同样在模型调用中被杀，接管后完成）；F2 的三个断点（[f2-kill-resume-live](../f2-kill-resume-live/README.md)）。本次是干净主机上的新一轮。

用量（step 的 usage）：18 次已结算调用，命中 747648、未命中 52215、输出 32476 token；高峰 1 次（北京时间 17:54），其余闲时，约 CNY 0.20。另有 1 次未结算（上面的 `reserved`）。

## 第 4 步：按 digest 钉住的发布镜像，升级、回滚、再升级

### 构建与 CI（引用，没有重跑）

- `v0.1.0` 指向 `dd9534316a6b95e3513f2c68c3f13e45bc7ea322`（main 上，#508）。release 工作流 [run 42226724770](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/42226724770)：tag push 触发，`publish` 任务成功（2026-11-06 13:35–13:57Z）。工作流先确认 tag 与 pyproject 版本相同、提交在 main 上，然后跑 `make check`，最后构建 amd64 + arm64 并推送。
- 同一提交的 CI：[run 42223101968](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/42223101968)，success。
- 发布镜像 `ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd`；本机匿名拉取成功，镜像 label revision `dd953431…`，version `0.1.0`。
- 更正：第 1 步证据写的是「发布镜像和 `v0.1.0` tag 尚未完成」，这是错的。tag 与镜像在 11-06 就已发布，72h soak 用的就是这个镜像。第 1 步用本机构建，是因为照了 README 的写法，不是因为没有发布。GitHub Release 页面（`gh release view v0.1.0`）不存在，只有 tag 和 GHCR 镜像。

### 两个版本是否真的不同

本机构建来自 `8de996d9`，比 `v0.1.0` 晚 11 个提交。两者在 `opspilot/` 下只差 `opspilot/evaluation/{expectations,gate}.py`（评测代码，运行时不加载）；`deploy/`、`Dockerfile`、`pyproject.toml` 相同；schema head 都是 `0024_trace_outbox`；`opspilot_runs.versions`（prompt `bef70649a3a8` / ctx `a30e65ea52f4` / tool `otel-demo-a3a95a0f888e`）三个 Run 都一样。所以这次换的是镜像（不同的 digest，`opspilot_build_info` revision 不同），不是一次带 schema 迁移的版本变化。跨 schema 的升级与回滚已在 [F8 第 3 步（#429）](../f8-step3-upgrade-rollback/README.md) 做过（`0023 → 0024`，本机 colima，fixture 档）。

### 明确的环境配置

`install.env`（不含密钥，值都写在这里）：`OPSPILOT_PROJECT=opspilot`、`OPSPILOT_WEB_HOST_PORT=18180`、`OPSPILOT_TOOL_PROFILE=otel-demo`、`OPSPILOT_START_AGENT=1`、`OPSPILOT_OBSERVER_PROMETHEUS_URL=http://opspilot-m1-control-plane:30090`、`OPSPILOT_TRACE=off`，以及 `OPSPILOT_IMAGE`：每次升级在命令行显式给出，`install.sh` 写回 `install.env`（下表）。密钥文件（worker_env、observer_env、pgpass、ui_password、alertmanager_token）从未重新生成，只按路径引用。

### 过程（opspilot 用户；每次 `install.sh upgrade` 之后都跑 `kind_lab.py connect-control --project opspilot`，三个都 `connected`）

| 段 | 时间 | 命令 | 结果 |
|---|---|---|---|
| U1 升级 | 10:08:01–10:08:23 | `OPSPILOT_IMAGE=ghcr.io/…@sha256:228df9f7… deploy/compose/install.sh upgrade` | 退出码 0，22.9 s：拉取 → 停三个运行时 → postgres healthy → `schema unchanged: 0024_trace_outbox` → init-roles 四个登录都在 head → 三个运行时 healthy。在途：观察会话 `12259232`（第 3 步的事故） |
| 升级后新 Run | 10:09:26 | `POST /intake/events`（alertmanager 的 event token，目标 `m0-otel-20260909`，问题「升级后 checkout 是否健康」） | 201，incident `5124066a-a720-53a0-9cf8-ef9e254ea61a`，Run `76666388-57a2-5f52-b41f-c4c362fc3f15`，在 `v0.1.0` 上 epoch 1 跑了 4 轮 |
| R 回滚 1 | 10:09:33–10:10:08 | `deploy/compose/install.sh compose stop web worker observer` | 退出码 0，35 s（worker 在 40 s 宽限内把手头的一轮做完：round-4 10:10:01 提交） |
| R 回滚 2 | 10:10:08 | `deploy/compose/install.sh compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to 0024_trace_outbox` | 退出码 1，`schema current: 0024_trace_outbox`，`revision 0024_trace_outbox is not lower than the current …: downgrade only goes to an older revision`，没有改动。两个版本 head 相同，这一步本来就不需要；UPGRADE.md 没有写 head 相同时跳过这一步（见下「文档与实际」） |
| R 回滚 3 | 10:10:10–10:10:20 | `OPSPILOT_IMAGE=opspilot:local deploy/compose/install.sh upgrade` | 退出码 0，10.1 s：从 checkout 构建（全部 cache）→ `schema unchanged` → init-roles → 三个运行时 healthy，`opspilot_build_info` revision 回到 `8de996d9` |
| 回滚后续跑 | 10:17:04–10:21:15 | — | 旧租约 10:17:02 到期，本机构建的 worker 领取（epoch 2），从 round-5 续跑到 round-18，`run_completed`（`published`，`handoff false`）。10:21:16 自动授权观察 `b9ea386c` |
| U3 再升级 | 10:22:49–10:23:00 | `OPSPILOT_IMAGE=ghcr.io/…@sha256:228df9f7… deploy/compose/install.sh upgrade` | 退出码 0，10.7 s，镜像已在本地（`Image is up to date`）。在途：观察 `12259232` 与 `b9ea386c`。`12259232` 在 10:23:37 `recovery_confirmed` |

每次切换后：三个运行时 `/readyz` 200 `{"status": "ready", "version": "0.1.0"}`；`alembic_version` 一直是 `0024_trace_outbox`；incident `3c6deab1` 一直是 `closed / generation 1`；各业务表行数只增不减（S0 → S5：incidents 2 → 3，runs 2 → 3，steps 32 → 51，evidence 49 → 82，controls 3 → 4，subject_events 90 → 145，observation_samples 21 → 33，postmortem_versions 2 → 2）。

观察会话 `12259232` 的样本间隔在三次停机前后是 82–92 s（10:06:52、10:08:14、10:09:46、10:11:12 …），每次停机都比采样间隔短，看不出缺口。这和 soak 里约 83 s 的节奏（#522）一致。

升级后的 Run（`76666388`）：`completed`，结论 `partial`。它说 checkout「现在不健康」，依据是截至 10:09:26 的 10 分钟窗口，而故障 10:07:03 才恢复、flagd 约 1 分钟后生效，所以这个窗口几乎全在故障期内。PlaceOrder 错误率接近 100%，延迟正常，错误在 payment 一侧。这里只用它证明升级后能跑完一次真实 Run，不评判调查质量。用量：18 次已结算调用（epoch 1 4 次，epoch 2 14 次），命中 704384、未命中 54597、输出 38885 token，全部闲时，约 CNY 0.22；另有 1 次未结算（回滚的停机打断了 round-5 的模型调用，预留停在 `reserved`。这是 graceful stop，不是 kill，同属 #527）。工具调用 epoch 1 8 次、epoch 2 25 次，没有重复。

### 最后的状态

`v0.1.0` 按 digest 运行（`install.env` 的 `OPSPILOT_IMAGE` 是 digest 形式）。lab 与 OpsPilot 都在运行。incident `487fac98` 是 `resolved`（等人关闭），`5124066a` 在 `observing_recovery`（观察 `b9ea386c` 进行中）。

### 文档与实际

1. 回滚的第 2 步只适用于新版 schema head 更高的情况。head 相同时，按原文照做会得到退出码 1（拒绝，不改动）。UPGRADE.md 没有说这种情况下跳过这一步。本 PR 不改文档，记在这里。
2. 回滚后用 `opspilot:local` 重建同一提交，镜像 ID 从第 1 步的 `sha256:2de313b0…` 变成 `sha256:263148ca…`：层全部命中 cache，只有 manifest/attestation 不同。所以本机构建不是可复现的 pinned artifact，只有发布镜像的 digest 是。
3. `install.sh compose start worker`（第 3 步把 worker 拉起来的那条命令）也跑了 `migrate` 和 `init-roles`：Compose 的 `depends_on` 生效了，两者都正常退出。没有造成问题，记录在这里。

## 费用与 trace

三个 Run 合计 48 次已结算模型调用，约 CNY 0.73（token × [soak 费用页](../m3-soak-2026-11-07/README.md#费用) 的 deepseek-flash 价目，按北京时间分高峰与闲时）。2 次未结算调用的 token 不知道，没有计入。复盘生成是否调用模型，本次没有核对（未确认），上面的数字只算 Run 的 step。没有用控制台或余额核对。没有 LangSmith key，`OPSPILOT_TRACE=off`，没有 trace。

## 这份证据不证明的

- 不是生产观察，不是 soak。每项只跑了一次，都是单机。
- 第 2 步：真人接手转人工的 Run、在系统外处理并登记 `register_remediation`，本机都没有做。复盘 v2 没有经人审核，知识库为 0。
- 第 3 步：只杀了一次，断点是「模型调用进行中」。工具调用进行中、响应已提交未落工具结果这两种断点没有在本机重做（F2 有）。worker 是靠手动 `compose start` 拉起的，不是重启策略。
- 第 4 步：没有跨 schema 版本（两个版本 head 相同，运行时代码相同），没有不兼容版本（`INCOMPATIBLE_STATE`）。这些在 #429 的本机演练里有，不在这台主机上。没有重跑 CI 或 release，没有新建 tag 或推送镜像。
- 没有 LangSmith trace，费用没有按余额对账。

## 原始记录

不入库（ADR-0006），在仓库外 `~/.cache/opspilot-f9-steps-2-4-2026-11-10/raw/`（主机 `/root/f9-steps-2-4/`）：`step3.log`、`step4.log`（每条命令及其输出、每次快照）、PG 查询输出、两份报告 JSON、工作台页面。已检查：不含主机地址（只有 127.0.0.1 与 0.0.0.0），不含密钥值。
