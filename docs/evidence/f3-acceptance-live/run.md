# F3 PR-3b kind 真实会话：第 5 步 2 次竞争原因 Run、第 6 步代理复测、第 1 步投影数据

- 日期：2026-10-24（UTC 16:23–18:57），kind-opspilot-m1 / otel-demo；分支 `feature/F3-acceptance`，代码 `e1ea7cd`（起点 main `3c18bdb`），运行期间未改产品代码
- 依据：[F3 任务记录](../../tasks/2026-10-17-f3.md)；合同 r1 U3-a、U4-a、U9-a；[PR-3a 合同](../../tasks/2026-10-22-f3-pr3a-contract.md) D1-a、D4-a、D5-a、R-1、R-6；PR-3b 预审（仓库外 `~/.agent/handoffs/opspilot-prereview-f3-3b-2026-10-24.md`）§2、R-1、R-2、R-4、R-7、R-10 与 U-A 裁决；[M2 授权](../../tasks/2026-10-24-m2-remaining.md) Q6 与 runbook
- 冻结摘要：[run-1/summary.json](run-1/summary.json)、[run-2/summary.json](run-2/summary.json)；代理探测 [f7-authz-probe.json](f7-authz-probe.json)。原始 ledger 在主仓库 `tmp/lab-ledgers/f3-competing-cause/f3-3b-run{1,2}/`（驱动的 plan、release、fault.jsonl、follow-up、summary，加本次收集的 `db-extract.json` 与 `rollout.json`），不入库，sha256 记在各 summary 的 `raw_ledger`、`rollout.ledger_sha256`、`driver_ledger.files`
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

- **swap 前提用 `--swap-min-free-mib 512`，不是默认 2048。** 16:41Z swap 余量 901 MiB（总 9216 MiB），开跑前 1.0–1.3 GiB。内存大头是 colima VM 和其他会话的应用，按规则不能停。2048 是 PR-3a 自定的可逆默认，不是验收步骤；F11 PR-4b 在 367 MiB 下跑过且 p95 未升高。开跑前已发消息告知 lead，并自定「p95 > 300 ms 就不开跑」。实际读数：Run 1 前（16:58Z）p95 97.5 ms、swap 余量 1232 MiB；Run 2 前 p95 97.5 ms、swap 余量 977 MiB；全程内存空闲 37–50%，p95 在 48–188 ms 之间波动（低流量下 5 分钟分位数抖动大），没有出现 runbook 所说的 0.5–1.8 s
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
