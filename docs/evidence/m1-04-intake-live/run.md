# M1-04 第 2 步：Alertmanager 接入真实端到端运行

- 日期：2026-10-10 12:54–13:08Z；分支 `feature/m1-04-alertmanager-intake`，commit `495004c`（干净工作区，本目录为运行后新增）
- 合同：[任务记录](../../tasks/2026-10-10-m1-04-alert-intake.md) r1–r4（A–D、E1–E15、I1–I12）
- 冻结摘要：[live-runs/99f53e89-c53e-59b6-a206-7f019d69167f/summary.json](live-runs/99f53e89-c53e-59b6-a206-7f019d69167f/summary.json)；原始 ledger（运行前后两次库投影 + trace 回读）不入库，sha256 `46aa0a2a…19ad9`、977997 字节（ADR-0006）
- 类别：有界真实软件环境运行 + 真实模型调用。驱动只有 kind lab 的故障钩子，告警由真实 Prometheus/Alertmanager 产生，不是脚本伪造的 webhook；不是生产证明

## 环境

- 实验环境：kind `opspilot-m1`（colima `m1-kind`），OTel Demo chart 0.37.8，Alertmanager v0.28.1（chart 1.24.0），webhook 目标 `http://192.168.5.2:8080/intake/alertmanager`。注入前 frontend 2 分钟 span rate = 2.30/s，计数器正常，未重启 otel-collector。
- PostgreSQL 17：本 worktree `tmp/pg-live`，端口 55491，库 `m0_budget`，新建空库后 `make migrate` → `0010_alert_intake`。运行结束 `pg_ctl stop`。
- 进程：`python -m opspilot.web serve`（127.0.0.1:8080，`OPSPILOT_TOOL_PROFILE=otel-demo`，`OPSPILOT_EVENT_TOKENS` 来自 `workbench-alertmanager.env` 只含哈希，`OPSPILOT_TARGET_IDENTITIES` 一条 `m0-otel-20260909` 带 `match.labels = {cluster: opspilot-m1, namespace: otel-demo, service: checkout}`；另有一个只读查看页面用的实验 UI 账号）；`python -m opspilot.worker_main`（同 profile，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号）。worker 版本：`prompt-replay-candidate-bd28790117a0` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-34bc78980747`。
- trace：round `m1-04-intake-live-20261010`，project `opspilot-lab-m1-04-intake-live-20261010`，运行前经 `lab_evidence.prepare_lab_project` 建立并回读 `trace_tier=longlived`。
- 防循环上限：产品默认每 Run 100 次模型请求（Run 输入 `limits.model_requests=100`），未另加。

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 12:54:25 | web 与 worker 启动；worker 日志 `trace export enabled mode=lab` |
| 12:54:34 | `kind_lab.py fault inject --experiment-id m1-04-step2-live`（paymentFailure 100%，ConfigMap sha256 校验通过） |
| 13:00:54 | 告警 `startsAt`（注入后约 6 分 20 秒） |
| 13:01:03 | 工作台收到 firing，`200 OK`；建事故与 Run |
| 13:01:05–13:02:56 | worker 领取并调查：8 次模型请求、14 次工具调用，`status=published` |
| 13:03:06 | `fault restore`（ConfigMap 回到注入前 sha256 `1ee5c025…`，fault log `verified: true`） |
| 13:04:00 | 运行前投影快照（resolved 之前） |
| 13:06:54 / 13:07:03 | 告警 `endsAt` / 工作台收到 resolved，`200 OK` |
| 13:08:03 | 停 worker、web；随后停 PG |

## 结果

| 检查 | 预期 | 实际 |
|---|---|---|
| 真实 webhook 送达与认证 | 两次 `POST /intake/alertmanager`，bearer 映射到 `alertmanager` actor | 两次 200；两条投递记录 `actor = alertmanager`；库中投递总数 2 |
| firing 结果 | `outcome = created`，新事故 + Run | delivery 1：`firing` / `created`，事故 `ab011263-451c-530c-b923-2588e3312218`，Run `99f53e89-c53e-59b6-a206-7f019d69167f` |
| 身份规范化（I3） | `starts_at` UTC 秒，另存原文 | `2026-10-10T13:00:54Z`，原文 `2026-10-10T13:00:54.365Z`；fingerprint `83f7541ae0105d42` |
| 目标解析（I4） | 恰好一条命中 → 绑定 `m0-otel-20260909` | `target_id = m0-otel-20260909`，`handoff = false`，`handoff_reason = null` |
| 受影响服务（I5） | `scope_facts.affected_service` | `{"namespace": "otel-demo", "workload": "checkout"}`；投影 `affected_service = (otel-demo, checkout)`；`scope_facts.target_ids` 仍为 `[m0-otel-20260909]` |
| 问题模板（I6） | 只含告警名、severity、服务、`starts_at`、PromQL；不含 annotation | 见下方原文。两版 annotation 的 `description`（「100% …」「66.67% …」）与 `summary` 均不在问题中（摘要 `question_contains_annotation_text = false`） |
| 审计（I7） | 每次投递一条，annotation 变化递增版本 | firing `annotation_revision = 1`（description「100% …」），resolved `annotation_revision = 2`（description「66.67% …」）；两条 `truncated = false`；`raw_sha256` 分别 `3175b249…` / `d44356ca…` |
| worker 调查 | 真实 `deepseek-flash` 跑完：报告或明确交接 | Run `completed`，结论发布；`outcome_from_durable`：`decision = report_available`，`handoff_reasons = []`，11 个证据 ID，`actions = [read_only_query]`。报告 `assessment_status = completed`、`conclusion = partial`：确认 checkout PlaceOrder 错误率 12:57 起上升、13:00 达 1.0，错误源自 payment `Charge`（「Invalid token」），payment 为何拒绝所有扣款没有证据，故为 partial |
| resolved（C、E8） | `resolved_attached`，lifecycle 不变，不建 Run、不开观察 | delivery 2：`resolved` / `resolved_attached`，同一事故；resolved 前后投影 `lifecycle = open`、`run_ids` 只有一个、`observation_session_ids = []`，`control_generation` 仍为 0 |
| 工作台（I9，附带） | 事故页展示身份、受影响服务、annotation 最新版本、resolved 记录 | 页面含身份 `83f7541ae0105d42`、「Affected service otel-demo/checkout」、「Annotations (revision 2, untrusted alert text, credentials redacted)」、resolved 表与「Recorded only」说明；事故出现在列表页 |
| trace | LangSmith 可见完整 span 树 | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/87692eff-c100-4b56-ba90-08bdbbf41d9c/r/00000000-0000-0000-fa82-2768da25fccb?poll=true)，`read_back = found`，OTel trace `5bd060dff8ff5c93bbafe4dfb536d552`，丢弃 span 0；子 run：llm 8、tool 14，与库中 `budget_spent = 8`、`tool_operations_used = 14` 一致；token 201177 输入 / 27615 输出 |

Run 输入中的问题原文：

```
Alertmanager alert CheckoutPlaceOrderErrorRatioHigh (severity critical) is firing for service otel-demo/checkout since 2026-10-10T13:00:54Z.
Alert expression (PromQL): label_replace(label_replace((sum by (k8s_namespace_name, service_name) (rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m]))) / (sum by (k8s_namespace_name, service_name) (rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))), "namespace", "$1", "k8s_namespace_name", "(.*)"), "service", "$1", "service_name", "(.*)") > 0.5
Investigate why this alert is firing.
```

## 观察到但不属本步判定

- 授权时间框为 `[接收时刻 - 24h, 接收时刻]`（`2026-10-09T13:01:03Z` – `2026-10-10T13:01:03Z`），锚定接收时刻而不是 `startsAt`。这是第 3 步的范围，本步按现状记录。
- 事故行 `state` 列在 Run 完成后仍为 `queued`，lifecycle 为 `open`；未核查该列在现有 web 提交路径下是否同样如此（未确认）。
- 报告 `conclusion = partial` 是模型对根因的判断，不是失败；本次没有交接。

## 失败与未覆盖

- 本次所有检查通过，没有重试，没有实验环境动作（计数器正常，未重启 otel-collector）。
- 未覆盖：重复通知（`repeat_interval` 1h，未等待）、组内多告警、目标不可解析/歧义的仅交接路径、超大或恶意 annotation、逐条持久化失败（503）——均由合同测试覆盖，本次真实运行只走了单告警的 created → resolved_attached 路径。

## 费用

DeepSeek 余额 4.61 → 4.46 CNY（差 0.15 CNY），取于 12:54:19Z 与 13:07Z 之后。同一账户在此期间可能有其他会话调用，余额差是本次费用的上界（未确认是否有并发消耗）。token：201177 输入 / 27615 输出（LangSmith 根 run 聚合）。

## 凭据

证据中无 token、口令或 API key：gitleaks 8.30.1 `dir` 扫描本目录 `no leaks found`；另以 DeepSeek/LangSmith key、webhook token、Prometheus investigator 口令、实验 UI 口令的原值逐一在本目录做字面匹配，均 0 次（不打印原值）。
