# 租机操作手册：工具拓展真实评测（ITBench-Lite 冒烟与全量、集群取证、A7 主路径 Run）

- 状态：草案（2026-11-14 按 #616 改为分阶段执行，对照 `main` `a2eb0135`；前一版 2026-11-13 对照 `130225f9`）。A7 三项（A7-8/9/12）与 L-V7 已于 2026-11-13 裁决（决定表 r7、r6）；A7 本机部分 #614、#605 生成器 #610 已合并。阶段顺序由用户 2026-11-14 在 #616 裁决。
- **用户裁决（2026-11-14，见 #616 评论）**：本手册中「④ 由用户决定」「用户确认后冻结」、A7 版本停点与 A7-14 停点，一律改为 Agent 自行决定并继续；全程只在全量（阶段 6）开跑前停一次向用户简报。检查点不通过仍须修复后才往下走。仓库模块（`scripts.*`）一律用 `.venv/bin/python` 运行，主机系统 `python3` 没有项目依赖（`scripts/kind_lab.py` 等独立脚本除外）。
- 依据：#616 及子项 #617、#618、#619，#580 验收项、#559 与 A7 预审决定表（仓库外，A7-1 至 A7-17）、#571、#497（2026-11-06 Q1 彩排规则）、[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3/r4/r7](2026-11-12-tool-expansion-contract.md)、[A1 开工说明](2026-11-12-tool-expansion-a1.md)、[ITBench-Lite 适配](2026-11-12-itbench-lite-adapter.md)、[Linux kind lab](2026-11-04-linux-kind-lab.md)、[开发指南「kind 实验环境」](../development.md)、README「The OpenTelemetry Demo lab on the same host」，以及照搬步骤的 [F3 真实会话](../evidence/f3-acceptance-live/run.md) 和 [F7 授权探测](../evidence/f7-authz-probe/run.md)。
- 用法：开机前先过完第 1 节；开机后按第 2 节的阶段顺序执行。每个阶段末尾是检查点：通过标准全部满足才进下一阶段，任一不满足就停在原地，不往下走。标「未核实」的命令或参数在本机没法确认，到主机上先核对再用。

## 1. 前提与开机前确认

**主机**：Ubuntu 24.04 x86_64，8 核 / 32 GB（`install_linux.sh` 只支持这个系统）。磁盘建议 ≥ 100 GB（数据集大小未核实）。系统镜像只允许 root 登录时，按 README 先建 `opspilot` 用户。主机不用 colima，所以 colima 密钥目录必须放在 `$HOME` 下这条坑不适用。swap 和内存改用 `free -m` 与 `/proc/meminfo` 查。修代码期间租机一直开着（#616）。

**必须已合并（开机前用 `gh pr view` 核对）**：

| 项 | 现状（2026-11-13） | 为什么必须 |
|---|---|---|
| A1–A6（#589、#588、#598、#594、#593、#600）、#581–#583（#586、#587、#603）、#604（`130225f9`） | 已合并 | 工具、回放组件、逐题驱动与 `run_freeze` |
| #605 分档文件与词表生成器（#610） | 已合并（`56e22b93`） | 驱动的 `--vocabulary`/`--vocabulary-dir` 与 `coverage.py --tiers` 都要它的产物 |
| B2 #597 | 已合并（`dc9156b7`） | 改只读代理与 `f7_authz_probe.py` 的期望表；没合并时 `up` 渲染出的代理不放行 rules/targets/metadata |
| A7（#559，变体 `replay-candidate-k8s`，PR #614） | 已合并（`af179708`），只剩租机真实 Run | A7-14：扩展档位的最终 `prompt_revision` 要用于 A7 后一次 Run 和 Lite 全量（L12），所以 A7 后一次 Run 排在冒烟改动之后 |

**三个提交的叫法**（下文都用这三个名字，实际哈希记进证据目录的 `run.md`）：

- **提交 S**：开机时检出的 `main`，冒烟（阶段 2）用。
- **提交 P**：冒烟改动全部处理完后的 `main`，kind 部分（阶段 4）与小批（阶段 5）用。冒烟没有引出改动时 P = S。
- **提交 F**：冻结提交，全量（阶段 6）用。小批没有引出改动时 F = P。

**本机工作（均已完成，租机上不临时写代码）**：

1. **A7 #559 代码与驱动**（A7-13，PR #614，`scripts/a7_main_path.py`）：驱动调 `kind_lab.py fault inject/restore`，Run 结束后用 `scripts/run_freeze.py` 的同一套代码冻结，判定用纯函数 `cited_categories(summary)` 等（有单元测试）；用法见阶段 4c。`run_freeze` 写出的 summary 已带报告、引用证据的工具名与视图、`tool_requests`、版本号、token 用量与告警投递，`cited_categories` 直接读它。
2. **Linux 上的 Run 拓扑**（核实过的事实）：lab trace 闸门只接受回环地址或 `*.svc` 的数据源地址（`opspilot/tracing.py:152-206`）。Compose 里的 worker 用 `http://opspilot-m1-control-plane:30090`，开 `OPSPILOT_TRACE=lab` 会报 `PROMETHEUS_NOT_LAB`，只能用 `outbox` 模式；outbox 只发 id、版本、token 数和固定码。另外 Linux 上 Alertmanager 的 webhook 渲染成容器别名 `opspilot-web:8080`。**推荐**：A7 与 #580 的 Run 用宿主进程拓扑（F3 的接法：web 与 worker 在宿主上跑，数据源走 127.0.0.1），告警用开发指南写的退路 `OPSPILOT_LAB_HOST_ADDRESS=<kind 网关>`，web 设 `OPSPILOT_BIND=<kind 网关>:8080`。这条路在 Linux 上没有端到端测过：F9 S4 当时被 ufw 挡住。它需要宿主装 PostgreSQL 17，并把 `initdb`、`pg_ctl` 放进 PATH（`postgres_lab.py` 与 #604 的 `LocalPgLauncher` 都用 `shutil.which` 找；Ubuntu 的 PGDG 包装在 `/usr/lib/postgresql/17/bin`，未核实）。ITBench 驱动自己在宿主上起 web/worker，不受这项影响。
3. **#604 ITBench-Lite 逐题驱动**（`130225f9`）：在本机只用替身和一次性 PG 测过；docker 里的 promtool、Prometheus `v3.5.0`、Jaeger `1.53.0`，worker 真调模型，LangSmith 回读，judge 真调，真实数据的灌入耗时，都在冒烟（阶段 2）第一次验证（[适配记录「实施记录（#604）」](2026-11-12-itbench-lite-adapter.md)）。
4. **#605 分档文件与词表生成器**（#610，`56e22b93`；L-V7 = a，去掉 Namespace 对象名与 `kubernetes`、`default`）。驱动要每题一个词表 `S<id>.txt`，`coverage.py` 要分档 JSON；两者都由生成器从快照重算，不读普查文件，命令见阶段 2 第 2.1 步。冒烟按分档明细挑题，所以这一步在冒烟之前做。
5. **直接取样脚本**（#607，`97e9919c`，`scripts/tool_sample.py`）：不经模型直接调 `otel-demo-ext` 工具（按列查询、Pod 日志、Secret 拒绝、trace、即时查询、B2 规则），命令见阶段 4c 第 4c.6 步。
6. **主机准备清单**：PostgreSQL 17、`uv`、宿主 `jq`（宿主 worker 跑 `otel-demo-ext` 时，没有 jq 就启动失败，`JQ_NOT_FOUND`）、DeepSeek、LangSmith、Z.ai 三个 key（放在仓库外 600 文件里；ITBench 驱动用 `--env-file` 读，只交给 worker 与 trace 回读，web 不拿），评测器在 `14f026fc` 重新克隆并按其 `uv.lock` 建 venv（本机那份在 `~/.agent/eval-probes/`），数据集 `ibm-research/ITBench-Lite` SRE `v0.2` 的下载命令（未核实）。

