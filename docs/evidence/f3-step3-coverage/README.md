# F3 第 3 步真实 Run 证据（PR-2，2026-10-24）

两次有界真实 DeepSeek Run（`deepseek-flash`），驱动 `scripts/m1_live_runner.py`，走真实 runner、本地 PostgreSQL 和产品 `otel-demo` 工具 profile（ledger、control、transport 都是产品代码）。合同见 F3 PR-2 预审 r1 第 8 节（D6-a、D7-a）。

**工具后端是本机替身，不是 kind 实验环境。** 替身 `scripts/m1_otel_stub.py` 在 127.0.0.1:29417 提供合成的 Prometheus `query_range`、发现工具的 `/api/v1/label/<label>/values` 和 Jaeger `/api/traces`，每个请求固定延迟 1.0 s；替身不解析 PromQL，任何表达式都返回同样三条序列（场景 A 的模型在 gaps 里也指出了这一点）。所以这两次 Run 证明的是覆盖缺口节与截断标注在真实模型行为下的展示，不证明对真实遥测的调查质量。

数据库是本 worktree `tmp/` 下的一次性 PG 17（端口 56057，schema 在 head `0020_release_comparisons`）。原始 ledger 在被忽略的 `tmp/lab-ledgers/` 下，没有入库；仓库里只有每次 Run 冻结的 `summary.json`，记有 ledger sha256。

模型可见面与 main `e2fcea4` 相同：`tool_schema_revision=otel-demo-68ed2ca8f5d6`、`prompt_revision=prompt-replay-candidate-2f30c2380b23`、`context_policy_revision=ctx-ctx-policy-v1-a30e65ea52f4`、`PROJECTION_REVISION=m1-01-tool-view-v10`（D9-a：PR-3b 开工时若这些值变了，在最终模型可见面上重跑场景 A 一次）。

## 命令

公共环境变量（凭据只从私有 env 文件读取，不打印）：

```
env -u OPSPILOT_OTEL_TOKEN -u OPSPILOT_OTEL_PROMETHEUS_USERNAME -u OPSPILOT_OTEL_PROMETHEUS_PASSWORD \
  PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=56057 dbname=m0_budget user=postgres" \
  OPSPILOT_TRACE=lab OPSPILOT_TOOL_PROFILE=otel-demo \
  OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:29417 \
  M1_ACCEPTANCE_OUT=docs/evidence/f3-step3-coverage/<missing-source|truncated>
```

替身：`.venv/bin/python scripts/m1_otel_stub.py --port 29417 --latency-seconds 1.0`

- 场景 A（缺源）：`OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29418/jaeger/ui`（29418 无监听，真实 urllib 连接被拒），`python -m scripts.m1_live_runner --tool-profile otel-demo --model-requests 100 --lab-round f3-pr2-missing-source --f3-coverage --f3-answer evals/f3/missing-source/answer.json`
- 场景 B（截断）：`OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29417/jaeger/ui`，`python -m scripts.m1_live_runner --tool-profile otel-demo --model-requests 3 --lab-round f3-pr2-truncated --f3-coverage`

冻结答案 `evals/f3/missing-source/answer.json` 在首个 Run 前写定，sha256 `b9a541ab9d13b7fce5433a923263eb133936b95e59822561d136ff6fa3ddaab5`（由 `tests/test_f3_pr2_coverage_impl.py` 钉住）。

## 场景 A：Jaeger 不可达

- Run `5b4f35a8-72f8-43d1-a838-f97fe3f44b36`，结果 `published`，13 次 HTTP。
- [summary](missing-source/5b4f35a8-72f8-43d1-a838-f97fe3f44b36/summary.json) · [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/dfbe1b3f-cec0-415d-be30-90e81ca3bda0/r/00000000-0000-0000-d9e4-3b0a4b4d70e3?poll=true)
- 覆盖节（投影与页面一致）：jaeger `failed`（ok 0 / 非 ok 5，`SOURCE_UNAVAILABLE ×5`）；prometheus `partial_failure`（ok 14、no_data 3、`INVALID_PARAMS ×1`）；kubernetes_state、pod_logs、change_history 为 `not_integrated`。页面 `#coverage-gaps` 出现，`#not-integrated` 段落仍在，无 `#collection-truncated`；投影 `collection_truncated=False`。
- U6-a 判定（脚本按冻结答案计算）：**通过**。报告 `completed / partial`（非 `supported`），无 fact 引用满足签名的证据，jaeger 行 `gap=true`。
- 照实记录（不设门）：模型自己的 gaps 点名了 Jaeger 不可用，但写的是「three separate attempts」，确定性计数是 5 次。

## 场景 B：上限处截断

- Run `20eb53bd-2cca-43e8-b096-36b33d84c4b3`，3 次 HTTP（`model_requests` 上限 3 全部用完），前两轮都调用工具，第 3 轮为强制最终轮。
- [summary](truncated/20eb53bd-2cca-43e8-b096-36b33d84c4b3/summary.json) · [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/5bdbd112-f611-4811-9783-47637a26502e/r/00000000-0000-0000-bc56-91bb22eef779?poll=true)
- 结果是**交接**，不是发布：强制最终轮的报告合格但 `assessment_status=incomplete`，按现有规则（ADR-0005）以 `INCOMPLETE_INVESTIGATION` 交接，Run `waiting_human`。合同第 8 节判据允许「照实记录为交接」。
- 截断标注：投影 `collection_truncated=True`；页面显示交接报告，`#collection-truncated` 出现。判据**通过**。覆盖节：prometheus `all_ok`（ok 3、no_data 1），jaeger `not_queried`。

## 失败轮与费用

- 无失败轮，未重跑。
- DeepSeek 余额：08:05:46Z 为 95.48 CNY，08:08:38Z 为 95.45 CNY，差额 0.03 CNY。两次 `known_cost_cny_upper` 合计 0.660 CNY（A 0.603、B 0.057），是不计缓存折扣的上界，实际扣费以余额差为准。
