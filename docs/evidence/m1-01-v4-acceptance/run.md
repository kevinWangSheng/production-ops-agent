# M1-01 6b：v4 有界开发验收包的真实 2+2 Run（结果：**不通过**）

- 日期：2026-09-27 UTC 03:46–04:30（本机日志时区为 2026-09-26 20:46–21:30 PDT）。代码 `main` `73e6100`（worktree `chore/m1-01-v4-acceptance`，无产品代码改动）。
- 授权：AGENTS.md「费用与真实调用」常设授权；v4 包冻结上限（每 Run 4 模型 HTTP、20 工具、300 秒新观测窗；本包总计至多 16 HTTP）。
- 路径：真实产品路径——`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 与 worker 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动（`worker.log` 首行 `profile=otel-demo`，`tool_schema_revision otel-demo-3936d7ae7edb`）；模型 `deepseek-flash`（4 个 Run 的每条响应 `response_model` 均为该名）；工具后端是锁定的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-up.log`）；本 worktree 专属 PostgreSQL 55431（数据保留在 `tmp/m0-b/postgres`，raw 证据字节只在库内，本目录只记 sha256 与长度）。
- 四次提交使用同一段症状问题（`request.json`），不含注入参数、flag 名或答案；每次的观测窗由工作台在提交时刻固定（`window.json`），四个窗互不重叠。
- 故障案：工程钩子 `scripts/otel_demo_lab.py fault inject --experiment-id v4-fault-01`（M0 `development_fault.py`，`paymentFailure` → 100%）于 04:03:12Z 注入、04:21:52Z 恢复（`fault-timeline.jsonl`，前后 SHA256 往返一致，flag 已回到 `off`；`observe-after-restore.json`：恢复后 8 条 checkout trace 无失败调用）。注入与恢复都是工程操作，产品与模型无入口。
- 独立观察（与产品 Run 分离的脚本 `observe.py`，直接读 Prometheus/Jaeger）：每个 Run 提交前对最近 300 秒做一次（`observe-pre.json`），Run 后对精确窗口再做一次（`observe-post.json`）。控制窗前提（≥2 条不同 checkout trace、相关调用有正增量）四次均成立；故障案在调查者看到任何东西之前已确认 ≥2 条不同的 checkout 失败 trace 与失败的 `PaymentService/Charge` 调用关联（fault-1：4/4，fault-2：8/8）。
- 独立审查：每个 Run 一个全新上下文 Agent（Fable 5.1），拿 v4 判据、PRODUCT-CONSTRAINTS、ADR-0005、本目录证据与 55431 只读查询，不拿执行者评估（且被要求不读 `handoff-cause.md`）；结论原文见各目录 `review.md`。
- 费用：DeepSeek 余额 49.62 → 49.13 CNY（`deepseek-balance-before.json` / `-after.json`），本包 16 次请求合计 **0.49 CNY**；token 见各 `ledger.json` 的 `usage_totals`。
- 安全：写盘后以 key 全文与末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径扫描本目录，0 命中（`run_case.sh` 中的本机路径已换为占位符）。

## 汇总表

| Run | 案例 | 事故 / Run | 观测窗（UTC） | 执行 | 报告状态 | 判据 1–5 | P1 / P2 / P3 | 模型 HTTP | 工具 | token（prompt / completion） | 用时 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | `0628a430` / `546f3ba3` | 03:41:59–03:46:59 | completed，已发布 | `completed` / `partial`，11 claim | P P **F** P **F** | 0 / **3** / 6 | 4 | 16 | 118,070 / 8,769 | 41 s |
| 2 | normal-2 | `2599753a` / `f771d566` | 03:51:56–03:56:56 | handoff `REPORT_INVALID`，未发布，`waiting_human` | 正文可解析（`completed`/`partial`，16–17 claim），fact 引用 `no_data` 视图被引用绑定拒绝 | **F** P **F** P **F** | 0 / **1** / 5 | 4 | 16 | 71,661 / 11,348 | 47 s |
| 3 | fault-1 | `5082fe9a` / `017adaaf` | 04:05:58–04:10:58 | handoff `REPORT_INVALID`，未发布，`waiting_human` | 正文可解析，正确定位 checkout→payment `Charge`（gRPC 13，Invalid token，4 trace id 与独立观察完全一致），claim 7 引用两条 `no_data` 视图被拒 | **F** P P P **F** | 0 / **1** / 3 | 4 | 12 | 72,989 / 9,371 | 43 s |
| 4 | fault-2 | `3a5090fe` / `86fe860c` | 04:15:09–04:20:09 | handoff `REPORT_INVALID`，未发布，`waiting_human` | 正文可解析，定位与 8 trace id 与独立观察一致，但全部 14 条 claim 引用截断的 `<step>-tN`（即 `operation_id`）而非交付的 `evidence_id` | **F** **F** **F** P **F** | 0 / **3** / 3 | 4 | 14 | 112,064 / 9,184 | 44 s |

