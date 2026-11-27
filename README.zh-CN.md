# OpsPilot — 只读运维调查 Agent

[English](README.md) | 简体中文

OpsPilot 是持续运行的事故与发布后调查 Agent。它只读取遥测数据，从不执行生产变更或修复。合格结论发布后，恢复观察（只读）可以自动授权；其余一切仍由人控制。

本文与中文合同文档（[SPEC.md](SPEC.md)、[PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)、[C3 技术方案](docs/design/technical-proposal-2026-09-07.md)、任务记录）不一致时，以中文合同文档为准。

## 它做什么

- **调查。** Alertmanager 告警会开一个事故。告警匹配到已配置的目标时启动一次调查 Run，否则事故交给人。模型通过按目标授权的只读工具收集证据，同时保留相互竞争的假设，发布的报告把事实、假设和缺口分开写。证据不够时直接说明，或者把事故交给人，不做猜测。
- **与人协作。** 操作者在工作台读报告、追问（每次追问开一个新 Run）、全局或按目标暂停调查、关闭或重开事故。人工控制始终优先于 Agent。
- **独立观察恢复。** 人处理完事故后，或合格结论发布后自动开始，独立的 Observer 进程用自己的只读 Prometheus 账号采样服务健康，记录服务是否恢复。它从不执行恢复动作。
- **起草待审核的复盘。** worker 根据事故记录起草复盘；人在工作台批准后，它才作为知识进入后续调查。
- **观察发布。** 版本变更事件开启观察窗口，延迟出现的回归会变成事故。OpsPilot 不批准、不阻断、不执行发布。

数据源全部只读：Prometheus 与 Jaeger；`otel-demo-ext` 工具档位另加 Kubernetes 对象、Pod 日志与 Pod/节点用量、Elasticsearch/OpenSearch、Loki、VictoriaLogs、SQL 数据库（PostgreSQL、MySQL/MariaDB、ClickHouse、SQL Server）、MongoDB、Redis/Valkey、Kafka、Argo CD 应用和 Helm release。

不做的事写在 [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)：不写生产、不做发布门、不执行恢复步骤。

## 状态

M3（效果与交付验收）已达到完成线（F8、F9、F14 全部步骤加 F1 第 1–4 步）；F1 第 5 步未达成，所以 F1 未标为通过。这是按生产工程标准做的作品集项目，**从未在生产环境运行过**。

