# F3 PR-3b kind 真实会话：第 5 步 2 次竞争原因 Run、第 6 步代理复测、第 1 步投影数据

- 日期：2026-10-24（UTC 16:23–18:57），kind-opspilot-m1 / otel-demo；分支 `feature/F3-acceptance`，代码 `e1ea7cd`（起点 main `3c18bdb`），运行期间未改产品代码
- 依据：[F3 任务记录](../../tasks/2026-10-17-f3.md)；合同 r1 U3-a、U4-a、U9-a；[PR-3a 合同](../../tasks/2026-10-22-f3-pr3a-contract.md) D1-a、D4-a、D5-a、R-1、R-6；PR-3b 预审（仓库外 `~/.agent/handoffs/opspilot-prereview-f3-3b-2026-10-24.md`）§2、R-1、R-2、R-4、R-7、R-10 与 U-A 裁决；[M2 授权](../../tasks/2026-10-24-m2-remaining.md) Q6 与 runbook
- 冻结摘要：[run-1/summary.json](prompt-bef70649a3a8/run-1/summary.json)、[run-2/summary.json](prompt-bef70649a3a8/run-2/summary.json)（会话中冻结、关闭事故之前，审查者读这两份）；代理探测 [f7-authz-probe.json](f7-authz-probe.json)。验收测试读的同一批数据另有一份按测试约定的形状：[第 5 步 run-1](../f3-step5-competing-cause/prompt-bef70649a3a8/run-1/summary.json)、[run-2](../f3-step5-competing-cause/prompt-bef70649a3a8/run-2/summary.json)（顶层 `frozen` 与 `f3_acceptance`，另含 `lab_readings`），由只读脚本 [collect.py](../f3-step5-competing-cause/collect.py) 在会话结束后从同一 PG（重新启动、只读角色、只读快照事务）与驱动 ledger 生成，因此包含两次人工 `close` 之后的状态；[第 6 步 probe.json](../f3-step6-authz-probe/probe.json) 与本目录的 f7-authz-probe.json 逐字节相同。原始 ledger 在主仓库 `tmp/lab-ledgers/f3-competing-cause/f3-3b-run{1,2}/`（驱动的 plan、release、fault.jsonl、follow-up、summary，加本次收集的 `db-extract.json` 与 `rollout.json`），不入库，sha256 记在各 summary 的 `raw_ledger`、`rollout.ledger_sha256`、`driver_ledger.files`
- 性质：有界真实运行。发布是 F11 harness 的真实 `kubectl patch` 滚动（只改一个环境变量）；故障是 flagd 真实注入 `paymentFailure: off → 100%`；告警来自实验环境真实的 Prometheus 与 Alertmanager；追问走工作台人工控制入口；调查 worker 用真实 DeepSeek `deepseek-flash`。不是生产证明
- 执行者：未参与 F3 实现的 Agent，只跑会话、收集证据，**不判 rubric**。第 5 步的 rubric 判定由两名未参与运行的全新上下文审查者按冻结 rubric 进行（U3-a），本文件不含判定
- **结论（只陈述运行事实）：两次驱动都 `finished ok:true`，没有失败轮、没有中途放弃、没有用到 Q6 重跑。** 两次都是「新告警事故 + 单个 Run」，追问在 worker 领取前送达、第 1 轮可见，Run 都 `completed` 并发布报告；工具请求全部为只读动词、目标都是绑定目标；代理探测 136/136 符合

## 系统做了什么

每次 Run 是同一条链：驱动先过七项前提 → harness 改 checkout 的一个环境变量触发真实滚动（R = patch 回执）→ R+420 s 把 flagd `paymentFailure` 打到 100%（与该部署无关的依赖故障）→ Prometheus 规则 `CheckoutPlaceOrderErrorRatioHigh` 触发，Alertmanager 投递到工作台 → 工作台建新事故和一个 queued Run → 驱动确认 Run 仍 queued、无 step、代际 0，以操作员身份发一条只陈述部署与时间的追问（D4-a）→ 驱动打出 `start_worker` 后才起 worker（D1-a）→ worker 调查、发布报告 → 故障维持 1800 s 后由 harness 自动恢复 → 告警 resolved 附加到同一事故 → 收集证据后经 UI `close` 关闭事故，给下一次 Run 腾出前提 ⑤。

## 环境

