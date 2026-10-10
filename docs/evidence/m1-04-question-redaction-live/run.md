# M1-04 F9：首轮问题凭据脱敏真实运行

- 日期：2026-10-10 14:03–14:10Z；分支 `feature/m1-04-question-redaction`，commit `2ffe5e9`（干净工作区，本目录为运行后新增）
- 被测修复：`opspilot/investigation/context.py` `initial_messages` 把 Run 的问题经 `redact_credentials` 后放进首条 user 消息；输入快照保留原文
- 冻结摘要：[live-runs/92fdbc19-1b42-5204-8310-8eaf00862a2f/summary.json](live-runs/92fdbc19-1b42-5204-8310-8eaf00862a2f/summary.json)；原始 ledger（库重建 + 重建 transcript + trace 子 run 全量）不入库，sha256 `888c0bfb…a46be3`、2391220 字节（ADR-0006）
- 类别：有界真实软件环境运行 + 真实模型调用。问题里的凭据是合成值，不是任何真实凭据；不是生产证明

## 环境

- 实验环境：kind `opspilot-m1`，只读查询，未注入故障，未做任何实验环境动作。
- PostgreSQL 17：本 worktree `tmp/pg-live`，端口 55493，库 `m0_budget`，新建空库后 `make migrate` → `0010_alert_intake`。运行结束 `pg_ctl stop`。
- 进程：`python -m opspilot.web serve`（127.0.0.1:8080，`OPSPILOT_TOOL_PROFILE=otel-demo`，`OPSPILOT_TARGET_IDENTITIES` 一条 `m0-otel-20260909`，`OPSPILOT_UI_USERS` 一个实验账号，口令只存在本机临时文件）；`python -m opspilot.worker_main`（同 profile，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号）。worker 版本：`prompt-replay-candidate-bd28790117a0` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-34bc78980747`。
- trace：round `m1-04-question-redaction-20261010`，project `opspilot-lab-m1-04-question-redaction-20261010`，运行前经 `lab_evidence.prepare_lab_project` 建立并回读 `trace_tier=longlived`。
- 防循环上限：产品默认每 Run 100 次模型请求，未另加。

## 输入

`POST /intake/ui`（Basic Auth 实验账号，`target_id=m0-otel-20260909`，`idempotency_key=f9-live-1`）。问题是一句真实的 checkout 调查问题，中间嵌入四段合成凭据，形状取自 `tests/test_m1_question_redaction.py` 的 `SECRETS`（Bearer token、URL userinfo 口令、`api_key=` 查询参数、`token=` 赋值）。本文与摘要不复述原文，下称 `<synthetic>`；原文 sha256 `da4b85fa…dcdfbb3a6cb`。

模型实际收到的首条 user 消息（库中重建，哈希与第 1 步记录一致，见下）：

```
Checkout appears to show elevated HTTP errors since about 13:50Z; the external probe used Authorization: [REDACTED_CREDENTIAL] against https://[REDACTED_CREDENTIAL]@metrics.internal/api?api_key=[REDACTED_CREDENTIAL]&q=1 and token=[REDACTED_CREDENTIAL] -- query the authorized metrics and return a json investigation report for the authorized window: is checkout failing, and why?
```

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:03:24 | 建 trace project，记余额 |
| 14:03:29 | web 与 worker 启动；worker 日志 `trace export enabled mode=lab` |
| 14:03:39 | `POST /intake/ui` → 201，事故 `d4f7a7cd-14cf-5c3c-96d3-3178fa142428`，Run `92fdbc19-1b42-5204-8310-8eaf00862a2f` |
| 14:03:39–14:07:48 | worker 调查：17 次模型请求、41 次工具调用，`status=published` |
| 14:09:45 | 停 worker、web；随后停 PG |

## 结果

| 检查 | 预期 | 实际 |
|---|---|---|
| (a) 模型请求（库） | 首条 user 消息不含 `<synthetic>`，含 `[REDACTED_CREDENTIAL]` | 通过。`rebuild_transcript` 按每步记录的 `input_snapshot_hash` 逐步校验，无 `INCOMPATIBLE_STATE`；首条 user 消息 4 段均为 `[REDACTED_CREDENTIAL]`，四个合成值命中 0；重建的全部消息与 18 行 step 记录中命中 0 |
| (a') 发出的是脱敏版 | 第 1 步记录的请求哈希只与脱敏前缀吻合 | 通过。第 1 步 `input_snapshot_hash = 3de7260e…12e3683`；前缀（system + 脱敏问题 + 证据上下文，3 条）哈希相同；把问题换回原文后哈希为 `a7d917c5…9ac6012f`，不相等 |
| (b) LangSmith trace | 回读的全部子 run 不含 `<synthetic>` | 通过。[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/e1806a2a-dafd-4694-ae6f-c87c8a0a5b41/r/00000000-0000-0000-eed4-268550b5b7fe?poll=true)，`read_back = found`，OTel trace `4b03056ffb123b4a12e9ccccffac3e8d`，丢弃 span 0。全部子 run（17 个 `deepseek.chat.completions`、41 个工具）的 inputs/outputs/metadata 中四个合成值命中 0；17 个模型 span 的 prompt 都带产品侧标记 `[REDACTED_CREDENTIAL]`（trace 自身的兜底遮蔽用的是 `[REDACTED]`，故此标记说明脱敏发生在发给模型之前，而不只是导出时） |
| (c) 冻结摘要 | summary.json 不含 `<synthetic>` | 通过。冻结前对摘要全文检查命中 0；冻结后对本目录逐个字面匹配，0 次 |
| 输入快照保留原文 | 存储的问题与提交原文一致 | 通过。Run 输入 `question` 的 sha256 与提交原文相同（`da4b85fa…`），其中含四段合成值；结论记录的 `question_sha256` 也是同值 |
| Run 结束 | 完成或交接 | 完成，未交接。`outcome_from_durable`：`decision = report_available`，`handoff_reasons = []`，38 个证据 ID，`actions = [read_only_query]`。报告 `assessment_status = completed`、`conclusion = partial` |

报告要点（模型判断，非本步判定项）：checkout 失败集中在 12:57–13:05Z，失败一跳是 payment `Charge`；问题声称的约 13:50Z 无法测量，因为指标在 13:26Z 到 14:01Z 之间没有样本、14:01Z 后计数器重置。报告另写明问题文本里带有凭据材料与外部 URL，按不可信输入处理、未使用。

## 观察到但不属本步判定

- 实验环境指标断档：报告指出 span 指标 13:26Z–14:01Z 无样本、14:01Z 后计数器从低值重启。与 kind lab 已知的「VM 或 collector 重启后计数器停滞/重置」一致，本次未核查原因（未确认）；运行未注入故障，不影响本步检查。
- trace 属性上限：worker 日志 10 次 `opentelemetry.attributes WARNING Attributes dict is full. Dropping the oldest key-value pair`。回读中第 16、17 次模型调用的 span 丢了 `model_call_seq` 元数据，其中一个丢了 `langsmith.span.kind`，在 LangSmith 不被归为 `llm`（子 run 按类型计 llm 16、按名字计 17，库中 `budget_spent = 17`）。两个 span 的 prompt 仍在、仍是脱敏版。长 transcript 下 span 属性超过 OTel 默认上限是独立问题，本次不处理。

## 失败与未覆盖

- 本次检查全部通过，没有重试，没有实验环境动作。
- 未覆盖：追问与交接续跑的问题拼接（合同测试覆盖）、Alertmanager 生成的问题（模板不含自由文本凭据）、压缩后重建（合同测试覆盖）。

## 费用

DeepSeek 余额 4.46 → 4.29 CNY（差 0.17 CNY），取于 14:03:24Z 与 14:08Z。同一账户在此期间可能有其他会话调用，余额差是本次费用的上界（未确认是否有并发消耗）。token：613521 输入 / 39646 输出（LangSmith 根 run 聚合）。

## 凭据

证据中无真实 token、口令或 API key，也无本次合成凭据：gitleaks 8.30.1 `dir` 扫描本目录 `no leaks found`；另以 DeepSeek/LangSmith key、Prometheus investigator 口令、实验 UI 口令的原值和四个合成值逐一在本目录做字面匹配，均 0 次（不打印原值）。

## 运行之后的提交（lead 注，2026-10-10）

本次真实运行使用 `2ffe5e9`（上文）。之后 PR #189 依机器人审查加入两处 `redact_credentials` 修正：带引号的值（`5a6c3ca`）、缺少闭合引号时脱敏到行尾（`495f9fa`）。二者只改纯函数 `opspilot/tools/registry.py` 的匹配规则，不改调查 loop 的调用路径（`initial_messages` → `redact_credentials` 不变），由 `tests/test_redact_credentials_quoted.py` 的确定性测试覆盖（含红绿）；本次运行的问题不含带引号形式，其冻结结果不受影响。未为这两处修正另做真实运行。