**答案隔离（硬约束，全程适用）**：执行者不打开、不打印、不读取 `ground_truth.yaml/.yml`、`assertion.json`、`status.json` 和评测器的 `prompts/few_shot.py`；答案根目录只作为路径传给判分进程。issue、PR、反馈简报与入库文件都不写答案内容。挑题只看覆盖分档，不看故障类型（故障类型来自答案）。

**证据先不提交**：各阶段证据先留在运行 checkout 里，到收尾再提交。原因是两个驱动都核对提交：ITBench 驱动把 `itbench_lite.git.commit` 记进每个 summary（未跟踪文件不算脏，`driver.py` `git_state` 用 `--untracked-files=no`），A7 驱动要求部署 checkout 就是驱动所在的提交；在运行 checkout 里提交会改变 HEAD。中途需要把冒烟简报之类的材料推出去，就复制到另一个 worktree 的分支上提交。

**冒烟与小批不计分**：分数只以全量（阶段 6）为准；冒烟和小批用了哪几题照实写进各自的 `run.md`（#616）。

## 2. 阶段与检查点

时间都是估计值，依据写在括号里。所有命令都以 `opspilot` 用户在运行 checkout（下称主 checkout）根目录执行。每个阶段的起止时刻都记进对应证据目录的 `run.md`。

### 阶段 1：主机核对与环境准备（#617，约 0.5 小时，不跑任何调查）

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 1.1 | 照 README「The OpenTelemetry Demo lab on the same host」：以 root 执行 `adduser --disabled-password --gecos "" opspilot`；以 `opspilot` 身份克隆 `su - opspilot -c 'git clone https://github.com/kevinWangSheng/production-ops-agent.git'`，再以 `opspilot` 身份检出提交 S（克隆必须归 `opspilot` 所有：`install_linux.sh` 用 `runuser -u opspilot -- test -w <仓库根>` 核对，root 拥有的克隆会直接退出）；然后以 root 跑 `/home/opspilot/production-ops-agent/scripts/kind_lab/install_linux.sh --user opspilot` | 退出码 0，末尾 health 报告 `ok: true` | 看安装日志；版本或哈希不符就停，不改钉住的版本 | 约 5 分钟（F9 主机全程 3 分 27 秒） |
| 1.2 | 装第 1 节第 6 项的工具，执行 `make setup`；克隆评测器并建 venv；下载数据集；`docker pull prom/prometheus:v3.5.0 jaegertracing/all-in-one:1.53.0`。再跑 `gh issue list --label needs-kind-run --state open`，把待补跑的项并入阶段 4 | `make setup` 退出码 0；`which jq uv initdb pg_ctl docker` 全部有输出；`docker image inspect` 两个镜像退出码 0；`git -C <评测器> rev-parse HEAD` 以 `14f026fc` 开头；数据集根目录下能被 `scenario_key` 识别的场景目录有 35 个（只列目录名，不打开文件） | 区分环境前提与代码问题，不改代码 | 约 15 分钟 |
| 1.3 | lab 前提：`python3 scripts/kind_lab.py health`、`python3 scripts/kind_lab.py rules-check`；跑 `free -m`；检查 frontend 的 span 速率，先 `set -a; . tmp/m1-kind-lab/prometheus-lab.env`，再 `curl -sG -u "$OPSPILOT_LAB_PROMETHEUS_USERNAME:$OPSPILOT_LAB_PROMETHEUS_PASSWORD" http://127.0.0.1:19090/api/v1/query --data-urlencode 'query=sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))'`（代理拒绝 POST 查询，只能用 GET） | health 为 ok；rules-check 全 0；速率 > 0；flagd 开关全 off | 速率为 0：用 `kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo rollout restart deployment otel-collector`，动作记进 `lab_actions` | 5 分钟 |
| 1.4 | 主机信息记录：供应商、规格、系统、单价、开机时刻、提交 S 的哈希，写进 `docs/evidence/rental-2026-11/run.md`（目录名自定）；不写凭据 | 各项都有值；未执行的步骤照实写出 | — | 5 分钟 |

**检查点 1**：1.1–1.4 的判定全部满足。任一不满足就停，按「区分环境前提与代码问题」处理，不进入冒烟。

### 阶段 2：ITBench-Lite 冒烟，2 题 × 1 次（#618，约 1 小时）

驱动是 `scripts/itbench_lite/driver.py`（#604），子命令有 `attempt`、`round`、`judge`、`scores`。回放服务默认绑 127.0.0.1 的 29110（PG）、29190（Prometheus）、29186/29118（Jaeger 查询与 OTLP）、29143（假 server）、29180（web），都避开了 lab 的 19090、16686、18080、8080、55431、9464，要改就用 `--{pg,prometheus,jaeger-query,jaeger-otlp,fake-k8s,web}-port`。镜像钉 `prom/prometheus:v3.5.0` 与 `jaegertracing/all-in-one:1.53.0`（`services.py`）。证据写到 `docs/evidence/itbench-lite-sre-<轮次>/S<id>/trial-<n>/summary.json`，其余都在 git 忽略的 `tmp/itbench-lite/<轮次>/`。轮次标签只能用小写字母、数字、`.`、`-`（`driver.py` `_ROUND`）；冒烟、小批、全量各用自己的轮次，例如 `lite-smoke-1114`、`lite-batch-1115`、`lite-full-1116`，同一轮次里已完成的 trial 会被跳过，所以重跑同一题要换新轮次。档位 `otel-demo-ext`，summary 在 `run.versions` 里记 `prompt_revision` 与 `tool_schema_revision`（L12）。冒烟期间 lab 保持运行（阶段 4 要用），`run.md` 注明灌入耗时是在 lab 同时运行时测的。

