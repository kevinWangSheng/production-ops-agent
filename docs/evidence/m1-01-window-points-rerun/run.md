# M1-01 6e：窗内计算点候选上的 v4 有界开发验收包 2+2 真实 Run（结果：**4/4 发布，v4 口径 1/4 通过；上游口径 4/4**）

- 日期：2026-09-27 UTC 12:16–12:53（本机日志时区为 2026-09-27 05:16–05:53 PDT）。候选代码冻结于 `feature/m1-01-window-points` `b352230`（相对 6d 候选 `8c58978` 的唯一产品改动：`metrics_range_query` 只返回窗内计算点，见[任务记录](../../tasks/2026-09-27-m1-01-window-points.md)），4 个 Run 期间无任何代码改动。worktree 专属 PostgreSQL 55431（本 worktree 新建的 `tmp/m0-b/postgres`，未触碰其他 worktree 的数据；raw 证据字节只在库内，本目录只记 sha256 与长度）。
- 授权：AGENTS.md「费用与真实调用」常设授权；v4 包冻结上限不变（每 Run 4 模型 HTTP、20 工具、300 秒新观测窗、1800 秒 wall；本包总计至多 16 HTTP）。
- 流程：与 6d 完全相同——同一段症状问题（`request.json`，不含注入参数、flag 名或答案）、同一 `run_case.sh`（只改路径与日志名）、同一独立观察脚本 `scripts/otel_demo_observe.py`、同一账本导出 `../m1-01-v4-acceptance/extract_ledger.py`、同一 v4 判据（未修改）。`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 与 worker 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动。`worker.log` 首行 `versions` = `prompt-replay-candidate-04a9d1a1a80e` / `ctx-ctx-policy-v1-b4b4c61e0eec` / **`otel-demo-585e5ef89c39`**，与预期冻结 revision 一致（prompt 与 context policy 与 6d 相同，只有 tool schema 因描述改写而变）。模型 `deepseek-flash`（4 个 Run 每条响应 `response_model` 均为该名）；工具后端是锁定的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-up.log`，Run 后已 `stop`，数据保留）。
- 启动插曲（工程环境，不涉及产品行为）：空库上 web 与 worker 同时执行 `install()`，web 因 `CREATE TABLE` 竞争报 `IDENTITY_CONFLICT` 退出（`web-first-attempt.log`），worker 已建好 schema；单独重启 web 后正常。四个 Run 都在重启后的 web/worker 上完成。
- 观测窗由工作台在提交时刻固定（`window.json`），四个窗互不重叠：12:16:54–12:21:54、12:22:21–12:27:21、12:29:20–12:34:20、12:34:33–12:39:33。
- 故障案：工程钩子 `scripts/otel_demo_lab.py fault inject --experiment-id 6e-fault-01`（`paymentFailure` → 100%）12:28:42Z 注入、12:41:14Z 恢复（`fault-timeline.jsonl`，前后 SHA256 往返一致，与 6b/6d 相同的两个摘要；`observe-after-restore.json`：恢复后 12:47:52–12:52:52 窗内 10 条 checkout trace，0 失败）。注入与恢复都是工程操作，产品与模型无入口。
- 独立观察：每个 Run 提交前对最近 300 秒做一次（`observe-pre.json`），Run 后对精确窗口再做一次（`observe-post.json`）。控制窗前提四次均成立（窗内 checkout trace 6 / 6 / 6 / 3 条，正常窗 0 失败）；故障案在调查者看到任何东西之前已确认 ≥2 条不同的 checkout 失败 trace 与失败的 `PaymentService/Charge` 调用关联（fault-1 提交前 6/6、窗内 6/6；fault-2 提交前 3/3、窗内 3/3）。
- 独立审查：每个 Run 一个全新上下文 Agent（Fable 5.1），拿 v4 判据、PRODUCT-CONSTRAINTS、ADR-0005、模型可见的报告契约与工具描述、本目录证据与 55431 只读查询，不拿执行者评估；结论原文见各目录 `review.md`。审查者按 v4 判据给 P1/P2/P3，并另答一个上游式口径（报告是否正确指出根因 / 正确判定无故障），后者只作补充，不替代 v4 结论。
- 费用：DeepSeek 余额 48.50 → 48.01 CNY（`deepseek-balance-before.json` / `-after.json`；fault-2 结束即时查询只显示 48.11，12 分钟后再查为 48.01，以后者为准，与 6d 同样的延迟入账），本包 16 次请求合计 **0.49 CNY**；token 见各 `ledger.json` 的 `usage_totals`。
- 资源守卫：VM 启动前与每个 Run 前查内存压力，free 29–44%、swap free 1.0–1.1 GB，均在阈值内（最低 26% 出现在审查结束后）。
- 安全：写盘后以 key 全文与末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径与 scratchpad 路径扫描本目录：key 0、口令 0、路径 0；`authorization` 6 处均为模型报告正文里的 "outside this authorization"，非请求头。`run_case.sh`、`lab-up.log`、`web-first-attempt.log` 中的本机路径已换为占位符。

