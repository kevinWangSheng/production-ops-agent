# M1-01 6c：报告合同修复后的一次有界真实故障 Run（结果：**已发布**）

- 日期：2026-09-27 UTC 05:11–05:40（本机日志 2026-09-26 22:11–22:40 PDT）。代码 `feature/m1-01-report-contract`（基线 `main` `7172ea8`；产品改动见[任务记录](../../tasks/2026-09-27-m1-01-report-contract.md)），worktree 专属 PostgreSQL 55431（数据在 `tmp/m0-b/postgres`，raw 证据字节只在库内，本目录只记 sha256 与长度）。
- 目的：本 PR 触碰报告校验与模型可见文字，按 AGENTS.md 附一次有界真实 Run，看修复后的报告是否通过引用绑定并发布。这**不是** v4 包的 2+2 重跑（重跑是下一项），只算一次故障案样本。
- 路径与上限：与 v4 包相同——`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 与 worker 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动（`worker.log` 首行 `versions` = `prompt-replay-candidate-04a9d1a1a80e` / `ctx-ctx-policy-v1-26cf1129e6c7` / `otel-demo-79788429f835`）；模型 `deepseek-flash`（4 条响应 `response_model` 均为该名）；每 Run 4 模型 HTTP、20 工具、300 秒新观测窗、1800 秒 wall。工具后端是锁定的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-up.log`，Run 后已 `stop`，数据保留）。
- 提交：与 v4 包同一段症状问题（`request.json`），不含注入参数、flag 名或答案；观测窗由工作台在提交时刻固定（`window.json`：05:22:46–05:27:46Z）。
- 故障：工程钩子 `scripts/otel_demo_lab.py fault inject --experiment-id 6c-fault-01`（`paymentFailure` → 100%）05:13:32Z 注入、05:29:05Z 恢复（`fault-timeline.jsonl`，前后 SHA256 往返一致，与 v4 包相同的两个摘要）。注入与恢复都是工程操作，产品与模型无入口。
- 独立观察：`scripts/otel_demo_observe.py`（本 PR 新增，故障谓词收紧为「授权依赖服务的失败 span 且 `CHILD_OF` 父 span 属 checkout」）。提交前对最近 300 秒（`observe-pre.json`）与 Run 后对精确窗口（`observe-post.json`）各一次：控制窗前提成立；10/10 窗内 checkout trace 有 checkout 错误 span 且失败的 payment 子 span，失败依赖只有 `payment`。
- 费用：DeepSeek 余额 49.13 → 49.00 CNY（`deepseek-balance-before.json` / `-after.json`；Run 结束即时查询只显示 49.12，11 分钟后再查为 49.00，以后者为准），本 Run 4 次请求 **0.13 CNY**；token：prompt 106,345（cache hit 27,520）、completion 10,694（reasoning 4,943）。
- 安全：写盘后以 key 全文与末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径扫描本目录，0 命中（`lab-up.log` 与 `run_case.sh` 中的本机路径已换为占位符）。

## 结果

| 项 | 值 |
|---|---|
| 事故 / Run | `632bcf49-c759-5521-80a8-0349780e6425` / `666b3274-d00f-5ae0-9270-d36158677b29` |
| 执行 | `completed`，`published: true`（`events.jsonl` seq 21 `run_completed`；`worker-attempt.txt` `status=published`） |
| 报告 | `m0-report-v2`，`assessment_status=completed`，`conclusion=partial`，12 claim（9 fact、1 hypothesis、1 counter_evidence、1 rejected_hypothesis），7 gaps，14,476 字节 |
| 引用 | 全部 claim 引用完整 `evidence_id`；fact 类只引用 `status=ok`、`citable_as_fact=true` 的视图；两条 `no_data` 视图（email trace 搜索、payment 服务端直方图）只出现在 gaps，模型原文写明「citable_as_fact=false … unknown, not evidence of zero errors or of health」 |
| 结论 | checkout `PlaceOrder` 10 个错误 span（gRPC 13，「Payment request failed. Invalid token」），失败依赖调用为 checkout→payment `Charge`；报告列出的 10 个 trace id 与独立观察的 10 个失败 trace 完全一致 |
| 工具 | 14 次（8 traces_search、6 metrics_range_query）；`rate(...[5m])` 视图 `lookback_seconds=300`、`lookback_start_at=05:17:46Z`，`source_start_at` 为窗内首个样本 |
| 用时 | 提交 05:27:46Z → 发布 05:28:33Z（47 秒） |

