# F9 第 1 步：干净主机上一键装 lab 与 OpsPilot 并接上 Agent（2026-11-09）

类别：集成运行（租机，真实 OpenTelemetry Demo lab，真实 DeepSeek 模型，`otel-demo` 工具档）。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。

- 结论：**通过**。主机刚在控制台 Reinstall，从 `git clone` 开始，按文档跑 `sudo scripts/kind_lab/install_linux.sh`（lab）和 `deploy/compose/install.sh install` / `upgrade`（控制侧），退出码都是 0。三个运行时 `/readyz` 200，lab 流量正常，lab Prometheus 抓到三个 OpsPilot 目标。注入一次 `paymentFailure` 后，告警经 Alertmanager webhook 送进工作台，开出 incident 和 Run，Run `completed`，报告 `supported`，在工作台可见。
- 有 4 处文档与实际不符，记在下面「文档与实际」，都做了最小绕过。第 1、2、4 条已在 `docs/development.md` 的 Linux 小节补了一条单机接法。
- 从第一条命令到 Agent 就绪约 6 分钟，到 Run 完成约 16 分钟。

## 主机与版本

- Vultr 租机 `opspilot-f9-1`（共享 vCPU），用户 2026-11-09 22:56 PT 在控制台 Reinstall。Ubuntu 24.04.5 LTS，内核 6.8.0-147，x86_64，8 vCPU，15984 MiB 内存，328 GB 盘。地址不写入仓库。
- 改动前的状态：开机 3 分钟；827 个 ii 包；PATH 上有 git、python3、tmux，没有 docker/kind/kubectl/helm；`/usr/local/bin` 为空；`/home` 只有镜像自带的 `linuxuser`；只监听 22 和本地 DNS；ufw active。
- 代码：main `8de996d9151e121109d51ebc2e064355369ba7ff`（#528 合并后）。`git clone` 后 `git checkout` 到这个 SHA。
- 镜像：README 写的是从 checkout 构建 `opspilot:local`（发布镜像和 `v0.1.0` tag 尚未完成），所以没有拉 GHCR。构建结果 `sha256:2de313b01125003ac586cb7fce5047cccb45c086fd228fd856483e429864ca9a`，版本 0.1.0，revision 标签为上面的 SHA。PostgreSQL 用 compose 钉住的 `postgres@sha256:2a0d0fe14825b0939f78a8cad5cd4e6aa68bf94d0e5dd96e24b6d23af4315545`。schema head `0024_trace_outbox`。
- lab 工具版本（安装脚本钉住）：docker 29.2.1，kind v0.33.0，kubectl v1.37.1，helm v4.3.0。

## 按顺序执行的命令（时间为 UTC，11-10）

| 时间 | 身份 | 命令 | 结果 |
|---|---|---|---|
| 07:00:26 | root | `adduser --disabled-password opspilot`；临时 sudoers 一行，只允许 opspilot 以 root 跑 `install_linux.sh`（见文档与实际第 3 条） | 口令锁定 |
| 07:00:26 | opspilot | `git clone https://github.com/kevinWangSheng/production-ops-agent.git && git checkout 8de996d9…` | 3 s |
| 07:00:29–07:04:47 | opspilot | `/usr/bin/time -v sudo scripts/kind_lab/install_linux.sh` | 退出码 0，4 分 18 秒；结尾 `kind_lab.py health` ok |
| 07:05 | root | 删除临时 sudoers 文件；`kind_lab.py health`（opspilot） | ok |
| 07:05:31 | opspilot | 把 lab 的 `tmp/m1-kind-lab/alertmanager-webhook-token` 复制为 `~/.config/opspilot/alertmanager_token`（见第 2 条） | 两边 sha256 前缀都是 `d3140cac184e` |
| 07:05:31–07:05:57 | opspilot | `OPSPILOT_WEB_HOST_PORT=18180 OPSPILOT_TOOL_PROFILE=otel-demo deploy/compose/install.sh install`（端口见第 1 条） | 退出码 0，26 s；构建、生成密钥、migrate、init-roles、起 web |
| 07:06:20 | opspilot | 往 `worker_env` 追加 `DEEPSEEK_API_KEY`（由主机间 ssh 管道从 da-server 写入，不打印）、`kind_lab.py` 生成的 investigator 账号两行、`OPSPILOT_OTEL_PROMETHEUS_URL` / `OPSPILOT_OTEL_JAEGER_URL`；往 `observer_env` 追加 observer 账号两行；写 `target_identities.json`（见第 4 条） | 日志只有键名 |
| 07:06:20–07:06:30 | opspilot | `OPSPILOT_START_AGENT=1 OPSPILOT_OBSERVER_PROMETHEUS_URL=http://opspilot-m1-control-plane:30090 deploy/compose/install.sh upgrade` | 退出码 0，10 s；web/worker/observer 都 healthy |
| 07:06:31 | opspilot | `python3 scripts/kind_lab.py connect-control --project opspilot` | 三个都 `connected`，退出码 0 |
| 07:07:39 | opspilot | `python3 scripts/kind_lab.py fault inject --experiment-id f9-clean-install-20261109` | 已校验（ConfigMap sha256 `e47d27c2fa01`） |
| 07:19:37 | opspilot | `python3 scripts/kind_lab.py fault restore --experiment-id f9-clean-install-20261109` | 已校验（`1ee5c0258e3e`） |

安装期间每 15 s 采样 `free -m`：峰值 used 2351 MiB，最低 available 13632 MiB，swap 0。

## 健康与连通