## 汇总表

| Run | 案例 | 事故 / Run | 观测窗（UTC） | 执行 | 报告状态 | 判据 1–5 | P1 / P2 / P3 | 模型 HTTP | 工具 | token（prompt / completion） | 用时（提交→发布） | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | `34d23902` / `2efd6d11` | 12:16:54–12:21:54 | completed，已发布 | `completed` / `partial`，12 claim，7 gaps；6 trace id 与独立观察完全一致 | P P **F** P **F** | 0 / **1** / 2 | 4 | 17（17 ok） | 102,631 / 12,462 | 54 s | 正确判定无故障 |
| 2 | normal-2 | `dc2bce70` / `bfc51712` | 12:22:21–12:27:21 | completed，已发布 | `completed` / `partial`，19 claim，7 gaps；1 条 `no_data` 视图只进 gaps；6 trace id 一致 | P P **F** P **F** | 0 / **4** / 3 | 4 | 19（18 ok，1 no_data） | 98,077 / 11,836 | 49 s | 正确判定无故障 |
| 3 | fault-1 | `2892bfb0` / `d826e986` | 12:29:20–12:34:20 | completed，已发布 | `completed` / `partial`，16 claim，7 gaps；定位 checkout→payment `Charge`（gRPC 2「Invalid token」→ PlaceOrder gRPC 13 → HTTP 500），6 trace id 与独立观察完全一致；1 条 `no_data` 视图只进 gaps | P P P P P | 0 / 0 / 3 | 4 | 18（17 ok，1 no_data） | 104,692 / 16,256 | 72 s | 正确指出根因 |
| 4 | fault-2 | `99943dbe` / `7393ccfa` | 12:34:33–12:39:33 | completed，已发布 | `completed` / `supported`，15 claim，7 gaps；同一定位，3 trace id 与独立观察完全一致 | P P **F** P **F** | 0 / **1** / 3 | 4 | 13（13 ok） | 61,373 / 12,837 | 58 s | 正确指出根因 |

合计：模型 HTTP 16/16，工具 67 次，四个 Run 均在 4/4 请求预算、≤20 工具、1800 秒 wall 内结束；`budget_unknown` 均为 0；证据行 raw sha256 重算全部一致（67/67，各审查者在库内复算；fault-1、fault-2 审查者另复算了 view_sha256）。

## 判定

- **v4 口径：1/4 通过，本有界开发包不通过。** v4 包要求正常/故障各 2 次全部满足 5 条判据且无未处理 P1/P2；fault-1 五条全过、P2 为 0，其余三次都在判据 3 失败（判据 5 随之失败）。四次判据 1、2、4 全过，无 P1，无交接，引用绑定全过（只引用完整 `evidence_id`，非 `ok` 视图只进 gaps）。
- **上游口径：4/4。** 两个正常窗都正确判定无故障且未认证健康；两个故障窗都正确定位到 checkout→payment `Charge`，引用的失败 trace id 与独立观察逐一吻合。
- 每次失败都保留在分母，未重跑替换；四个窗口不可比，不计算混合成功率。

