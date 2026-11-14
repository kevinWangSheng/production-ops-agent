# 租机操作手册：工具拓展集中取证、A7 主路径 Run 与 ITBench-Lite 35 题

- 状态：草案（2026-11-13；首版对照 `main` `bc7ff9d0`，本版对照 `130225f9`，已含 #604 驱动与 `scripts/run_freeze.py`）。A7 节有三项待用户裁决；#605 生成器等 L-V7 裁决。
- 依据：#580 验收项、#559 与 A7 预审决定表（仓库外，A7-1 至 A7-17）、#571、[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3/r4](2026-11-12-tool-expansion-contract.md)、[A1 开工说明](2026-11-12-tool-expansion-a1.md)、[ITBench-Lite 适配](2026-11-12-itbench-lite-adapter.md)、[Linux kind lab](2026-11-04-linux-kind-lab.md)、[开发指南「kind 实验环境」](../development.md)、README「The OpenTelemetry Demo lab on the same host」，以及照搬步骤的 [F3 真实会话](../evidence/f3-acceptance-live/run.md) 和 [F7 授权探测](../evidence/f7-authz-probe/run.md)。
- 用法：开机前先过完第 1 节；开机后按第 2 节顺序执行。标「未核实」的命令或参数在本机没法确认，到主机上先核对再用。

## 1. 前提与开机前确认

**主机**：Ubuntu 24.04 x86_64，8 核 / 32 GB（`install_linux.sh` 只支持这个系统）。磁盘建议 ≥ 100 GB（数据集大小未核实）。系统镜像只允许 root 登录时，按 README 先建 `opspilot` 用户。主机不用 colima，所以 colima 密钥目录必须放在 `$HOME` 下这条坑不适用。swap 和内存改用 `free -m` 与 `/proc/meminfo` 查。

**必须已合并（开机前用 `gh pr view` 核对）**：

| 项 | 现状（2026-11-13） | 为什么必须 |
|---|---|---|
| A1–A6（#589、#588、#598、#594、#593、#600）、#581–#583（#586、#587、#603）、#604（`130225f9`） | 已合并 | 工具、回放组件、逐题驱动与 `run_freeze` |
| #605 分档文件与词表生成器 | 未实现，等 L-V7 裁决 | 驱动的 `--vocabulary`/`--vocabulary-dir` 与 `coverage.py --tiers` 都要它的产物 |
| B2 #597 | OPEN，BEHIND | 改只读代理与 `f7_authz_probe.py` 的期望表；没合并时 `up` 渲染出的代理不放行 rules/targets/metadata |
| A7（#559，变体预计叫 `replay-candidate-k8s`） | 未实现 | A7-14：扩展档位的最终 `prompt_revision` 要用于 A7 后一次 Run 和 Lite 35 题（L12）。下文写作「A7 合并后的提交」 |

**租机前还缺的本机工作**（不做完就开机，会在机上临时写代码）：

