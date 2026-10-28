# F2 PR-B1 有界真实 Run：容量 2 的并发 Run 与优先级领取

- 日期：2026-10-22 16:57–17:03Z；分支 `feature/F2-capacity-priority`，commit `818fdab`（运行时工作区干净）
- 合同：[B1 合同](../../tasks/2026-10-21-f2-b1-contract.md) 第六节 B1
- 冻结摘要：[summary.json](summary.json)。原始 ledger（事故、Run、事件、step、采样、trace 列表）不入库，sha256 `2a24bc7c…41bea22`，191558 字节（ADR-0006）
- 类别：有界真实运行。真实 DeepSeek `deepseek-flash`，fixture 工具档，本机临时 PG。不是生产证明
- **结论：断言全部通过**。两个 Run 同时 `running`，峰值 2，从未超过 2。两个 Run 的模型请求在时间上重叠，各自按自己的结果交接或发布，没有超时或中止。后到的 critical 在第一个槽位空出后 6 ms 被领取，比早一秒排队的 warning 先领

## 环境与命令

- PG 17：`tmp/pg-55722`（127.0.0.1:55722），新建库 `f2_b1_live`。owner 连接执行 `python -m opspilot.schema migrate` 到 `0019_incident_priority`，再执行 `OPSPILOT_LAB_DSN=… python -m scripts.m0.postgres_lab runtime-roles`；web/worker 以 `opspilot_web_lab` / `opspilot_worker_lab` 连接
- web：`python -m opspilot.web serve`，`OPSPILOT_BIND=127.0.0.1:8093`，`OPSPILOT_TOOL_PROFILE=fixture`，`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`。目标登记表只有 fixture 的 `checkout-prod`，匹配标签 `namespace=otel-demo, service=checkout`。UI 口令与事件 token 是本次随机生成的一次性值，只把哈希放进进程环境
- worker：`python -m opspilot.worker_main`，`OPSPILOT_WORKER_CAPACITY=2`，连接池用默认值（`max_size=4`），`OPSPILOT_TOOL_PROFILE=fixture`，`OPSPILOT_TRACE=lab`，`LANGSMITH_PROJECT=opspilot-lab-f2-b1-live-20261022`（运行前用 `lab_evidence.prepare_lab_project` 建立，回读 `trace_tier=longlived`）。`OPSPILOT_ENV_FILE` 指向私有 env 文件，key 只进进程，不打印
- 告警：合成 Alertmanager v4 webhook，经 `POST /intake/alertmanager`（Bearer 事件 token）投递，标签带 `severity`。状态采样每 2 s 一次，执行者用只读 owner 连接查询 Run 状态、租约和事故优先级
- 两次 attempt 都保留，没有重试：attempt 1 在 worker 启动前先投 warning、再投 critical；attempt 2 在 worker 运行中先投 3 条 warning（w1、w2、w3），再投 1 条 critical（c1）

## 时间线（UTC，节选自 summary 的 `timeline` 与 `runs`）

| 时间 | 事件 | 同时 running |
|---|---|---|
| 16:57:57.6 / 16:57:58.7 | attempt 1：warning、critical 两个事故建立，Run `queued` | 0 |
| 16:58:12.937 | worker 启动后两个槽位同时领取：warning 先于 critical 25 µs | 2 |
| 16:58:14.0–16:58:24.5 | 两个 Run 的第 2 轮模型请求同时在途（采样 6 次都是 2） | 2 |
| 16:58:25.06 / 16:58:27.17 | 两个 Run 各自交接，原因都是 `INCOMPLETE_INVESTIGATION`（模型的结论被判为调查不完整） | 1 → 0 |
| 17:01:35.2–35.7 | attempt 2：w1、w2 建立后立即被两个槽位领取 | 2 |
| 17:01:36.36 / 17:01:37.43 | w3、c1 建立，排队等待（采样 6 次：running 2、queued 2） | 2 |
| 17:01:52.215 | w2 的 Run 交接，空出一个槽位 | 1 |
| **17:01:52.220** | **c1（critical）被领取**，距空槽 5.6 ms，比 w3 晚 1 s 排队却先被领 | 2 |
| 17:01:53.138 → 17:01:53.147 | w1 的 Run 发布结论（`run_completed`），w3 随即被领取 | 2 |
| 17:02:05.99 / 17:02:08.55 | c1、w3 各自交接（`INCOMPLETE_INVESTIGATION`） | 1 → 0 |
| 17:03:03.9 | SIGTERM 停 worker：日志记 "stop claiming"，随后 "worker stopped"；web 同样停止 | — |

