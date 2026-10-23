# F3 PR-2（#311 + #310）最小真实证据（2026-10-23）

判据：[#311 合同](../../tasks/2026-10-22-311-source-errors-contract.md)「最小真实证据」与 [#310 合同](../../tasks/2026-10-22-310-investigation-method-contract.md) §3「最小冒烟」。只核对机制，不评报告质量，不算 F3 第 5 步证据。

环境：kind 实验环境 `opspilot-m1` / `otel-demo`（OTel Demo 2.0.2，Prometheus v3.5.0），经只读 nginx 代理 `127.0.0.1:19090`（NodePort 30090）；**不是本机替身**。代码 detached HEAD `9bdd640`（`feature/F3-311-source-errors` 待审版本）。一次性 PG 17（端口 56211，迁移到 `0020_release_comparisons`），用完已停。kind 以 `--holder f3-pr2 --batch f3-pr2-live` 占用 07:11:47Z–07:19Z 后释放。开跑前：`memory_pressure` 空闲 38%，swap 余 1.27 GiB / 9 GiB，frontend span 速率 2.35/s，checkout 服务端 p95 180 ms（Run 后 91 ms），`ALERTS` 为空。未注入故障。

## 1. lab 直连（#311，不经模型）

一次性直连脚本（未入库，副本留在被忽略的 `tmp/`；做法如下）用产品 `otel_demo_executor_factory` 建真实 `ReadOnlyToolExecutor` 和真实 HTTP 传输（investigator 账号，经代理），只把 DurableStore 换成测试用 `FakeStore`（租约与控制快照），发一条少右括号的 PromQL：`sum(rate(traces_span_metrics_calls_total{service_name="checkout"}[5m])`。同一表达式另用原始 GET 各发一次到代理和 Prometheus 本体（`kubectl port-forward svc/prometheus`，F7 后本体只认上游账号，用代理转发时用的 `proxy` 账号）。输出冻结在 [lab-probe-311.json](lab-probe-311.json)（07:13:21Z）。

- 执行器结果：`status=error`，`reason=INVALID_PARAMS`，`source_contact=confirmed`，无 evidence；视图 `trust=gateway`、`citable_as_fact=false`、`content=null`、无 `evidence_id`。
- `source_error`：`{"trust":"untrusted-evidence","http_status":400,"error_type":"bad_data","error":"invalid parameter \"query\": 1:71: parse error: unclosed left parenthesis","error_truncated":false}`。原文不含凭据，脱敏后与原文一致。
- 代理没有改写 body：经代理与直连本体都是 HTTP 400、`application/json`、125 字节，sha256 都是 `c4cd53c40fc68d963822eb3afaf37ca82892a5c68078e9c89ab206ac304dc248`（`bodies_identical: true`）。

## 2. 有界真实 Run（#311 + #310）

驱动 `scripts/m1_live_runner.py`，真实 DeepSeek `deepseek-flash`、产品 `otel-demo` 工具 profile、真实 runner 与 PG。提问是该脚本的固定 `OTEL_QUESTION`（"Checkout appears to show elevated errors. Investigate with the authorized metrics and traces …"），不点名工具。命令（凭据由 shell 从 `tmp/m1-kind-lab/prometheus-investigator.env` 与私有 `.env` 读入，不打印）：

```
set -a; . tmp/m1-kind-lab/prometheus-investigator.env; set +a; unset OPSPILOT_OTEL_TOKEN
PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=56211 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_TOOL_PROFILE=otel-demo \
  OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:19090 \
  OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:16686/jaeger/ui \
  M1_ACCEPTANCE_OUT=docs/evidence/f3-pr2-live OPSPILOT_LEDGER_DIR=<worktree>/tmp/f3-pr2-live/ledgers \
  .venv/bin/python scripts/m1_live_runner.py --tool-profile otel-demo --tool-parallelism 2 --model-requests 8 --lab-round f3-pr2-live
```

- Run `b442e7c5-0521-4391-b374-b077593f724a`，事故 `80667bf5-652d-42d5-a431-d9005be41d4b`，07:14:25Z–07:16:07Z；8 次模型请求，15 次工具调用。
- [summary.json](b442e7c5-0521-4391-b374-b077593f724a/summary.json)；原始 ledger sha256 `8efee6eeec6f2756b460733ea5dc6340964c879e6ff81cfafd96304b1ff26ae5`，在被忽略的 `tmp/f3-pr2-live/ledgers/` 下，未入库。
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f49c1e40-9b05-4734-bd65-435e47afddf9/r/00000000-0000-0000-0cf5-3dbbf82cb13b?poll=true)（项目 `opspilot-lab-f3-pr2-live`，read_back found，dropped_spans 0）。

逐项核对（来源：ledger、PG 行（停库前读取）、trace 回读）：

| 项 | 结果 | 来源 |
|---|---|---|
| `prompt_revision` = `prompt-replay-candidate-2f30c2380b23` | 通过 | ledger `attempts[0].outcome.loop`、Run 行 `versions`、trace 根 span metadata |
| `projection_revision` = `m1-01-tool-view-v10` | 通过：15/15 个工具结果视图都带此值 | PG `opspilot_steps.tool_results`、trace metadata |
| `tool_schema_revision` = `otel-demo-68ed2ca8f5d6` | 通过 | Run 行 `versions`、trace metadata |
| system 消息含 INVESTIGATION_METHOD 与 IMPACT_SCOPE | 通过：8 次模型请求各有 1 条 system 消息，两个常量全文都是其子串 | trace 回读 |
| 初始上下文含 `data_sources` | 通过：Run 输入 `evidence_context.data_sources` 为 available（prometheus：get_label_values / get_metric_names / metrics_range_query；jaeger：traces_search）+ not_integrated（kubernetes_state / pod_logs / change_history）+ 固定 instruction；第 1 次请求的第 2 条 user 消息带同一对象 | PG Run `input`、trace |
| 工具请求全部只读 | 通过：只调用了 get_metric_names ×1、get_label_values ×2、metrics_range_query ×9、traces_search ×3，全部是 profile 的只读工具，传输只发 GET | PG steps、trace tool span |
| Run 正常结束 | 按既有规则交接：`handed_off` / `REPORT_INVALID`，Run `waiting_human`，事件流以 `run_handoff` 结束，未发布 | summary `verdicts` |
| 模型自然触发 400 | **未触发**：15 次调用结果为 ok 11、no_data 3、`RESULT_TOO_LARGE` 1（traces_search），没有任何视图带 `source_error` | PG steps、trace |

交接原因（照实记录，不评质量）：预算用尽后的强制最终报告能解析为 v2，但一条 `counter_evidence` 引用了 `d68a7979-ad3b-47f0-9c00-d8ab2573b9ac-meta`。这个 evidence_id 本 Run 没有交付过（交付的是 `…-t0:…` / `…-t1:…`），所以引用校验拒绝。这是既有的引用校验规则，与本 PR 改动无关。

## 费用

DeepSeek 余额：07:14:13Z 97.65 CNY（Run 前）→ 07:19:20Z 97.44 CNY，差额 0.21 CNY。同一账号可能有其他会话同时在用，扣费也可能延迟，所以差额只作参考。`known_cost_cny_upper` 0.380（不计缓存折扣的上界；prompt 106,944 / completion 16,656 tokens）。lab 直连不调用模型，不产生模型费用。