- 三个运行时容器内 `GET 127.0.0.1:9464/readyz`：都是 200 `{"status": "ready", "version": "0.1.0"}`。
- worker 日志：`profile=otel-demo`，两个 slot，`worker started`；`trace outbox delivery off: TRACE_MODE_OFF`。observer 日志：`prometheus=http://opspilot-m1-control-plane:30090`，`observer started`。
- lab Prometheus（lab 账号）：`up{job=~".*opspilot.*"}` 三个目标都是 1。各服务 span 速率非零，frontend 约 2.25/s、checkout 约 1.03/s（2 分钟窗口）。
- 工作台：`127.0.0.1:18180`，Basic Auth `operator` 取页面 200，匿名 401。

## 一次真实 Run

| 项 | 值 |
|---|---|
| 注入 | 07:07:39 `paymentFailure` → 100% |
| 告警 | `CheckoutPlaceOrderErrorRatioHigh`，startsAt 07:12:54；07:13:04 webhook POST 200，delivery outcome `created`，actor `alertmanager` |
| incident | `3c6deab1-96cf-549a-859c-466f4a385848` |
| Run | `97890bd2-ffe6-520c-8689-ed8d32b52b37`，`completed`，worker `status=published`（07:16:17） |
| 用量 | 12 次模型调用（`deepseek-flash`），prompt 364474 token（缓存未命中 39866），completion 26185 token；20 次工具调用，工具耗时 27.1 s；预算 12 spent / 0 unknown of 100 |
| 报告 | assessment `completed`，conclusion `supported`，content sha256 `8c393d2b81ac2181e384bf4058cc127109cf216e44ecd6ef2c15ab69026cec54`；事实 9、假设 1、反证 2、已否定假设 1、未知/缺口 7、建议 0 |
| 恢复观察 | Run 结束后系统自动授权（07:16:26），第 1 个样本 `degraded`（窗口覆盖故障期） |
| trace | 无。da-server 的 `worker_env` 里没有 LangSmith key，本机也没有可用的，`OPSPILOT_TRACE` 保持 `off` |

报告摘要（工作台页面文本）：告警是因为 checkout 的 PlaceOrder 服务端 span 全部报错；证据里最深的原因在 payment 服务自己的 `PaymentService/Charge`，每次都抛 `Payment request failed. Invalid token. app.loyalty.level=gold`（`charge.js:37`），沿 checkout → frontend → frontend-proxy → load-generator 向外传播。约 07:08–07:09 出现阶跃；延迟或下游依赖的解释不成立。flagd 开关导致的故障是首要假设，报告明确没有当成已证事实。这与实际注入的 flagd `paymentFailure` 一致。

## 文档与实际

1. **端口冲突**：lab 的 frontend 按 `scripts/kind_lab/kind-config.yaml` 发布在 127.0.0.1:18080，`install.sh` 的工作台默认端口也是 18080。装好 lab 后 18080 已有 1 个监听。README 和 `docs/development.md` 都没提单机时要换端口。绕过：`OPSPILOT_WEB_HOST_PORT=18180`。
2. **webhook token 没有对齐的说明**：`kind_lab.py up` 生成自己的 `alertmanager-webhook-token`，`install.sh install` 又生成自己的 `alertmanager_token`，两边随机生成，告警会被拒。soak 文档只写了两台机器的做法（把控制侧 token 拷到 lab）。绕过：`install` 之前把 lab 的 token 复制到 secrets 目录（installer 的 write-once 会保留它）。这样做之后，第一次告警就被接受（`created`）。没有先按原文档跑一次去复现 401，这一条是读代码得出的（`kind_lab.webhook_token`、`install.sh` 的 `write_once alertmanager_token`）。
3. **非 root 用户怎么来**：README 要求专用非 root 用户，`install_linux.sh` 要求通过 sudo 运行，而且用 `SUDO_USER` 决定 lab 用户。Vultr 镜像只能用 root 登录，文档没写怎么建这个用户、怎么给 sudo。绕过：建 `opspilot`，只在安装那 4 分钟里放一条 NOPASSWD sudoers（只允许这一个脚本），装完删除。这一条没改文档。
4. **`otel-demo` 档单机接线没有写值**：README 只说「`OPSPILOT_OTEL_*` endpoints and credentials in `worker_env`」，`target_identities.json` 只给了 fixture 的例子。容器里能用的地址（kind 节点 `opspilot-m1-control-plane` 的 NodePort 30090 / 30686）、`kind_lab.py env-file` 生成的账号文件、`m0-otel-20260909` 目标的写法，都要从 #393 证据、`scripts/eval_live_inject.py` 和 soak README 拼出来。

第 1、2、4 条已在 `docs/development.md` 的 Linux 小节加了一条「单机」说明。README（英文安装指南）没有改，见下文。

## 这份证据不证明的

- 不是生产观察，也不是 soak；只跑了一次 Run，一个故障类型。
- 没有 LangSmith trace（没有 key）；费用没有按余额差对账，只记了 token。
- 没有用发布镜像或 `v0.1.0` tag（还不存在）；镜像是本机构建的。
- 安装需要的 3 处手工步骤（建用户、对齐 token、填 env 与目标身份）不是一键完成的；「一键」只针对两个脚本本身。
- 恢复观察没有结束：故障 07:19:37 已恢复，incident 停在 `observing_recovery`，留给用户做手工关闭演练。
- README 首页「clean-host install … still open」的状态句与 feature_list 的 `passes` 没有改，由协调者决定。

## 原始记录

不入库（ADR-0006），在仓库外 `~/.cache/opspilot-f9-clean-install-2026-11-09/raw/`（主机上的 `/root/f9-evidence/` 与 `/home/opspilot/f9-evidence/`）：准备与 lab 安装日志、内存采样、lab health、控制侧安装与升级日志、Prometheus 查询、注入与恢复日志、incident 页面 HTML 与文本。已检查不含主机地址，也不含密钥值（只有键名和文件路径）。