私有 env 文件放 DeepSeek、LangSmith、judge 的 key（`--env-file`，600，放在仓库外，下称 `~/rental-private/lite.env`）；判分要 `ITBENCH_EVALUATIONS_DIR` 指向重新克隆的评测器。`--env-file` 只导入 DeepSeek 与 LangSmith 变量，judge 子进程的凭据（如 `ZAI_API_KEY`）与 `ITBENCH_EVALUATIONS_DIR` 只从父进程环境继承（`driver.py` 的 `judge_env`），所以跑 `attempt`、`round`、`judge` 之前都先在同一个 shell 里 `set -a; . ~/rental-private/lite.env; set +a`。数据集根目录本身带答案文件，驱动只按名字复制白名单文件，不打开答案。trace 默认 `--trace lab`，项目名由驱动按轮次生成（`itbench-lite-<轮次>`），开跑时驱动设为长期保留。

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 2.1 | 分档与词表（#605）。两个生成器都拒绝含答案文件的目录，所以先用驱动自己的白名单复制，把每题的干净副本建到冒烟轮次下（驱动发现已有副本会重新校验后复用），再每题生成一份词表、全量生成一次分档。词表与分档目录放在轮次目录之外（例如 `tmp/itbench-lite/inputs/`），三个阶段共用。命令见表下「2.1」 | 复制循环与两个生成器退出码 0；`tiers.json` 正好 35 题；词表目录里 35 个 `S<id>.txt` | 复制循环未在真实数据集上跑过；生成器报错属类别 ①，开 issue，不在机上改代码 | 约 20 分钟（460 MB trace 的耗时未验证） |
| 2.2 | 挑题，只按数据覆盖：从分档明细取 trace 与日志都覆盖故障窗的题，去掉 S18、S38，按编号取前两个（本手册的默认，可逆），再在数据集根目录下按名字找到对应的场景目录名。命令见表下「2.2」 | 输出正好 2 个编号；两个编号写进 `run.md` | 不足 2 个：停，报 lead（分档结果与预期不符） | 2 分钟 |
| 2.3 | 第 1 题：`.venv/bin/python -m scripts.itbench_lite.driver attempt --round <冒烟轮次> --dataset <数据集根> --scenario <场景目录名> --trial 1 --vocabulary <词表目录>/S<id>.txt --env-file ~/rental-private/lite.env` | 见表下「每题判定」 | 不通过就停，不跑第 2 题；按阶段 3 分类 | 约 10 分钟 |
| 2.4 | 第 2 题：第 1 题判定全部通过后，同一条命令换场景 | 同上 | 同上 | 约 10 分钟 |
| 2.5 | 反馈简报：每题一节，写下表的数值；按阶段 3 的类别 ①–④ 归类发现；①②③ 各开缺陷 issue，④ 由 lead 决定改不改（理由写进 run.md） | 简报写进冒烟证据目录的 `run.md`，同时发给 lead | — | 20 分钟 |

**2.1**（复制、词表、分档）：

```sh
for d in <数据集根>/*/; do
  n=$(basename "$d")
  k=$(.venv/bin/python -c 'import sys; from scripts.itbench_lite.coverage import scenario_key; print(scenario_key(sys.argv[1]))' "$n") || continue
  .venv/bin/python -c 'import sys; from pathlib import Path; from scripts.itbench_lite.driver import copy_whitelisted; copy_whitelisted(Path(sys.argv[1]), Path(sys.argv[2]))' "$d" "tmp/itbench-lite/<冒烟轮次>/S$k/snapshot"
done
# 词表，每题一份
.venv/bin/python -m scripts.itbench_lite.vocabulary tmp/itbench-lite/<冒烟轮次>/S<id>/snapshot --out <词表目录>/S<id>.txt
# 分档，全量一次
.venv/bin/python -m scripts.itbench_lite.tiers tmp/itbench-lite/<冒烟轮次> --out <分档目录>/tiers.json --detail <分档目录>/tiers_detail.json
```

**2.2**（挑题）：

```sh
jq -r 'to_entries[] | select(.value.coverage.traces=="covered" and .value.coverage.logs=="covered") | .key' <分档目录>/tiers_detail.json | grep -vxE 'S18|S38' | sort -V | head -2
```

**每题判定（2.3、2.4）**：

- 驱动退出码 0。`attempt` 在 trial 记为缺失时也返回 0，所以还要看：`summary.json` 的 `status` 为 `completed`（为 `missing` 时 `reason` 写明原因，例如 `CHANCES_EXHAUSTED`，算不通过），`tmp/itbench-lite/<冒烟轮次>/S<id>/trial-1/complete.json` 已写出。退出码 2 是整轮中止（stdout 打印 `{"status": "aborted", "code": …}`，例如 `CLEANUP_FAILED`、`WORKTREE_DIRTY`），算不通过。
- 作废线按 r4 原文（L-D7）：灌完立即投递，灌完到投递超过 2 分钟这次作废（驱动每个 trial 最多给 3 次机会，L-D8）。看 `itbench_lite.gap_seconds` 与 `itbench_lite.history`，作废次数照实写。
- **必须记录并评估 `itbench_lite.load_seconds` 与 `itbench_lite.data_end_to_delivery_seconds`**（约等于 60 秒 + 灌入耗时 + worker 起动）。灌数据超过约 4 分钟时，以查询窗终点为时刻的即时查询可能落在数据之后（适配记录已知风险 1 的 5 分钟线）。超过就把数值写进 `run.md`，报 lead 评估后再往下走，不自行改平移量。`--load-budget-seconds` 只是灌入耗时的上限（默认 600），不是作废线。
- 同时记：假 server 加载完整日志的耗时（`itbench_lite.load.fake_k8s.seconds`）与内存（`itbench_lite.memory_before` 与 `free -m`）、`itbench_lite.target_fallback` 是否为 true、`run.outcome` 是否 available、`trace` 块里的 LangSmith 链接。
- 新工具调用：`jq -r '.run.tool_requests[] | [.tool, .status, (.reason // "")] | @tsv' summary.json | sort | uniq -c`，写出模型实际调用了哪些新工具、各自成功或报错（字段名沿用 F3 summary 的形状，真实输出先看一眼再用）。
- 报告能转答案：`itbench_lite.conversion.reason` 为 null；judge 返回分数：`tmp/itbench-lite/<冒烟轮次>/S<id>/trial-1/scores.json` 的 `judged` 为 true、`official` 非空。分数只记录，不作门槛，也不作改动理由。

**检查点 2**：两题都满足「每题判定」（分数除外），反馈简报已交 lead。第 1 题不通过就不跑第 2 题；任一题不通过都停在阶段 2，按阶段 3 处理后重跑同一题。

### 阶段 3：冒烟反馈的改动（时长取决于改动，租机一直开着）

改动分类（#616，冒烟与小批共用）：

| 类别 | 例子 | 处理 |
|---|---|---|
| ① 环境与脚本 | 驱动起不来、端口、灌数据超时、环境变量 | 开缺陷 issue 修，重跑同一题 |
| ② 工具缺陷 | 新工具对真实 API 报错、解析不对 | 开缺陷 issue，按缺陷 PR 修并独立审查，重跑 |
| ③ 适配层 | 假 server 与真实 API 不一致、报告转答案出错 | 同 ②；改动理由只能来自日志与报错，不能来自分数或答案 |
| ④ 提示词与调查指导 | 模型漏调某类工具、方向不对 | 列入反馈简报，由 lead 决定改不改（理由写进 run.md）；改了就开 issue 实现 |