1. **A7 #559 代码与驱动**（A7-13，未完成）：薄驱动调 `kind_lab.py fault inject/restore`，Run 结束后用 `scripts/run_freeze.py` 冻结（见下），再写纯函数 `cited_categories(summary)` 并配单元测试。`run_freeze` 写出的 summary 已带报告、引用证据的工具名与视图、`tool_requests`、版本号、token 用量与告警投递，`cited_categories` 直接读它。
2. **Linux 上的 Run 拓扑要先定**（核实过的事实）：lab trace 闸门只接受回环地址或 `*.svc` 的数据源地址（`opspilot/tracing.py:152-206`）。Compose 里的 worker 用 `http://opspilot-m1-control-plane:30090`，开 `OPSPILOT_TRACE=lab` 会报 `PROMETHEUS_NOT_LAB`，只能用 `outbox` 模式；outbox 只发 id、版本、token 数和固定码。另外 Linux 上 Alertmanager 的 webhook 渲染成容器别名 `opspilot-web:8080`。**推荐**：A7 与 #580 的 Run 用宿主进程拓扑（F3 的接法：web 与 worker 在宿主上跑，数据源走 127.0.0.1），告警用开发指南写的退路 `OPSPILOT_LAB_HOST_ADDRESS=<kind 网关>`，web 设 `OPSPILOT_BIND=<kind 网关>:8080`。这条路在 Linux 上没有端到端测过：F9 S4 当时被 ufw 挡住。它需要宿主装 PostgreSQL 17，并把 `initdb`、`pg_ctl` 放进 PATH（`postgres_lab.py` 与 #604 的 `LocalPgLauncher` 都用 `shutil.which` 找；Ubuntu 的 PGDG 包装在 `/usr/lib/postgresql/17/bin`，未核实）。ITBench 驱动自己在宿主上起 web/worker，不受这项影响。
3. **#604 ITBench-Lite 逐题驱动：已完成**（`130225f9`，命令见阶段 4）。它在本机只用替身和一次性 PG 测过；docker 里的 promtool、Prometheus `v3.5.0`、Jaeger `1.53.0`，worker 真调模型，LangSmith 回读，judge 真调，真实数据的灌入耗时，都要在租机上第一次验证（[适配记录「实施记录（#604）」](2026-11-12-itbench-lite-adapter.md)）。
4. **#605 分档文件与词表生成器**（未完成，等用户裁决 L-V7：namespace 名与 `kubernetes`、`default` 这类通用词进不进词表）：驱动要每题一个词表 `S<id>.txt`，`coverage.py` 要分档 JSON。分档只能来自两次普查（L11）。
5. **直接取样脚本：已完成**（#607，`97e9919c`，`scripts/tool_sample.py`）：不经模型直接调 `otel-demo-ext` 工具（按列查询、Pod 日志、Secret 拒绝、trace、即时查询、B2 规则），命令见阶段 3 第 3.6 步。
6. **主机准备清单**：PostgreSQL 17、`uv`、宿主 `jq`（宿主 worker 跑 `otel-demo-ext` 时，没有 jq 就启动失败，`JQ_NOT_FOUND`）、DeepSeek、LangSmith、Z.ai 三个 key（放在仓库外 600 文件里；ITBench 驱动用 `--env-file` 读，只交给 worker 与 trace 回读，web 不拿），评测器在 `14f026fc` 重新克隆并按其 `uv.lock` 建 venv（本机那份在 `~/.agent/eval-probes/`），数据集 `ibm-research/ITBench-Lite` SRE `v0.2` 的下载命令（未核实）。

## 2. 步骤清单

时间都是估计值，依据写在括号里。所有命令都以 `opspilot` 用户在仓库根目录执行。每个阶段的起止时刻都记进对应证据目录的 `run.md`。

### 阶段 0：主机与 lab（约 0.5 小时）

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 0.1 | 以 root 照 README 执行 `adduser`、`git clone`（检出「A7 合并后的提交」），再跑 `scripts/kind_lab/install_linux.sh --user opspilot` | 退出码 0，末尾 health 报告 `ok: true` | 看安装日志；版本或哈希不符就停，不改钉住的版本 | 约 5 分钟（F9 主机全程 3 分 27 秒） |
| 0.2 | 装第 1 节第 6 项的工具，执行 `make setup`，再跑 `gh issue list --label needs-kind-run --state open`，把待补跑的项并入本窗口 | `make setup` 退出码 0 | 区分环境前提与代码问题，不改代码 | 约 15 分钟 |
| 0.3 | lab 前提：`python3 scripts/kind_lab.py health`、`python3 scripts/kind_lab.py rules-check`；跑 `free -m`；检查 frontend 的 span 速率，先 `set -a; . tmp/m1-kind-lab/prometheus-lab.env`，再 `curl -sG -u "$OPSPILOT_LAB_PROMETHEUS_USERNAME:$OPSPILOT_LAB_PROMETHEUS_PASSWORD" http://127.0.0.1:19090/api/v1/query --data-urlencode 'query=sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))'`（代理拒绝 POST 查询，只能用 GET） | health 为 ok；rules-check 全 0；速率 > 0；flagd 开关全 off | 速率为 0：用 `kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo rollout restart deployment otel-collector`，动作记进 `lab_actions` | 5 分钟 |

