# #309 真实 Run 证据（2026-10-22）

两次有界真实 DeepSeek Run（`deepseek-flash`），驱动是 `scripts/m1_live_runner.py`，走的是真实 runner、PostgreSQL 和产品 `otel-demo` 工具 profile（ledger、control、transport 都是产品代码）。

**工具后端是本机替身，不是 kind 实验环境。** kind 实验环境由另一个会话占用，本轮没有使用。替身 `scripts/m1_otel_stub.py` 在 127.0.0.1:29317 上提供合成的 Prometheus `query_range` 和 Jaeger `/api/traces` 数据，每个请求固定延迟 1.5 s。因此这两次 Run 证明的是并行派发、提交顺序和压缩路径，不证明对真实遥测数据的调查质量。

数据库是一次性 PG 17，端口 55731，已执行 `opspilot.schema migrate`。原始 ledger 在被忽略的 `tmp/lab-ledgers/` 下，没有入库；仓库里只有每次 Run 冻结的 `summary.json`，其中记有 ledger 的 sha256。

## 命令

公共环境变量（凭据只从私有 env 文件读取，不打印）：

```
env -u OPSPILOT_OTEL_TOKEN -u OPSPILOT_OTEL_PROMETHEUS_USERNAME -u OPSPILOT_OTEL_PROMETHEUS_PASSWORD \
  PYTHONPATH=. M0_ENV_FILE=<private .env> \
  OPSPILOT_LAB_DSN="host=127.0.0.1 port=55731 dbname=m0_budget user=m0_lab" \
  OPSPILOT_TRACE=lab OPSPILOT_TOOL_PROFILE=otel-demo \
  OPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:29317 \
  OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:29317/jaeger/ui \
  M1_ACCEPTANCE_OUT=docs/evidence/309-live/run-<a|b>
```

替身启动命令：`.venv/bin/python scripts/m1_otel_stub.py --port 29317 --latency-seconds 1.5`

- Run A：`scripts/m1_live_runner.py --tool-profile otel-demo --tool-parallelism 2 --model-requests 4 --lab-round 309-a`
- Run B 第 1 次：`... --tool-parallelism 2 --model-requests 8 --context-tokens 74036 --lab-round 309-b`
- Run B 第 2 次：`... --tool-parallelism 2 --model-requests 10 --context-tokens 76536 --lab-round 309-b`

## Run A：同轮工具并行（cap=2）

- Run `ae302367-2a3c-414c-9486-bbaa0814ed59`，结果 `published`，4 次 HTTP。
- [summary](run-a/ae302367-2a3c-414c-9486-bbaa0814ed59/summary.json)
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8d7ee55b-7f19-4655-a28d-5c13043be6d4/r/00000000-0000-0000-9be8-58d2d2f25d2d?poll=true)
- 三个工具轮次分别有 2、4、4 个调用。全部 10 个工具 span 的父节点都是 run span（从 LangSmith 读回核对）。调用成对重叠，同时在途的从未超过 2 个。读回的 UTC 时间：

| 轮次（step 前缀） | 调用 | 开始 → 结束 |
|---|---|---|
| 7e85752f | metrics_range_query / traces_search | 17:01:10.136→11.714 / 10.145→11.713 |
| 579f726c | metrics ×2，然后 metrics ×2 | 19.786→21.324 / 19.792→21.333，然后 21.332→22.849 / 21.338→22.853 |
| 802f6fa1 | metrics ×2，然后 traces ×2 | 27.750→29.287 / 27.752→29.293，然后 29.295→30.830 / 29.299→30.843 |

- 10 次工具结果全部为 `ok`，提交的 ordinal 在每轮内都连续，没有 `TIMEOUT` 或 `CONTROL_DENIED`。
- ledger 计数：`tool_operations_used` 为 10；`tool_seconds_used` 为 15.1 s，是各调用耗时之和（U-2），墙钟时间约为它的一半。

## Run B：被接受的压缩

**第 1 次（失败，保留）**
- Run `c2ebccff-4749-4ce0-a89d-367a49d9604c`，[summary](run-b/c2ebccff-4749-4ce0-a89d-367a49d9604c/summary.json)，[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/ed676823-31e0-40d3-8911-f7894359ca9c/r/00000000-0000-0000-95f3-a45c21a27092?poll=true)
- 3 个工具轮次之后出现一次被接受的压缩（`ctx0:compact-1`）。但摘要约 11k 字符，压缩后上下文仍放不进 8.5k token 的预算，Run 以 `CONTEXT_EXHAUSTED` 交接。原因是窗口取得过窄，属于本实验的参数选择问题，不是缺陷。

**第 2 次**
- Run `9fc18d11-12f9-4406-a95b-b488a922a59f`，结果 `published`，8 次 HTTP。
- [summary](run-b/9fc18d11-12f9-4406-a95b-b488a922a59f/summary.json)
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/ed676823-31e0-40d3-8911-f7894359ca9c/r/00000000-0000-0000-bab6-b6d909fa4236?poll=true)
- 已提交行的顺序：round-1、round-2、round-3、`ctx0:compact-1`（accepted）、`ctx1:round-4`、`ctx1:compact-2`（accepted）、`ctx2:round-5`、`ctx2:round-6`、conclusion。trace 里 8 个模型 span 中的第 4 个（17:05:17–17:05:44）和第 6 个（17:05:50–17:06:09）就是这两次压缩请求。各工具轮次同样是两两重叠，全部挂在 run span 下。
- 压缩后有没有重查压缩前的同一调用：**未发生**。round-4 和 round-5 的调用（同工具、同参数）与压缩前的调用都不相同，也没有出现 `DUPLICATE_TOOL_CALL`。这项行为以确定性测试为准（独立验收第 1 条，以及 `tests/test_309_loop_parallel_impl.py::test_an_accepted_compaction_lets_the_same_call_run_again`）。
- 13 次工具结果中 12 次为 `ok`，1 次为 `no_data`（frontend-proxy 的 traces，替身不提供这个服务）。

## 费用

- DeepSeek 余额：17:00:47Z 为 99.86 CNY，17:07:29Z 为 99.69 CNY，差额 0.17 CNY。
- 三次 Run 的 `known_cost_cny_upper` 合计 0.548 CNY（Run A 0.137、Run B 第 1 次 0.117、Run B 第 2 次 0.294）。这个数是不计缓存折扣的上界，实际扣费以余额差为准。
