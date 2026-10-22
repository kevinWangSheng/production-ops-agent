# #307 A：Prometheus 发现工具真实运行（U4-a）

- 日期：2026-10-22 16:17–16:29Z；分支 `feature/F3-307-prom-discovery`，commit `4358a5b`（已合入 main 的 F3 PR-3a；干净工作区，本目录 `run.md`、`buildinfo.json`、`live-runs/` 为运行后新增，`review-2026-10-22.md` 是已有文件，未改动）
- 被测内容：新增的只读发现工具 `get_metric_names` / `get_label_values`（合同见 [任务记录](../../tasks/2026-10-22-307a-prom-discovery-contract.md)，判据 U4）
- 冻结摘要：[live-runs/d689c695-c9f4-5492-9a64-37a5a77a3cb7/summary.json](live-runs/d689c695-c9f4-5492-9a64-37a5a77a3cb7/summary.json)。原始 ledger 不入库（库中全部 step、证据原文、事件，以及 trace 子 run 全量），sha256 `d477138d…7b63fac`，1477566 字节（ADR-0006）
- 类别：有界真实软件环境运行加真实模型调用，只做了一次 Run，没有重跑。不是生产证明

## 环境

- 实验环境：kind `kind-opspilot-m1`，只读查询。未注入故障，未改 flagd，未做任何 kind/kubectl 写操作。
- lead 在 16:16Z 的检查：余额 99.98 CNY，lab health 全部正常，lab 上没有其他 web、worker 或 harness；swap used 12692/13312M（free 620M），内存 free 30%。本 Run 不评判延迟。
- 本执行者记录的 swap：开工前（第一次尝试，14:00Z）used 12023.19M / 13312M。Run 前（16:16:57Z）used 12587.38M / 13312M（free 724.62M）。Run 后（16:29:24Z）used 13038.25M / 14336M（free 1297.75M；系统在此期间把 swap 总量扩到 14336M）。
- PostgreSQL 17.9：本 worktree `tmp/live/pg`，端口 55731，库 `m0_budget`。空库执行 `make migrate`，升到 `0017_web_suspension_grants`；合入 main 后再跑一次，结果 `schema unchanged`。用 `scripts.m0.postgres_lab runtime-roles`（`OPSPILOT_LAB_DSN` 指向本库）建了 `opspilot_web_lab` 和 `opspilot_worker_lab`。
- 进程：
  - web：`python -m opspilot.web serve`，`OPSPILOT_BIND=127.0.0.1:18431`，`OPSPILOT_TOOL_PROFILE=otel-demo`，`OPSPILOT_UI_USERS` 一个实验账号（口令只存在本机临时文件），`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`（本步不做自动观察）。
  - worker：`python -m opspilot.worker_main`，同一 profile，`OPSPILOT_TRACE=lab`，`LANGSMITH_PROJECT` 设为本轮 project。Prometheus 用 investigator 账号（env 文件由主仓库 `kind_lab.py env-file investigator` 生成，权限 600）。
- trace：round `307a-discovery-20261022`，project `opspilot-lab-307a-discovery-20261022`（`0e83e022-a3bd-4b81-9673-7a5119574180`）。运行前经 `lab_evidence.prepare_lab_project` 建立，回读 `trace_tier=longlived`。
- 防循环上限：用产品默认值，没有另加。

## 版本

