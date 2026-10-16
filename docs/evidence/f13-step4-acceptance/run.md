# F13 第 4 步真实验收运行记录（2026-10-16）

- 目的：按 [任务记录](../../tasks/2026-10-15-f13-step4-acceptance.md)「验收运行计划」跑 3 个有界真实 Run，判定 F13 第 4 步。
- **结论：Run 1、Run 2 通过；Run 3 首次失败（核对结果为 `conflicting`，不是 `contradicted`），原因已在下文记录。用户批准只重跑一次（选项 B），重跑通过。F13 `passes` 翻为 `true`。首次失败的 Run 3 证据保留不动，重跑是新增证据，不替换首次结果。**
- 运行代码：分支 `feature/F13-step4-acceptance` `e1582ed`（= `main` `d19e78a` + 本 PR），web 与 worker 都用非 owner 角色；Observer 未启动（本验收不作恢复判定）。模型 `deepseek-flash`，trace 只在实验环境导出。
- 判定：[`knowledge_use.py`](knowledge_use.py)（投影加事故页 `#knowledge-hints` 逐条比对）；人工动作：[`workbench.py`](workbench.py)；冻结摘要：[`collect.py`](collect.py)（从 m1-clean-rerun 复制，只改实验名、round、产物清单和输出目录；`--scenario S1` 的恢复字段在这里不作判定）。第一次冻结（05:06Z）之后对 `collect.py` 跑过一次 `ruff format`（只改排版），所以 06:38Z 用排版后的 `collect.py` 对 Run 1–3 重新冻结，现在各 `summary.json` 里 `artifacts` 记录的 sha256 与入库文件一致。重新冻结读的是同一实验库副本（`tmp/lab-pg`，状态为 Run 2 之后），`--timeline` 用的是当时时间线的副本。与第一次冻结相比的差别：`frozen_at`、`collector_commit`、页面字节数，以及 Run 2 多出一条投递记录——重启 web 时 Alertmanager 补投了 I4 的 `resolved` 告警（delivery 8，`resolved_attached`，06:38:28Z）。ledger 也随之重新冻结，sha256 以当前 `summary.json` 为准。
- 原始 ledger 不入库，在本机 `production-ops-agent/tmp/lab-ledgers/f13-step4-acceptance/<run_id>/ledger.json`（sha256 见各 `summary.json` 的 `raw_ledger`）。

## 环境

- 起点库：#247 干净重跑库 `production-ops-agent/tmp/m1-clean/pg` 的副本（原库未启动、未改动），本机 127.0.0.1:55593，schema 在 head `0017_web_suspension_grants`。K1 = `4eb40edf-a9ca-44a3-90cd-5f62b3daf8ab` r1（active，`observation_ended_at` 2026-10-15T18:24:31Z）。
- kind + OTel Demo；开跑前 `kind_lab.py health` 全部 ok、所有 flag off，checkout 速率 0.0125 req/s、错误率 0、p95 96 ms。宿主 swap 15.7/16.4 GB（与 #247 相近）。运行中共享 heavy 锁一直持有，其他线没有跑 `make check`。
- 与 #247 相同，运行期间把 load-generator 的 `LOCUST_USERS` 从 2 临时调到 6（只改实验集群上的 Deployment），结束后已改回 2。
- 顺序 1 → 3 → 2：起点库只有 K1 一条知识，Run 2 撤销后 Run 3 就没有可用知识；三个 Run 的断言不变。协调者已批准（2026-10-16）。

## 过程与结果

| 时间（UTC） | 事件 |
|---|---|
| 04:44:36 | 注入 `paymentFailure`（实验 `f13-step4-20261016-r1`） |
| 04:49:04 | 告警建事故 I2 `31f0bf6e`，Run `d89aa6e5`；04:51:19 结论发布 |
| 04:51:38 | 恢复故障；04:51:54 以 `OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS=3600` 重启 web |
| 04:56:19 | checkout 5 分钟错误率回到 0（速率 0.079 req/s，p95 420 ms） |
| 04:56:27 | 网页提问建事故 I3 `25444bd5`，Run `4981b1b3`；04:57:22 结论发布 |
| 04:57:58 | 以默认阈值重启 web；04:58:03 在知识页撤销 K1 r1（HTTP 200，generation 1→2） |
| 04:58:03 | 再次注入（实验 `f13-step4-20261016-r2`） |
| 05:04:04 | 告警建事故 I4 `73c80dbc`，Run `823ee04b`；05:05:13 结论发布；05:05:21 恢复故障 |