- [feature_list.json](feature_list.json) 中 11 个功能有 9 个通过全部验收步骤（F2、F6、F7、F8、F9、F11、F12、F13、F14）；F1 未完成。F3 于 2026-10-25 通过，2026-11-16 改回未通过：换用后来的报告提示词后，两次受审 Run 中有一次在 summary 里把假设写成了事实（[#664](https://github.com/kevinWangSheng/production-ops-agent/issues/664)）。F8（持续运行）与 F9（交付）只凭 lab 证据通过，不是生产证明。
- M3 的全部验收证据都在运行 OpenTelemetry Demo 的 kind 实验环境里测得：流量是 load-generator 合成的，故障是注入的，模型是真实的（DeepSeek）。包括[与 HolmesGPT 的同条件对比](docs/results/f1-same-condition-comparison-2026-11.md)（F1 第 5 步「校准后的判分」未达成）、[72 小时 lab soak](docs/evidence/m3-soak-2026-11-07/README.md)，以及在刚重装的租用主机上完成的安装、完整事故流程、杀 worker 和按 digest 钉住镜像的升级与回滚（[第 1 步](docs/evidence/f9-final-clean-host-2026-11-11/README.md)、[第 3 步](docs/evidence/f9-steps-2-4-2026-11-10/README.md)、[第 2、4 步](docs/evidence/f9-final2-2026-11-11/README.md)；限制见[限制](docs/portfolio/limitations.md)）。
- 版本由 release workflow 打 tag 并发布到 GHCR。最新是 `v0.1.1`（2026-11-10）；soak 与前两轮干净主机运行用的是 `v0.1.0`，[干净主机终轮](docs/evidence/f9-final-clean-host-2026-11-11/README.md)（2026-11-11）在钉住的 `v0.1.0` 与 `v0.1.1` digest 之间切换。`v0.1.0` 的 digest 是 `ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd`。

### 评测结果（2026-11）

在租用主机上做的集成运行（kind 集群；ITBench-Lite 和 Cloud-OpsBench 为回放的离线快照），模型是真实的 DeepSeek `deepseek-flash`，judge 用 Z.ai `glm-5.3-flashx`，与上游评测的 judge 不同；Cloud-OpsBench 用基准自带的确定性评分器，不用 judge。每个 trial 都计入，没有报告的 trial 记 0。这些都不是生产证据。

| 评测 | 运行 | OpsPilot | 对照 |
|---|---|---|---|
| HolmesGPT 公开评测集，同题、同模型、同 judge | 53 题 × 3 遍，全量 2026-11-21 | 133/159 | HolmesGPT 150/159，同一轮 |
| 同上，修复 [#772](https://github.com/kevinWangSheng/production-ops-agent/pull/772)、[#775](https://github.com/kevinWangSheng/production-ops-agent/pull/775)、[#776](https://github.com/kevinWangSheng/production-ops-agent/pull/776) 后 | 只跑 OpsPilot 侧的定向重跑，2026-11-22 | 145/159，拼合值（见下） | HolmesGPT 未重跑 |
| SREGym Lite 诊断 | 17 题 × 3 遍，2026-11-21 | 43/51 | — |
| ITBench-Lite SRE，离线快照 | 35 个场景 × 3 遍，2026-11-18 | 82/105，官方根因 pass@1 | — |
| HolmesGPT 公开评测集，同模型、同 judge | 57 题 × 3 遍（上面的 53 题加新增 4 题），全量 2026-11-23（[证据](docs/evidence/rental-2026-11-23/full/run.md)） | 163/171；同一 53 题 151/159 | HolmesGPT 162/171：新增 4 题本次跑（12/12），其余 53 题沿用 2026-11-21 的结果（150/159） |
| SREGym Lite 诊断 | 17 题 × 3 遍，2026-11-23（[证据](docs/evidence/rental-2026-11-23/full/sregym/run.md)） | 38/51 | — |
| ITBench SRE 现场版（在新部署的环境里注入故障，从告警开始调查） | 24 个场景 × 1 遍，2026-11-26（[证据](docs/evidence/itbench-live-full-1125/run.md)） | 官方根因通过 14/24；去掉 2 道注入失败题 14/22；有官方分的 14/19 | — |
| Cloud-OpsBench（LLM4Ops；基于快照的 K8s 根因分析，754 题） | 分层抽样 100 题子集，跑 1 次，2026-11-26（[任务记录](docs/tasks/2026-11-26-cloudopsbench.md)） | JRA 0.96（全集加权估计 0.962）；Boutique 0.973，TrainTicket 0.923 | 基准自带 ReAct agent，同一模型：JRA 0.89（0.898）；Boutique 0.932，TrainTicket 0.769 |

- HolmesGPT 评测集分组（全量 → 修复后，对 HolmesGPT）：数据库 21/21 对 16/21（HolmesGPT 丢的 5 遍是 pytest 报错或跳过、没有回答，是否为上游 harness 问题未确认）；Elasticsearch 与多集群 72/90 → 82/90 对 86/90；runbook/skill 10/18 → 12/18 对 18/18；Loki 与 VictoriaLogs 27/27 对 27/27；任务清单 3/3 对 3/3。HolmesGPT 按上游默认配置运行，其中包含 OpsPilot 按只读约束不提供的工具。
- 145/159 不是单次全量。重跑 3 遍的题（12 道 Elasticsearch、6 道 runbook，以及追加 3 遍的 1 道 Loki 题）取重跑分；只重跑 1 遍的题保留全量分；235 题取隔离重跑的 3 遍（3/3），因为批量重跑时并行题的命名空间混进了它（若用那次受污染的 1/3，总分为 143）。
- ITBench-Lite：分数是官方评测器的每个 trial `root_cause_entity_f1 == 1`，judge 用上面那个，不是官方示例的。按 OpsPilot 自己的确定性实体匹配（`det_pass`），同样 105 个 trial 是 22/105。结论偏向 `partial`：`supported` 22 份、`partial` 83 份。这套题没有跑 HolmesGPT。
- 2026-11-21 那轮发现的已知缺口：回看窗当时按设计固定为 24 小时，HolmesGPT 评测集里 185、280 两题 0/3 就归因于此（[#779](https://github.com/kevinWangSheng/production-ops-agent/issues/779)）；没有按目标常驻的操作员说明作为输入，而 162 题（0/3）依赖它（[#780](https://github.com/kevinWangSheng/production-ops-agent/issues/780)）；SREGym 第 p12 题（Valkey）0/3，因为它的目标没有可交给 Redis/Valkey 工具的只读账号（[#773](https://github.com/kevinWangSheng/production-ops-agent/issues/773)）。
- 2026-11-23 那轮（冻结提交 `51257e79`，含 30 天回看窗与报告合同修复）：HolmesGPT 侧只重跑了新增 4 题，其余 53 题是 2026-11-21 的结果，不是重新跑的。185、280 两题在 30 天回看窗下为 3/3；162 仍是 0/3（没有常驻操作员说明，[#780](https://github.com/kevinWangSheng/production-ops-agent/issues/780)）。运行途中 judge 余额耗尽，70 遍事后按保存的答案补判，judge 与口径不变。
- SREGym 38/51 对之前的 43/51：没过的每一遍都提交了完整报告。2026-11-24 在同一提交上对下降的三题做了摆动补跑，得 8/9，与之前那轮相同，因此把这次下降看作运行间波动（这是推断，每题只有 6 遍；[#831](https://github.com/kevinWangSheng/production-ops-agent/issues/831)、[摆动补跑](docs/evidence/rental-2026-11-24/swing/run.md)）。p12 仍是 0/3。
- ITBench 现场版：自托管、自测，不是榜单成绩。分数是官方评测器的 `root_cause_entity_f1 == 1`，judge 用上面那个。每个场景只跑 1 遍，单题结果可能波动。未得分的 5 个场景里，2 个上游注入故障失败，2 个没有出现入口告警，1 个 Run 正常完成但官方评测器重试 3 次都没给分；这 5 个在 24 里都计 0。评测侧打了一个 namespace 标签补丁，偏离上游默认。
- Cloud-OpsBench：两条线各跑 1 次，温度 0，没有测重复运行的方差。OpsPilot 保持默认的 100 次模型请求上限，对照用基准的 20 次迭代；对照有 19 题的输出被基准默认的 `max_tokens` 4096 截断。失败题按 0 计入分母，这与基准自带的 `summarize` 不同，后者只对有答案文件的题求平均。冻结摘要：[OpsPilot](docs/evidence/cloudopsbench-r20261126/opspilot/summary.json)、[对照](docs/evidence/cloudopsbench-r20261126/control/summary.json)。
- 证据：[HolmesGPT 全量](docs/evidence/rental-2026-11-20/full/run.md)（分组见 `compare-*.md`）、[定向重跑](docs/evidence/rental-2026-11-20/rerun-2026-11-22/run.md)及其[逐题对照](docs/evidence/rental-2026-11-20/rerun-2026-11-22/compare.md)、[SREGym 分数](docs/evidence/rental-2026-11-20/full/sregym/sregym-lite-full/scores.json)、[ITBench-Lite 运行](docs/evidence/itbench-lite-sre-lite-full-r3/run.md)；汇总见[工具面拓展任务记录](docs/tasks/2026-11-12-tool-expansion.md)。

导览见[作品集导览](docs/portfolio/README.md)（英文），证据不能说明的事见[限制](docs/portfolio/limitations.md)。工作顺序见 [ROADMAP.md](ROADMAP.md)。

## 用 Docker Compose 安装

安装在控制侧：一台 Linux 主机上运行 PostgreSQL、web 工作台、worker（调查者）和 Observer，各自是同一个产品镜像起的独立容器。工作台只发布在 `127.0.0.1`，PostgreSQL 不发布端口。

需要：装有 Docker Engine 与 Compose v2 插件的 Linux 主机、`bash`、`python3`（只用标准库，给生成的凭据算哈希）、本仓库的 checkout。全程用一个能使用 Docker 的专用**非 root** 用户执行，`install.sh` 拒绝以 root 运行。macOS + colima 只用于开发。

如果镜像只允许 root 登录，先以 root 建这个用户，再从它的登录 shell 继续；它不需要 sudo（要在同一台主机上跑固定版本的 lab，改按[同机 lab 一节](#同一台主机上的-opentelemetry-demo-lab)做，那一节也会安装 Docker）：

```sh
adduser --disabled-password --gecos "" opspilot
usermod -aG docker opspilot
su - opspilot
```

### 1. 安装

```sh
git clone https://github.com/kevinWangSheng/production-ops-agent.git
cd production-ops-agent
deploy/compose/install.sh install
```

它从 checkout 构建镜像 `opspilot:local`，一次性生成全部口令和令牌，迁移数据库，创建四个运行时数据库 login，启动 web 工作台。密钥放在 `${XDG_CONFIG_HOME:-$HOME/.config}/opspilot`（仓库之外，权限 0600；用 `OPSPILOT_SECRETS_DIR` 可换目录）。下文用 `$SECRETS` 指这个目录，用 `$PORT` 指工作台端口：

```sh
SECRETS="${OPSPILOT_SECRETS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opspilot}"
PORT="${OPSPILOT_WEB_HOST_PORT:-18080}"
```

如果安装时用了自定义的 `OPSPILOT_SECRETS_DIR`，在当前 shell 里重新 export 它，或把 `SECRETS` 设为 `install.sh install` 输出的目录（最后几行会写出 `ui_password` 和 Alertmanager 令牌所在位置）。同样，如果安装时用了自定义的 `OPSPILOT_WEB_HOST_PORT`，重新 export 它，或从记住端口的 `$SECRETS/install.env` 读取（安装器也会输出工作台地址）。

重跑 `install` 是安全的，不会重新生成口令；不打印任何密钥。

### 2. 登录工作台

```sh
cat "$SECRETS/ui_password"
```

打开 `http://127.0.0.1:$PORT`，用户名 `operator`，口令即上面的内容。从其他机器访问用 SSH 隧道，本地端口保持一致（允许的 origin 跟随 `OPSPILOT_WEB_HOST_PORT`）：

```sh
ssh -L "$PORT:127.0.0.1:$PORT" <user>@<control-host>
```

### 3. 配置 worker 与 Observer

worker 需要模型 key；Observer 需要自己的只读 Prometheus 账号。两个文件已经以只含注释的占位文件存在。用编辑器写入，不要用 `echo`，免得 key 留在 shell 历史里：

```sh
${EDITOR:-vi} "$SECRETS/worker_env"
${EDITOR:-vi} "$SECRETS/observer_env"
```

写成生效的行（行首不带 `#`）；只有注释的文件会被下一次 `upgrade` 拒绝。

`worker_env`：

```text
DEEPSEEK_API_KEY=<你的 DeepSeek API key>
```

`observer_env`（用户名加口令，或改为一行 `OPSPILOT_OBSERVER_PROMETHEUS_TOKEN=<令牌>`）：

```text
OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=<只读用户>
OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD=<口令>
```

### 4. 启动 worker 与 Observer

```sh
OPSPILOT_START_AGENT=1 \
OPSPILOT_OBSERVER_PROMETHEUS_URL=http://<prometheus-host>:<port> \
  deploy/compose/install.sh upgrade
```

安装器在任何 Docker 调用之前检查：`worker_env` 有非空的 `DEEPSEEK_API_KEY`，URL 是 http(s)；否则拒绝，不做任何改动。升级会停掉全部运行时、迁移、校验数据库 login，然后启动 web、worker 和 Observer。这两个设置记在 `install.env` 里，之后只需 `deploy/compose/install.sh upgrade`。Prometheus URL 必须能从 Observer 容器访问到。

确认三个运行时都是 healthy（每个容器的 healthcheck 读自己在容器端口 9464 上的健康端点 `/readyz`）：

```sh
deploy/compose/install.sh compose ps
deploy/compose/install.sh compose logs worker
```

### 5. 把告警绑定到目标并发送

`$SECRETS/target_identities.json` 初始为 `{}`：这时每条告警都直接交给人，不开 Run。要让匹配的告警开 Run，按目标 id 加一项。默认工具档 `fixture` 下，worker 只认目标 `checkout-prod`，调查的是一份固定样本数据，不是你的真实系统；用其他键时 Run 的工具调用会被拒（`TARGET_NOT_REGISTERED`）。

```json
{
  "checkout-prod": {
    "integration_id": "fixture",
    "cluster_uid": "fixture-cluster",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {"version": 1, "labels": {"alertname": "<你的告警名>"}}
  }
}
```

改完后重建 web 容器（普通 `restart` 会沿用旧文件）：

```sh
deploy/compose/install.sh compose up -d --force-recreate web
```

告警发到 `POST http://127.0.0.1:$PORT/intake/alertmanager`，bearer 令牌在 `$SECRETS/alertmanager_token`。同一主机上的 Alertmanager 可以这样配 receiver：

```yaml
receivers:
  - name: opspilot
    webhook_configs:
      # 如果改过 OPSPILOT_WEB_HOST_PORT，把 18080 换成该端口
      - url: http://127.0.0.1:18080/intake/alertmanager
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/alertmanager_token
```

之后事故、Run 和报告都会出现在工作台。实际取证时是向这个端点发了一条 Alertmanager 格式的合成告警；第二次 Run 完成，报告显示在工作台。那次运行与上面步骤的差别只有：端口 18180、非默认密钥目录，以及 lab 接线（Observer 容器接入 kind 网络以访问 lab 的只读 Prometheus 代理）。

`otel-demo` 工具档（`OPSPILOT_TOOL_PROFILE=otel-demo`，并在 `worker_env` 写入 `OPSPILOT_OTEL_*` 地址与凭据）读取固定版本的 OpenTelemetry Demo lab，而不是 fixture。

### 同一台主机上的 OpenTelemetry Demo lab

要在一台干净的 Ubuntu 24.04 x86_64 主机上让 OpsPilot 对接固定版本的 lab（kind 集群上的 OpenTelemetry Demo、它的 Prometheus、Jaeger 和一个 Alertmanager），用下面的步骤代替第 1–5 步。以 root：

```sh
adduser --disabled-password --gecos "" opspilot
su - opspilot -c 'git clone https://github.com/kevinWangSheng/production-ops-agent.git'
/home/opspilot/production-ops-agent/scripts/kind_lab/install_linux.sh --user opspilot
su - opspilot
```

`install_linux.sh` 安装固定版本的 Docker Engine、kind、kubectl 和 helm，把 `opspilot` 加入 `docker` 组，并以该用户启动 lab（约 5 分钟，最后输出健康报告）。然后以 `opspilot`：

```sh
cd production-ops-agent
python3 scripts/kind_lab.py control-secrets
${EDITOR:-vi} ~/.config/opspilot/worker_env      # 加入 DEEPSEEK_API_KEY=<你的 key>
OPSPILOT_START_AGENT=1 deploy/compose/install.sh install
python3 scripts/kind_lab.py connect-control --project opspilot
```

`control-secrets` 在首次安装前填好密钥目录：lab 的 Alertmanager 令牌写成 `alertmanager_token`（工作台据此接受 lab 的告警），lab 的 Prometheus 与 Jaeger 地址和只读账号写进 `worker_env` 与 `observer_env`，lab 目标 `m0-otel-20260909` 写进 `target_identities.json`，`install.env` 写入 `otel-demo` 工具档、Observer 的 Prometheus URL 和工作台端口 `18090`（18080 被 lab 前端占用）。已有的值不会被覆盖，也不打印任何密钥；如果 `install.sh install` 已经生成了自己的令牌，它会停下并说明怎样把那个令牌交给 lab。`connect-control` 把三个运行时容器接入 lab 的 `kind` 网络；每次 `install.sh upgrade` 之后都要再跑一次。之后工作台在 `http://127.0.0.1:18090`，`python3 scripts/kind_lab.py fault inject --experiment-id <id>` 会让 lab 的 checkout 告警在约 5–7 分钟后触发（`fault restore --experiment-id <id>` 撤销）。

### 安装器设置

在 `install`、`upgrade`、`uninstall` 或 `compose` 时以环境变量给出。实际使用的值保存在 `<密钥目录>/install.env`，之后的运行沿用，除非重新给出（`OPSPILOT_SECRETS_DIR` 本身记在 `${XDG_STATE_HOME:-~/.local/state}/opspilot/installs/<项目名>`）。

| 名称 | 默认值 | 作用 |
|---|---|---|
| `OPSPILOT_SECRETS_DIR` | 该项目安装时用的目录，否则 `${XDG_CONFIG_HOME:-$HOME/.config}/opspilot` | 密钥与设置，须在仓库之外。项目的数据库卷已存在时换用其他目录会被拒绝。 |
| `OPSPILOT_PROJECT` | `$COMPOSE_PROJECT_NAME`，否则 `opspilot` | Compose 项目名（小写字母、数字、`-`、`_`），一个名称一套安装。 |
| `OPSPILOT_WEB_HOST_PORT` | `18080` | 工作台在 `127.0.0.1` 上的端口，允许的 origin 跟随它。 |
| `OPSPILOT_IMAGE` | `opspilot:local` | 产品镜像。默认值从 checkout 构建；其他值改为 pull，且必须按 digest 固定（`<name>@sha256:<64 位十六进制>`）。 |
| `OPSPILOT_TOOL_PROFILE` | `fixture` | worker 工具档：`fixture`（固定样本数据，目标 `checkout-prod`）或 `otel-demo`（固定版本的 OpenTelemetry Demo lab）。 |
| `OPSPILOT_START_AGENT` | `0` | `1` 时同时启动 worker 与 Observer；`worker_env` 没有 `DEEPSEEK_API_KEY` 或 Prometheus URL 不是 http(s) 时，在任何 Docker 调用之前拒绝。 |
| `OPSPILOT_OBSERVER_PROMETHEUS_URL` | 空 | Observer 的只读 Prometheus 地址；`OPSPILOT_START_AGENT=1` 时必填。 |
| `OPSPILOT_TRACE` | `off` | worker 的 trace 导出：`off`、`lab`（只用于合成 lab）或 `outbox`（把 trace outbox 里的标识、版本、token 数和固定码发到 LangSmith；仅当 `worker_env` 有 `LANGSMITH_API_KEY` 且 `LANGSMITH_PROJECT=opspilot-lab-*`，可选 SaaS `LANGSMITH_ENDPOINT` 与 `LANGSMITH_WORKSPACE_ID`；否则不发送，worker 的 `/metrics` 报原因）。 |

### 升级与卸载

```sh
git pull && deploy/compose/install.sh upgrade        # 从 checkout 重建；密钥与数据不变
deploy/compose/install.sh uninstall                  # 删除容器；保留数据库卷与密钥
deploy/compose/install.sh uninstall --purge-data     # 同时删除数据库；需输入项目名确认
```

升级顺序、镜像固定（`OPSPILOT_IMAGE=<name>@sha256:<digest>`）与目标身份见 [deploy/compose/UPGRADE.md](deploy/compose/UPGRADE.md)。全部设置列在 [deploy/compose/install.sh](deploy/compose/install.sh) 开头。

## 延伸阅读

1. [作品集导览](docs/portfolio/README.md)：架构、威胁模型、运维、上游归属、评测结果与限制（英文）。
2. [PRD.md](PRD.md) 与 [feature_list.json](feature_list.json)：用户能力与验收步骤。
3. [SPEC.md](SPEC.md)：范围、技术选型、实施门槛。
4. [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)：产品代码的运行时约束。
5. [范围 ADR](docs/adr/0001-readonly-investigation-boundary.md)：为什么不做生产写入和发布门。
6. [ROADMAP.md](ROADMAP.md)：状态表与工作顺序。
7. [文档导航](docs/README.md) 与 [开发指南](docs/development.md)：权威来源、从源码运行、测试。
8. [M1 端到端演示](docs/demo/m1-end-to-end.md)：kind lab 上从告警到调查、恢复与复盘。

## 许可证

OpsPilot 以 [MIT 许可证](LICENSE) 发布。仓库中的第三方材料保留各自的许可证：内置的 DeepSeek tokenizer（MIT，`opspilot/tools/tokenizer/LICENSE`）和 HolmesGPT 文本摘录（Apache-2.0，`third_party/holmesgpt/LICENSE`）；复制了什么、放在哪里见[上游归属](docs/portfolio/upstream-attribution.md)（英文）。