- PostgreSQL 17.9：本 worktree 新建一次性实例（initdb，端口 55771，trust，开启连接日志），`python -m opspilot.schema migrate` 升到 `0021_release_incident_links`；`postgres_lab runtime-roles` 建 web、worker 登录角色；另建只读角色 `f3_lab_reader`（只有 `SELECT`，`default_transaction_read_only=on`）给驱动的 `F3_LAB_DSN` 与证据收集用（R-1）。会话结束后 `pg_ctl stop`。其他 lane 的 PG 只做了一次只读查询（见「第 1 步」）
- web：`python -m opspilot.web serve`，`127.0.0.1:8080`（Alertmanager webhook 目标），最小环境；`OPSPILOT_EVENT_TOKENS` 来自 `workbench-alertmanager.env`（只有哈希）；`OPSPILOT_TARGET_IDENTITIES` 为 lead 核实的 `m0-otel-20260909`（`cluster_uid` 与 kube-system UID 一致）；`OPSPILOT_TOOL_PROFILE=otel-demo`；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS` 用产品默认 30；实验 UI 账号 `f3lab`（随机口令，只存在仓库外私有文件）
- worker：`python -m opspilot.worker_main`，最小环境，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号；版本 `prompt-replay-candidate-bef70649a3a8` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-68ed2ca8f5d6`（与 D9 重跑同一模型可见面）；防循环上限为产品默认每 Run 100 次模型请求。每次 Run 只在驱动打出 `start_worker` 后启动，两次 Run 之间停掉
- 端口转发：flagd `18013 → svc/flagd:8013`，本次启动、结束后停止；`ResolveInt` 与 `ResolveFloat` 都可解析 `paymentFailure`，按既有 harness 用法传 `--flag-method Int`
- 实验目录：worktree 的 `tmp/m1-kind-lab` 以符号链接指向主仓库同名目录（git 忽略，与 F7 第 1 步做法相同），凭据没有复制进 worktree
- trace：LangSmith 项目 `opspilot-lab-f3-acceptance-live-20261024`，运行前建立，回读 `trace_tier=longlived`
- 占用：`occupation show` 为 null 后 claim（holder `lane-f3-3b`，batch `f3-3b-live`，16:38:30Z，至 20:00Z），18:57Z release，回读 null
- 开跑前实验环境：`kind_lab.py health` ok；flagd 11 个 flag 全部 off（ConfigMap sha256 前缀 `1ee5c025`），无 `fault-active.json`，`ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空；frontend span rate 1.3–2.8/s（> 0，未重启 collector）
- web 启动时立刻收到一条 Alertmanager 补发的 resolved（startsAt 15:59:54Z，F2 PR-E 会话的告警，web 不在线期间 Alertmanager 一直重试）：记为 `resolved_recorded`，没有事故、没有 Run。这是投递 1，不属于两次 Run

### 偏离与照实记录

- **swap 前提用 `--swap-min-free-mib 512`，不是默认 2048（lead 2026-10-24 批准，条件：两次开跑前各记一次读数，p95 > 300 ms 不开跑；Run 期间 swap 余量 < 300 MiB 或 p95 持续 > 500 ms 单列）。** 16:41Z swap 余量 901 MiB（总 9216 MiB），开跑前 1.0–1.3 GiB。内存大头是 colima VM 和其他会话的应用，按规则不能停。2048 是 PR-3a 自定的可逆默认，不是验收步骤；F11 PR-4b 在 367 MiB 下跑过且 p95 未升高。开跑前已发消息告知 lead，并自定「p95 > 300 ms 就不开跑」。实际读数：Run 1 前（16:58Z）p95 97.5 ms、swap 余量 1232 MiB；Run 2 前 p95 97.5 ms、swap 余量 977 MiB；全程内存空闲 37–50%，p95 在 48–188 ms 之间波动（低流量下 5 分钟分位数抖动大），没有出现 runbook 所说的 0.5–1.8 s。两次开跑前读数（也写在第 5 步 summary 的 `lab_readings`）：Run 1 16:58:43Z p95 97.5 ms、frontend span rate 1.48/s、swap 余量 1232/10240 MiB、memory_pressure 该次输出被截断（最近完整读数 16:41Z 空闲 39%）；Run 2 18:10:44Z p95 97.5 ms、span rate 1.50/s、swap 余量 977/10240 MiB、内存空闲 39%。**Run 期间没有连续监控**：Run 1 在 17:00:21–17:38:29Z、Run 2 在 18:11:01–18:57:06Z 之间没有读数；其后的点读数为 17:38:29Z（swap 余量 1652 MiB、p95 48 ms）与 18:57:06Z（1153 MiB、94 ms）。所以「Run 期间 swap < 300 MiB 或 p95 > 500 ms」既没有观察到，也不能排除
- 前提 ⑥（70 分钟内无其他 checkout 发布）：16:41Z 首次 preflight 因 F2 PR-E 15:49:20Z 的发布失败，等到 17:00Z 后通过；Run 2 等到 R1+70 分钟（18:10:23Z）之后才开跑
- R-2 只读 SQL（未关闭的、由发布异常开出的事故；预审 #343 缺口）在开跑前（17:00:13Z）和两次 Run 之间（17:42:29Z、18:10:44Z）各跑一次，三次都是 0；`opspilot_release_observations` 全程 0 行，`opspilot_release_incident_links` 全程 0 行
- 自动恢复观察（#184）：两次 Run 发布后，web 以 `system` 身份各自动授权一次恢复观察（Run 1 17:18:22Z，Run 2 18:31:54Z）。本次没有起 Observer，会话一直停在 authorized，随后被人工 `close` 撤销
- 复盘生成：第一次 `close` 后，worker 的复盘线程开始为事故 1 生成复盘；停 worker 时该次尝试在 30 s grace 后被中断，Run 2 期间重启的 worker 接着完成（18:28:50Z，version 1）。第二次 `close` 后停 worker 时，日志同样记录一次尝试在 grace 后被中断；事故 2 的复盘是否已产出版本未确认（PG 已停，未查）。这些模型调用计入下面的余额差
- Run 2 的 24 小时授权窗口包含了 Run 1 的故障时段（17:08–17:38Z），报告里把它记为「更早的同类 episode」。两次 Run 共用一个实验环境、间隔 70 分钟，这是场景设计的固有结果，不是额外注入

## 时间线（UTC）

| 时间 | Run 1（`f3-3b-run1`） | 时间 | Run 2（`f3-3b-run2`） |
|---|---|---|---|
| 17:00:21 | preflight 七项通过 | 18:11:01 | preflight 七项通过 |
| 17:00:23 | 发布 R，`rel-20261024170023` | 18:11:03 | 发布 R，`rel-20261024181103` |
| 17:00:24–25 | 新 RS `checkout-75c8bcd746` 建出，新 Pod Ready 17:00:25，旧 Pod Killing 17:00:25 | 18:11:04–05 | 新 RS `checkout-5f6d95b5c7`，新 Pod Ready 18:11:05 |
| 17:07:24 | flagd ConfigMap 注入回执已校验 | 18:18:03 | flagd ConfigMap 注入回执已校验 |
| 17:08:00 | flagd 首次确认 `100%` | 18:18:25 | flagd 首次确认 `100%` |
| 17:13:44 | 投递 2 firing（startsAt 17:12:54Z）→ `created`，事故 `325082d5`，Run `dbf02dca` | 18:26:43 | 投递 4 firing（startsAt 18:25:54Z）→ `created`，事故 `01a65994`，Run `1a9b37f1` |
| 17:13:45 | 追问 200，代际 0→1 | 18:26:46 | 追问 200，代际 0→1 |
| 17:13:46 | 起 worker；17:13:47 领取 | 18:26:47 | 起 worker；18:26:48 领取 |
| 17:17:57 | Run `completed`，报告发布（12 轮） | 18:31:26 | Run `completed`，报告发布（11 轮） |
| 17:38:14 | flagd 确认恢复 `off`；驱动 `finished ok:true` | 18:48:53 | flagd 确认恢复 `off`；驱动 `finished ok:true` |
| 17:41:43 | 投递 3 resolved → `resolved_attached` | 18:54:43 | 投递 5 resolved → `resolved_attached` |
| 约 17:42:30 | 冻结摘要后 UI `close`，代际 1→2 | 约 18:55:20 | 冻结摘要后 UI `close`，代际 1→2 |

R 到 flagd 生效：Run 1 7.6 分钟、Run 2 7.4 分钟；新 Pod Ready 到 flagd 生效：7.6 / 7.3 分钟（D5-a (iii) 的门槛是错误起点晚于 rollout 完成 ≥5 分钟，是否由证据佐证由审查者判）。flagd 生效到告警投递：5.7 / 8.3 分钟。

## 每次 Run 的结果

| 项 | Run 1 | Run 2 |
|---|---|---|
| 驱动结局 | `finished ok:true`，reason null | `finished ok:true`，reason null |
| 事故 / Run | `325082d5-135b-55c1-90e9-02b436302375` / `dbf02dca-b980-5f66-b9e7-5c62b200f411` | `01a65994-0597-5087-872b-84db8e9fe4e5` / `1a9b37f1-3bfd-5a04-aa6f-0ad3b8f83530` |
| 每事故 Run 数、投递 outcome | 1 个 Run；firing `created`，resolved `resolved_attached`，无 `attached` | 同左 |
| 追问可见 | 输入 1 条（`follow_up`，actor `f3lab`），`ctx0:round-1` 起 `input_watermark=1` | 同左 |
| Run 状态 | `completed`，`assessment_status=completed`，`conclusion=partial` | 同左 |
| claim 分组 | fact 13、counter_evidence 2、hypothesis 2 | fact 16、rejected_hypothesis 3、hypothesis 2、counter_evidence 1 |
| 模型轮次（含结论轮）/ 上限 | 12 / 100 | 11 / 100 |
| 工具请求 | 21：metrics_range_query 18（ok 17、no_data 1），traces_search 2（ok 1、error `RESULT_TOO_LARGE` 1），get_metric_names 1 | 26：metrics_range_query 19（ok 18、no_data 1），traces_search 3（ok 2、`RESULT_TOO_LARGE` 1），get_metric_names 2，get_label_values 1，read_evidence 1 |
| trace | [Run 1 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/fddc0e84-a2c2-4978-8098-649395630411/r/00000000-0000-0000-3f27-476a5c84f334?poll=true) | [Run 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/fddc0e84-a2c2-4978-8098-649395630411/r/00000000-0000-0000-936e-285b738d4089?poll=true) |

报告全文、结构化 claim 分组、被引证据的 view、ledger 时刻（R、新 Pod Ready、flagd 生效与恢复）都在各自 summary 的 `report`、`projection.reports`、`cited_evidence_views`、`driver_ledger`、`rollout` 里，供审查者按 rubric 判定（预审 R-6）。

## 第 6 步：请求检查与代理复测

- 请求检查（U9-a）：两次 Run 共 47 次工具请求，逐条列在 summary 的 `tool_requests`（工具、动词、参数、状态、目标）与 `audit.query_entries`（审计时间线 query 条目，来自 `opspilot_tool_charges`）。动词只有 `query`、`list`、`get`，全部属于 `READ_ONLY_VERBS`，注册 `read_only=true`；目标全部是 `m0-otel-20260909`；`read_evidence` 是内部读，不出站。两次各有 1 条 `refused_operations`（`traces_search` `RESULT_TOO_LARGE`，`source_contact=confirmed`），是结果过大，不是写请求。审计 query 条目数等于工具请求数（21、26）
- 人工与系统控制：每次 Run 有一次人工 `follow_up`、一次 `system` 的 `auto_authorize_observation`，冻结后另有一次人工 `close`（未进 summary，时刻见时间线）
- 代理复测（R10，Run 1 之前 16:38:36–16:40:19Z）：`python -m scripts.f7_authz_probe`，结果 [f7-authz-probe.json](f7-authz-probe.json) `ok: true`，136/136 符合。investigator、observer、lab 三个账号：6 个读接口 GET 200（含 `/api/v1/label/__name__/values`），HEAD 405，OTLP、remote write、admin、lifecycle、POST 查询、OPTIONS 与 5 种路径变形 403；匿名 401；写探测前后无新样本。文件 sha256 `ae2110c9…`，与 2026-10-16 的 F7 探测逐字节相同（探测输出不含时间戳）
- 说明：探测只覆盖 `/api/v1/label/__name__/values`；Run 2 的 `get_label_values` 走 `/api/v1/label/<label>/values`，由代理同一条 location 规则放行（`scripts/kind_lab.py:370`），本次 Run 里该请求返回 ok

## 第 1 步：投影数据

- 两次 Run 的验收投影（`opspilot.acceptance_investigation.investigation_evidence`，在只读快照事务里读）全部写入各自 summary 的 `projection.observations`：Run 1 20 条（ok 19、no_data 1），Run 2 25 条（ok 24、no_data 1）。逐条核对：`source`、`target_id`、`evidence_id`、`query`、`window`、`tool`、`tool_version`、`registry_revision`、`tool_registry_revision` 都非空，`resolvable=true`，`missing_fields` 为空；`freshness_seconds` 为 None 的条目 Run 1 2 条、Run 2 4 条（显式 unknown，R2）。这是数据收集，第 1 步的断言由独立测试作者对冻结 summary 执行
- 旧库兼容（预审 R-7）：`poa-f3-1/tmp/pg55611`（PR-1 测试库）只读查询结果为 `alembic_version=0017`，`opspilot_evidence` 与 `opspilot_runs` 都是 0 行，不含任何旧投影修订的真实 Run。本次新库也只有当前修订（`m1-01-tool-view-v10`）。**无可用归档库**；按 R-7 引用 PR-1 的合成旧行测试（`tests/integration/test_f3_pr1_projection_acceptance_postgres.py`）

## 失败轮与费用

- 失败轮：无。中途放弃：无。Q6 重跑：未用
- DeepSeek 余额：开跑前 92.51 CNY（16:3xZ 与 17:00Z 两次读数相同），Run 1 结束后 92.25 / 92.13，会话结束 18:57Z 为 91.67，19:00Z 起稳定在 91.53 CNY。**差额 0.98 CNY**，包括两次调查 Run、一次完成的复盘生成和两次被中断的复盘尝试。余额扣减有延迟，且同一账户在此期间可能被其他会话使用，所以这是本会话费用的上界
- 进程：本次启动的 web、worker、flagd 端口转发都按 PID 核实命令行后 SIGTERM 停止（worker 两次都在 30 s grace 后退出，中断的是复盘尝试）；一次性 PG `pg_ctl stop`。结束时 flagd 全部 off（`1ee5c025`），无 `fault-active.json`，`ALERTS` 为空，checkout p95 94 ms

## #646 重跑：第 5 步两次真实 Run（2026-11-16，`prompt-replay-candidate-a6b4dfd02030`）

#646 把 L2 报告升为 `m0-report-v3`，基础档位 `prompt_revision` 由 `bef70649a3a8` 变为 `a6b4dfd02030`。用户 2026-11-15 裁决「F3 证据 = A」：在租机上用新提示词重跑第 5 步的两次 Run。上面 10-24 的两份冻结摘要移到 [prompt-bef70649a3a8/](prompt-bef70649a3a8/)，内容未改。本节两次 Run 的摘要在 #676 之后移到了 [prompt-a6b4dfd02030/](prompt-a6b4dfd02030/)（见文末一节）。

- 主机：Vultr，Ubuntu 24.04，8 vCPU / 32 GB；kind lab 由 `install_linux.sh` 新装（05:59Z）。代码 `feature/646-eval-root-cause` `c772eae7`（含 PR-A `fa7c72bf`），运行期间未改代码。版本 `prompt-replay-candidate-a6b4dfd02030` / `otel-demo-a3a95a0f888e` / `ctx-ctx-policy-v1-a30e65ea52f4`，与当前代码的 `otel_demo_versions()` 三项相同，因此不需要等价证明。
- 拓扑（Linux 宿主进程，参照租机手册 4b）：PG 17 在 55431（`postgres_lab start`、`make migrate` 到 `0024_trace_outbox`、`runtime-roles`），另建只读角色 `f3_lab_reader`（只有 SELECT，`default_transaction_read_only=on`）。web `OPSPILOT_BIND=0.0.0.0:8080`：驱动走 127.0.0.1，Alertmanager 走 kind 网关 172.18.0.1；ufw 只放行 172.18.0.0/16 → 8080。lab 以 `OPSPILOT_LAB_HOST_ADDRESS=172.18.0.1` 重新渲染 webhook。目标身份文件由 `kind_lab.py control-secrets --secrets-dir <私有目录>` 写出。worker 设 `OPSPILOT_TOOL_PROFILE=otel-demo`、`OPSPILOT_TRACE=lab`、`OPSPILOT_HEALTH_BIND=`（空值；见下「偏离」）。flagd 端口转发 18013 → `svc/flagd:8013`，`--flag-method Int`。trace 项目 `opspilot-lab-f3-646-live-20261116`，开跑前用 `lab_evidence.prepare_lab_project` 设为 `longlived`。
- 环境垫片：驱动的 swap 前提调用 macOS 的 `sysctl -n vm.swapusage`。Linux 上用 `~/shim/sysctl` 按 `/proc/meminfo` 输出同样格式，其他参数交给真 sysctl，没有改代码。阈值用默认 2048 MiB，实际全程空闲 8191 MiB。
- 收集：每次 Run 结束后、关闭事故前，`scripts/run_freeze.py` 冻结 `run-N/summary.json`，回读 trace。仓库里这份是 run_freeze 的形状，另加一个顶层 `cited_evidence_views`：它由 `run.cited_evidence[].stored.view` 按 evidence_id 机械展开，是 `test_step5_live_cited_views_are_stable_under_current_redaction` 要读的键。10-24 那份是另一种形状，本次没有重建。第 5 步的 summary 由 `f3-step5-competing-cause/collect.py` 在 resolved 附加、UI `close` 之后，用只读角色生成，`lab_readings` 是本次每分钟的连续读数。

### 时间线（UTC）

| | Run 1（`f3-646-r1b`） | Run 2（`f3-646-run2`） |
|---|---|---|
| preflight 七项通过 / 发布 R | 08:52:20，`rel-20261116085220` | 10:02:43，`rel-20261116100243` |
| flagd 首次确认 `100%` | 09:00:44（R + 8.4 分钟） | 10:10:03（R + 7.3 分钟） |
| 告警开出事故与 Run | 事故 `eeceb91c`，Run `e6fa260e`，授权窗终点 09:06:04 | 事故 `2e351ada`，Run `33922153`，授权窗终点 10:16:04 |
| 追问 / 起 worker | 追问 200，代际 0→1；首个 worker 09:06:12 启动即退出，09:24:46 重起后领取 | 追问 200，代际 0→1；10:16:14 起 worker 并领取 |
| Run 完成 | 09:27 前后 `completed` | 10:20 前后 `completed` |
| 故障恢复 / 驱动结束 | 09:30:44 / 09:31:18 `finished ok:true` | 10:40:03 / 10:41:15 `finished ok:true` |
| resolved 附加 → UI `close` | `resolved_attached` → 09:34 close（代际 1→2） | `resolved_attached` → 11:31 close（代际 1→2） |

### 每次 Run 的结果

| 项 | Run 1 | Run 2 |
|---|---|---|
| 报告 | `m0-report-v3`，`completed / supported` | `m0-report-v3`，`completed / supported` |
| `root_cause` | FeatureFlag / otel-demo / `paymentFailure`（3 条证据） | Deployment / otel-demo / `flagd` |
| claim 分组 | fact 12、counter_evidence 2、rejected_hypothesis 2、hypothesis 1、recommendation 1 | fact 12、counter_evidence 2、rejected_hypothesis 2、hypothesis 2、recommendation 1 |
| 模型轮次 / token | 12 轮；prompt 432,160（命中 357,504）、completion 30,775 | 17 轮；prompt 1,003,300（命中 894,720）、completion 36,715 |
| 工具请求 | 22：metrics_range_query 17、traces_search 2、get_metric_names 1、get_label_values 1、read_evidence 1 | 30：metrics_range_query 20、traces_search 4、get_label_values 3、get_metric_names 2、read_evidence 1 |
| 投递 outcome | `created`、`resolved_attached` | 同左 |
| trace | [Run 1 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/5548ae55-495b-4b22-b090-47c6e0e508df/r/00000000-0000-0000-4bea-6f93103e15dd?poll=true) | [Run 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/5548ae55-495b-4b22-b090-47c6e0e508df/r/00000000-0000-0000-6d36-3396ac77859a?poll=true) |

确定性判定：冻结的判定函数（gate1–4、scenario_shape、step1、step2、step6）两次都为空，`tests/acceptance/test_f3_step5_real_runs.py` 15 条全过（本机定向运行）。rubric 5–8 和第 2 步 summary 一致性要两名独立审查者判，**本次未做**，见 [第 5 步判定](../f3-step5-competing-cause/run.md)。

### 偏离与失败（照实记录）

- **第一次 Run 1 尝试 `f3-646-run1` 失败（07:19:26Z，退出 5，`FAULT_FAILED`）**：flagd 端口转发没有起来（执行者的命令链错误），注入前检查报 `ValueError`。没有注入，没有告警、事故、Run 或模型调用；07:12:25Z 的 checkout 发布真实发生，并按前提 ⑥ 计入 70 分钟窗口。这次没有产生 Run，按 U-A 换名 `f3-646-r1b` 重跑。ledger 保留在主机。
- **Run 2 第一次启动早了几十秒**（10:01:36Z，退出 3，`PRECONDITION_RECENT_ROLLOUT`）：r1b 的发布还没满 70 分钟；没有发布，也没有写 ledger。
- **r1b 晚领取约 19 分钟**：首个 worker 与 web 争 `OPSPILOT_HEALTH_BIND` 默认的 127.0.0.1:9464（errno 98），启动即退出。worker 设空值后 09:24:46Z 重起。追问在领取前送达（D1-a 顺序满足）；授权窗终点 09:06:04Z 早于晚领取和 09:30 的故障恢复，所以模型能读到的数据与准时领取相同。rubric 与 PR-3a 合同对领取时刻没有要求。
- 同一台主机同时跑了 ITBench-Lite 集成批（lead 批准，批记录 `docs/evidence/itbench-lite-sre-lite-batch-646/run.md`（PR-B）），trial 都避开了两次 Run 的调查窗口（告警到报告）。Run 2 的故障窗口与最后一个 trial（约 10:08–10:14Z）重叠。每分钟读数：checkout p95 最高 35 ms，swap 全程 8191 MiB 空闲，可用内存不低于 25.7 GB。
- 两次 Run 的 24 小时授权窗都包含更早的 checkout 发布：lab 安装 05:59Z、失败尝试 07:12Z，Run 2 还包含 r1b 的 08:52Z 发布与 09:00–09:30 的故障，这是同一 lab 上连续跑的固有结果。追问只陈述本次发布与时刻（D4-a）。
- 第 6 步代理探测没有在本窗口重跑，测试读的仍是 10-24 冻结的 `f3-step6-authz-probe/probe.json`。

### 费用

DeepSeek：两次 Run 的 token 见上表。单价同集成批记录 `docs/evidence/itbench-lite-sre-lite-batch-646/run.md`（PR-B，沿用 `m3-soak-2026-11-07/cost-primary.json`）：Run 1 调查在北京时间 17:24 前后，按高峰价，约 CNY 0.41；Run 2 在 18:16 之后，按闲时价，约 CNY 0.27；合计约 **CNY 0.68**。复盘生成另有少量调用，未计入。账户共用，余额差不作依据。

## #676 重跑：第 5 步两次真实 Run（2026-11-16，`prompt-replay-candidate-14eb2a0a6afd`）

#676 改了共用 L2 段（认定原因的 claim 是 hypothesis 时 conclusion 写 partial；summary 措辞）和 INVESTIGATION_METHOD（告警是入口，不是范围），基础档位 `prompt_revision` 由 `a6b4dfd02030` 变为 `14eb2a0a6afd`（[任务记录](../../tasks/2026-11-16-676-investigation-guidance.md) D9）。按 D11，#646 的两份摘要移到 [prompt-a6b4dfd02030/](prompt-a6b4dfd02030/)，内容未改；`run-{1,2}/summary.json` 现在是本节的两次 Run。

- 主机与拓扑同上一节（同一台 Vultr 主机；kind lab 由 `kind_lab.py up` 从停止状态重新拉起，health ok，frontend span rate 2.6/s；PG 55431 与只读角色沿用）。代码 `feature/676-investigation-guidance` `e8b98507`（PR #677），运行期间未改代码。版本 `prompt-replay-candidate-14eb2a0a6afd` / `otel-demo-a3a95a0f888e` / `ctx-ctx-policy-v1-a30e65ea52f4`，与当前代码的 `otel_demo_versions()` 三项相同。worker `OPSPILOT_HEALTH_BIND=` 空值；flagd 转发 18013；swap 垫片同上；trace 项目 `opspilot-lab-f3-676-live-20261116`（`longlived`）。收集方式同上一节。

### 时间线（UTC）

| | Run 1（`f3-676-run1`） | Run 2（`f3-676-run2`） |
|---|---|---|
| preflight 七项通过 / 发布 R | 22:32:04，`rel-20261116223203` | 23:42:48，`rel-20261116234248` |
| flagd 生效 `100%` | 22:39:57（R + 7.9 分钟） | 23:49:57（R + 7.2 分钟） |
| 事故与 Run / 授权窗终点 | 事故 `53f5ccd3`，Run `a10cf725`，22:45:04 | 事故 `6ece32c3`，Run `f3b73c46`，23:56:04 |
| 追问 / 起 worker | 追问 200，代际 0→1；22:45:05 起 worker 并领取 | 追问 200，代际 0→1；23:56:09 起 worker 并领取 |
| 故障恢复 / 驱动结束 | 23:09:58 / 23:10:40 `finished ok:true` | 00:19:57 / 00:20:31 `finished ok:true` |
| resolved 附加 → UI `close` | 约 23:18 close（代际 1→2） | 约 00:25 close（代际 1→2） |

### 每次 Run 的结果

| 项 | Run 1 | Run 2 |
|---|---|---|
| 报告 | `m0-report-v3`，`completed / partial` | `m0-report-v3`，`completed / partial` |
| `root_cause` | Deployment / otel-demo / `checkout` | Deployment / otel-demo / `payment` |
| claim 分组 | fact 15、counter_evidence 2、rejected_hypothesis 2、hypothesis 2 | fact 14、counter_evidence 3、rejected_hypothesis 3、hypothesis 2 |
| 模型轮次 / token | 10 轮；prompt 355,666（命中 279,424）、completion 27,691 | 10 轮；prompt 536,374（命中 437,632）、completion 45,418 |
| 工具请求 | 20：metrics_range_query 13、traces_search 3、get_label_values 2、get_metric_names 1、read_evidence 1 | 24：metrics_range_query 18、traces_search 3、read_evidence 2、get_metric_names 1 |
| 投递 outcome | `created`、`resolved_attached` | 同左 |
| trace | [Run 1 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/42c71272-664a-43c4-907d-373ee323b6b1/r/00000000-0000-0000-6908-046f84d36622?poll=true) | [Run 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/42c71272-664a-43c4-907d-373ee323b6b1/r/00000000-0000-0000-ba18-3105ba54a320?poll=true) |

确定性判定（gate1–4、scenario_shape、step1、step2、step6）两次都为空；`tests/acceptance/test_f3_step5_real_runs.py` 15 条全过（本机定向运行）。rubric 5–8 由两名独立审查者判，本节不含判定。

照实记录给审查者（不是判定）：
- Run 1 的 summary 写的是「The most likely cause is the checkout configuration-only rollout…」，并注明这个联系只靠时间先后和「没有别的变更」，还被约 10 分钟的间隔削弱了。认定部署的那条 claim 是 hypothesis；没有任何 claim 否定这次部署。
- Run 2 的主假设是 payment，把 checkout 部署列为「未排除的备选」。rejected_hypothesis 否掉的是 checkout 重启、其他依赖、延迟，没有一条否掉部署。两次都没有点名 flagd 的 `paymentFailure`（base 档位没有 K8s/ConfigMap 工具）。#646 那两次 Run 是 supported，根因分别是 paymentFailure 和 flagd。

### 偏离与说明

- 同一台主机先后跑了 ITBench-Lite 冒烟（lab 停着）和集成批 `lite-batch-676`（lab 开着）。集成批各段都避开了 Run 1 的调查窗口，并且在 Run 2 发布前结束（批记录 `docs/evidence/itbench-lite-sre-lite-batch-676/run.md`）。每分钟 lab 读数：checkout p95 最高 36 ms，swap 全程空闲。
- 两次 Run 的 24 小时授权窗里都有更早的 checkout 发布（#646 那几次、lab 安装）。Run 2 还包含 Run 1 的发布与故障。这是同一 lab 上连续跑的固有结果。
- Run 1 关闭后停 worker 时，复盘尝试在 30 秒 grace 后被中断，和 10-24 那次相同。
- 第 6 步代理探测没有重跑。

### 费用

DeepSeek：北京时间 11-17 06:45 与 07:56，闲时，两次合计约 CNY 0.48（命中 717,056、未命中 174,984、输出 73,109；单价同 `m3-soak-2026-11-07/cost-primary.json`）。
