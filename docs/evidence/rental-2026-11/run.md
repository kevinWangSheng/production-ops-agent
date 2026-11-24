# 租机记录：工具拓展真实评测（#616）

类别：环境准备（阶段 1，#617）。本阶段不跑调查、不调模型、不跑冒烟。依据 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 第 1 节与阶段 1。时间为太平洋时间（PST，UTC−8），2026-11-14。

## 主机

| 项 | 值 | 来源 |
|---|---|---|
| 供应商 | Vultr，区域 LAX（US） | 实例 metadata `169.254.169.254/v1.json` |
| 实例 | instanceid `114376412`，主机名 `vultr` | metadata、`hostname` |
| 规格 | 8 vCPU（AMD EPYC-Rome），内存 32089 MiB，swap 8191 MiB，磁盘 150 GB（`/dev/vda2`） | `nproc`、`lscpu`、`free -m`、`df -h /` |
| 系统 | Ubuntu 24.04.5 LTS，x86_64，Python 3.12.3 | `/etc/os-release`、`uname -m` |
| 公网 IP | 104.207.153.253（root 只允许密钥登录，用完即弃） | `ip -4 addr` |
| 防火墙 | ufw active，入站只放行 22/tcp | `ufw status verbose` |
| 单价 | 未确认（主机上看不到，Vultr 控制台未查） | — |
| 开机时刻 | 未确认（控制台未查）；08:30 时 uptime 3 小时 33 分，推算约 04:57 开机 | `uptime` |
| 初始状态 | 只有默认 `linuxuser`，无 `opspilot` 用户；内存已用 634 MiB，磁盘已用 12 GB | — |

**提交 S**：`c8f753f9263c4d85e584e6577a0f05415443d554`（`origin/main`，2026-11-14 08:30）。主 checkout `/home/opspilot/production-ops-agent` 在 `main` 分支，HEAD = S，无已跟踪修改。

## 阶段 1 步骤

| 时刻（PT） | 步 | 命令 | 结果 |
|---|---|---|---|
| 08:31:05 | 1.1 建用户、克隆 | `adduser --disabled-password --gecos "" opspilot`；`su - opspilot -c 'git clone …'`，检出 S | 克隆归 `opspilot` 所有，HEAD = S |
| 08:31:30–08:35:16 | 1.1 安装 lab | root：`scripts/kind_lab/install_linux.sh --user opspilot`（remote tmux，日志在主机 `/root/rental-logs/install.log`） | 退出码 0，末尾 health `ok: true`，`deployments.not_ready` 为空；用时 3 分 46 秒。没有版本或哈希不符 |
| 08:32–08:33:32 | 1.2 PG 与工具 | `apt-get install postgresql-common jq`；PGDG `apt.postgresql.org.sh -y`；`apt-get install postgresql-17`；`systemctl disable --now postgresql`（PG 只由脚本按需启动）；按 astral 安装脚本给 opspilot 装 `uv`；`/usr/lib/postgresql/17/bin` 与 `~/.local/bin` 写进 opspilot 的 `~/.profile` 与 `~/.bashrc` 的 PATH | 退出码 0；`which initdb pg_ctl uv jq docker` 全部有输出 |
| 约 08:35 | 1.2 `make setup` | opspilot：`make setup` | 退出码 0；`.venv` Python 3.12.3 |
| 约 08:33（起始秒数未记录）–08:34:22 | 1.2 评测器 | `git clone https://github.com/itbench-hub/ITBench-Evaluations.git ~/rental-data/ITBench-Evaluations`，`git checkout 14f026fc`，`uv sync --frozen` | HEAD `14f026fc9cc348c4ecec5ab32714de954c95c1b1`；venv Python 3.12.3；工作区干净 |
| 08:34:22–08:38:19 | 1.2 数据集 | `hf download ibm-research/ITBench-Lite --repo-type dataset --revision d0916b08… --include 'snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7/*' --local-dir ~/rental-data/itbench-lite`（huggingface_hub 2.4.0，未登录） | 见下「数据集」 |
| 08:59 | 1.3 lab health | `python3 scripts/kind_lab.py health` | 退出码 0，`ok: true`（prometheus `span_metrics`、`kube_state_metrics` 都为 true；jaeger 200，15 个服务；frontend 200） |
| 08:59 | 1.3 rules-check | `python3 scripts/kind_lab.py rules-check` | 退出码 0，`{"rules": 0, "config": 0, "alertmanager": 0, "self_route": 0}` |
| 08:59 | 1.3 span 速率 | 手册的 GET 查询 `sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))` | `success`，值 1.9167 > 0；没有重启 collector，`lab_actions` 为空 |
| 08:59 | 1.3 flagd | `kubectl -n otel-demo get cm flagd-config -o json`，只读各开关的 `defaultVariant` | 11 个开关全为 `off`（含 `paymentFailure`、`cartFailure`、`productCatalogFailure`） |
| 08:59 | 1.3 内存 | `free -m` | 见上表；lab 起来后另见检查点 |
| 09:00 | 1.2 镜像 | `docker pull prom/prometheus:v3.5.0 jaegertracing/all-in-one:1.53.0`；`docker image inspect` | 退出码 0；`prom/prometheus@sha256:63805ebb…4996`，`jaegertracing/all-in-one@sha256:60e65bff…409c` |
| 09:00 | 1.2 needs-kind-run | 本机：`gh issue list --label needs-kind-run --state open` | 见下 |
| 09:01–09:02 | key 文件 | 见下「私有 env 文件」 | 首次缺 3 个，补齐后复核通过 |

