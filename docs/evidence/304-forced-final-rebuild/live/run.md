# #304 kind 最小真实 Run（D-U2 ②）

- 日期：2026-10-22 21:15–21:25Z；分支 `fix/304-forced-final-rebuild`，commit `2fc953b`（运行期间产品代码未改）
- 类别：有界真实运行。真实 DeepSeek `deepseek-flash`，产品 `otel-demo` profile 查 kind `opspilot-m1` 的真实 Prometheus / Jaeger，不注入故障。不是生产证明
- 执行者：未参与 #304 实现的新上下文 Agent
- **结论：D-U2 ② 未完成。** 3 次 Run 都以合格报告发布，没有出现 `REPORT_INVALID` 交接，所以没有执行 follow_up，断言 1、2 都不成立（不是修复失败，而是前提没出现）。按任务约定 3 次后停止，没有用改代码或改提示的方式硬凑
- 附带观察：其中 2 次 Run 自然走了强制最终轮（前一轮无工具、报告被拒，下一轮 `final=true` 合格发布）。对这 2 个 Run 的真实行做只读重建：`main` a7b296a 报 `INCOMPATIBLE_STATE`，修复分支通过。这相当于合同第 5 节第 3 条「崩溃变体」的重建一侧，但没有走 worker 重新领取

## 环境与驱动

- 驱动：`scripts/m1_live_runner.py --tool-profile otel-demo --model-requests 20 --follow-up --deadline-seconds 1200 --lab-round 304-forced-final-rebuild-live`。`--follow-up` 只在第一次尝试 `handed_off` 时才发 follow_up 并再次 `resume`；问题文本是脚本固定的 `OTEL_QUESTION`。没有用测试替身或脚本化模型
- PostgreSQL 17.9：worktree 内新建 `tmp/pg-304-live`，端口 55741，`opspilot.schema migrate` 到 `0017_web_suspension_grants`，用完 `pg_ctl stop`（数据目录保留在被忽略的 tmp/）
- 后端：`OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:19090`（investigator 账号，来自 `tmp/m1-kind-lab/prometheus-investigator.env`），`OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:16686/jaeger/ui`，`OPSPILOT_TRACE=lab`
- 占用：`occupation claim`（holder `lane-304-live`，batch `304-live`）21:15:30Z，21:24 `release`，回读 `null`；无 `.active-*`、`fault-active.json`
- 开跑前读数（21:15Z）：memory free 44%，swap 剩 985 MiB，pageouts 15495963；`kind_lab.py health` ok；checkout PlaceOrder p95 93.75 ms；frontend 2 分钟 span rate 2.15/s；`ALERTS{CheckoutPlaceOrderErrorRatioHigh}` 为空。结束时（21:24Z）：swap 剩 1073 MiB，pageouts 15499758，p95 87.5 ms，span rate 1.93/s

## 三次 Run

trace project `opspilot-lab-304-forced-final-rebuild-live`（`trace_tier=longlived`，三个根 run 都已回读）。

| Run | 结果 | 模型请求 / 工具调用 | 已提交行 | trace |
|---|---|---|---|---|
| `f3dd8fc4-8cdc-4a61-8ed9-cf23acf76158` | published | 11 / 26 | round 1–10 都有工具，round-11 无工具、非 final，合格发布 | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3378346b-c326-4f7c-aff3-e4eda4ab940c/r/00000000-0000-0000-ecd3-ce13021d07bc?poll=true) |
| `ca946e79-75db-45a0-8294-7b347e3c5b55` | published | 10 / 14 | round 1–8 有工具；**round-9 无工具、`final=false`，未被接受；round-10 `final=true`（强制最终轮）合格发布** | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3378346b-c326-4f7c-aff3-e4eda4ab940c/r/00000000-0000-0000-7edf-3ce20fd6a9da?poll=true) |
| `c9f7f63d-eebe-472e-a8a9-119bf716032d` | published | 9 / 15 | round 1–7 有工具；**round-8 无工具、`final=false`，未被接受；round-9 `final=true`（强制最终轮）合格发布** | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3378346b-c326-4f7c-aff3-e4eda4ab940c/r/00000000-0000-0000-d875-4e8a1bebb1f3?poll=true) |

冻结摘要（ADR-0006）：[f3dd8fc4](f3dd8fc4-8cdc-4a61-8ed9-cf23acf76158/summary.json)、[ca946e79](ca946e79-75db-45a0-8294-7b347e3c5b55/summary.json)、[c9f7f63d](c9f7f63d-eebe-472e-a8a9-119bf716032d/summary.json)。原始 ledger 在被忽略的 `tmp/lab-ledgers/m1-01-handoff-runner/`，sha256 分别 `46940bd8…`、`c33a31fb…`、`2a68deaa…`（全值见各 summary）。三次的 `control` 都是 `null`。

预算 20 次都没用完，所以两个 `final=true` 行不是预算型最终轮，只能是前一轮报告未通过校验触发的强制最终轮。前一轮的拒绝原因没有单独读出（`未确认` 是 `REPORT_INVALID` 还是别的校验原因）；下面的重建对照与「重算 feedback 后哈希一致」相符。

## 附带：真实行只读重建（无模型调用）

脚本在会话 scratchpad，连接带 `default_transaction_read_only=on`，对每个 incident 调 `DurableStore.rebuild` 后 `rebuild_transcript(authorized_targets={"m0-otel-20260909"})`；`main` 代码用 scratchpad 里 `git worktree add --detach a7b296a`（用完已 remove），通过 `PYTHONPATH` 切换。

| Run | main a7b296a | 修复 2fc953b |
|---|---|---|
| f3dd8fc4（无强制最终轮） | ok，next_round=12 | ok，next_round=12 |
| ca946e79（强制最终轮 round-10） | `INCOMPATIBLE_STATE` | ok，next_round=11 |
| c9f7f63d（强制最终轮 round-9） | `INCOMPATIBLE_STATE` | ok，next_round=10 |

这说明 #304 的缺陷不只出现在交接后的 follow_up：只要真实 Run 走过强制最终轮，修复前这个 Run 的已提交行就无法重建；修复后可以。这些 Run 都已发布，重建在真实产品路径中只会在「conclusion 提交前崩溃、重新领取」时发生，本次没有制造这种崩溃。

## 未覆盖

- 断言 1（以 `REPORT_INVALID` 交接且前一轮为强制最终轮）：**未出现**，3 次自然 Run 里强制最终轮的报告都合格
- 断言 2（follow_up 后同一 Run 重新领取、重建成功、调用模型且 follow_up 文本进入输入）：**未执行**，没有交接就没有 follow_up
- 确定性覆盖仍以独立验收测试（PG 集成）和 [D-U2 ①](../offline-rebuild.md) 为准

## 费用

- DeepSeek 余额 21:16:36Z 99.45 CNY → 21:24:39Z 99.13 CNY，差 0.32 CNY（同账户可能有其他会话调用，是上界）
- 三次 `known_cost_cny_upper` 合计 2.55 CNY（不计缓存折扣的上界）；实际以余额差为准

## 凭据

证据只含 UUID、哈希、计数与 trace 链接；Prometheus 账号、DeepSeek / LangSmith key 只进进程环境，没有打印。