## 与 6d 的对比

| | 6d（`8c58978`） | 6e（`b352230`） |
|---|---|---|
| v4 通过 | 0/4 | 1/4（fault-1） |
| P1 / P2 / P3 合计 | 0 / 13 / 19 | 0 / 6 / 11 |
| 「回看首点当窗内值」类 P2 | 3/4 Run（normal-2、fault-1、fault-2） | **0**（见下） |
| 「区间端点/计数与视图不符」类 P2 | 3/4 Run（normal-1、normal-2、fault-1） | 0 作为 P2；两处降为 P3（normal-2 claim 11「mostly」、fault-2 GetQuote 范围混入子 span） |
| `increase(...[300s])` 按「每步」表述 | fault-1 P2 | 0 |
| 显式零 vs 缺失 series 混淆 | fault-2 P2 | normal-2 P2-1、P2-2 |
| 空 `status_tags` 说成 status 0 | fault-2 P2、normal-1 P3 | normal-2 P2-3、fault-2 P2-1、normal-1 P3 |
| 新出现类型 | — | normal-1 P2：把 trace 工具 `limit`（trace 数）与视图行数（span 数，上限 20）混为一谈，并据此说 `incomplete=false` 的视图「达到上限」；normal-2 P2-4：`incomplete=true` 视图数报 2 实为 3 |
| 上游口径 | 未单独评（四份核心结论均与独立观察一致） | 4/4 |

- **「回看首点」类为何消失，须如实说明前提**：本包 38 个 `metrics_range_query` 视图（9 / 11 / 10 / 8）全部 `lookback_seconds=300`、`lookback_start_at` = 窗口起点、每条 series 只有 1 个点（窗口终点，`source_start_at = source_end_at` = 窗口终点），即模型四次都只用 `[300s]` 范围选择器，候选合同「L = 窗口长度时只在终点求值一次」在全部视图上成立，四位审查者都核对了这一点，也都没有发现任何值被当成窗内区间端点或故障起点。**但 L < 窗口长度的多点路径本包未被模型触发**，「首点仍在窗内」的行为只有合同测试（`tests/test_m1_window_points_contract.py`）覆盖，没有真实 Run 证据；这是 4 次样本的限制，不是候选缺陷。
- 剩余 P2 全部是模型对「缺失 vs 显式」（series 未返回说成 0、`status_tags` 为空说成 code 0）与「视图 vs 后端计数」（`limit`/行数/`incomplete`）的可见范围混淆；四位审查者都没有把任何 P2 归因于产品的证据绑定、投影或执行器，与 6d 相同。
- 审查者的可选产品备注（非本包缺陷，交 lead / 用户）：(1) trace 视图并列暴露 `result_count`（span 数）与 `query.limit`（trace 数）而无单位提示，且不暴露后端返回的 trace 数（normal-1）；(2) trace 视图的 `incomplete: true` 含义未写进模型可见描述（fault-1）；(3) 发布后 `opspilot_incidents.state` 仍为 `queued`（三包都记，四位审查者均记 P3 或可选项，未确认是否有意）。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在本包范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机本时段的全部调用，本时段无其他调用者（未做独立核实）。
- 多点（L < 窗口长度）指标视图在真实 Run 中的模型行为未观察到（见上）。
- 描述改写对模型行为的影响无法从 4 次样本分离。

## 目录

`lab-up.log`、`web.log`、`web-first-attempt.log`、`worker.log`、`fault-timeline.jsonl`、`observe-after-restore.json`、`deepseek-balance-*.json`、`run_case.sh`（账本导出沿用 `../m1-01-v4-acceptance/extract_ledger.py`）；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`、`observe-post.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`。