### 版本

Docker 29.2.1（`docker-ce 5:29.2.1-1~ubuntu.24.04~noble`），kind v0.33.0，kubectl v1.37.1，helm v4.3.0，PostgreSQL 17.12（`17.12-1.pgdg24.04+1`），uv 0.13.8，jq（Ubuntu 包），Helm chart：opentelemetry-demo 0.37.8、kube-state-metrics 8.6.0、alertmanager 1.24.0。其中 Docker、kind、kubectl、Helm 由 `install_linux.sh` 钉住，未改动；Helm chart 版本由 lab 脚本选定；PostgreSQL、uv、jq 为阶段 1.2 单独安装的当时版本，不是钉住的版本。

### 数据集

- HF 数据集 `ibm-research/ITBench-Lite`，revision `d0916b08ba421ce5e672e9ad68aa947d938dfef0`（下载时 API 返回的 `sha`，下载按此 revision 固定）。
- 路径（仓库外）：`/home/opspilot/rental-data/itbench-lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7`，3456 个文件，31,020,214,628 字节（约 31.0 GB，`du -sb`），比手册写的 ≥ 6.8 GB 大。
- 根目录下 35 个 `Scenario-<n>` 目录，编号为 1, 2, 4, 5, 6, 7, 8, 9, 11–25, 29, 31, 33, 34, 35, 38, 80, 81, 83, 91, 102, 105。只列了目录名、文件数和大小，没有打开任何文件。`scenario_key` 识别 `Scenario-<n>` 形式（`coverage.py`）；正式按 `scenario_key` 过一遍是阶段 2 第 2.1 步的复制循环。
- 磁盘：下载与 lab 之后 `/` 已用 50 GB，剩 94 GB。

### needs-kind-run

09:01 本机 `gh issue list --label needs-kind-run --state open` 只返回一项：#580（工具拓展：租机集中取证与真实 Run），本来就排在阶段 4，不需要另外并入。

### 私有 env 文件

路径 `/home/opspilot/rental-private/lite.env`（目录 700、文件 600，属主 opspilot）。由用户从本机复制（执行者对本机 `.env` 没有读取权限）。只核对变量名与权限，不读值。

09:01 第一次核对：文件只有 `LANGSMITH_ENDPOINT`、`LANGSMITH_WORKSPACE_ID` 两行，缺 `DEEPSEEK_API_KEY`、`LANGSMITH_API_KEY`、`ZAI_API_KEY`（环境前提，不是代码问题），报 lead。

09:02 复核（用户 09:01:30 补齐后）：目录 700、文件 600，属主 opspilot，5 行，变量名为 `DEEPSEEK_API_KEY`、`LANGSMITH_API_KEY`、`LANGSMITH_ENDPOINT`、`LANGSMITH_WORKSPACE_ID`、`ZAI_API_KEY`，没有空值，不带 `export` 前缀。值没有读取或打印。

## 检查点 1

| 判定项 | 结果 | 证据 |
|---|---|---|
| 1.1 `install_linux.sh` 退出码 0、health `ok: true` | 通过 | 安装日志末尾 `EXIT=0`、`"ok": true` |
| 1.2 `make setup` 退出码 0 | 通过 | `EXIT=0` |
| 1.2 `which jq uv initdb pg_ctl docker` 都有输出 | 通过 | 见步骤表 |
| 1.2 两个镜像 `docker image inspect` 退出码 0 | 通过 | digest 见步骤表 |
| 1.2 评测器 HEAD 以 `14f026fc` 开头 | 通过 | `14f026fc9cc3…` |
| 1.2 数据集 35 个场景目录 | 通过（按目录名；`scenario_key` 正式校验在 2.1） | 35 个 `Scenario-<n>` |
| 1.3 health ok、rules-check 全 0、span 速率 > 0、flagd 全 off | 通过 | 1.9167；四项 exit 0；11 个开关 off |
| 1.4 主机信息 | 部分：单价与开机时刻未确认 | 主机表 |
| 三个 key 放进 600 文件（#617） | 通过（09:02 复核） | 私有 env 文件一节 |

