# F2 PR-A 有界真实 Run：查询整请求时限（经宿主卡顿代理）

- 日期：2026-10-19 14:24–14:42Z；分支 `feature/F2-query-deadline`，commit `ab34df1`（运行期间工作区干净，本目录为运行后新增）
- 合同：[F2 合同 r1](../../tasks/2026-10-17-f2-contract.md) 第六节「真实运行」与 R1、R2、R7
- 冻结摘要：[summary.json](summary.json)。原始 ledger（Run 行、事故行、全部 step、工具计费行、代理日志、trace 工具 span、事故页 HTML）不入库，sha256 `b4ad85b4…bee0dc001b`，351560 字节（ADR-0006）
- 类别：有界真实运行。真实 DeepSeek `deepseek-flash`，真实 kind 实验环境，Prometheus 卡顿由宿主代理注入。这不是生产证明
- 执行者：未参与 PR-A 实现的新上下文 Agent
- **结论：全部断言通过**。两次 `query_range` 被代理卡住，各自在约 30.0 s 时以 `timeout` / `TOOL_TIMEOUT` / `source_contact=possible` 落库；Run 继续执行并发布报告，报告缺口写出了两个超时查询的 operation id；工作台两条工具行都显示 "source may have received the request"

## 环境

- PostgreSQL 17.9：本 worktree 新建 `tmp/pg-f2-live`，端口 55491，超级用户 `m0_lab`，库 `m0_budget`。`make migrate` 升到 `0017_web_suspension_grants`，`scripts.m0.postgres_lab runtime-roles` 建 `opspilot_web_lab`、`opspilot_worker_lab`。日志开启 connections / disconnections，`log_line_prefix` 含 user、application_name、SQLSTATE。运行结束后 `pg_ctl stop`。主仓库 55561 的 PG（PID 46716）没有碰
- web 和 worker 都用最小环境启动（只含 PATH、HOME 和下列变量），都设 `OPSPILOT_TOOL_PROFILE=otel-demo`，都载入 `prometheus-investigator.env`（只进进程环境）：
  - web：`python -m opspilot.web serve`，用户 `opspilot_web_lab`，`OPSPILOT_BIND=127.0.0.1:8091`。不用 8080，避免故障期间 Alertmanager 的 webhook 另建事故；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`；实验 UI 账号 `f2lab`
  - worker：`python -m opspilot.worker_main`，用户 `opspilot_worker_lab`，`OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:19091`（卡顿代理），Jaeger 用默认直连地址，`OPSPILOT_TRACE=lab`。版本为 `prompt-replay-candidate-dd9018031487` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-3a6a3ded3d76`
  - 卡顿代理：`scripts/f2_stall_proxy.py --listen 127.0.0.1:19091 --upstream http://127.0.0.1:19090 --count 2`。前 2 次 `/api/v1/query_range` 只回状态行和头，之后每 5 s 送一个空格，不转发上游；从第 3 次起直通