### 阶段 1：拒绝取证，不调模型（约 0.75 小时）

| 步 | 目的与命令 | 判定 | 失败时 | 耗时 |
|---|---|---|---|---|
| 1.1 | 准备已知秘密值文件，放在仓库外：`umask 077; mkdir -p ~/rental-private; { jq -r '.[]' tmp/m1-kind-lab/prometheus-auth.json; cat tmp/m1-kind-lab/alertmanager-webhook-token tmp/m1-kind-lab/release-deployer-token; } > ~/rental-private/known-secrets.txt`。另把 DeepSeek 与 LangSmith 的 key 值追加进去（自定：这两个不在 lab 里，搜到就说明泄漏） | 每行一个值，文件权限 600 | — | 2 分钟 |
| 1.2 | 宿主侧 A1 探测：`python3 scripts/k8s_authz_probe.py --env-file tmp/m1-kind-lab/k8s-investigator.env --known-secrets ~/rental-private/known-secrets.txt --side host --manifest scripts/kind_lab/opspilot-readonly-rbac.yaml --out-dir docs/evidence/a1-cluster-identity/host` | 退出码 0，`probe.json` 里 `ok: true`、`known_secret_hits` 为空；读类 200、Secret/写/exec/proxy/watch/token 403；自定义资源 403 或 404 | 期望值写错（A1 风险 3）时，修正期望后只重跑一次并照实记录（A1 规则）；真实授权缺口就停下报 lead | 2 分钟 |
| 1.3 | 起 Compose 栈，只为容器侧探测：`python3 scripts/kind_lab.py control-secrets`，在 `~/.config/opspilot/worker_env` 加 `DEEPSEEK_API_KEY`，`OPSPILOT_START_AGENT=1 deploy/compose/install.sh install`，再跑 `python3 scripts/kind_lab.py connect-control --project opspilot` 和 **再一次** `control-secrets --project opspilot`（`install` 重建的 worker 没挂 CA，见 A1「偏离」）。档位先用 `control-secrets` 写入的 `otel-demo`（F9 验证过），避免 worker 因为扩展档位的启动前提起不来 | 三条命令退出码 0；`docker ps` 里能看到 worker | 按 F9 记录排查；Compose 对缺失 secret 文件的表现是 A1 风险 4 | 15 分钟（镜像构建时间未核实） |
| 1.4 | 容器侧 A1 探测：`W=$(docker ps -q --filter label=com.docker.compose.project=opspilot --filter label=com.docker.compose.service=worker)`，然后 `docker exec -i "$W" python -c "$(cat scripts/k8s_authz_probe.py)" --env-file /run/secrets/worker_env --side worker --known-secrets - < ~/rental-private/known-secrets.txt > docs/evidence/a1-cluster-identity/worker/probe.json`（写法来自探测脚本 docstring，过滤标签未核实） | 同 1.2；能连上，说明证书名对得上（A1 风险 1） | 证书名不符（A1 风险 1），就记录并停这一项，报 lead：要改 `kind_lab.py` 的地址生成 | 3 分钟 |
| 1.5 | B2 与 F7 代理探测：`python3 scripts/f7_authz_probe.py > docs/evidence/b2-authz-probe/probe.json` | 退出码 0、`ok: true`；四个账号与 holmes 的 rules、targets、metadata GET 都是 200，新增的 `/api/v1/alerts`、`/api/v1/targets/metadata`、`/api/v1/status/config` GET 是 403；其余行与 [F7 探测](../evidence/f7-authz-probe/run.md) 一致 | 不符就逐行列出，不改代理 | 3 分钟 |
| 1.6 | 记下 token 过期时间：只从 `tmp/m1-kind-lab/k8s-token.json` 抄过期时间，连同清单 sha256 写进 `a1-cluster-identity/run.md`，再跑 1.7 的 token 搜索 | — | — | 2 分钟 |
| 1.7 | 拆掉 Compose 栈：`deploy/compose/install.sh uninstall`（保留数据卷与密钥）。这样 webhook 可以改走宿主拓扑 | 容器已删 | — | 2 分钟 |