**结论：检查点 1 通过**（09:02）。唯一未满足的是 1.4 的单价与开机时刻，主机上看不到，记为未确认；这两项不影响冒烟。

## 阶段 6.2 冻结（2026-11-14，PT 15:10 前后）

- **提交 F**：`5f731318679afa9873b8ba405283badf2982d752`（main，#634 合并后，含 #632 恢复修复）。主机主 checkout 快进到 F，`uv.lock` 自 P 起未变，无已跟踪修改。快进前，主机上与 main 已入库内容同名的未跟踪证据移到 `/home/opspilot/rental-evidence-backup/20261114T224123/`（29 个文件，28 个与入库版本逐字相同，1 个是入库后又改过的 `a1-cluster-identity/run.md`），没有删除。
- **冻结版本**（主 checkout 上 `otel_demo_ext_versions()`）：`prompt_revision = prompt-replay-candidate-k8s-0282ee7d0e7a`，`tool_schema_revision = otel-demo-ext-d467c4b436b0`（`context_policy_revision = ctx-ctx-policy-v1-a30e65ea52f4`）。与小批 6 个 summary 及 F 上 3 次重新彩排的 `run.versions` 相同。
- 从这一刻到全量结束，不再合并会进入主 checkout 的改动，主 checkout 不 pull。全量尚未开跑（用户先听简报）。

### 全量规划数字（依据：冒烟 2 题与小批 6 题，另有 F 上 3 次彩排作参考）

| 项 | 数值 | 依据 |
|---|---|---|
| 规模 | 35 题 × 3 次 = 105 个 trial | 手册阶段 6 |
| 每 trial 墙钟 | 中位约 6.7 分钟，范围 5.0–7.6 分钟；带一次重试的 S4 为 12.7 分钟 | `started_at` 到 `complete.json`（含灌数据、调查、judge） |
| 全量墙钟 | 约 11.5–13.5 小时（105 × 6.6–7.7 分钟，含少量重试） | 同上；judge 已在每个 trial 内 |
| 每 trial DeepSeek token（8 题） | prompt 均值 1,404,714、最大 2,351,280（S4）、最小 702,592；其中缓存命中均值 1,303,728（约 92.8%），未命中均值 100,986；completion 均值 43,359、最大 58,668；模型轮次均值 21.6、最大 28 | 各 summary `run.tokens`（冒烟 2 题用的是 #628 之前的提示词） |
| 全量 token 预期 | prompt 约 1.47 亿（缓存命中约 1.37 亿、未命中约 1060 万），completion 约 455 万；按每 trial 最大值算的上界为 prompt 约 2.47 亿 | 均值 × 105 |
| 磁盘 | 现有 `/` 已用 94 GB、剩 50 GB。全量轮次会把 35 题白名单副本复制一份（约 30 GB）再加 105 个 trial 目录（每个约 240–490 MB，约 25–35 GB），合计约 55–65 GB，**超过剩余空间** | `du`：冒烟轮次副本 30 GB，每 trial 目录 238–492 MB |
| 内存 | lab 停着时开跑前 used 约 1.0–1.1 GB，单 trial 峰值约 4.5 GB，swap 0 | `memory_before` 与 `free -m` |

磁盘需要在全量开跑前腾出空间（例如删掉冒烟轮次 `tmp/itbench-lite/lite-smoke-1114/S*/snapshot` 这批可重建的白名单副本，约 30 GB；分档与词表已生成在 `tmp/itbench-lite/inputs/`），由 lead 决定。2026-11-14 lead 决定只删这批副本，删后 `df -h /` 为 65G 已用 / 79G 可用；S1 的失败 trial 与 `inputs/` 保留。

## 未执行

- 阶段 1（#617）本身不跑调查、模型调用或 judge；之后各阶段的真实运行见冒烟（#627）、kind 部分（#633）、小批（#631）与本记录的再彩排三轮。
- 单价、开机时刻、Vultr 控制台信息：未确认。
- 销毁：用户 2026-11-15 在 Vultr 控制台销毁主机；19:27 PST 本机核实 ssh 拿不到服务端协议标识（本机代理 TUN 会替任意地址完成 TCP 握手，端口可连不作数），随后删除本机 `~/.ssh/config` 的 `opspilot-tool-1` 条目与 known_hosts 里该 IP 的记录。按推算开机时刻约 04:57（11-14）计，在线约 38.5 小时；单价未确认，主机费用以 Vultr 账单为准。
- 中途事件：08:35–约 08:55，本机经代理出站的 22 端口不通（连 GitHub 22 端口也一样），ssh 连不上主机；主机上的 tmux 任务不受影响，照常跑完。这是本机网络问题，不是主机问题。
