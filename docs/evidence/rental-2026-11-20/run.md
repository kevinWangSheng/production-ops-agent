# 租机记录 2026-11-20（#669 + #671，主机准备）

不含凭据。证据先留在运行 checkout，不提交。

## 主机

| 项 | 值 |
|---|---|
| 供应商 / 名称 | Vultr，`opspilot-tool-1`（新实例） |
| 开机时刻 | 2026-11-20 16:14:34 UTC（08:14 PT），`uptime -s` |
| 规格 | AMD EPYC-Rome 8 vCPU / 32089 MiB / 8 GiB swapfile / 根盘 150 GB |
| 系统 | Ubuntu 24.04.5 LTS，内核 6.8.0-149-generic，x86_64 |
| 主机 ED25519 指纹 | SHA256:u30lT2h4XC2BPsRRw86RCmBDYZdWzajwstuQoGaqLNA |
| 单价 | 未确认，以 Vultr 账单为准 |
| 提交 S | `a5e70d380f454fab2c9fd156202889a66f2aaf40`（`main`），运行 checkout `/home/opspilot/production-ops-agent`，无本地改动 |

## 阶段 1 各步（2026-11-20 16:25–17:20 UTC 前后）

| 步 | 结果 |
|---|---|
| 加固 | `/etc/ssh/sshd_config.d/00-opspilot.conf`：Port 22 + 443、PasswordAuthentication no、KbdInteractiveAuthentication no、PermitRootLogin prohibit-password；`sshd -T` 生效；ufw 入站只放 22/tcp、443/tcp；443 密钥登录成功，口令登录被拒（`Permission denied (publickey)`） |
| 用户与克隆 | `opspilot` 用户，以 opspilot 克隆并检出提交 S |
| `install_linux.sh --user opspilot` | 退出码 0，health `ok: true`（日志 `/root/rental-logs/install.log`） |
| 工具 | PostgreSQL 17.12（PGDG，`create_main_cluster = false`，未建 5432 默认实例；`initdb`/`pg_ctl` 等软链到 `/usr/local/bin`）、uv 0.11.8（SREGym 要求 ≥ 0.11.8）、jq、gh、python3.12-venv、lsof；`make setup` 退出码 0 |
| lab 检查 | health ok；rules-check `{"rules":0,"config":0,"alertmanager":0,"self_route":0}`；frontend span 速率 1.30/s；flagd 11 个开关全 off |
| `/etc/hosts` | 加 `127.0.0.1 kafka`；9092 无监听 |
| Holmes harness（#668） | 上游 `a0009195`、Poetry 1.8.5、`--with dev` 安装完成，工作树无改动；`--collect-only --no-cov -p no:sugar -n 0` 收集 294 项 |
| SREGym（#667） | 上游 `ac60c642`，子模块 SREGym-applications `647cd558`，`uv sync --locked` 完成；`verify_upstream` 通过。`kind-sregym` 集群未建（见下） |
| 镜像 | 24 个全部拉取成功（见下）；20 个题目夹具镜像以 amd64 单平台导入 `opspilot-m1` 节点 |
| 凭据目录 | `/home/opspilot/.config/opspilot/`（700，opspilot），`rental.env` 未建，待放入 |

## 镜像

宿主 docker：VictoriaLogs `@sha256:ee4e820c…`、`apache/kafka:4.3.2@sha256:852a0d49…`、ES `@sha256:5bfdf431…`（CI 钉的 8.19.25）、SREGym 节点 `ghcr.io/sregym/kind-node:k8s-v1.32.5-…@sha256:4ea90b49…`。
题目夹具（宿主 + `opspilot-m1` 节点）：grafana/loki:2.9.0、grafana/promtail:2.9.0、grafana/grafana:12.3.1、grafana/tempo:2.8.2、grafana/loki|promtail|grafana:latest、nginx:latest、python:3.9/3.11/3.12-slim、nginx:1.27-alpine、nginx:1.25、busybox:1.36、postgres:17.2-alpine、mysql:8.0.40、mariadb:11.6.2、clickhouse/clickhouse-server:24.1-alpine、mcr.microsoft.com/mssql/server:2022-latest、mongo:7.0.17。
`kind load docker-image` 在 docker 29 上失败（`ctr import --all-platforms` 找不到其他架构的 blob）；改用 `docker save --platform linux/amd64 | ctr -n k8s.io images import`，属于环境处理。`:latest` 镜像在 Pod 里默认 `imagePullPolicy: Always`，预导入不能避免每遍重新拉取。

## 发现（未修，报 lead）

- `scripts/holmes_compare/install_upstream.sh` 给相对 DEST 时会失败：先 `cd "$DEST"`，再调用 `"$DEST/.tools/bin/poetry"`（相对路径），退出码 127。`run_rental.sh` 固定传相对路径 `tmp/holmes-compare/upstream`，所以照原样跑每次都会失败。主机上用绝对路径装完。
- `scripts/c4_argocd_scenario.py`（C4 记录「自建功能验收」与交接文件都写了）在 `a5e70d38` 中不存在。

## 未执行

- 没有跑真实 Run、评测题，也没有调用任何模型。
- `kind-sregym` 集群没建：上游 4 节点（1 控制面 + 3 worker）加 Calico，和 lab 同时开会抢 8 vCPU，留到 SREGym 阶段再建（`KIND_CLUSTER_NAME=sregym bash kind/setup_kind_cluster.sh`）。
- `rental.env` 由 lead/用户放入。