规则：

- 每个缺陷单独一个 issue（链接 #616 与本手册），在别的会话实现，不在编排会话里修，也不在租机上临时改代码。只是环境操作（装包、放行端口、重启 collector）不改代码的，在机上做，记进 `lab_actions`。
- 改动合并后，主 checkout 更新到新的 `main`（`git pull --ff-only`；`uv.lock` 变了就再跑 `make setup`），用新轮次重跑出问题的那一题（①②③），判定同「每题判定」。
- **版本停点**：A7 前一次 Run 的版本期望由驱动所在提交的代码算出（`scripts/a7_main_path.py` `expected_versions` 的说明：A7 不改扩展档位的 `tool_schema_revision`，前一次的期望才能从后来的代码复现）。所以改动全部合并后，在主 checkout 跑 `.venv/bin/python -c 'from opspilot.tools.otel_demo_ext import otel_demo_ext_versions as v; print(v())'`，把 `tool_schema_revision` 与 `context_policy_revision` 和冒烟 summary 的 `run.versions` 对照。任一不同：报 lead，说明 A7 前一次 Run 的版本核对与 `compare` 会拒绝；由 lead 决定处理方式并把理由写进 run.md，之后继续。

**检查点 3**：冒烟开出的 ①②③ issue 全部合并且重跑通过，或由 lead 决定延后；④ 每项都有 lead 的决定（改或不改，改的已合并）；版本停点没有触发或 lead 已处置；主 checkout 在提交 P、无已跟踪修改。P 的哈希与 `otel_demo_ext_versions()` 的输出记进 `run.md`。

### 阶段 4：kind 部分，#580 集群取证 → #559 A7 主路径 Run（约 4–5.5 小时）

在提交 P 上做。A7 后一次 Run 用的就是 P 的扩展档位提示词（A7-14）；驱动核对部署 checkout 的版本号与驱动所在提交（P）的代码算出的值逐项相等。

#### 4a：拒绝取证，不调模型（约 0.75 小时）

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 4a.1 | 准备已知秘密值文件，放在仓库外：`umask 077; mkdir -p ~/rental-private; { jq -r '.[]' tmp/m1-kind-lab/prometheus-auth.json; cat tmp/m1-kind-lab/alertmanager-webhook-token tmp/m1-kind-lab/release-deployer-token; } > ~/rental-private/known-secrets.txt`。另把 DeepSeek 与 LangSmith 的 key 值追加进去（自定：这两个不在 lab 里，搜到就说明泄漏） | 每行一个值，文件权限 600 | — | 2 分钟 |
| 4a.2 | 宿主侧 A1 探测：`python3 scripts/k8s_authz_probe.py --env-file tmp/m1-kind-lab/k8s-investigator.env --known-secrets ~/rental-private/known-secrets.txt --side host --manifest scripts/kind_lab/opspilot-readonly-rbac.yaml --out-dir docs/evidence/a1-cluster-identity/host` | 退出码 0，`probe.json` 里 `ok: true`、`known_secret_hits` 为空；读类 200、Secret/写/exec/proxy/watch/token 403；自定义资源 403 或 404 | 期望值写错（A1 风险 3）时，修正期望后只重跑一次并照实记录（A1 规则）；真实授权缺口就停下报 lead | 2 分钟 |
| 4a.3 | 起 Compose 栈，只为容器侧探测：`python3 scripts/kind_lab.py control-secrets`，在 `~/.config/opspilot/worker_env` 加 `DEEPSEEK_API_KEY`，`OPSPILOT_START_AGENT=1 deploy/compose/install.sh install`，再跑 `python3 scripts/kind_lab.py connect-control --project opspilot` 和 **再一次** `control-secrets --project opspilot`（`install` 重建的 worker 没挂 CA，见 A1「偏离」）。档位先用 `control-secrets` 写入的 `otel-demo`（F9 验证过），避免 worker 因为扩展档位的启动前提起不来 | 三条命令退出码 0；`docker ps` 里能看到 worker | 按 F9 记录排查；Compose 对缺失 secret 文件的表现是 A1 风险 4 | 15 分钟（镜像构建时间未核实） |
| 4a.4 | 容器侧 A1 探测：`W=$(docker ps -q --filter label=com.docker.compose.project=opspilot --filter label=com.docker.compose.service=worker)`，然后 `docker exec -i "$W" python -c "$(cat scripts/k8s_authz_probe.py)" --env-file /run/secrets/worker_env --side worker --known-secrets - < ~/rental-private/known-secrets.txt > docs/evidence/a1-cluster-identity/worker/probe.json`（写法来自探测脚本 docstring，过滤标签未核实） | 同 4a.2；能连上，说明证书名对得上（A1 风险 1） | 证书名不符（A1 风险 1），就记录并停这一项，报 lead：要改 `kind_lab.py` 的地址生成 | 3 分钟 |
| 4a.5 | B2 与 F7 代理探测：`python3 scripts/f7_authz_probe.py > docs/evidence/b2-authz-probe/probe.json` | 退出码 0、`ok: true`；四个账号与 holmes 的 rules、targets、metadata GET 都是 200，新增的 `/api/v1/alerts`、`/api/v1/targets/metadata`、`/api/v1/status/config` GET 是 403；其余行与 [F7 探测](../evidence/f7-authz-probe/run.md) 一致 | 不符就逐行列出，不改代理 | 3 分钟 |
| 4a.6 | 记下 token 过期时间：只从 `tmp/m1-kind-lab/k8s-token.json` 抄过期时间，连同清单 sha256 写进 `a1-cluster-identity/run.md`，再跑收尾一节的 token 搜索 | token 搜索 0 命中 | 有命中：停，报 lead | 2 分钟 |
| 4a.7 | 拆掉 Compose 栈：`deploy/compose/install.sh uninstall`（保留数据卷与密钥）。这样 webhook 可以改走宿主拓扑 | 容器已删 | — | 2 分钟 |

#### 4b：切到宿主拓扑（约 0.5 小时，按第 1 节第 2 项的推荐）

1. 查 kind 网关：`docker network inspect kind -f '{{(index .IPAM.Config 0).Gateway}}'`（F9 主机上是 172.18.0.1，取下标 0 未核实）。然后执行 `OPSPILOT_LAB_HOST_ADDRESS=<网关> python3 scripts/kind_lab.py up`，重新渲染 webhook。`ufw status` 为 active 时，放行 kind 网段到 8080（这台租机用完即弃）。
2. 宿主 PG 按开发指南「本地运行演示」执行：`python -m scripts.m0.postgres_lab start`，按开发指南导出 owner 连接 `export OPSPILOT_DSN=<owner 连接>`（`make migrate` 缺它会报 `OPSPILOT_DSN is required`），再 `make migrate`、`python -m scripts.m0.postgres_lab runtime-roles`。web 与 worker 的环境照 F3 run.md「环境」一节，有三处不同：`OPSPILOT_TOOL_PROFILE=otel-demo-ext`；worker 读 `tmp/m1-kind-lab/k8s-investigator.env`（宿主形态）；web 设 `OPSPILOT_BIND=<网关>:8080`。trace 用 `OPSPILOT_TRACE=lab`，项目按 A7-17 设为 `opspilot-lab-a7-main-path-<日期>`，开跑前由驱动设成长期保留。
3. 冒烟告警：用 lab 的 Alertmanager 发一条测试告警（F9 S4 用的是 `amtool alert add`，在 lab 里执行的确切命令未核实），确认 web 收到 `POST /intake/alertmanager`，`up{job="opspilot"}` 的状态先不管（抓取目标指向网关，可能是 down）。
4. 判定：web 日志里有这次 `POST /intake/alertmanager`。失败时：网关不通就停，报 lead；退路是第 1 节第 2 项的备选（Compose 加 `outbox`），A7 与 #580 的 trace 证据会降级，由 lead 决定是否采用。

