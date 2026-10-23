# #308 PR2 真实 Run 证据（2026-10-22）

判据（[合同](../../tasks/2026-10-22-308-overlimit-evidence-contract.md) 最小 smoke Run）：出现超限拒绝（带 evidence_id 与摘要），且至少一次 `read_evidence` 返回 ok。只看机制，不评调查质量。

两次有界真实 DeepSeek Run（`deepseek-flash`），驱动 `scripts/m1_live_runner.py`，真实 runner、PostgreSQL 与产品 `otel-demo` 工具 profile。**工具后端是本机替身 `scripts/m1_otel_stub.py`（127.0.0.1:29318），不是 kind 实验环境**；替身对每个不含 `topk`、不点名 pod 的 range query 追加 `--wide-series N` 条合成 per-pod 序列，使视图超过 25k token。数据库为一次性 PG 17（端口 56120，`schema unchanged: 0018_alert_correlation`）。原始 ledger 在被忽略的 `tmp/` 下，未入库；仓库只有冻结的 `summary.json`（含 ledger sha256）。

版本：`prompt-replay-candidate-dd9018031487` / `ctx-ctx-policy-v1-a30e65ea52f4`（均与 main 相同）/ `otel-demo-2a944edd110a`（main 为 `otel-demo-5270fd8c543f`）；`projection_revision` `m1-01-tool-view-v9`。代码 HEAD `c8516f3`。

命令（凭据只由脚本从私有 env 文件读取，不打印）：

```
env -u OPSPILOT_OTEL_TOKEN -u OPSPILOT_OTEL_PROMETHEUS_USERNAME -u OPSPILOT_OTEL_PROMETHEUS_PASSWORD \
  PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=56120 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_TOOL_PROFILE=otel-demo \
  OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:29318 \
  OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29318/jaeger/ui \
  M1_ACCEPTANCE_OUT=docs/evidence/308-pr2-read-evidence-live/run-<1|2> \
  .venv/bin/python scripts/m1_live_runner.py --tool-profile otel-demo --tool-parallelism 2 --model-requests 8 --lab-round 308-pr2-<a|b>
```

## Run 1（失败，保留）

- 替身 `--wide-series 30 --latency-seconds 0.5`；Run `6197ab71-b8ef-4e43-868d-7dbefd1c9707`，`handed_off` / `INCOMPLETE_INVESTIGATION`，7 次 HTTP。
- [summary](run-1/6197ab71-b8ef-4e43-868d-7dbefd1c9707/summary.json)，[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f1c2527d-7eb2-4be1-8d47-b08f11a3f798/r/00000000-0000-0000-b7dc-8a83de096886?poll=true)
- **没有出现超限**：模型对 24 h 窗口用 `step_seconds=3600`，每条序列只有约 25 个点，33 条序列的视图约 5.8 KB。判据不满足。原因是替身数据量按点数放大、而模型选了粗步长，属于实验参数问题。

## Run 2（通过）

- 替身改为每条 per-pod 序列最多 4 个点、`--wide-series 400`（不随步长放大，远低于 1 MiB 字节上限；该改动在本目录同一提交入库）。Run `1e76a35b-442f-40ca-af02-cae6179fb9dc`，`published`，8 次模型请求、14 次工具调用。
- [summary](run-2/1e76a35b-442f-40ca-af02-cae6179fb9dc/summary.json)，[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/fbfb7b8d-b93a-43bf-a698-a83f20a1ce54/r/00000000-0000-0000-b8cb-22b3887c4c80?poll=true)
- 6 次 `metrics_range_query` 超限：`error/RESULT_TOO_LARGE`，模型视图带 `evidence_id`、`adopted:false`、`view_tokens`（25908–42090）/`max_view_tokens` 25000、`total_rows` 403、`row_unit` series、`suggested_limit`（380 或 231）、`series_count`、`total_points`、`label_cardinality`、topk `suggestion`；库内对应行 `adopted=false`、`committed=true`，view 与模型视图一致。
- 2 次 `read_evidence` 返回 `ok`（`offset 0, limit 10` 与 `offset 0, limit 6`），`slice.total_rows` 403、`returned` 10/6，`parent_tool` metrics_range_query；均为 adopted、committed 的新证据。
- 发布的报告把其中一个片段（`a9a5799c-…-t0:690d24ba-…`）作为 `fact` 引用，`time_scope_ref` `policy-window-1`，通过报告校验（R4 时间绑定生效）。
- 未出现同参重查超限调用，DUPLICATE 路径本次未触发（以确定性测试为准）。

## 费用

DeepSeek 余额：23:18:53Z 98.95 CNY（Run 1 前）→ 23:21:29Z 98.95 CNY（Run 2 前，Run 1 的扣费尚未体现）→ 23:22:48Z 98.91 CNY，两次合计差额 0.04 CNY。`known_cost_cny_upper`：Run 1 0.370、Run 2 0.261（不计缓存折扣的上界）。