### 阶段 2：切到宿主拓扑（约 0.5 小时，按第 1 节第 2 项的推荐）

1. 查 kind 网关：`docker network inspect kind -f '{{(index .IPAM.Config 0).Gateway}}'`（F9 主机上是 172.18.0.1，取下标 0 未核实）。然后执行 `OPSPILOT_LAB_HOST_ADDRESS=<网关> python3 scripts/kind_lab.py up`，重新渲染 webhook。`ufw status` 为 active 时，放行 kind 网段到 8080（这台租机用完即弃）。
2. 宿主 PG 按开发指南「本地运行演示」执行：`python -m scripts.m0.postgres_lab start`、`make migrate`、`postgres_lab runtime-roles`。web 与 worker 的环境照 F3 run.md「环境」一节，有三处不同：`OPSPILOT_TOOL_PROFILE=otel-demo-ext`；worker 读 `tmp/m1-kind-lab/k8s-investigator.env`（宿主形态）；web 设 `OPSPILOT_BIND=<网关>:8080`。trace 用 `OPSPILOT_TRACE=lab`，项目按 A7-17 设为 `opspilot-lab-a7-main-path-<日期>`，开跑前由驱动设成长期保留。
3. 冒烟：用 lab 的 Alertmanager 发一条测试告警（F9 S4 用的是 `amtool alert add`，在 lab 里执行的确切命令未核实），确认 web 收到 `POST /intake/alertmanager`，`up{job="opspilot"}` 的状态先不管（抓取目标指向网关，可能是 down）。
4. 失败时：网关不通就退回第 1 节第 2 项的备选（Compose 加 `outbox`），A7 与 #580 的 trace 证据会降级，要报 lead。

### 阶段 3：A7 主路径 Run 与 #580 真实证据（约 2–3.5 小时）

> **待用户裁决，确认后改定**：A7-8 判定口径，按推荐写成「四类证据按被引证据的来源工具归类，任一结论类别都算」；A7-9 场景，按推荐写成「flagd `paymentFailure` 100%」；A7-12 失败策略，按推荐写成「后一次 Run 最多补跑 2 次，照实记录 x/n」。

| 步 | 内容 | 判定（确定性） | 耗时（依据：F3 每次约 40 分钟，故障维持 1800 秒） |
|---|---|---|---|
| 3.1 | 前一次 Run：worker 用 A7 起点提交（`git worktree add ../poa-a7-before <A7 起点提交>`，变体 `replay-candidate`），web 与 worker 在故障注入前起好（A7-10）。驱动执行 `fault inject --flag paymentFailure --experiment-id a7-before`。故障期间工程侧顺带探一次：`kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo logs deploy/payment --since=10m`，checkout 同样跑一次，看有没有应用错误日志（A7 §4 第 1 项），只把有或没有、条数记进 run.md | 事故由 `alertmanager` 身份开出；没有人工控制；按下方「冻结」用 `run_freeze` 冻结 | 40 分钟 |
| 3.2 | 恢复与冷却：`fault restore --experiment-id a7-before`，等告警 resolved 附到同一事故，冻结后再关闭事故（F3 做法），为下一次腾出新事故 | `ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空 | 15 分钟 |
| 3.3 | 后一次 Run：worker 换成「A7 合并后的提交」重启，执行 `--experiment-id a7-after-1`。A7 驱动的命令未核实（驱动还没写） | `cited_categories(summary)` 含指标、trace、集群对象、日志四类；`prompt_revision` 以 `prompt-replay-candidate-k8s-` 开头 | 40 分钟 |
| 3.4 | #580 人工提问 Run，放在 3.3 同一个故障窗里（故障还开着，省一个 30 分钟窗口）：在 3.3 的 Run 完成后，用 `curl -u <账号> -H 'Origin: http://<网关>:8080' -d target_id=m0-otel-20260909 -d idempotency_key=r580-1 --data-urlencode 'question=…' http://<网关>:8080/intake/ui` 提交（开发指南「提交事故」）。问题要明确请调查读 checkout 的 Pod 状态与事件、Pod 日志、按标签搜 trace 再按 ID 取整条、即时查询与序列/标签发现、告警规则与抓取目标，并要求读 `otel-demo` 里的 Secret | `tool_requests` 里有：集群对象查询 ok、`kind` 含 secret 的请求被拒为 `RESOURCE_NOT_ALLOWED`（A2-6）、`fetch_pod_logs` ok、`traces_search_by_tags` 或 `traces_query_trace_by_id` ok、`execute_prometheus_instant_query`/`get_series`/`get_all_labels` 之一 ok、B2 规则/目标/元数据之一 ok；报告引用了 Pod 状态或事件，以及某 Pod 的日志行 | 15 分钟 |
| 3.5 | 补跑（只在 3.3 没达到四类时）：恢复、冷却后，同一份代码再跑 `a7-after-2`、`a7-after-3` | x/n 照实写 | 每次 55 分钟，最多 2 次 |
| 3.6 | 直接取样（#607，不调模型；lab 起着就能跑，不需要故障窗），命令见表下「直接取样」 | 退出码 0，且 `result.json` 的 `all_requested_passed` 为 `true`；之后再做一遍 token 原文 grep，见阶段 5 | 10 分钟 |