#### 4c：A7 主路径 Run 与 #580 真实证据（约 2–3.5 小时）

A7-8 判定口径、A7-9 场景、A7-12 失败策略已由用户 2026-11-13 裁决（决定表 r7，均为 a）：四类证据按被引证据的来源工具归类，任一结论类别都算；场景是 flagd `paymentFailure` 100%；后一次 Run 最多补跑 2 次，照实记录 x/n。

**A7 的 web/worker 启动方式（宿主拓扑，驱动强制）**：web 与 worker 一律在*同一个部署 checkout 里*以宿主进程启动，用该 checkout 目录和它自己的 `.venv`：`cd <checkout> && .venv/bin/python -m opspilot.web serve`，`cd <checkout> && .venv/bin/python -m opspilot.worker_main`。环境照 4b（`OPSPILOT_TOOL_PROFILE=otel-demo-ext`、`OPSPILOT_TRACE=lab`、`LANGSMITH_PROJECT=opspilot-lab-a7-main-path-<日期>` 等）。部署 checkout 里不改已跟踪文件、不放未跟踪文件；`.venv` 要在 checkout 里就地建成目录（`uv sync --frozen`），不要用软链：`.gitignore` 的 `.venv/` 不覆盖软链，驱动会把它当未跟踪文件拒绝。驱动在注入前核对这个 checkout：提交号且无修改、无未跟踪文件（`checkout_dirty` 用 `--untracked-files=all`）；checkout 自己的代码算出的版本号与该侧期望值逐项相等；运行中的 web/worker（读 `/proc`）的工作目录是该 checkout、`OPSPILOT_TOOL_PROFILE=otel-demo-ext`。任一不符就拒绝，不注入。**主 checkout 此时已有阶段 2 与 4a 的未跟踪证据，不能当部署 checkout**，后一次 Run 也另建一个提交 P 的干净 worktree。驱动从主 checkout（提交 P）运行，命令统一是 `RUN_FREEZE_DSN=<只读 DSN> .venv/bin/python -m scripts.a7_main_path run --side <before|after> [--attempt N] --deployment-checkout <checkout> --project opspilot-lab-a7-main-path-<日期>`（先加 `--dry-run` 看一遍步骤，不碰任何东西）。驱动自己完成：设 LangSmith 项目长期保留、核对部署、确认没有未关闭的同告警事故与 flagd 全 off、`fault inject`、等告警开出事故与 Run、等 Run 结束、冻结 `summary.json`（带判定）、`fault restore` 并复读 flagd、冷却 900 秒。退出码：0 = Run 到终态；4 = Run 停在 waiting_human/paused（算一次失败尝试，不要去恢复它）；5 = 超时（不完整，不能参与对照）；3 = 恢复后 `paymentFailure` 可能仍开着，立即处理；其他非零 = 前提不满足，未注入。证据目录用驱动默认的 `docs/evidence/a7-main-path/<side>-<attempt>/summary.json`。

| 步 | 内容 | 判定（确定性） | 耗时（依据：F3 每次约 40 分钟，故障维持 1800 秒） |
|---|---|---|---|
| 4c.1 | 前一次 Run：`git worktree add ../poa-a7-before dc9156b7`，在里面 `uv sync --frozen` 建 `.venv`，按上文在 `../poa-a7-before` 里起 web 与 worker（worker 在注入前起好，A7-10；变体 `replay-candidate`）。在主 checkout 跑 `... a7_main_path run --side before --deployment-checkout ../poa-a7-before --project <项目>`（实验身份 `a7-main-path-before-1`）。驱动等 Run 期间（故障开着）工程侧顺带探一次：`kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo logs deploy/payment --since=10m`，checkout 同样跑一次，看有没有应用错误日志（A7 §4 第 1 项），只把有或没有、条数记进 run.md | 驱动退出码 0；`docs/evidence/a7-main-path/before-1/summary.json` 的 `a7.verdicts.human_free` 为真（事故由 `alertmanager` 身份开出、没有人工控制）、`a7.version_problems` 为空。驱动因版本不符拒绝（未注入）：停，报用户（阶段 3 版本停点漏掉的情况） | 40 分钟 |
| 4c.2 | 恢复、冷却与关闭：驱动已在冻结后恢复故障并冷却；之后在工作台关闭这次的事故（不关的话，下一次告警会附到旧事故上、不开新 Run，驱动开跑前也会拒绝）；停掉 `../poa-a7-before` 的 web 与 worker（各自终端 Ctrl-C） | `ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空；事故已关闭 | 15 分钟 |
| 4c.3 | 后一次 Run：`git worktree add ../poa-a7-after <提交 P>`，在里面 `uv sync --frozen` 建 `.venv`，按上文在 `../poa-a7-after` 里起 web 与 worker，在主 checkout 跑 `... a7_main_path run --side after --attempt 1 --deployment-checkout ../poa-a7-after --project <同一项目>`（实验身份 `a7-main-path-after-1`）。驱动冻结后即恢复故障（4c.4 因此在无故障状态下做） | 驱动退出码 0；`after-1/summary.json` 的 `a7.verdicts.all_four_cited` 与 `human_free` 为真（`cited_categories` 含指标、trace、集群对象、日志四类）；`a7.version_problems` 为空（`prompt_revision` 精确等于提交 P 的扩展档位值） | 40 分钟 |
| 4c.4 | #580 人工提问 Run：4c.3 的驱动已恢复故障；4c.4 在无故障状态下提交（需故障数据时可另用 `kind_lab.py fault inject/restore` 手动开一个窗，本轮不需要）。在 4c.3 驱动退出后，用 `curl -u <账号> -H 'Origin: http://<网关>:8080' -d target_id=m0-otel-20260909 -d idempotency_key=r580-1 --data-urlencode 'question=…' http://<网关>:8080/intake/ui` 提交（开发指南「提交事故」）。问题要明确请调查读 checkout 的 Pod 状态与事件、Pod 日志、按标签搜 trace 再按 ID 取整条、即时查询与序列/标签发现、告警规则与抓取目标，并要求读 `otel-demo` 里的 Secret | `tool_requests` 里有：集群对象查询 ok、`kind` 含 secret 的请求被拒为 `RESOURCE_NOT_ALLOWED`（A2-6）、`fetch_pod_logs` ok、`traces_search_by_tags` 或 `traces_query_trace_by_id` ok、`execute_prometheus_instant_query`/`get_series`/`get_all_labels` 之一 ok、B2 规则/目标/元数据之一 ok；报告引用了 Pod 状态或事件，以及某 Pod 的日志行 | 15 分钟 |
| 4c.5 | 补跑（只在 4c.3 没达到四类时）：关闭上一次的事故后，同一份代码再跑 `--attempt 2`、`--attempt 3`（驱动拒绝第 4 次，A7-12 a）。全部跑完后做对照：`... a7_main_path compare --before docs/evidence/a7-main-path/before-1/summary.json --after docs/evidence/a7-main-path/after-1/summary.json [--after .../after-2/summary.json ...] --out docs/evidence/a7-main-path/compare.json`；项目、场景、目标不同，Run ID 重复，版本不精确，或有不完整尝试，都拒绝（退出码 2） | `compare.json` 的 `after_reached`（x/n）照实写；`done` 为真即满足完成标志 | 每次 55 分钟，最多 2 次 |
| 4c.6 | 直接取样（#607，不调模型；lab 起着就能跑，不需要故障窗），命令见表下「直接取样」 | 退出码 0，且 `result.json` 的 `all_requested_passed` 为 `true`；之后再做一遍 token 原文搜索，见收尾 | 10 分钟 |

