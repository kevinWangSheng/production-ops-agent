# M1-04 第 3 步：告警 Run 时间框与锚点真实端到端运行

- 日期：2026-10-10 14:11–14:26Z；分支 `feature/m1-04-alert-window`。进程启动时 HEAD `8daaedd` 加未提交的 `opspilot/acceptance_alert.py` r7 改动（`git diff 8daaedd -- opspilot` sha256 前 16 位 `af8d4786038e74dd`）；运行期间实现者把同一改动提交为 `56c4007`（`git diff 8daaedd 56c4007 -- opspilot` 同一哈希），所以实际运行的 `opspilot/` 与 `56c4007` 相同。摘要里的 `commit` 字段取于冻结时，即 `56c4007`
- 合同：[任务记录](../../tasks/2026-10-10-m1-04-alert-intake.md) r5 J1–J6 + r6（+ r7 投影字段）
- 冻结摘要：[live-runs/bc327b19-0d42-502c-ad60-54cd32b78da0/summary.json](live-runs/bc327b19-0d42-502c-ad60-54cd32b78da0/summary.json)；原始 ledger（resolved 前后两次库投影 + trace 回读）不入库，sha256 `922e67e8…7b464a12`、925278 字节（ADR-0006）
- 类别：有界真实软件环境运行 + 真实模型调用。只用 kind lab 的故障钩子驱动，告警由真实 Prometheus/Alertmanager 产生；不是生产证明

## 环境