**直接取样（3.6）**：

```sh
set -a; . tmp/m1-kind-lab/k8s-investigator.env; . <investigator 的 Prometheus env>; . <LangSmith/trace env>; set +a
.venv/bin/python -m scripts.tool_sample \
  --evidence-dir docs/evidence/tool-expansion-live/direct-sample \
  --log-pod <最近 1 小时日志超过 100 行的 Pod> \
  --trace-service frontend \
  --trace-tags '<先确认 lab 里存在的标签>' \
  --prom-query 'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[5m]))' \
  --allow-skip rules
```

- 运行环境必须带全套 lab 闸门变量：`OPSPILOT_TRACE=lab`、`LANGSMITH_PROJECT=opspilot-lab-*`、`LANGSMITH_API_KEY`。
- 后两个 env 文件名是占位，未核实。investigator 的 Prometheus env 推测是 `tmp/m1-kind-lab/prometheus-investigator.env`，同样未核实。LangSmith/trace env 是仓库外的私有文件。
- `--trace-tags` 用 logfmt 写法，例如 `error=true`。先在 lab 的 Jaeger 里确认这个标签真的存在：任取一条 frontend trace，看它的 span 标签。
- `--log-pod` 选最近 1 小时日志超过 100 行的 Pod，用工程侧 `kubectl --kubeconfig tmp/m1-kind-lab/kubeconfig -n otel-demo logs <pod> --since=1h | wc -l` 确认。
- **B2 #597 已合并时去掉 `--allow-skip rules`**，让规则一项也必须通过。

**冻结**：每个 Run 结束（3.2 关闭事故之前）就冻结一次，用 `RUN_FREEZE_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab" .venv/bin/python -m scripts.run_freeze --run-id <Run id> --experiment <a7-before|a7-after-1|r580-1> --evidence-dir docs/evidence/<a7-main-path-live|tool-expansion-live>/<同名> --project opspilot-lab-a7-main-path-<日期>`。DSN 只要能读产品表，也可以照 F3 另建只读角色；DSN 从环境读，不写进文件。判定：退出码 0，`summary.json` 带 trace 块，原始 ledger 落在 `tmp/lab-ledgers/`。以 `failed` 或 `budget_exhausted` 结束的 Run，`outcome` 会是 `{"available": false, "reason": "UNKNOWN_DURABLE_STATE"}`（产品投影的既有规则），其余字段照常完整，照实记录，不当作冻结失败。在同一事故上，已不是当前 Run 的那次记作 `RUN_NOT_CURRENT`，所以要在关闭事故、开下一次之前冻结。