**直接取样（4c.6）**：

```sh
set -a; . tmp/m1-kind-lab/k8s-investigator.env; . <investigator 的 Prometheus env>; . <LangSmith/trace env>; set +a
.venv/bin/python -m scripts.tool_sample \
  --evidence-dir docs/evidence/tool-expansion-live/direct-sample \
  --log-pod <最近 1 小时日志超过 100 行的 Pod> \
  --trace-service frontend \
  --trace-tags '<先确认 lab 里存在的标签>' \
  --prom-query 'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[5m]))'
```

- 运行环境必须带全套 lab 闸门变量：`OPSPILOT_TRACE=lab`、`LANGSMITH_PROJECT=opspilot-lab-*`、`LANGSMITH_API_KEY`。
- 后两个 env 文件名是占位，未核实。investigator 的 Prometheus env 推测是 `tmp/m1-kind-lab/prometheus-investigator.env`，同样未核实。LangSmith/trace env 是仓库外的私有文件。
- `--trace-tags` 用 logfmt 写法，例如 `error=true`。先在 lab 的 Jaeger 里确认这个标签真的存在：任取一条 frontend trace，看它的 span 标签。
- `--log-pod` 选最近 1 小时日志超过 100 行的 Pod，用工程侧 `kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo logs <pod> --since=1h | wc -l` 确认。
- B2 #597 已合并，所以不加 `--allow-skip rules`，规则一项也必须通过。

**冻结**：A7 的 Run 由驱动在 Run 结束后自动冻结（用同一套 `run_freeze` 代码），不必手动执行。其余 Run（如 `r580-1`）每个 Run 结束（关闭事故之前）就冻结一次，用 `RUN_FREEZE_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab" .venv/bin/python -m scripts.run_freeze --run-id <Run id> --experiment r580-1 --evidence-dir docs/evidence/tool-expansion-live/r580-1 --project opspilot-lab-a7-main-path-<日期>`。DSN 只要能读产品表，也可以照 F3 另建只读角色；DSN 从环境读，不写进文件。判定：退出码 0，`summary.json` 带 trace 块，原始 ledger 落在 `tmp/lab-ledgers/`。以 `failed` 或 `budget_exhausted` 结束的 Run，`outcome` 会是 `{"available": false, "reason": "UNKNOWN_DURABLE_STATE"}`（产品投影的既有规则），其余字段照常完整，照实记录，不当作冻结失败。在同一事故上，已不是当前 Run 的那次记作 `RUN_NOT_CURRENT`，所以要在关闭事故、开下一次之前冻结。

前后对照只记确定性数字（A7-11）：引用了哪些类别、各类调用次数、模型请求数、token、校验是否通过、工具失败次数。每边只有 1 次，不下因果结论。

**检查点 4**：4a.2、4a.4、4a.5 的探测判定满足（或按 A1 规则修正期望后重跑一次通过）；4b 告警到达；4c.1、4c.3（或补跑后 `compare.json` 已写出）、4c.4、4c.6 的判定满足；token 搜索 0 命中。A7 补跑 3 次都没达到四类时，照实记 x/n，#559 保持开着，写进全量前给用户的简报，不现场改提示词（A7-12 c 不取），继续阶段 5。4c 结束后停掉 `../poa-a7-after` 的 web 与 worker，lab 继续运行到阶段 5 结束。

### 阶段 5：ITBench-Lite 小批与彩排（#619、#497，约 1.5–2 小时）

小批是全量前的最后检查点，同时是 #497 规则（2026-11-06 Q1：任何几小时的真实运行前，每种题型真实跑一次，再加一次故意中断与恢复）对 Lite 全量的落实。Lite 的「题型」按 #605 分档（`指标+trace`、`故障窗无 trace`）算，因为故障类型来自答案，不能用来挑题。在提交 P 上做，用新轮次。

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 5.1 | 挑题：从 `tiers.json` 每档按编号取 3 题，跳过冒烟用过的两题；`故障窗无 trace` 档里有 S18 或 S38 时，取其中编号小的一个代替该档第 3 题，让数据残缺的路径在全量前走一次（本手册的默认，可逆）。共约 6 题，编号写进 `run.md` | 两档都有题；编号已记录 | 某档不足：有几题跑几题，`run.md` 写明 | 5 分钟 |
| 5.2 | 开跑：`.venv/bin/python -m scripts.itbench_lite.driver round --round <小批轮次> --dataset <数据集根> --vocabulary-dir <词表目录> --scenarios <编号,逗号分隔> --trials 1 --env-file ~/rental-private/lite.env`，在前台终端运行 | 见 5.3 与检查点 5 | — | 约 1 小时（每题约 10 分钟） |
| 5.3 | 故意中断一次（#497）：在第 2 题进行中（第 1 题那一行 JSON 打印后约 3 分钟），在驱动的前台终端按 Ctrl-C。驱动不在前台时，只对驱动本身发 SIGINT：先用 `ps -o pid,ppid,args -u opspilot` 找到 args 为 `.venv/bin/python -m scripts.itbench_lite.driver round …` 的那个进程（不是外层 shell），核对后 `kill -INT <该 pid>`。不用 pkill、killall，不按名字批量发信号 | 驱动退出且没有打印 `CLEANUP_FAILED`；`ss -ltn` 里 29110、29190、29186、29118、29143、29180 都没有监听；`docker ps --filter name=itbl-` 为空 | 有端口没释放或容器残留：属类别 ①，停，开 issue，不手动清理后假装通过 | 5 分钟 |
| 5.4 | 恢复：用 5.2 的同一条命令重跑（文档写的续跑方式：完整的 trial 会跳过，只差判分的只补判分，被中断的 trial 的旧证据移到 `tmp/…/trial-1/stale/` 后重跑） | 第 1 题的输出带 `skipped: [1]`；被中断的那题这次 `status` 为 `completed`；其余题照常跑完；跑完后 5.3 的端口与容器检查再做一遍，同样干净 | 不符：属类别 ①，停，开 issue | 约 50 分钟 |
| 5.5 | 逐题核对：每题按阶段 2 的「每题判定」记数值，另统计作废次数（`itbench_lite.history` 里 `kind` 为 `void` 的条数）与基础设施失败次数（`kind` 为 `infra`） | 见检查点 5 | — | 15 分钟 |