合计：模型 HTTP 16/16，工具 58 次，四个 Run 均在 4/4 请求预算、≤20 工具、1800 秒 wall 内结束；`budget_unknown` 均为 0；证据行 raw sha256 重算全部一致（58/58）。

## 判定

**本有界开发包不通过（0/4）。** v4 包要求正常/故障各 2 次全部满足 5 条判据且无未处理 P1/P2；实际：

1. normal-1 是唯一发布的报告，核心结论（窗口内无错误、无失败依赖调用）与独立观察一致，但审查发现 3 个 P2（span 计数/状态字段省略混淆、显式零 series 说成缺失、`rate(...[5m])` 在窗口起点回看窗口之前的数据被报成窗内异常）。
2. normal-2、fault-1、fault-2 都在第 4 次（最后一次）请求给出可解析、内容基本正确的报告后，被 `unsupported_citations` 引用绑定拒绝，按 ADR-0005 交接、未发布、事故开放、人工控制可用（审查确认状态/事件/行一致，无 P1）。判据 1 明确「无法调查可有准确 handoff，但不能抵作正常/故障正向能力通过」。
3. 每次失败都保留在分母，未重跑替换；四个窗口不可比，不计算混合成功率。

## 审查发现中的产品项（不在本 PR 修，交 lead/用户）

- **P2（产品，normal-2 审查记 P3-1、fault-1 与 fault-2 审查记 P2）：`REPORT_CONTRACT` / `EVIDENCE_DISCIPLINE` 未告诉模型 fact 类 claim 只能引用 `status == "ok"` 的视图，而工具结果对 `no_data` 视图标 `adopted: true` 并给出 evidence_id。** 模型按判据 3「缺失 series 是未知」写成 fact 并引用 `no_data` 视图，被 `reports.py` `unsupported_citations` 的状态检查拒绝。normal-2 与 fault-1 的交接直接由此触发；fault-2 在把截断 id 修正后的反事实复跑仍会因此被拒。审查者指出该规则在设计文档、C3、v4 包正文与测试中均无出处，属合同层取舍（把规则写进模型可见合同 / 允许 `no_data` 支撑「缺失」事实 / 只拒单条 claim 并重试），需用户决定。
- **P2（产品，normal-1 审查 P2-3）：`otel-demo` metrics 工具描述承诺「越窗读取返回错误」，但 `promql_problem` 允许范围选择器在窗口起点回看自身长度，视图 `source_start_at` 因而低估了源数据范围**；后果是背景异常（recommendation flagd EventStream）被按错误的时间范围报告。normal-2 审查也观察到同一现象（记 P3-2）。
- P3（产品）：视图同时暴露 `operation_id`（`evidence_id` 的严格前缀）与 `evidence_id`，无提示只有后者可引用（fault-2 全部引用截断即踩此坑，审查定为模型缺陷、产品面为诱因）；交接后工作台头部显示事故 `state queued`（fault-1、fault-2 审查各记一条，ADR-0005 未定义该字段，未确认是否有意）。事后核对：normal-1 已发布后 `opspilot_incidents.state` 也仍为 `queued`（本次执行者观察，未确认）。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在 6b 范围；有 PG 合同用例覆盖）。
- 独立审查者只能重算视图与 raw 的 sha256、比对独立观察，未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机唯一使用者本时段的全部调用，本时段无其他调用者（未做独立核实）。

## PR #55 机器人分诊（一次 push）

Codex 对 `observe.py` 提出两条：P1「控制窗前提只检查 checkout 有正增量，未要求依赖调用」、P2「distinct/failing 计数未按 `any_span_in_window` 过滤」。两条都是对验收前提的收紧（AGENTS.md：验收步骤只能收紧），均采纳：`observe.py` 现要求 checkout、payment 与 checkout 客户端调用三组 series 都有正增量，且只统计窗内有 span 的 trace。观察文件在改动前录制，因此用 `recheck_observations.py` 对已录的 9 份观察按收紧后的谓词重算（`observe-recheck.json`）：9/9 窗内 trace = 返回 trace（无窗外 trace 混入），9/9 依赖流量成立，四个 Run 的 `control_window_ok` 与故障案的 `fault_confirmed` 结论不变。

## 目录

`lab-up.log`、`web.log`、`worker.log`、`fault-timeline.jsonl`、`observe-after-restore.json`、`deepseek-balance-*.json`、脚本 `observe.py` / `extract_ledger.py` / `run_case.sh`；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`、`observe-post.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`，交接的 Run 另有执行者的 `handoff-cause.md`。