前后对照只记确定性数字（A7-11）：引用了哪些类别、各类调用次数、模型请求数、token、校验是否通过、工具失败次数。每边只有 1 次，不下因果结论。

### 阶段 4：ITBench-Lite 35 题 × 3（约 16–20 小时）

驱动是 `scripts/itbench_lite/driver.py`（#604），子命令有 `attempt`、`round`、`judge`、`scores`。回放服务默认绑 127.0.0.1 的 29110（PG）、29190（Prometheus）、29186/29118（Jaeger 查询与 OTLP）、29143（假 server）、29180（web），都避开了 lab 的 19090、16686、18080、8080、55431、9464，要改就用 `--{pg,prometheus,jaeger-query,jaeger-otlp,fake-k8s,web}-port`。镜像钉 `prom/prometheus:v3.5.0` 与 `jaegertracing/all-in-one:1.53.0`（`services.py`）。证据写到 `docs/evidence/itbench-lite-sre-<轮次>/S<id>/trial-<n>/summary.json`，其余都在 git 忽略的 `tmp/itbench-lite/<轮次>/`。

1. 准备：#605 的词表目录（每题 `S<id>.txt`）与分档文件；私有 env 文件放 DeepSeek、LangSmith、judge 的 key（`--env-file`，600，放在仓库外）；判分要 `ITBENCH_EVALUATIONS_DIR` 指向重新克隆的评测器。数据集根目录本身带答案文件，驱动只按名字复制白名单文件，不打开答案。
2. 冒烟 1 题 × 1 次（题目选数据完整的，不选 S18、S38）：`python3 -m scripts.itbench_lite.driver attempt --round <轮次> --dataset <数据集根> --scenario <场景目录名> --trial 1 --vocabulary <词表>/S<id>.txt --env-file ~/rental-private/lite.env`。判定：
   - 退出码 0，`summary.json` 已冻结，`tmp/…/trial-1/complete.json` 已写出。
   - 作废线按 r4 原文：灌完立即投递，灌完到投递超过 2 分钟这次作废（驱动每个 trial 最多给 3 次机会）。看 `gap_seconds`。
   - **必须记录并评估 `load_seconds` 与 `data_end_to_delivery_seconds`**（约等于 60 秒 + 灌入耗时 + worker 起动）。灌数据超过约 4 分钟时，以查询窗终点为时刻的即时查询可能落在数据之后（适配记录已知风险 1 的 5 分钟线）。超过就把数值写进 run.md，报 lead 评估后再开全量，不自行改平移量。
   - `--load-budget-seconds` 只是灌入耗时的上限（默认 600），不是作废线。
   - 同时看：假 server 加载完整日志的耗时与内存（`free -m`）、`target_fallback` 是否为 true、`outcome` 是否 available。
3. 全量：`python3 -m scripts.itbench_lite.driver round --round <轮次> --dataset <数据集根> --vocabulary-dir <词表目录> --env-file ~/rental-private/lite.env`（默认全部题、`--trials 3`，按题串行，每次全新 PG 并重算平移量，L15）。可续跑：中断后用同一条命令重跑，完整的 trial（标记、completed summary、答案输出与确定性分都在）会跳过；只差判分的只补判分。清理失败（含端口没释放）会停整轮，查明后再续跑。档位 `otel-demo-ext`，summary 记 `prompt_revision` 与 `tool_schema_revision`（L12）。trace 默认 `--trace lab`；LangSmith 项目在开跑前设为长期保留。
4. 补判分：judge 失败的 trial 已写入确定性分，官方分留空（退出码 3）。之后先把私有 env 导入当前 shell（`judge` 子命令没有 `--env-file`，只从进程环境取 judge 凭据与 `ITBENCH_EVALUATIONS_DIR`）：`set -a; . ~/rental-private/lite.env; set +a`，再用 `python3 -m scripts.itbench_lite.driver judge --round <轮次> --ground-truth <数据集根>` 补判。judge 默认 Z.ai `glm-5.3-flashx`（L6 = a），结果写明「judge 与官方示例不同」。官方分的键名带 `root_cause_entity_pass`，确定性分键名以 `det_` 开头。
5. 分数表：`python3 -m scripts.itbench_lite.driver scores --round <轮次> --out tmp/itbench-lite/<轮次>/scores.json`，再用 `python3 -m scripts.itbench_lite.coverage --tiers <分档文件> --scores tmp/itbench-lite/<轮次>/scores.json --metric <键名> --json-out docs/evidence/itbench-lite-sre-<轮次>/table.json --markdown-out docs/evidence/itbench-lite-sre-<轮次>/table.md`，官方分与确定性分各出一份。主分取根因实体 F1 与 pass@1（L7）。`--metric` 要填 `scores` 输出里的实际键名，到主机上先看一眼再填（未核实）。没有报告的 trial 记 0 分，留在分母（L5）。

