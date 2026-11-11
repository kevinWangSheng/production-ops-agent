# F9 终轮：重装后的干净主机上做第 1、2、4 步（2026-11-11）

类别：集成运行。租机 `opspilot-f9-1`（单机：kind lab 加 Compose 项目 `opspilot`，`otel-demo` 工具档，真实 DeepSeek 模型），用户在控制台 Reinstall 后从零开始。这不是生产证明，也不是 soak；流量是 lab 的 load-generator，故障都是 flagd `paymentFailure` 注入。时间均为 UTC，2026-11-11（太平洋时间 11-10 晚 22:08 起）。冻结摘要见 [summary.json](summary.json)，主机上的原始文本副本在 [raw/](raw/)。第 3 步（杀 worker）本轮没有重做，沿用 [F9 第 2–4 步证据](../f9-steps-2-4-2026-11-10/README.md)；第 5 步是文档，见 [docs/portfolio/](../../portfolio/README.md)。

| 步 | 判定 | 一句话 |
|---|---|---|
| 1 干净主机一键安装并接上 Agent | **达成** | 干净状态核对后，只用 README「The OpenTelemetry Demo lab on the same host」一节的命令，脚本与命令全部退出 0，三个运行时接入 lab，随后告警自动开出 incident 和 Run。没有文档外的步骤 |
| 2 全流程 | **部分达成** | 七个要素在本机都观察到了，但分在两个 incident 上：转人工、系统外处理、恢复观察、关闭归档在 `fb89cdf2` 上；有证据的调查在 `1fbdce34` 上。`fb89cdf2` 的转人工是切断模型出口造成的，它的 Run 没有任何调查与证据。两个 incident 合起来是否满足这一步，由用户裁决 |
| 4 钉住版本的构建、升级与恢复 | **部分达成** | 按 digest 从 `v0.1.0` 升到 `v0.1.1` 再按 UPGRADE.md 回滚，命令都按文档退出，在途恢复观察跨三次切换走到 `recovery_confirmed`。缺：独立的 staging 主机、跨 schema 的版本变化、每次切换后的数据库快照（脚本 SQL 写错）、环境配置文件内容 |

## 第 1 步：从干净主机到 Agent 接上

代码：`git clone` 得到 main `9416041d`（`9416041 docs: fix portfolio tool-count and human-decision claims …`），没有另外 checkout。

| 时间 | 身份 | 做了什么 | 结果（原始记录） |
|---|---|---|---|
| 06:08:16 | root | 记录干净状态 | Ubuntu 24.04.5 LTS，内核 6.8.0-147；`/home` 只有镜像自带的 `linuxuser`；没有 docker、kind；只监听 22 和本地 DNS（[00-clean-state.txt](raw/root/00-clean-state.txt)） |
| 06:08:16–06:11:54 | root | README root 段的三条命令：`adduser --disabled-password --gecos "" opspilot`、`su - opspilot -c 'git clone …'`、`install_linux.sh --user opspilot`（[step1-root.sh](raw/root/step1-root.sh)，另加两条 `date` 与一条 `git log`） | 3 分 38 秒，结尾 lab health `"ok": true`：Prometheus、Jaeger（15 个服务）、frontend 200，没有未就绪的 deployment（[10-root-install.log](raw/root/10-root-install.log)） |
| 06:12:59 | opspilot | `python3 scripts/kind_lab.py control-secrets` | 退出码 0，写入 `alertmanager_token`、`worker_env`、`observer_env`、`target_identities.json`、`install.env`（[20-control-secrets.log](raw/opspilot/20-control-secrets.log)） |
| 约 06:13 | 用户 | 往 `worker_env` 追加 `DEEPSEEK_API_KEY`（README 写的是用编辑器，实际由用户经 ssh 管道追加） | 没有日志，值不出现在任何文件里 |
| 06:14:04–06:14:33 | opspilot | `OPSPILOT_START_AGENT=1 deploy/compose/install.sh install` | 退出码 0，29 秒：从 checkout 构建 `opspilot:local`（version 0.1.1，revision `9416041d`，镜像 `sha256:d7ec4b21…`）→ 生成口令文件 → postgres（钉住的 digest）→ `schema upgraded: 0024_trace_outbox` → init-roles 四个登录在 head → web、worker、observer 都 healthy，工作台 `127.0.0.1:18090`（[30-install.log](raw/opspilot/30-install.log)）。日志不打印命令行本身；起了 worker 和 observer 说明带了 `OPSPILOT_START_AGENT=1` |
| 06:14:33 | opspilot | `python3 scripts/kind_lab.py connect-control --project opspilot` | 退出码 0，三个运行时都 `connected`（[31-connect-control.log](raw/opspilot/31-connect-control.log)） |