- 实验环境：kind `opspilot-m1`。注入前检查（14:24Z）：`kind_lab.py health` ok；swap 已用 86%（7957/9216 MiB，偏高，按记录标注）；frontend 2 分钟 span rate 为 2.0/s，所以没有重启 otel-collector；checkout PlaceOrder p95 为 180 ms，不在 0.5–1.8 s 区间。占用登记 `occupation claim`，holder `lane-f2-a`，batch `f2-a-live`
- 故障：主仓库 `scripts/kind_lab.py fault inject/restore --experiment-id f2-query-deadline-live`（paymentFailure）
- trace：round `f2-query-deadline-live-20261019`，project `opspilot-lab-f2-query-deadline-live-20261019`。运行前用 `lab_evidence.prepare_lab_project` 建立，回读 `trace_tier=longlived`
- 防循环上限：沿用产品默认，每个 Run 最多 100 次模型请求；工具注册超时 30 s

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:24:04 | 登记实验环境占用，随后做注入前检查 |
| 14:25 前后 | 新建 PG，`make migrate`，建运行角色，建 LangSmith project |
| 14:27:22 | DeepSeek 余额 183.37 CNY |
| 14:27:27 | `fault inject`：ConfigMap `1ee5c025…` → `e47d27c2…`，`verified: true` |
| 14:27:34 | 启动代理、web、worker；worker 日志 `trace export enabled mode=lab` |
| 14:34:10 | `POST /intake/ui` 返回 201：事故 `580a2c5b-…`，Run `40d9a79b-…` |
| 14:34:12.960 | 第 1 次 `metrics_range_query` 发出，代理卡住（第 1 轮） |
| 14:34:42.977 | 代理记到客户端断开（距请求 30.0 s）；14:34:43.013 同轮下一个工具发出，此前超时结果已落库 |
| 14:34:46.194 | 第 2 次 `metrics_range_query` 发出，代理卡住（第 2 轮） |
| 14:35:16.201 | 代理记到客户端断开（30.0 s）；14:35:16.238 同轮下一个工具发出 |
| 14:35:18.539 | 第 3 次 `query_range` 经代理直通上游，结果 `no_data`；之后 9 次直通全部正常返回 |
| 14:36:38.931 | worker：`status=published`，Run `completed` |
| 14:36:56 | `fault restore`：ConfigMap 回到 `1ee5c025…`，`verified: true` |
| 14:39:01 | 按核实过的 PID 用 SIGTERM 停 worker、web、代理，都正常退出，8091 与 19091 不再监听 |
| 14:39:14 | `occupation release`，回读 `show` 为 `null` |
| 14:39:21 | DeepSeek 余额 183.33 CNY |
| 14:41:26 | 冻结摘要；之后 `pg_ctl stop` |

## 结果

| 断言 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1 两次 `timeout` 各在 31 s 内落库 | 被卡的两次查询结果为 `timeout` / `TOOL_TIMEOUT` / `source_contact=possible`，从发出到落库 ≤ 31 s | 第 1 轮 `58344d87-…-t0`、第 2 轮 `f16681f5-…-t0`，两条都是 `timeout` / `TOOL_TIMEOUT` / `possible`。网关计时 30.008 s / 30.003 s（`opspilot_tool_charges.seconds`），trace 工具 span 30.041 s / 30.030 s，代理侧连接在 30.0 s 时被客户端关闭。落库时刻的上界是同轮下一个工具的 `requested_at`（`loop.py` 先 `commit_tool`，再续租、派发下一个工具），即请求后 30.054 s / 30.043 s | 通过 |
| 2 卡住的连接被关闭，不转发 | 超时后关闭连接；卡住的请求不到达上游 | 代理两条 `stalled: true` 记录都有 `client_disconnected_at`；按代理的设计，卡住的请求不会转发上游 | 通过 |
| 3 Run 继续，报告缺口写出该查询，或按 ADR-0005 交接 | 超时作为工具错误交给模型，Run 不中止 | 模型在第 3 轮起改用其他查询，代理之后直通 10 次。Run `completed`，epoch 1，16 次模型请求、23 次工具调用，`handoff=false`，报告 `assessment_status=completed`、`conclusion=partial`。`gaps` 第 5 条原文："Two earlier metrics queries in this run timed out (operation 58344d87-c626-4156-8769-6fece58f6a98-t0 and f16681f5-9adc-416f-850d-408031ed7c78-t0), so broader per-service error series beyond those queried remain unverified." | 通过 |
| 4 工作台工具行 | 显示 "source may have received the request" | `GET /incidents/580a2c5b-…` 返回 200，页面上恰有 2 处 `<code>metrics_range_query</code> → <span class="badge">timeout</span>` 后接 `source may have received the request`，分别在第 1、2 轮的 step 行。同页第 2 轮的 `traces_search` 是 `error` / `SOURCE_UNAVAILABLE` / `possible`，没有这行字，与模板条件（仅 `timeout` 且 `possible`）一致 | 通过 |
| 不重试 | 网关不自动重试（U3-A） | 代理一共收到 12 次 `query_range`：2 次卡住，10 次直通，与 Run 里的 12 次 `metrics_range_query` 调用一一对应。每次超时后模型都换了查询：第 1 轮是 24 小时窗口的 `sum by (service_name, status_code)`，第 2 轮缩到 1 小时并加 `span_name="PlaceOrder"`，第 3 轮改成 `status_code="ERROR"` | 通过 |
| PG 日志 | web、worker 没有错误 | 212 行：ERROR 1 次，是执行者自己在 owner 只读会话里查错列名（`lab-readonly`，42703），与产品无关；FATAL 0，42501 为 0 | 通过 |
| trace | LangSmith 可见 | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/440fa87f-4d7e-4315-a9e0-1ff247f16894/r/00000000-0000-0000-7d34-5423cb5c4585?poll=true)：OTel `7e6696979c8977a2d3a3d4ce247d5433`，子 run 为 llm 16、tool 23，与库里 `budget_spent=16`、`tool_operations_used=23` 一致；前两个 `tool:metrics_range_query` span 的输出是 `TOOL_TIMEOUT`。token 195677 输入 / 15293 输出 | 通过 |