**答案隔离（硬约束）**：执行者不打开、不打印、不读取 `ground_truth.yaml/.yml`、`assertion.json`、`status.json` 和评测器的 `prompts/few_shot.py`；答案根目录只作为路径传给判分进程。issue、PR 与入库文件都不写答案内容。

耗时依据：每次尝试约 9–11 分钟（建 PG 加灌数据 3–5 分钟，未实测，冒烟的 `load_seconds` 出来后重算；调查约 4–5 分钟，F3 从领取到完成约 4 分钟；收尾 1 分钟），共 105 次；judge 另计约 1 小时（并发数未定）。与适配记录的估计「1–1.5 天」一致。

**可选并行**：驱动的默认端口不占 lab 的端口，所以冒烟通过后，阶段 4 的全量可以和阶段 3 同时跑，总机时省 3–4 小时。代价是 CPU 争用可能影响 A7 的耗时与 token 对照，以及冒烟测出的灌入耗时，A7 的 run.md 要注明。默认串行。串行时先跑 `python3 scripts/kind_lab.py stop` 停掉 lab，腾出内存。lab 的 Jaeger 内存上限现在是 2Gi，旧记录里 1Gi 跑约 13.7 小时被 OOM；阶段 0–3 加起来不到 6 小时，不受影响。

### 阶段 5：收尾（约 0.5 小时）

- token 原文搜索（3.6 直接取样之后先做一遍，收尾时再做一遍）：`T=$(sed -n 's/^OPSPILOT_K8S_TOKEN=//p' tmp/m1-kind-lab/k8s-investigator.env); grep -rlF "$T" docs/evidence/ ; echo "exit=$?"`，期望 exit=1（0 命中）。另对 `~/rental-private/known-secrets.txt` 里的每个值做同样搜索。
- `make secrets-branch`（gitleaks 只扫本分支新提交），通过后提交证据，推分支。
- 主机上的 key、`~/rental-private/` 与 lab 凭据文件随主机销毁；销毁前确认证据已经推送。

**总机时估计**：阶段 0–3 约 3.75–5.25 小时（kind 部分；原计划估计 8–10 小时，这里把 A1、B2、#580、A7 合进一次 lab 启动、一个共用故障窗，所以更短），阶段 4 约 16–20 小时，收尾 0.5 小时。串行合计约 20–26 小时；按上面的条件并行约 17–22 小时。最坏情况是 A7 补跑两次，再加约 2 小时。

## 3. 证据落盘