从第一条命令到 Agent 接上约 6 分 17 秒。接上之后第一次注入（第 2 步）的告警被工作台接受（delivery outcome `created`），说明 webhook token、目标身份、端口三处在 11-09 那轮要手工处理的地方，现在都由 `control-secrets` 处理了。

对照验收步骤：干净的受支持主机（Ubuntu 24.04 x86_64，核对过干净状态）、文档写的自动安装（README 原样命令）、搭起选定的真实软件环境（lab health ok）、接上 Agent（三个运行时接入并处理了告警），四项都有记录。

缺口：放 key 那一步没有日志；运行时的 `/readyz` 没有单独探测，健康只来自 Compose 的 `healthy` 状态；Reinstall 的时刻不在归档里，干净状态只靠 06:08:16 那份核对。

## 第 2 步：转人工的一次事故，加一次正常调查

### incident `fb89cdf2`：转人工 → 系统外处理 → 恢复观察 → 关闭

为了在本机得到一次真实的转人工，先切断 worker 的模型出口（与 soak A3 同法），再注入故障。

| 时间 | 事件 | 原始记录 |
|---|---|---|
| 06:15:08 | 以 drill-egress 覆盖重建 worker（`extra_hosts api.deepseek.com:127.0.0.1`），再 `connect-control` | [40-drill-egress-and-inject.log](raw/opspilot/40-drill-egress-and-inject.log) |
| 06:15:10 | `fault inject`（`paymentFailure` → 100%，ConfigMap 已校验 `e47d27c2fa01`） | 同上 |
| 06:22:04 | 告警 `CheckoutPlaceOrderErrorRatioHigh` firing（startsAt 06:21:54），outcome `created`，actor `alertmanager`：开出 incident `fb89cdf2-98a8-5ec0-a70f-cd93fb321214`，Run `79970de4-e57f-5d09-8189-5d05bd160f5b` | [60-step2-timeline.txt](raw/opspilot/60-step2-timeline.txt)、[50-handoff-state.txt](raw/opspilot/50-handoff-state.txt) |
| 06:22:05.920 | worker 日志 `status=handed_off reason=MODEL_UNAVAILABLE`（version 0.1.1，revision `9416041d`）；Run `waiting_human`，两个模型预留停在 `unknown` | [50-handoff-worker-log-line.txt](raw/opspilot/50-handoff-worker-log-line.txt)、50-handoff-state.txt |
| 06:22:46 | 去掉 drill 覆盖重建 worker，恢复模型出口（`extra_hosts=[]`） | [51-egress-restore.log](raw/opspilot/51-egress-restore.log) |
| 06:58:54 / 07:01:54 | 告警 resolved（`resolved_attached`），07:01:54 再次 firing（`attached`），07:11:54 再次 resolved。故障此时还没恢复，第一次 resolved 的原因没有查 | 60-step2-timeline.txt |
| 07:08:11 | `takeover`，actor `operator`，channel `web`，generation 0→1 | 60-step2-timeline.txt |
| 约 07:10 | 用户在自己的终端跑 `fault restore`（已校验 `1ee5c0258e3e`） | [52-fault-restore-by-user.txt](raw/opspilot/52-fault-restore-by-user.txt)（执行 Agent 写的说明加用户贴回的输出） |
| 07:10:14 | `register_remediation`，actor `operator`，web，generation 1→2，revision `payment:fault-restored`；开出观察 `7244f7fd-29e0-599f-95f1-a9ce7dfc0a57`，`authorization_source post_remediation`，`authorized_by operator` | 60-step2-timeline.txt |
| 07:11–07:26 | 12 个样本（间隔 60 s，持续窗口 600 s）：第 1–2 个 `degraded`，其余 `healthy`；healthy_since 07:15:21 | 60-step2-timeline.txt |
| 07:26:39 | `recovery_confirmed`，lifecycle `observing_recovery → resolved` | 同上 |
| 07:42:10 | `close`，actor `operator`，web，generation 2→3；incident `closed`，mode `human_owned`，Run `cancelled` | 同上 |
| 07:42:44 | 复盘生成 v1，状态 `under_review`；知识库 0 条（没人审核，复盘还没成为知识） | [72-normal-run-state.txt](raw/opspilot/72-normal-run-state.txt) 的 `pm_version`、[81-step4-post-state.txt](raw/opspilot/81-step4-post-state.txt) 的 `pm` |