本 Run 与 v4 包 fault-1 的差别只在模型可见合同：同样的问题、同样的故障、同样的上限，v4 包 fault-1 在第 4 轮因 fact 引用 `no_data` 视图被拒并交接；本 Run 的模型把两条 `no_data` 视图写进 gaps，报告发布。

## 独立审查

全新上下文 Agent（Fable 5.1）拿目标、用户决定、约束、diff 与本目录原始证据（不拿执行者结论），并对 55431 只读查询。结论：**P1 0；P2 1**（产生证据的描述改动当时未提交——已随本 PR 提交，HEAD 重算的 `tool_schema_revision` 即 `otel-demo-79788429f835`）；P3 4 条：`lab-up.log` 本机路径（已替换占位符）；描述里的最大回看只对范围选择器成立，瞬时选择器的 Prometheus staleness 回看（默认 5 分钟）不计入 `lookback_seconds`（描述与 docstring 已写明只计范围选择器，残余项）；metrics 描述与 `values_format` 一句多约束，不符 DeepSeek 参考 §2.5「一句一个约束」（未改：改字面会再次 bump `tool_schema_revision`，使本 Run 证据与代码不再对应，留给重跑前处理）；`PROJECTION_REVISION` 仍是手工编号（既有设计，本次同时有三个内容哈希 revision 变化，在途 Run 仍会被 blocked）。审查者核对了：校验器四处未改；三份 v4 交接报告用自己的脚本回放仍被拒；本 Run claim 0–8、11 的数值与被引视图内容一致；窗口、时间策略、观察窗、故障时间线互相吻合；v4 判据 1–4 成立，判据 5 即本审查。审查者未核：发布后事故行 `state=queued` 是否有意（v4 包已记的既有问题）。

## PR #56 机器人分诊（一次 push）

Codex 对 `scripts/otel_demo_observe.py` 提出一条 P1：失败 span 只按「trace 里有任一 span 在窗内」计数，窗外的 checkout/依赖失败也会被算进 `fault_confirmed`。这是对验收前提的收紧（AGENTS.md：验收步骤只能收紧），采纳：现在每个失败 span 自身的 `startTime` 须在窗内，依赖失败的 checkout 父 span 也须在窗内，输出记录 `start_us` 以便离线重算。本目录的 `observe-pre.json` / `observe-post.json` 由收紧前的谓词录制，且未记 span 时间戳，实验环境已停，无法对完整 trace 离线重算；改用产品自己的 trace 视图（`ledger.json`，每次搜索最多 20 个有偏采样 span）交叉核对：10 个失败 trace 的 74 个采样 span 全部在窗内，其中 6/10 在采样内就满足收紧后的谓词（checkout 错误 span 在窗内、payment 失败 span 在窗内且 `CHILD_OF` 父 span 为窗内 checkout span），其余 4 条只是采样里没有 payment span，不构成反证；`fault_confirmed` 要求 ≥2，结论不变。这是产品采样上的核对，不替代独立的完整 trace 观察；重跑时用收紧后的脚本。

## 未执行 / 未验证

- 未跑正常案；未做 2+2 重跑（下一项）。一次样本不证明泛化。
- 供应商余额差含本机唯一使用者本时段全部调用，本时段无其他调用者（未独立核实）。
- 恢复后未再做独立观察（v4 包做了 `observe-after-restore.json`）；恢复钩子返回成功且 SHA256 回到原值。

## 目录

`lab-up.log`、`web.log`、`worker.log`、`fault-timeline.jsonl`、`deepseek-balance-*.json`、`run_case.sh`；`fault-1/`：`request.json`、`intake.json`、`window.json`、`observe-pre.json`、`observe-post.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`。导出脚本沿用 `../m1-01-v4-acceptance/extract_ledger.py`。