| Run | 判定文件 | 结果 | 要点 |
|---|---|---|---|
| 1 知识一致 | [run1/knowledge-use.json](run1/knowledge-use.json) | **通过** | 输入含 K1 r1，`fresh`（年龄 37,473 秒，阈值 30 天）。核对结果照实记录为 `consistent`、`handled`：一条带标签的 `fact`，引用本 Run 证据 |
| 3 陈旧且矛盾 | [run3/knowledge-use.json](run3/knowledge-use.json) | **失败** | K1 标为 `stale`（年龄 37,916 秒，阈值 3600），页面可见。核对结果是 `conflicting`：一条带标签的 `fact`（「客户端 RPC 只有 PaymentService 非零」与当前窗口一致），两条带标签的 `rejected_hypothesis`（错误率不是 100%，当前 0.47；payment Charge 错误率当前 0.49），都引用本 Run 证据。判定失败项：`verdict_contradicted`、`handled_with_run_evidence`、`no_fact_on_knowledge`、`page_shows_stale_and_contradicted`；`unprovided_tags` 为空 |
| 2 撤销后变化 | [run2/knowledge-use.json](run2/knowledge-use.json)、[run2/revoke.json](run2/revoke.json) | **通过** | 撤销后输入 `status: ok`、`entries: []`，不含 K1 r1 |

Run 3 失败的直接原因：问题写的是「过去 10 分钟」，提问时故障恢复才约 4.5 分钟，查询窗口里还带着故障尾部，当前证据显示约一半的请求在失败。模型因此对 K1 里不同的论断分别给出一致和否定，结果是 `conflicting`。问题的时间窗是运行设计（lead）定的，不是环境异常，也不是产品拒绝了报告。

## Run 3 重跑（用户裁决 2026-10-16，选项 B：只重跑一次，断言不变）

- 起点重新复制 `production-ops-agent/tmp/m1-clean/pg`（`tmp/lab-pg2`，原库未动），K1 r1 为 active；web 以 `OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS=3600` 启动；不注入故障。`LOCUST_USERS` 保持 2。
- 开跑前：swap 14.5/15.4 GB；health ok；所有 flag off；故障从 05:05:21Z 起一直处于恢复状态（约 94 分钟）；checkout 速率 0.0125 req/s、错误率 0、p95 185 ms。
- 06:39:25Z 网页提问「Are checkout payments failing right now? Check the PlaceOrder error ratio over the past 5 minutes.」（[run3-rerun/ask.json](run3-rerun/ask.json)），建事故 `9e147bfa`、Run `f0cd48fb`；06:40:39Z 结论发布。
- 判定 [run3-rerun/knowledge-use.json](run3-rerun/knowledge-use.json)：**通过**。K1 r1 标 `stale`（年龄 44,094 秒，阈值 3600）；核对结果为 `contradicted` 且 `handled`：一条带标签的 `rejected_hypothesis`，引用本 Run 的 3 个证据（最近 5 分钟 PlaceOrder 的 ERROR span 为 0，到 PaymentService 的 status 2 调用为 0），另有一条带标签的 `hypothesis`（未核对较早的故障片段）。没有带标签的 `fact`，`unprovided_tags` 为空，页面显示 `stale` 与 `check: contradicted`。

## trace（project `opspilot-lab-f13-step4-20261016`，保留期 longlived）

| Run | 冻结摘要 | trace |
|---|---|---|
| 1 `d89aa6e5` | [run1/summary.json](run1/summary.json) | [opspilot.run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/2ac94101-1233-41aa-846f-aaf7640c88ca/r/00000000-0000-0000-d6bb-3a0ba39019d0?poll=true) |
| 3 `4981b1b3` | [run3/summary.json](run3/summary.json) | [opspilot.run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/2ac94101-1233-41aa-846f-aaf7640c88ca/r/00000000-0000-0000-80cf-a377f814e476?poll=true) |
| 2 `823ee04b` | [run2/summary.json](run2/summary.json) | [opspilot.run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/2ac94101-1233-41aa-846f-aaf7640c88ca/r/00000000-0000-0000-ec35-9af860f0d69a?poll=true) |
| 3 重跑 `f0cd48fb` | [run3-rerun/summary.json](run3-rerun/summary.json) | [opspilot.run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/2ac94101-1233-41aa-846f-aaf7640c88ca/r/00000000-0000-0000-eb8d-63e6043c3253?poll=true) |

## 费用

DeepSeek 余额 203.94 → 203.70 CNY（赠送余额 3.92 → 3.67，充值余额 200.02 不变），3 个调查 Run 共约 0.25 CNY。重跑窗口前后（06:39、06:42Z）余额都是 203.61，扣费在 3 分钟内还没有反映出来，所以重跑的费用**未确认**，按单个 Run 估计约 0.1 CNY 以内。

## 收尾

两次窗口结束时都已收尾：故障已恢复，所有 flag 为 off，`kind_lab.py health` 为 ok，`LOCUST_USERS` 为 2；web、worker 与实验库（55593，`tmp/lab-pg`、`tmp/lab-pg2`）已停，heavy 锁已释放。这是 kind 实验环境上的有界运行，不是生产证明。