## 观察到但不属本项判定

- 第 1 轮的 `traces_search`（Jaeger 直连，不经代理）返回 `RESULT_TOO_LARGE`（148084/25000 token），第 2 轮返回 `SOURCE_UNAVAILABLE` / `possible`，此后缩窄的 trace 查询全是 `no_data`。所以报告没有 trace 证据，结论为 `partial`。Jaeger 为什么不可用没有核查
- Run 的授权窗口（工具结果里的 `window`）是 24 小时（`2026-10-18T14:34:10Z`–`2026-10-19T14:34:10Z`），不是开发指南里写的提交前 300 秒。原因（策略窗口是否已改）没有核查
- 报告说 checkout 的错误从 2026-10-19 07:34Z 那个小时起就断续出现，早于本次注入（14:27Z）。可能是当天其他实验留下的，没有核查
- 卡住的请求不到达上游，所以 `source_contact=possible` 在本场景是保守的标记。这符合合同：超时时源端是否收到请求无法确定

## 失败与未覆盖

- 没有失败项。没有重试，没有改代码或测试断言
- 没有验证超时期间人工 `pause` 的顺序（合同点 3）和连续 5 次卡住时的线程泄漏，这两项只由 `tests/test_f2_query_deadline_acceptance.py` 覆盖。真实 Run 只卡了 2 次
- DNS 阻塞线程残留（任务记录里的待决项）不在本次范围；lab 的 Prometheus 地址是 IP，不会触发

## 费用

DeepSeek 余额从 183.37 降到 183.33 CNY，差 0.04 CNY，分别取于 14:27:22Z 和 14:39:21Z。同一账户在此期间可能有其他会话调用，所以这是本次费用的上界。token 为 195677 输入 / 15293 输出（调查根 run）。

## 实验环境释放

- `fault restore` 已校验（`1ee5c025…`）。之后用 lab kubeconfig 读回 flagd ConfigMap：11 个 flag 的 `defaultVariant` 全是 `off`
- `occupation show` 回读为 `null`；`tmp/lab-ledgers/` 下没有 `.active-*` 和 `fault-active.json`
- 执行者启动的代理、web、worker 都已按 PID 用 SIGTERM 停止；自建 PG 已停。这次没有用 port-forward

## 凭据

证据中没有 token、口令或 API key。Prometheus 账号文件、DeepSeek / LangSmith key 和 UI 口令只进了进程环境，没有打印，也没有写进日志。gitleaks 扫描结果见提交记录。