**小批之后的改动**：按阶段 3 的分类处理，①②③ 开 issue 修，合并后用新轮次重跑受影响的题。**A7-14 停点**：如果小批引出的改动是类别 ④（改提示词），或改变了扩展档位的 `tool_schema_revision`，而 A7 后一次 Run 已在提交 P 上跑过，那么 A7 后一次用的就不再是最终提示词或最终工具面：报 lead，由 lead 决定是否在最终提交上重跑 A7 后一次（默认重跑），理由写进 run.md。两个版本号都不变的改动不触发这个停点。

**检查点 5**：
- 无基础设施失败，或失败已开 issue 并处置；作废次数照实记录。
- 每题 judge 正常返回（`scores.json` 的 `judged` 为 true）。
- 5.3 与 5.4 的判定全部满足（#497 彩排通过）；两档都跑到了。
- 反馈按阶段 3 分类处置完；A7-14 停点没有触发，或 lead 已处置。
- 有任一条不满足就停，不开全量。

### 阶段 6：冻结版本号，跑全量 35 题 × 3（#571，约 16–20 小时）

1. **冻结前简报（全程唯一一次停下等用户）**：由 lead 决定冻结点，把冒烟与小批的反馈、各阶段检查点结果、改动与当前提交简报给用户，用户回复后开全量。
2. **冻结**：记下提交 F（`git rev-parse HEAD`，主 checkout 无已跟踪修改），在主 checkout 跑 `.venv/bin/python -c 'from opspilot.tools.otel_demo_ext import otel_demo_ext_versions as v; print(v())'`，把 `prompt_revision` 与 `tool_schema_revision` 的值写进本记录与全量证据目录的 `run.md`。这两个值要与小批 summary 的 `run.versions` 相同；不同说明小批之后又有改动，回到阶段 5 重跑受影响的题。从这一刻到全量结束，不再合并任何会进入主 checkout 的改动，主 checkout 不 pull。
3. **停 lab，腾内存**：`python3 scripts/kind_lab.py stop`（kind 部分已做完；lab 的 Jaeger 内存上限是 2Gi，旧记录里 1Gi 跑约 13.7 小时被 OOM，停掉就不受影响）。
4. **全量**：`.venv/bin/python -m scripts.itbench_lite.driver round --round <全量轮次> --dataset <数据集根> --vocabulary-dir <词表目录> --env-file ~/rental-private/lite.env`（默认全部题、`--trials 3`，按题串行，每次全新 PG 并重算平移量，L15）。可续跑：中断后用同一条命令重跑，完整的 trial（标记、completed summary、答案输出与确定性分都在）会跳过；只差判分的只补判分。清理失败（含端口没释放）会停整轮（退出码 2），查明后再续跑；查明需要改代码的，停，报用户，全量期间不改代码。作废线、每题判定同阶段 2；每个 trial 最多 3 次机会，用完记缺失、计 0、留在分母（L-D8、L5）。
5. **全量判定**：
   - 105 个 `summary.json` 都在（`find docs/evidence/itbench-lite-sre-<全量轮次> -name summary.json | wc -l` 为 105），`status` 为 `completed` 或带原因的 `missing`。
   - 每个 completed summary 的 `run.versions.prompt_revision`、`run.versions.tool_schema_revision` 等于冻结值，`itbench_lite.git.commit` 等于 F、`itbench_lite.git.dirty` 为 false。逐个用 `jq -r '[.run.versions.prompt_revision, .run.versions.tool_schema_revision, .itbench_lite.git.commit, .itbench_lite.git.dirty] | @tsv'` 取出后 `sort | uniq -c`，应只有一行。不止一行：停，报用户，这一轮不能作为全量分数。
   - 作废与缺失次数照实写进 `run.md`。
6. **补判分**：judge 失败的 trial 已写入确定性分，官方分留空。之后先把私有 env 导入当前 shell（`judge` 子命令没有 `--env-file`，只从进程环境取 judge 凭据与 `ITBENCH_EVALUATIONS_DIR`）：`set -a; . ~/rental-private/lite.env; set +a`，再用 `.venv/bin/python -m scripts.itbench_lite.driver judge --round <全量轮次> --ground-truth <数据集根>` 补判，退出码 0 表示全部补上。judge 默认 Z.ai `glm-5.3-flashx`（L6 = a），结果写明「judge 与官方示例不同」。官方分的键名带 `root_cause_entity_pass`，确定性分键名以 `det_` 开头。
7. **分数表**：`.venv/bin/python -m scripts.itbench_lite.driver scores --round <全量轮次> --out tmp/itbench-lite/<全量轮次>/scores.json`（输出的 `scenarios` 应为 35），再用 `.venv/bin/python -m scripts.itbench_lite.coverage --tiers <分档目录>/tiers.json --scores tmp/itbench-lite/<全量轮次>/scores.json --metric <键名> --json-out docs/evidence/itbench-lite-sre-<全量轮次>/table.json --markdown-out docs/evidence/itbench-lite-sre-<全量轮次>/table.md`，官方分与确定性分各出一份。主分取根因实体 F1 与 pass@1（L7）。`--metric` 要填 `scores` 输出里的实际键名，到主机上先看一眼再填（未核实）。没有报告的 trial 记 0 分，留在分母（L5）。`tiers.json` 与明细一并入库。

耗时依据：每次尝试约 9–11 分钟（建 PG 加灌数据 3–5 分钟，未实测，冒烟的 `load_seconds` 出来后重算；调查约 4–5 分钟，F3 从领取到完成约 4 分钟；收尾 1 分钟），共 105 次；judge 另计约 1 小时（并发数未定）。与适配记录的估计「1–1.5 天」一致。

**不并行**：前一版写过「冒烟通过后全量可以和 A7 同时跑」。按 #616 的顺序，全量排在 kind 部分与小批之后、版本冻结之后，所以不再并行；全量开跑前停 lab（第 3 步）。

**检查点 6**：第 5 步的判定全部满足，补判分退出码 0（仍失败的照实记录），分数表两份都已写出。

### 收尾（约 0.5 小时）

