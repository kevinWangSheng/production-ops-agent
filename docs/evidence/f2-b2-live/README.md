# F2 PR-B2 有界真实风暴：积压上限与 BACKLOG_FULL 停放

- 日期：2026-10-22 18:40–18:43Z；分支 `feature/F2-backpressure`，commit `f98e9aa`（运行时工作区干净）
- 合同：[B1/B2 合同](../../tasks/2026-10-21-f2-b1-contract.md) 第六节 B2；F2 合同 U4-A、U5-A
- 冻结摘要：[summary.json](summary.json)。原始 ledger（事故、Run、事件、控制、step、采样、投递结果、trace 列表）不入库，sha256 `7c1557f9…b8c68e34`，134863 字节（ADR-0006）
- 类别：有界真实运行。真实 DeepSeek `deepseek-flash`，fixture 工具档，本机临时 PG。不是生产证明
- **结论：6 条断言全部通过**，1 次 attempt，没有重试

## 环境与命令

- PG 17：`tmp/pg-55742`，新建库 `f2_b2_live`。owner 执行 `python -m opspilot.schema migrate` 到 `0019_incident_priority`，再执行 `scripts.m0.postgres_lab runtime-roles`；web 与 worker 分别用 `opspilot_web_lab`、`opspilot_worker_lab` 连接
- web：`python -m opspilot.web serve`，`127.0.0.1:8094`，`OPSPILOT_RUN_SECONDS=1800`，`OPSPILOT_TOOL_PROFILE=fixture`，自愈循环关闭；目标登记表只有 fixture 的 `checkout-prod`。UI 口令和事件 token 是本次随机生成的一次性值，进程环境里只放哈希
- worker：`python -m opspilot.worker_main`，`OPSPILOT_WORKER_CAPACITY=2`，`OPSPILOT_BACKLOG_MAX=6`，连接池默认（`max_size=4`），轮询 2 s，`OPSPILOT_TRACE=lab`，`LANGSMITH_PROJECT=opspilot-lab-f2-b2-live-20261022`（longlived）
- 风暴：worker 先启动，然后 `python scripts/f2_backlog_storm.py send` 经 `POST /intake/alertmanager` 投 12 条合成 Alertmanager v4 webhook，间隔 0.2 s，低优先级先发：4 条无 severity、4 条 warning、4 条 critical。每条告警的 alertname 和 fingerprint 都不同。`scripts/f2_backlog_storm.py sample` 每 2 s 用只读 owner 连接采样一次
- 人工：18:42:21Z 通过真实 UI 控制路由（Basic 认证 + Origin）对被停放的 warning `b6ec0292` 执行一次 `follow_up`

## 时间线（UTC）

| 时间 | 事件 | running / queued |
|---|---|---|
| 18:40:54.9 | 第 1 条（无 severity）到达时 worker 空闲，立即被领取 | 1 / 0 |
| 18:40:56.89 | 积压 pass：此时已到 8 条以上，停放 3 条无 severity（`BACKLOG_FULL`） | — |
| 18:40:56.91 | critical `ce409f89` 被第二个槽位领取 | 2 / 5 |
| 18:40:57.21 | 第 12 条送达 | — |
| 18:40:58.07 | 采样：queued 7（这时下一次 pass 还没跑） | 2 / 7 |
| 18:40:58.91 | 积压 pass：停放 deadline 最晚的 warning `b6ec0292`，距最后一条送达 1.7 s | — |
| 18:41:00 起 | 之后每次采样 queued 都 ≤ 6 | 2 / 6 |
| 18:41:13–18:41:28 | 剩下的 3 条 critical 先领取，然后按 deadline 领取 warning `401f0b5b`、`4406fb97`、`8c1123a9` | 2 / ↓ |
| 18:42:00 | 队列清空：3 个 Run 发布结论，其余交接 | 0 / 0 |
| 18:42:21.11 → .29 | 人工 follow_up `b6ec0292` → 重新 queued → 0.18 s 后被领取 | 1 / 0 |
| 18:42:36.8 | 该 Run 交接（`INCOMPLETE_INVESTIGATION`），没有被再次停放 | 0 / 0 |

## 结果

| 断言 | 结果 |
|---|---|
| 12 条全部可追溯 | 通过：12 条投递结果都是 `created`，各有一个事故、一个 Run 和一条 `intake_accepted` 事件 |
| 峰值 running ≤ 2 | 通过：采样中峰值为 2 |
| 一次 pass 后 queued ≤ 6 | 通过：最后一条送达 1.7 s 后的那次 pass 之后，最大 queued 为 6；pass 之前有 1 个采样是 7，在 R-B2-4 容差内（一个轮询周期加本轮处理时间） |
| 停放最低优先级、带原因 | 通过：停放了 3 条无 severity 和 1 条 deadline 最晚的 warning，事件都是 `run_handoff`，`reasons=["BACKLOG_FULL"]`，`execution="not_started"`；没有出现停放之后又领取更低优先级 Run 的情况 |
| 人工 follow_up 后重新排队并被领取 | 通过：`b6ec0292` 代际 0→1，0.18 s 后被领取，跑完后以普通交接结束，没有出现第二条 `BACKLOG_FULL` |
| 无模型错误 | 通过：没有 `MODEL_*` 或 `TIMEOUT` 交接，worker 日志里没有 error 或 timeout |

## 与预期不符或未覆盖

- 第 1 条无 severity 的告警到达时 worker 空闲，所以被直接领取：优先级只决定排队中谁先领取，不会抢占已经在跑的 Run（U5-A）
- 3 条被停放的无 severity 事故最终仍是 `waiting_human`，这是 U4-A 的预期：停放后等人处理，不会自动回到队列
- 已执行的 9 个 Run 中，3 个发布结论，6 个以 `INCOMPLETE_INVESTIGATION` 交接（和 B1 真实 Run 的现象一样，属于 fixture 档下的调查质量问题，与 B2 无关，原因未排查）
- 只跑了一个 worker 进程；多 worker 并发 pass 由 PG 测试覆盖

## Trace

project `opspilot-lab-f2-b2-live-20261022`，9 个根 run（被停放、从未执行的 3 个 Run 没有 trace），每个 Run 的链接见 summary 的 `incidents[].trace.url`。例如：

- [critical ce409f89](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7154e080-adbb-4104-90e3-562b0ce91aba/r/00000000-0000-0000-e01d-1e050cfa5ab4?poll=true)（发布结论）
- [follow_up 后的 warning b6ec0292](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7154e080-adbb-4104-90e3-562b0ce91aba/r/00000000-0000-0000-5724-42de3513acde?poll=true)
- [首个无 severity fe9f8798](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7154e080-adbb-4104-90e3-562b0ce91aba/r/00000000-0000-0000-0e3d-79f5933df4c1?poll=true)

## 费用

DeepSeek 余额：18:40:00Z 为 99.58 CNY，18:43:19Z 为 99.57 CNY，差 0.01 CNY。期间同一账户可能有其他会话调用，所以这是上界。根 run 合计 prompt 33858 / completion 24596 token。

## 凭据与清理

证据中没有 token、口令或 API key。web、worker、采样进程按核实过的 PID 用 SIGTERM 停止，worker 日志记录了 "stop claiming" 和 "worker stopped"。
