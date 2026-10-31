# OpsPilot — 只读运维调查 Agent

[English](README.md) | 简体中文

OpsPilot 是持续运行的事故与发布后调查 Agent。它只读取遥测数据，不改动生产环境，所有决定留给人。

本文与中文合同文档（[SPEC.md](SPEC.md)、[PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)、[C3 技术方案](docs/design/technical-proposal-2026-09-07.md)、任务记录）不一致时，以中文合同文档为准。

## 它做什么

- **调查。** Alertmanager 告警或发布事件会开一个事故和一次调查 Run。模型通过按目标授权的只读工具收集证据，同时保留相互竞争的假设，发布的报告把事实、假设和缺口分开写。证据不够时直接说明，或者把事故交给人，不做猜测。
- **与人协作。** 操作者在工作台读报告、追问（每次追问开一个新 Run）、全局或按目标暂停调查、关闭或重开事故。人工控制始终优先于 Agent。
- **独立观察恢复。** 人处理完事故后，独立的 Observer 进程用自己的只读 Prometheus 账号采样服务健康，记录服务是否恢复。它从不执行恢复动作。
- **起草待审核的复盘。** worker 根据事故记录起草复盘；人在工作台批准后，它才作为知识进入后续调查。
- **观察发布。** 版本变更事件开启观察窗口，延迟出现的回归会变成事故。OpsPilot 不批准、不阻断、不执行发布。

不做的事写在 [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)：不写生产、不做发布门、不执行恢复步骤。

## 状态

当前在 M3（效果与交付验收）阶段。这是按生产工程标准做的作品集项目，**没有经过生产验证**。

- [feature_list.json](feature_list.json) 中 11 个功能有 7 个通过全部验收步骤（F2、F3、F6、F7、F11、F12、F13）；F1、F8、F9、F14 未完成。
- 验收运行使用本机 kind 集群上的 OpenTelemetry Demo 加故障注入，以及真实模型（DeepSeek）。这些是集成运行，不是生产观察。
- 下面的 Compose 安装已在本机 Linux VM 上跑通，包括经安装后的栈完成一次真实调查（[证据](docs/evidence/f9-393-installed-investigation/README.md)）。全新 Linux 主机上的干净安装、镜像发布与 `v0.1.0` tag、72 小时 soak、与 HolmesGPT 的同条件对比都还没做。

工作顺序与证据见 [ROADMAP.md](ROADMAP.md)。

## 用 Docker Compose 安装

安装在控制侧：一台 Linux 主机上运行 PostgreSQL、web 工作台、worker（调查者）和 Observer，各自是同一个产品镜像起的独立容器。工作台只发布在 `127.0.0.1`，PostgreSQL 不发布端口。

需要：装有 Docker Engine 与 Compose v2 插件的 Linux 主机、`bash`、`python3`（只用标准库，给生成的凭据算哈希）、本仓库的 checkout。全程用一个能使用 Docker 的专用**非 root** 用户执行，`install.sh` 拒绝以 root 运行。macOS + colima 只用于开发。

### 1. 安装

```sh
git clone https://github.com/kevinWangSheng/production-ops-agent.git
cd production-ops-agent
deploy/compose/install.sh install
```

它从 checkout 构建镜像 `opspilot:local`，一次性生成全部口令和令牌，迁移数据库，创建四个运行时数据库 login，启动 web 工作台。密钥放在 `~/.config/opspilot`（仓库之外，权限 0600；用 `OPSPILOT_SECRETS_DIR` 可换目录）。重跑 `install` 是安全的，不会重新生成口令；不打印任何密钥。

### 2. 登录工作台

```sh
cat ~/.config/opspilot/ui_password
```

打开 `http://127.0.0.1:18080`，用户名 `operator`，口令即上面的内容。从其他机器访问用 SSH 隧道，本地端口保持一致（允许的 origin 跟随 `OPSPILOT_WEB_HOST_PORT`）：

```sh
ssh -L 18080:127.0.0.1:18080 <user>@<control-host>
```

### 3. 配置 worker 与 Observer

worker 需要模型 key；Observer 需要自己的只读 Prometheus 账号。两个文件已经以只含注释的占位文件存在。用编辑器写入，不要用 `echo`，免得 key 留在 shell 历史里：

```sh
${EDITOR:-vi} ~/.config/opspilot/worker_env
# DEEPSEEK_API_KEY=<你的 DeepSeek API key>

${EDITOR:-vi} ~/.config/opspilot/observer_env
# OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=<只读用户>
# OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD=<口令>
# （或 OPSPILOT_OBSERVER_PROMETHEUS_TOKEN=<令牌>）
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

`~/.config/opspilot/target_identities.json` 初始为 `{}`：这时每条告警都直接交给人，不开 Run。要让匹配的告警开 Run，按目标 id 加一项。默认工具档 `fixture` 下，worker 只认目标 `checkout-prod`，调查的是一份固定样本数据，不是你的真实系统；用其他键时 Run 的工具调用会被拒（`TARGET_NOT_REGISTERED`）。

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

告警发到 `POST http://127.0.0.1:18080/intake/alertmanager`，bearer 令牌在 `~/.config/opspilot/alertmanager_token`。同一主机上的 Alertmanager 可以这样配 receiver：