### incident `1fbdce34`：一次正常完成的调查

| 时间 | 事件 |
|---|---|
| 07:42:52 | `fault inject`（[70-normal-run-inject.log](raw/opspilot/70-normal-run-inject.log)） |
| 07:49:04 | 新 incident `1fbdce34-377c-5a57-9a2a-6ae81e1d5364`，`related_incident_id` 指向已关闭的 `fb89cdf2` |
| 约 07:52 | Run `f29d655b-66f6-5cc6-a57f-67c627134848` `completed`：10 轮，10 个模型预算预留全部 `spent`，`handoff false`；`opspilot_evidence` 共 17 行。报告 `completed / partial`：复现告警，失败来自 payment 的 `PaymentService/Charge` 抛 `Payment request failed. Invalid token. app.loyalty.level=gold`（`charge.js:37`），排除了 checkout 其他依赖和重启/发布；flag 值不可见，所以没有把「为什么 token 无效」当成已证事实（[72-normal-run-state.txt](raw/opspilot/72-normal-run-state.txt)） |
| 07:52:29 | 系统自动授权恢复观察 `95983fe0-c6aa-563c-9c2a-3853c9d0d9db`（`authorization_source automatic`），这个观察跨过了第 4 步的三次切换 |
| 07:52:55 | `fault restore`，由执行 Agent 跑（[71-normal-run-restore.log](raw/opspilot/71-normal-run-restore.log)） |
| 08:09:53 | `recovery_confirmed`，lifecycle `observing_recovery → resolved`（[82-inflight-observation-end.txt](raw/opspilot/82-inflight-observation-end.txt)）。这个 incident 没有关闭 |

### 对照验收步骤

| 要素 | 本机记录 | 是否覆盖 |
|---|---|---|
| 业务流量与故障 | load-generator 持续流量；两次 flagd 注入 | 覆盖（流量没有单独的速率记录，正常 Run 的报告读到了 checkout/payment 的 span） |
| 触发 | 两次告警都由 Alertmanager webhook 开出 incident | 覆盖 |
| 调查 | `1fbdce34` 的 Run 完成；`fb89cdf2` 的 Run 一次模型调用都没成功 | 只在 `1fbdce34` |
| 证据 | 证据表 17 行；按 Run 行计数（`79970de4` 为 0，`f29d655b` 为 17）推断都属于 `1fbdce34`，`fb89cdf2` 没有证据 | 只在 `1fbdce34` |
| 转人工 | `fb89cdf2` `MODEL_UNAVAILABLE` → `waiting_human`，人工 takeover | 覆盖，但原因是演练造成的模型不可用 |
| 系统外人工处理 | 用户本人跑 `fault restore`，在工作台 `register_remediation` | 覆盖 |
| 恢复观察 | `7244f7fd` 由人工登记开出，独立确认恢复 | 覆盖 |
| 归档 | `fb89cdf2` 关闭，复盘 v1 待审 | 覆盖到「关闭 + 复盘待审」；没有审核成知识 |

判为部分达成，因为没有一个 incident 从调查一直走到人工处理和归档：转人工那一个没有调查内容，有调查的那一个没有转人工、没有关闭。`otel-demo` 档下调查成功就不转人工（11-10 证据已写明），所以「先有证据的调查、再转人工」要么靠人工在 Run 完成后主动 takeover，要么接受两个 incident 合起来算。这是给用户的判断点。

缺口：

- worker 日志只留下一行，是从 `docker logs` 手抄的；06:22:46 恢复出口时重建了 worker 容器，之前的日志随容器删除。
- 用户的操作（takeover、register_remediation、close）只有数据库里的审计行，没有工作台截图。`fault restore` 那一条是执行 Agent 写的说明加用户贴回的输出。
- 转人工（06:22:05）到 takeover（07:08:11）隔了 46 分钟，是等用户上线，不是系统延迟。
- 复盘没有审核，知识库为 0。
- 没有 LangSmith trace，没有记录 token，也没有按余额对账。