45 次采样中 running 峰值都是 2，见 summary 的 `assertions` 与 `timeline`。

## 结果

| 断言 | 结果 |
|---|---|
| 峰值 running ≤ 2，两槽都忙时为 2 | 通过：两次 attempt 峰值都是 2 |
| 两个 Run 的模型请求重叠 | 通过：attempt 1 两个 Run 同时领取，第 2 轮模型请求同时在途约 11 s |
| 不互相中止 | 通过：6 个 Run 中，5 个以 `INCOMPLETE_INVESTIGATION` 交接，1 个发布结论。没有模型超时、中止、`TIMEOUT` 或连接池等待；worker 日志里唯一的错误是 trace 导出超时（见下） |
| 后到的 critical 占下一个空出的槽位 | 通过：c1 在第一个空槽后 5.6 ms 被领取，排在更早排队的 w3 之前 |
| attempt 1 的领取顺序 | 不可判定：两个槽都空闲时，两次领取在 25 µs 内先后提交，warning 略早。只有槽位不够时顺序才有意义，attempt 2 已经证明 |

## 与预期不符或未覆盖

- 5/6 的 Run 以 `INCOMPLETE_INVESTIGATION` 交接。每个 Run 都只有 2 次模型请求、1 次工具查询；推断是 fixture 档下模型过早给出结论，被报告校验判为调查不完整。这属于调查质量问题，与 B1 无关，原因未排查（未确认）
- trace 导出丢了 4 个 span：17:01:48Z 一次 OTLP 批量导出遇到 LangSmith 读超时（10 s）。attempt 2 的 w1、w2 两条 trace 各缺第 2 轮的 llm span 和工具 span，库里的行是完整的
- 本次没有模型请求超时，所以「一个 Run 的模型超时不中止另一个」只在正常并发下观察到，超时路径由验收测试覆盖（合同第五节第 5 项）
- 只跑了一个 worker 进程，多 worker 合计容量不在本次范围

## Trace

project `opspilot-lab-f2-b1-live-20261022`（longlived）。每个 Run 的根 run 链接见 summary 的 `runs[].trace.url`：

- attempt 1：[warning 9ed14131](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-136d-7060d8573ea0?poll=true)、[critical a2fd4512](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-be50-1fd0ee6a8e85?poll=true)
- attempt 2：[c1 critical 0390f9b2](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-9370-91103db29a00?poll=true)、[w3 7b7f87c8](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-b049-7a806635e989?poll=true)、[w1 f6ada557](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-6265-94a4655069d6?poll=true)、[w2 266a05b7](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/815f6c2b-7942-4fa8-b4d5-6088e86e65ff/r/00000000-0000-0000-c534-5acbc20460bb?poll=true)

## 费用

DeepSeek 余额：16:57:09Z 为 99.86 CNY，17:01:35Z（两次 attempt 之间）为 99.84 CNY，17:03:05Z 为 99.84 CNY，差 0.02 CNY。同一账户期间可能有其他会话调用，所以这是上界。导出的根 run 合计 token 见 summary 的 `cost.tokens`；w1、w2 的 trace 缺 span，这个合计偏低。

## 凭据与清理

证据中没有 token、口令或 API key。web 与 worker 按核实过的 PID 用 SIGTERM 停止，8093 不再监听；PG 已 `pg_ctl stop`。