```yaml
receivers:
  - name: opspilot
    webhook_configs:
      - url: http://127.0.0.1:18080/intake/alertmanager
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/alertmanager_token
```

之后事故、Run 和报告都会出现在工作台。实际取证时是向这个端点发了一条 Alertmanager 格式的合成告警；第二次 Run 完成，报告显示在工作台。那次运行与上面步骤的差别只有：端口 18180、非默认密钥目录，以及 lab 接线（Observer 容器接入 kind 网络以访问 lab 的只读 Prometheus 代理）。

`otel-demo` 工具档（`OPSPILOT_TOOL_PROFILE=otel-demo`，并在 `worker_env` 写入 `OPSPILOT_OTEL_*` 地址与凭据）读取固定版本的 OpenTelemetry Demo lab，而不是 fixture。

### 安装器设置

在 `install`、`upgrade`、`uninstall` 或 `compose` 时以环境变量给出。实际使用的值保存在 `<密钥目录>/install.env`，之后的运行沿用，除非重新给出（`OPSPILOT_SECRETS_DIR` 本身记在 `${XDG_STATE_HOME:-~/.local/state}/opspilot/installs/<项目名>`）。

| 名称 | 默认值 | 作用 |
|---|---|---|
| `OPSPILOT_SECRETS_DIR` | 该项目安装时用的目录，否则 `${XDG_CONFIG_HOME:-~/.config}/opspilot` | 密钥与设置，须在仓库之外。项目的数据库卷已存在时换用其他目录会被拒绝。 |
| `OPSPILOT_PROJECT` | `$COMPOSE_PROJECT_NAME`，否则 `opspilot` | Compose 项目名（小写字母、数字、`-`、`_`），一个名称一套安装。 |
| `OPSPILOT_WEB_HOST_PORT` | `18080` | 工作台在 `127.0.0.1` 上的端口，允许的 origin 跟随它。 |
| `OPSPILOT_IMAGE` | `opspilot:local` | 产品镜像。默认值从 checkout 构建；其他值改为 pull，且必须按 digest 固定（`<name>@sha256:<64 位十六进制>`）。 |
| `OPSPILOT_TOOL_PROFILE` | `fixture` | worker 工具档：`fixture`（固定样本数据，目标 `checkout-prod`）或 `otel-demo`（固定版本的 OpenTelemetry Demo lab）。 |
| `OPSPILOT_START_AGENT` | `0` | `1` 时同时启动 worker 与 Observer；`worker_env` 没有 `DEEPSEEK_API_KEY` 或 Prometheus URL 不是 http(s) 时，在任何 Docker 调用之前拒绝。 |
| `OPSPILOT_OBSERVER_PROMETHEUS_URL` | 空 | Observer 的只读 Prometheus 地址；`OPSPILOT_START_AGENT=1` 时必填。 |

### 升级与卸载

```sh
git pull && deploy/compose/install.sh upgrade        # 从 checkout 重建；密钥与数据不变
deploy/compose/install.sh uninstall                  # 删除容器；保留数据库卷与密钥
deploy/compose/install.sh uninstall --purge-data     # 同时删除数据库；需输入项目名确认
```

升级顺序、镜像固定（`OPSPILOT_IMAGE=<name>@sha256:<digest>`）与目标身份见 [deploy/compose/UPGRADE.md](deploy/compose/UPGRADE.md)。全部设置列在 [deploy/compose/install.sh](deploy/compose/install.sh) 开头。

## 延伸阅读

1. [PRD.md](PRD.md) 与 [feature_list.json](feature_list.json)：用户能力与验收步骤。
2. [SPEC.md](SPEC.md)：范围、技术选型、实施门槛。
3. [PRODUCT-CONSTRAINTS.md](PRODUCT-CONSTRAINTS.md)：产品代码的运行时约束。
4. [范围 ADR](docs/adr/0001-readonly-investigation-boundary.md)：为什么不做生产写入和发布门。
5. [ROADMAP.md](ROADMAP.md)：状态表与工作顺序。
6. [文档导航](docs/README.md) 与 [开发指南](docs/development.md)：权威来源、从源码运行、测试。
7. [M1 端到端演示](docs/demo/m1-end-to-end.md)：kind lab 上从告警到调查、恢复与复盘。