## 第 4 步：按 digest 钉住的发布镜像，升级与回滚

### 构建与 CI（引用，本轮没有重跑）

- `v0.1.1` 指向 `9b208cd13cc6c28e97ab9c0d1cb074a13dd3cf34`（`chore: version 0.1.1`，PR #537）。release 工作流 [run 42824773408](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/42824773408)（tag push，2026-11-10 13:28Z，success），日志里的推送 digest 就是 `sha256:0af7b9ff…`。同一提交的 CI [run 42824741602](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/42824741602) success。
- `v0.1.0` 的发布与 digest `sha256:228df9f7…` 见 [F9 第 2–4 步证据](../f9-steps-2-4-2026-11-10/README.md)「构建与 CI」节。
- 两个版本在 `opspilot/` 下只差 `__init__.py` 的版本号和评测代码（`evaluation/expectations.py`、`gate.py`），另有 `UPGRADE.md` 与 `pyproject.toml`；schema head 都是 `0024_trace_outbox`。所以这次换的是带不同 digest 的发布镜像，运行时代码与 schema 都没变。

### 过程（[step4.sh](raw/opspilot/step4.sh)，输出 [80-step4.log](raw/opspilot/80-step4.log)）

起点是第 1 步本机构建的 `opspilot:local`（0.1.1，`9416041d`）。

| 段 | 时间 | 命令 | 结果 |
|---|---|---|---|
| A 换到钉住的 v0.1.0 | 07:53:22–07:53:45 | `OPSPILOT_IMAGE=ghcr.io/…@sha256:228df9f7… deploy/compose/install.sh upgrade`，再 `connect-control` | 两条都退出 0；`schema unchanged`，init-roles 四个登录在 head，三个运行时 healthy，`docker inspect` 显示镜像 id `sha256:228df9f7…`。这一段从本机构建换到较旧的发布版本，不是升级 |
| B 升级到钉住的 v0.1.1 | 07:54:07–07:54:29 | `OPSPILOT_IMAGE=ghcr.io/…@sha256:0af7b9ff… deploy/compose/install.sh upgrade`，再 `connect-control` | 两条都退出 0；`schema unchanged`，三个运行时 healthy，镜像 id `sha256:0af7b9ff…` |
| C 回滚 1 | 07:56:00 | `deploy/compose/install.sh compose stop web worker observer` | 退出码 0 |
| C 回滚 2 | 约 07:56:0x | `… compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to 0024_trace_outbox` | 退出码 1，`schema current: 0024_trace_outbox`，`revision 0024_trace_outbox is not lower than the current 0024_trace_outbox`，没有改动。这正是 UPGRADE.md 现在写的 head 相同时的表现（文档说可以跳过这一步，跑了也是这个结果） |
| C 回滚 3 | 07:56:09–07:56:19 | `OPSPILOT_IMAGE=ghcr.io/…@sha256:228df9f7… deploy/compose/install.sh upgrade`，再 `connect-control` | 两条都退出 0；镜像已在本地（`Image is up to date`），三个运行时 healthy，镜像 id `sha256:228df9f7…` |

C 之后的状态（07:58:22，[81-step4-post-state.txt](raw/opspilot/81-step4-post-state.txt)）：`alembic_version` `0024_trace_outbox`；incident 2 个（`fb89cdf2` closed、generation 3；`1fbdce34` observing_recovery）；Run 2 个（`79970de4` cancelled，`f29d655b` completed）；controls 4 行；evidence 17 行。和切换前 07:42 与 07:52 两份记录里的 incident、Run、证据一致，没有丢。但切换前没有一份完整的计数快照（见缺口），所以「不变」是拿两份局部记录对的，不是同一查询前后对比。

在途工作：恢复观察 `95983fe0` 在 07:52:29 开始，A、B、C 都发生在它进行中。13 个样本：第 1 个 `failed`（窗口止于 07:53:41），第 2 个 `degraded`（07:54:44），第 3 个 `failed`（07:56:14），第 4–13 个 `healthy`；healthy_since 07:58:36，08:09:53 `recovery_confirmed`。三次切换后它都继续采样，并在 v0.1.0 上正常结束。