- worker：`prompt-replay-candidate-dd9018031487` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-5270fd8c543f`（与 lead 给出的预期 tool_schema_revision 一致；合入 PR-3a 前是 `otel-demo-021751107140`）。
- 证据视图 `projection_revision`：`m1-01-tool-view-v8`。发现工具 `tool_version`：`v1`。
- Prometheus：用 lab 账号取 `/api/v1/status/buildinfo`，版本 `3.5.0`，revision `8be3a9560fbdd18a94dedec4b747c35178177202`，buildDate `20250714-16:18:17`，go1.24.5。原文见 [buildinfo.json](buildinfo.json)。

## 输入

`POST /intake/ui`（Basic Auth 实验账号，`target_id=m0-otel-20260909`，幂等标签 `u4a-live-1`）。问题没有点名任何工具：

```
Is the recommendation service healthy over the last hour? Check its request rate, errors and resource usage, and tell me which signals are missing.
```

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:02 | 第一次尝试：余额 -0.01 CNY，1-token 探测返回 402 Insufficient Balance。没有起 web/worker，没有提交 Run |
| 16:16:57 | 用户充值后：重启 PG，`make migrate`（unchanged） |
| 16:17:21 | 记余额 99.98 CNY；web（PID 99800）与 worker（PID 99802）启动，worker 日志 `trace export enabled mode=lab` |
| 16:17:35 | `POST /intake/ui` → 201，事故 `0c044c4c-90bb-5467-b2f3-832dd9a927df`，Run `d689c695-c9f4-5492-9a64-37a5a77a3cb7` |
| 16:17:36–16:19:24 | worker 调查：11 次模型请求、29 次工具调用，`status=published` |
| 16:27:17 | 记余额 99.86 CNY |
| 16:27:25 | 对 worker、web 发 SIGTERM，两者正常退出 |
| 16:29:24 | 冻结 summary |

## 结果（对照 U4）

模型在第 1 轮就主动调用了发现工具。全 Run 共 5 次发现调用：

| 轮 | 工具 | 参数（窗口均为 15:17:35Z–16:17:35Z） | status | result_count | incomplete | 原始 warnings | 证据记录 |
|---|---|---|---|---|---|---|---|
| 1 | `get_metric_names` | `match={service_name="recommendation"}` | ok | 28 | false | 无 | 有，committed，raw 1029 B，sha 复算一致 |
| 1 | `get_label_values` | `label=__name__`，`match={__name__=~"traces_.*\|rpc_.*\|http_.*\|container_.*\|process_.*"}` | ok | 100 | **true** | `["results truncated due to limit"]` | 有，committed，raw 3973 B，sha 复算一致 |
| 2 | `get_label_values` | `label=span_name`，`match={service_name="recommendation",__name__="traces_span_metrics_calls_total"}` | ok | 4 | false | 无 | 有，committed，raw 192 B，sha 复算一致 |
| 4 | `get_metric_names` | `match={namespace="otel-demo",pod="recommendation-9d497476-gqmk2"}` | ok | 88 | false | 无 | 有，committed，raw 3048 B，sha 复算一致 |
| 9 | `get_metric_names` | `match={__name__=~".*cfs.*\|.*throttl.*\|.*_pressure_.*", namespace="otel-demo", pod="recommendation-9d497476-gqmk2"}` | ok | 6 | false | 无 | 有，committed，raw 315 B，sha 复算一致 |

| U4 判据 | 实际 |
|---|---|
| 至少一次发现调用，status 为 ok 或 no_data | 通过：5 次，全部 `ok` |
| `result_count ≤ 100` | 通过：最大 100 |
| 有 EvidenceRecord | 通过：5 次都有 `opspilot_evidence` 行（committed、adopted），原始字节的 sha256 与行内记录一致 |
| `incomplete` 与原始 warnings 一致 | 通过：5/5 一致。其中一次真实触发了 Prometheus 3.5.0 的截断：原始 `data` 100 条，带 warning `results truncated due to limit`，视图 `incomplete=true`、`truncated=false`、`limit=100`，`incomplete_reason` 提示改用更具体的 match（与 R7 一致） |
| trace 与 summary.json | 通过：见下 |

**U4-a 判定：通过。**

其余工具调用：`metrics_range_query` 22 次，全部 ok；`traces_search` 2 次，一次 `RESULT_TOO_LARGE`（全小时、limit 30，105442 字节超限），模型随后缩小到 15 分钟、limit 4，返回 ok。

Run 结束：完成，没有交接。`outcome_from_durable`：`final_state=completed`，`decision=report_available`，`handoff_reasons=[]`，28 个证据 ID，`actions=[read_only_query]`。报告 `assessment_status=completed`，`conclusion=partial`。

报告要点（属于模型判断，不是本步的判定项）：recommendation 在这一小时内 span 量平稳，自身 server span 没有 error，延迟与前一小时相当，CPU 和内存余量充足，pod 在窗口内没有重启。缺失信号是报告引用第 1 轮发现结果得出的：28 个指标名里没有服务级的请求或错误计数器；没有日志和 span 错误详情；没有 SLO；有 PSI 压力指标但没有 CFS 节流指标（第 9 轮发现）。报告写明「不在列表里不证明不存在」，与 R8 的 `cannot_prove` 一致。

## LangSmith trace

- [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/0e83e022-a3bd-4b81-9673-7a5119574180/r/00000000-0000-0000-6afd-48097428da50?poll=true)：`read_back = found`，OTel trace `b28508b5b5b983ab2131cf1dfd64ad3d`，元数据 `run_id` 与本 Run 一致，status success。project 内只有这一个根 run。
- 子 run：11 个 `deepseek.chat.completions`（llm 11），29 个工具 span（`metrics_range_query` 22、`get_metric_names` 3、`get_label_values` 2、`traces_search` 2），与库中计数一致。worker 日志没有 `Attributes dict is full` 警告，也没有 error。

## 失败与未覆盖

- 第一次尝试（14:02Z）因 DeepSeek 余额为负（402）没有发起 Run，不算 U4 的失败轮，也没有产生 Run 记录。
- 本次 Run 没有出现失败，没有重试。
- 没有出现 `no_data`，也没有 `INVALID_PARAMS` 类错误。这些路径只由合同测试覆盖，本 Run 没有观察到。
- 观察到但不属于本步判定：报告自述一处内部不一致，即按步长计算的 error increase 为 0，而原始计数器在窗口内 +6。本次没有核查原因（未确认）。

## 费用

DeepSeek 余额 99.98 → 99.86 CNY（差 0.12 CNY），取于 16:17:21Z 与 16:27:17Z。lead 确认 lab 上没有其他 harness，但同一账户在其他地方的消耗未确认，所以 0.12 CNY 是本次费用的上界。worker 的 postmortem 线程在 Run 发布后没有额外的模型调用：trace 中 llm 11 次，与 `budget_spent=11` 一致。token：309600 输入 / 16912 输出 / 合计 326512（LangSmith 根 run 聚合）。

## 凭据

- gitleaks 8.30.1 对本目录做 `dir` 扫描：`no leaks found`。
- 另用以下凭据的原值，在本目录逐一做字面匹配：DeepSeek key、LangSmith key、Prometheus investigator 口令、Prometheus lab 口令、实验 UI 口令。结果均为 0 次（不打印原值）。
- 请求中用的是幂等标签 `u4a-live-1`，本目录没有 request.json。