- token 原文搜索（4a.6 与 4c.6 之后各做一遍，收尾时再做一遍）：`T=$(sed -n 's/^OPSPILOT_K8S_TOKEN=//p' tmp/m1-kind-lab/k8s-investigator.env); grep -rlF "$T" docs/evidence/ ; echo "exit=$?"`，期望 exit=1（0 命中）。另对 `~/rental-private/known-secrets.txt` 里的每个值做同样搜索。
- `make secrets-branch`（gitleaks 只扫本分支新提交），通过后在证据分支上提交各阶段证据，推分支。
- 主机上的 key、`~/rental-private/` 与 lab 凭据文件随主机销毁；销毁前确认证据已经推送。关机时刻写进 `run.md`。

**总机时估计**：阶段 1 约 0.5 小时，阶段 2 约 1 小时，阶段 3 取决于改动（租机一直开着），阶段 4 约 4–5.5 小时（A1、B2、#580、A7 合进一次 lab 启动、一个共用故障窗），阶段 5 约 1.5–2 小时，阶段 6 约 16–20 小时，收尾 0.5 小时。不含阶段 3 合计约 23.5–29.5 小时。最坏情况是 A7 补跑两次，再加约 2 小时。

## 3. 证据落盘

| 证据 | 位置 | 入库 | 不入库 |
|---|---|---|---|
| 主机与阶段记录 | `docs/evidence/rental-2026-11/run.md`（目录名自定）：主机信息、提交 S/P/F、各检查点结果、版本号冻结值、开关机时刻 | 全部 | 凭据 |
| A1 拒绝取证 | `docs/evidence/a1-cluster-identity/{host,worker}/`（`probe.json`、`rules.json`）与 `run.md`（清单 sha256、token 过期时间、两侧结论）；A1-10 只写了一份 `probe.json`，两侧分目录是自定 | 探测表、规则、计数 | token、`k8s-investigator.env`、CA、已知秘密值文件 |
| B2/F7 代理探测 | `docs/evidence/b2-authz-probe/probe.json` 与 `run.md`（目录名自定） | 探测结果 | Prometheus 口令 |
| A7 两次或多次 Run | `docs/evidence/a7-main-path/<side>-<attempt>/summary.json`、`docs/evidence/a7-main-path/compare.json` 与 `docs/evidence/a7-main-path/run.md`（时间线、对照表、trace 链接、`cited_categories` 结果） | `summary.json`、LangSmith 链接 | 原始 ledger（`tmp/lab-ledgers/`，summary 里记 sha256） |
| #580 人工提问 Run 与直接取样 | `docs/evidence/tool-expansion-live/`（目录名自定）：`summary.json`、直接取样的 `direct-sample/`（含 `result.json`）、`run.md` 按 #580 验收项逐条对照 | 同上 | 原始响应体 |
| ITBench-Lite 冒烟与小批 | `docs/evidence/itbench-lite-sre-<冒烟或小批轮次>/`：驱动写的 `S<id>/trial-<n>/summary.json`、`run.md`（用了哪几题、每题判定数值、反馈简报或检查点结果、中断与恢复记录）；不出分数表 | 只有这些 | 同下 |
| ITBench-Lite 全量 | `docs/evidence/itbench-lite-sre-<全量轮次>/`（驱动写的 `S<id>/trial-<n>/summary.json`，加分数表）：分数表（json 与 markdown）、每题覆盖档位（`tiers.json` 与明细）、每次尝试的 `summary.json` 与 trace 链接、冻结的版本号与提交 F、限制说明（启发式根因、传播链受转换限制、日志与 trace 覆盖缺口、派生的调用总数） | 只有这些（L10） | `tmp/itbench-lite/<轮次>/` 下的 `agent_output.json`、`conversion_meta.json`、分数文件与评测器工作目录（驱动不写匹配明细，judge 工作目录读完数值就删）；任何答案文件 |

证据写完后，在 #580、#559、#571、#618、#619 各附链接；新数据源已在工具拓展记录登记过，不必重复。

## 4. 费用对账

- DeepSeek：只用 token × 单价。按每次调用的时刻分闲时、峰时，再分缓存是否命中，计价方法同 [soak 记录](2026-11-04-m3-soak.md) Q4 修订。数字取各 Run 冻结 summary 里的 token 用量。不用余额差：余额是账户级的，多个 key 共用。也不要求用户读控制台。单价在租机当天从 DeepSeek 官方定价页读取，写明读取日期（当前价未核实）。
- Z.ai judge：同样用 token × 单价。评测器输出里有没有 usage 字段未核实；没有的话，记调用次数和上限估计。
- LangSmith 不计费用。主机费用记开机、关机的时刻和单价，写进本记录。
- 汇总按阶段写一行：Lite 冒烟、A1/B2、A7、#580、Lite 小批、Lite 全量、judge。

## 5. 未确认项与可能返工点

- **拓扑**（第 1 节第 2 项）：宿主 web 绑 kind 网关后能否收到告警，在 Linux 上未测；不通就只能降级到 outbox trace，需要 lead 决定。
- **A1**：容器形态的证书名（风险 1）、24 小时有效期会不会被截短（风险 2）、exec 和 dry-run 写请求是否回 403（风险 3）、Compose 对缺失 secret 的表现（风险 4）、ConfigMap 里有没有 lab 凭据（风险 5）。期望值要修的，修正后只重跑一次。
- **A3、A4**：真实 API server 对日志 `Accept: */*` 的协商；Jaeger 标签值字符串化、整条 trace 的体积、404 响应体（N3 未取样项）。体积超过 4 MiB 时会报 `RESULT_TOO_LARGE`，trace 类证据可能缺席。
- **A7**：每 Run 20 次工具操作的上限（A7-16）可能挤掉指标或 trace 类；`paymentFailure` 下有没有错误日志；模型可能直接读 flagd ConfigMap，所以这是开关类场景（Q5）；`read_evidence` 在 summary 里的来源字段形状。冒烟改动若改了扩展档位的 `tool_schema_revision` 或 `context_policy_revision`，A7 前一次 Run 的版本核对会拒绝（阶段 3 版本停点）。
- **ITBench**：#605 生成器只在合成数据上测过，35 题真实数据上的分档结果与 460 MB trace 的耗时未验证；驱动在 docker 里的 promtool、Prometheus、Jaeger，worker 真调模型，LangSmith 回读，judge 真调，这些都没在真实环境跑过；灌入耗时与查询窗末端的关系（见阶段 2「每题判定」）；入口告警没有 `service_name` 时走回退（`target_fallback: true`，S12 这类题可能查到基础设施噪声，入口选择规则待定）；假 server 在完整日志文件上的加载时长与内存，以及有没有不带 Pod 属性的行（有就拒绝启动）；指标与 trace TSV 列名沿用原型，没有真实样本核对；数据集大小与下载方式；中断后的清理在本机只测过「worker 起动等待时被中断」这一处（`tests/test_itbench_lite_judge_freeze.py`），其余时刻（如并行灌数据、等 Run 期间）在阶段 5.3 第一次真实验证。
- **命令细节未核实**：4a.3 的镜像构建时长、4a.4 的容器过滤标签、4b 的网关下标与测试告警命令、阶段 2 的 `tool_requests` 字段名、阶段 6 第 7 步的 `--metric` 键名。