样本 1–3 的原因没有核实，只能从时间推断：第 1、3 个样本的时刻落在 A、C 的运行时刚起来、`connect-control` 还没跑完的几秒里，那时新 observer 容器还不在 kind 网络上，查不到 lab Prometheus，结果记为 `failed`；第 2 个样本的 5 分钟窗口大半在故障期内（07:52:55 恢复，flagd 约 1 分钟后生效），所以是 `degraded`。这是推断，样本的读数与原因字段没有导出。

### 对照验收步骤

| 子项 | 本机记录 | 是否覆盖 |
|---|---|---|
| 构建、CI 校验 | `v0.1.1` 的 release 与 CI 运行都 success，推送 digest 与本机拉到的一致 | 覆盖（引用 GitHub 运行，本轮没有重跑） |
| staging 部署 | 没有单独的 staging 主机，就在同一台租机上做 | **没有覆盖** |
| Agent 升级 | B：按 digest 从 v0.1.0 升到 v0.1.1 | 覆盖，但两版运行时代码与 schema 相同 |
| 恢复 | C：按 UPGRADE.md 回滚到 v0.1.0 digest；在途恢复观察跨切换走到确认 | 覆盖；没有跨 schema 的 downgrade |
| 钉住的 artifact | 三段都用 `@sha256:` digest，`docker inspect` 核对 | 覆盖 |
| 明确的环境配置 | `OPSPILOT_IMAGE` 每次在命令行给出；其余设置在 `install.env` | 部分：本轮没有导出 `install.env` 的内容 |

缺口（操作员脚本的问题与没做的事）：

- 每次切换后的数据库快照全部失败：`step4.sh` 想给每行加标签，写成了 `select alembic, version_num …`，标签没加引号，被当成列名，7 条查询都报 `column … does not exist`。C 之后另跑了一份正确的查询（81 号文件）。
- 版本探针失败：`importlib.metadata.version("opspilot")` 在三个容器里都报 `PackageNotFoundError`（镜像里的包不是按发行包装的）。版本只靠 `docker inspect` 的镜像 digest 核对。
- `curl http://127.0.0.1:18090/healthz` 四次都是 404：18090 是工作台，健康端点在容器内的 9464（`/readyz`）。所以没有 `/readyz` 记录，健康只来自 Compose 的 `healthy`。
- 没有保留 v0.1.1 运行期间的容器日志。
- 没有独立的 staging 主机。
- 没有跨 schema 的版本变化：两版 head 都是 `0024_trace_outbox`，回滚第 2 步按文档退出 1。跨 schema 的升级与回滚只在 [F8 第 3 步（#429 本机演练）](../f8-step3-upgrade-rollback/README.md) 做过（`0023 → 0024`，本机 colima，fixture 档），不在这台主机上。

## 这份证据不证明的

- 不是生产观察，不是 soak。每项只跑了一次，单机，合成流量加注入故障。
- 第 2 步没有一个 incident 覆盖全部要素；复盘没有审核。
- 第 3 步本轮没有重做。
- 第 4 步没有 staging 主机、没有跨 schema 版本、没有不兼容版本（`INCOMPATIBLE_STATE`）；没有重跑 CI 或 release，没有新建 tag 或推送镜像。
- 没有 LangSmith trace；费用没有记录 token，也没有按余额对账。
- 本 PR 不改 `feature_list.json`，F9 `passes` 仍为 false。

## 原始记录与脱敏

[raw/](raw/) 是主机上 `/root/f9-final/` 与 `/home/opspilot/f9-final/` 的 20 个文本文件的原样副本（归档 `f9-final-host-evidence.tgz`，sha256 `29567e235d651ed02b026d35b208ff10011015fa0644e62d718983fee7e0a376`，22 个条目含 2 个目录，存在仓库外）。入库前检查过：没有主机地址（只有 127.0.0.x 与 0.0.0.0）、没有 SSH 指纹（`fingerprint` 只出现在 Alertmanager 告警 JSON 里，是告警标签的哈希）、没有口令或 token 的值（只有文件名，例如 `ui_password`、`alertmanager_token`），也没有 10.x/172.x 地址；没有需要改写的内容，所以副本没有改动。gitleaks（与 CI 同一版本）扫描这个目录无发现。主机名只出现别名 `opspilot-f9-1`。