- 实验环境：kind `opspilot-m1`，Alertmanager v0.28.1（chart 1.24.0），webhook 目标 `http://192.168.5.2:8080/intake/alertmanager`。注入前（14:11:47Z）frontend 的 2 分钟 span rate 为 1.78/s，未重启 otel-collector。
- PostgreSQL 17：本 worktree `tmp/pg-live`，端口 55493，库 `m0_budget`，新建空库后 `make migrate` → `0010_alert_intake`。运行结束 `pg_ctl stop`。
- 进程：`python -m opspilot.web serve`（127.0.0.1:8080，`OPSPILOT_TOOL_PROFILE=otel-demo`，`OPSPILOT_EVENT_TOKENS` 来自 `workbench-alertmanager.env`（只含哈希），`OPSPILOT_TARGET_IDENTITIES` 与第 2 步相同：一条 `m0-otel-20260909`，`match.labels = {cluster: opspilot-m1, namespace: otel-demo, service: checkout}`）；`python -m opspilot.worker_main`（同 profile，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号）。worker 版本：`prompt-replay-candidate-bd28790117a0` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-d61d6f2a3b04`。
- trace：round `m1-04-alert-window-20261010`，project `opspilot-lab-m1-04-alert-window-20261010`，运行前经 `lab_evidence.prepare_lab_project` 建立并回读 `trace_tier=longlived`。
- 防循环上限：产品默认每 Run 100 次模型请求，未另加。

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:12:14 | web 与 worker 启动；worker 日志 `trace export enabled mode=lab` |
| 14:12:22 | `kind_lab.py fault inject --experiment-id m1-04-step3-live`（paymentFailure 100%，ConfigMap `1ee5c025…` → `e47d27c2…`，`verified: true`） |
| 14:17:54 | 告警 `startsAt`（原文 `14:17:54.365Z`） |
| 14:18:04 | 工作台收到 firing，`200 OK`；建事故 `66083747-403a-561e-8e71-1d17e844c31d` 与 Run `bc327b19-0d42-502c-ad60-54cd32b78da0` |
| 14:18:05–14:19:18 | worker 调查：6 次模型请求、15 次工具调用，`status=published` |
| 14:19:37 | `fault restore`（ConfigMap 回到 `1ee5c025…`，fault log `verified: true`） |
| 14:19:53 | resolved 前的库投影快照 |
| 14:24:04 | 工作台收到 resolved，`200 OK` |
| 14:24:16 | 停 worker、web；冻结后停 PG |

## 结果

| 检查 | 预期 | 实际 |
|---|---|---|
| 输入版本（J3） | 告警 Run 的 `run_input.version` 为 v3 | 通过。`opspilot-investigation-input-v3` |
| 锚点（J2、J3） | `scope_facts.alert_starts_at` = 规范化 `startsAt`，`adjusted = null`，保留原文 | 通过。`{"anchor": "2026-10-10T14:17:54+00:00", "adjusted": null, "original": "2026-10-10T14:17:54.365Z"}` |
| 授权框（J1） | `time_policies[0].window` = [received_at − 24h, received_at] | 通过。received_at `14:18:04.769659Z`，截到秒得 `14:18:04Z`；框为 `2026-10-09T14:18:04Z` – `2026-10-10T14:18:04Z` |
| 时间策略字段（J3 + r6） | `reference_rule = response_received_at`、`anchor_rule = alert_starts_at`、`anchor`；`default_query_window` = [max(anchor − 1h, 框起点), 框终点] | 通过。`reference_rule = "response_received_at"`，`anchor_rule = "alert_starts_at"`，`anchor = 14:17:54Z`；`default_query_window = [2026-10-10T13:17:54Z, 2026-10-10T14:18:04Z]`（anchor − 1h 晚于框起点，所以取 anchor − 1h） |
| 默认查询窗用于没给 start/end 的调用（J4） | 这类调用实际发出的查询窗 = `default_query_window` | 模型驱动的运行中未覆盖：15 次工具调用都由模型显式给了 `start`/`end`，一次也没有省略。逐次的查询窗见下表。由事后的无模型探针补证，通过，见「J4 探针（无模型）」 |
| 显式查询窗在框内（J4） | 显式窗必须落在框内 | 通过。13 次有查询窗的调用全部在框内；另 2 次 `INVALID_PARAMS` 拒绝的原因是 PromQL 里 5m 区间选择器放在 3 分钟查询窗中，不是窗越框；没有越框拒绝 |
| 报告与时间引用 | 发布（或交接），time scope 引用有效 | 通过。Run `completed`，`outcome_from_durable`：`decision = report_available`，`handoff_reasons = []`，11 个证据 ID，`actions = [read_only_query]`。报告 `assessment_status = completed`、`conclusion = supported`；17 条 claim 的 `time_scope_ref` 全是 `policy-window-1`，没有 `MISSING_TIME_SCOPE_REF`（r6 的顾虑没有出现） |
| resolved（C、E8） | `resolved_attached`，lifecycle 不变 | 通过。delivery 2：`resolved` / `resolved_attached`，同一事故、同一 fingerprint `83f7541ae0105d42`；resolved 前后投影都是 `lifecycle = open`，`run_ids` 只有一个，`observation_session_ids = []`；annotation 版本 1 → 2 |
| trace | LangSmith 可见完整 span 树 | 通过。[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/ab86fc17-c8b8-42bd-bd28-1d7cdf947e0e/r/00000000-0000-0000-b9e3-2847aa0ba2e5?poll=true)，`read_back = found`，OTel trace `04fd39b4db1dd31e76e48fc72af4a478`，丢弃 span 0；子 run llm 6、tool 15，与库中 `budget_spent = 6`、`tool_operations_used = 15` 一致；token 153472 输入 / 17979 输出 |

各次工具调用（来源：step 行的 assistant `tool_calls` 参数与对应 `tool_results[].result.query`；UTC，日期均为 2026-10-10）：

| 轮 | 工具 | 模型给的 start – end | 实际查询窗 | 状态 |
|---|---|---|---|---|
| 1 | metrics_range_query ×2 | 13:17:54 – 14:18:04 | 同左 | ok |
| 1 | traces_search | 13:17:54 – 14:18:04 | 同左 | RESULT_TOO_LARGE |
| 2 | traces_search | 14:15:00 – 14:18:04 | 同左 | RESULT_TOO_LARGE |
| 2 | metrics_range_query ×2 | 14:15:00 – 14:18:04 | （发出前拒绝） | INVALID_PARAMS |
| 3 | metrics_range_query ×2 | 14:08:04 – 14:18:04 | 同左 | ok |
| 3 | traces_search | 14:16:30 – 14:18:04 | 同左 | ok |
| 4 | metrics_range_query | 13:48:04 – 14:18:04 | 同左 | ok |
| 4 | metrics_range_query | 14:08:04 – 14:18:04 | 同左 | ok |
| 4 | traces_search | 14:16:00 – 14:18:04 | 同左 | ok |
| 5 | metrics_range_query ×3 | 13:18:04 – 14:18:04 | 同左 | ok |

第 1 轮模型给的窗与 `default_query_window` 逐字相同，说明它从证据上下文的时间策略里读到了这个窗并照抄，而不是省略参数让网关取默认值。J4 的默认分支由下文的无模型探针补证。

报告要点（模型判断）：告警因 checkout PlaceOrder 服务端错误比例越过 0.5（14:15:54Z 为 0.591、14:17:54Z 为 1.0），总流量没有变化；失败一跳是 payment `Charge`（「Payment request failed. Invalid token」，`charge.js:37`），从 14:15:34Z 起。

## J4 探针（无模型）

- 时间：2026-10-10 约 14:28Z（运行结束后）。lead 决定用这种方式补 J4 证据。不调用模型，无 trace。
- 做法：脚本只读启动本次的 PG（会话设 `default_transaction_read_only`），取出 Run `bc327b19…` 已提交的输入。然后按 `otel_demo_executor_factory` 的写法组装产品执行器：同样的 `ToolRegistry(_registrations())`、`TargetRegistry([_target(config)])`、`OtelDemoTransport`（`OtelDemoConfig.from_env`，investigator 账号）、`scope_window(input)`、`scope_default_query_window(input)`，scope deadline 取 Run 的 `deadline`。有一处不同：该 Run 已完成、没有租约，所以按租约记账的 `DurableToolLedger`、证据落库与控制读取换成了内存实现，证据库没有任何写入。探针在 transport 的 `fetch` 和 HTTP `_get` 两处记录请求，不改变它们的行为。
- 两次 `metrics_range_query`，`ToolRequest.window` 都是 loop 的常规值（完整框 `2026-10-09T14:18:04Z` – `2026-10-10T14:18:04Z`），表达式都是 `sum(rate(traces_span_metrics_calls_total{service_name="checkout",span_name=~".*PlaceOrder"}[1m]))`，`step_seconds = 60`。探针前（14:28Z）frontend rate 为 1.78/s，故障已恢复。

| 调用 | 参数 | 结果 |
|---|---|---|
| 不给 start/end | 无 `start`/`end` | 通过。transport 收到 `default_window = [13:17:54Z, 14:18:04Z]`，等于 `default_query_window`。实际发往 Prometheus 的 URL 是 `query_range?…&start=1791638334.000&end=1791641884.000&step=60`，即 13:18:54Z – 14:18:04Z：起点是窗起点加 `[1m]` 回看，这是 `_metrics` 既有的取点规则，保证每个点的区间都在窗内。返回视图的 `window = [13:17:54Z, 14:18:04Z]`。状态 `no_data`，`source_contact = confirmed`：Prometheus 被访问到，但这个 `[1m]` 表达式没有返回序列。本探针只判定查询窗，不判定数据 |
| 显式给框外窗 | `start = 2026-10-09T12:18:04Z`，`end = 2026-10-09T13:18:04Z`（框起点前 2h–1h） | 通过。`error` / `INVALID_PARAMS`，`source_contact = none`。transport 的 `fetch` 被调用一次，但 HTTP `_get` 调用 0 次，所以没有请求到达 Prometheus |

- 原始输出不入库（本机临时目录 `j4probe.json`），它的关键字段都已写进上表。

## 观察到但不属本步判定

- 实验环境指标稀疏：报告的 gaps 写到 checkout 的 PlaceOrder 序列在 13:25Z 到 14:13Z 之间几乎没有返回样本。F9 那次运行（同一实验环境，14:03Z）也看到 13:26Z–14:01Z 断档、计数器重置。注入前 frontend rate > 0，本次判定不受影响；原因未核查（未确认）。
- summary.json 的 `versions.worker_versions` 为 `null`，原因是采集脚本从 `run_input.versions` 取值，而 v3 输入里没有这个键；与产品行为无关。真实版本（库中 `run.versions`，与 worker 启动日志一致）：`prompt_revision = prompt-replay-candidate-bd28790117a0`，`context_policy_revision = ctx-ctx-policy-v1-a30e65ea52f4`，`tool_schema_revision = otel-demo-d61d6f2a3b04`。
- 工作台事故页（Basic Auth，200）显示「Affected service」与 resolved 的「Recorded only」说明；页面上锚点字段的呈现不属本步合同，未逐项核对。

## 失败与未覆盖

- 模型驱动的运行没有走到 J4 的默认查询窗分支（见上）。按 lead 决定改用无模型探针补证（见下节）。探针证明网关与 transport 的行为，不证明模型会省略参数。
- 没有重试，没有实验环境动作（计数器正常，未重启 otel-collector）。
- 未覆盖：`adjusted = future/before_frame` 的截断（真实告警的 startsAt 早于接收 10 秒，落在框内）、超时续开与 fresh 回退（J5）、重复通知。均由合同测试覆盖。

## 费用

DeepSeek 余额 4.15 → 4.03 CNY（差 0.12 CNY），取于 14:12Z 与 14:24Z。F9 运行结束（4.29）到本次开始（4.15）之间余额又降了 0.14 CNY，说明同一账户有其他会话在并发调用，所以 0.12 CNY 是本次费用的上界。token：153472 输入 / 17979 输出（LangSmith 根 run 聚合）。

## 凭据

证据中无 token、口令或 API key：gitleaks 8.30.1 `dir` 扫描本目录 `no leaks found`；另以 DeepSeek/LangSmith key、webhook token、Prometheus investigator 口令、实验 UI 口令的原值逐一在本目录做字面匹配，均 0 次（不打印原值）。