| 证据 | 位置 | 入库 | 不入库 |
|---|---|---|---|
| A1 拒绝取证 | `docs/evidence/a1-cluster-identity/{host,worker}/`（`probe.json`、`rules.json`）与 `run.md`（清单 sha256、token 过期时间、两侧结论）；A1-10 只写了一份 `probe.json`，两侧分目录是自定 | 探测表、规则、计数 | token、`k8s-investigator.env`、CA、已知秘密值文件 |
| B2/F7 代理探测 | `docs/evidence/b2-authz-probe/probe.json` 与 `run.md`（目录名自定） | 探测结果 | Prometheus 口令 |
| A7 两次或多次 Run | `docs/evidence/a7-main-path-live/<run>/summary.json` 与 `run.md`（时间线、对照表、trace 链接、`cited_categories` 结果） | `summary.json`、LangSmith 链接 | 原始 ledger（`tmp/lab-ledgers/`，summary 里记 sha256） |
| #580 人工提问 Run 与直接取样 | `docs/evidence/tool-expansion-live/`（目录名自定）：`summary.json`、直接取样的 `direct-sample/`（含 `result.json`）、`run.md` 按 #580 验收项逐条对照 | 同上 | 原始响应体 |
| ITBench-Lite | `docs/evidence/itbench-lite-sre-<轮次>/`（驱动写的 `S<id>/trial-<n>/summary.json`，加分数表）：分数表（json 与 markdown）、每题覆盖档位、每次尝试的 `summary.json` 与 trace 链接、限制说明（启发式根因、传播链受转换限制、日志与 trace 覆盖缺口、派生的调用总数） | 只有这些（L10） | `tmp/itbench-lite/<轮次>/` 下的 `agent_output.json`、`conversion_meta.json`、分数文件与评测器工作目录（驱动不写匹配明细，judge 工作目录读完数值就删）；任何答案文件 |

证据写完后，在 #580、#559、#571 各附链接；新数据源已在工具拓展记录登记过，不必重复。

## 4. 费用对账

- DeepSeek：只用 token × 单价。按每次调用的时刻分闲时、峰时，再分缓存是否命中，计价方法同 [soak 记录](2026-11-04-m3-soak.md) Q4 修订。数字取各 Run 冻结 summary 里的 token 用量。不用余额差：余额是账户级的，多个 key 共用。也不要求用户读控制台。单价在租机当天从 DeepSeek 官方定价页读取，写明读取日期（当前价未核实）。
- Z.ai judge：同样用 token × 单价。评测器输出里有没有 usage 字段未核实；没有的话，记调用次数和上限估计。
- LangSmith 不计费用。主机费用记开机、关机的时刻和单价，写进本记录。
- 汇总按阶段写一行：A1/B2、A7、#580、ITBench、judge。

## 5. 未确认项与可能返工点

- **拓扑**（第 1 节第 2 项）：宿主 web 绑 kind 网关后能否收到告警，在 Linux 上未测；不通就只能降级到 outbox trace，需要 lead 决定。
- **A1**：容器形态的证书名（风险 1）、24 小时有效期会不会被截短（风险 2）、exec 和 dry-run 写请求是否回 403（风险 3）、Compose 对缺失 secret 的表现（风险 4）、ConfigMap 里有没有 lab 凭据（风险 5）。期望值要修的，修正后只重跑一次。
- **A3、A4**：真实 API server 对日志 `Accept: */*` 的协商；Jaeger 标签值字符串化、整条 trace 的体积、404 响应体（N3 未取样项）。体积超过 4 MiB 时会报 `RESULT_TOO_LARGE`，trace 类证据可能缺席。
- **A7**：每 Run 20 次工具操作的上限（A7-16）可能挤掉指标或 trace 类；`paymentFailure` 下有没有错误日志；模型可能直接读 flagd ConfigMap，所以这是开关类场景（Q5）；`read_evidence` 在 summary 里的来源字段形状。
- **ITBench**：#605 生成器还没写（等 L-V7）；驱动在 docker 里的 promtool、Prometheus、Jaeger，worker 真调模型，LangSmith 回读，judge 真调，这些都没在真实环境跑过；灌入耗时与查询窗末端的关系（见阶段 4 第 2 步）；入口告警没有 `service_name` 时走回退（`target_fallback: true`，S12 这类题可能查到基础设施噪声，入口选择规则待定）；假 server 在完整日志文件上的加载时长与内存，以及有没有不带 Pod 属性的行（有就拒绝启动）；指标与 trace TSV 列名沿用原型，没有真实样本核对；数据集大小与下载方式。
- **命令细节未核实**：1.3 的镜像构建时长、1.4 的容器过滤标签、阶段 2 的网关下标与测试告警命令、3.3 的 A7 驱动命令（A7 驱动还没写）、阶段 4 第 5 步的 `--metric` 键名。
