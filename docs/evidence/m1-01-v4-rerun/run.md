# M1-01 6d：冻结候选上的 v4 有界开发验收包 2+2 真实 Run（结果：**4/4 发布，0/4 通过**）

- 日期：2026-09-27 UTC 06:58–07:54（本机日志时区为 2026-09-26 23:58–00:54 PDT）。候选代码冻结于 `feature/m1-01-v4-rerun` `8c58978`（基线 `main` `27d65e5`；冻结前四项改动见[任务记录](../../tasks/2026-09-27-m1-01-v4-rerun.md)），4 个 Run 期间无任何代码改动。worktree 专属 PostgreSQL 55431（数据在 `tmp/m0-b/postgres`，raw 证据字节只在库内，本目录只记 sha256 与长度）。
- 授权：AGENTS.md「费用与真实调用」常设授权；v4 包冻结上限（每 Run 4 模型 HTTP、20 工具、300 秒新观测窗、1800 秒 wall；本包总计至多 16 HTTP）。
- 路径：与上一包相同——`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 与 worker 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动（`worker.log` 首行 `versions` = `prompt-replay-candidate-04a9d1a1a80e` / `ctx-ctx-policy-v1-b4b4c61e0eec` / `otel-demo-e8c29b2fbffd`，即冻结 revision）；模型 `deepseek-flash`（4 个 Run 每条响应 `response_model` 均为该名）；工具后端是锁定的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-up.log`，Run 后已 `stop`，数据保留）。
- 提交：四次使用同一段症状问题（`request.json`），不含注入参数、flag 名或答案；观测窗由工作台在提交时刻固定（`window.json`），四个窗互不重叠。
- 故障案：工程钩子 `scripts/otel_demo_lab.py fault inject --experiment-id 6d-fault-01`（M0 `development_fault.py`，`paymentFailure` → 100%）07:24:44Z 注入、07:50:35Z 恢复（`fault-timeline.jsonl`，前后 SHA256 往返一致，与前两包相同的两个摘要；`observe-after-restore.json`：恢复后 2 条 checkout trace 无失败调用）。注入与恢复都是工程操作，产品与模型无入口。
- 独立观察：`scripts/otel_demo_observe.py`（#56 收紧后的谓词：失败 span 自身在窗内，且依赖失败 span `CHILD_OF` 父 span 属窗内 checkout）。每个 Run 提交前对最近 300 秒做一次（`observe-pre.json`），Run 后对精确窗口再做一次（`observe-post.json`）。控制窗前提四次均成立；故障案在调查者看到任何东西之前已确认 ≥2 条不同的 checkout 失败 trace 与失败的 `PaymentService/Charge` 调用关联（fault-1 提交前 8/8、窗内 8/8；fault-2 提交前 10/10、窗内 10/10）。
- 独立审查：每个 Run 一个全新上下文 Agent（Fable 5.1），拿 v4 判据、PRODUCT-CONSTRAINTS、ADR-0005、模型可见的报告契约与工具描述、本目录证据与 55431 只读查询，不拿执行者评估；结论原文见各目录 `review.md`。
- 费用：DeepSeek 余额 48.99 → 48.52 CNY（`deepseek-balance-before.json` / `-after.json`；fault-2 结束即时查询只显示 48.64，12 分钟后再查为 48.52，以后者为准），本包 16 次请求合计 **0.47 CNY**；token 见各 `ledger.json` 的 `usage_totals`。
- 资源守卫：VM 启动前与每个 Run 前查内存压力，free 27–37%、swap free 0.87–1.32 GB，均在阈值内。
- 安全：写盘后以 key 全文与末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径扫描本目录，0 命中（`run_case.sh` 与 `lab-up.log` 中的本机路径已换为占位符）。

## 汇总表

| Run | 案例 | 事故 / Run | 观测窗（UTC） | 执行 | 报告状态 | 判据 1–5 | P1 / P2 / P3 | 模型 HTTP | 工具 | token（prompt / completion） | 用时 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | `cbab01ba` / `9b68c913` | 07:05:05–07:10:05 | completed，已发布 | `completed` / `partial`，19 claim，6 gaps | P P **F** P **F** | 0 / **1** / 7 | 4 | 16（16 ok） | 94,402 / 12,974 | 58 s |
| 2 | normal-2 | `b05f5b9a` / `44f23fdb` | 07:11:44–07:16:44 | completed，已发布 | `completed` / `partial`，20 claim，7 gaps | P P **F** P **F** | 0 / **4** / 6 | 4 | 15（15 ok） | 84,354 / 10,576 | 46 s |
| 3 | fault-1 | `22fe0e26` / `03557486` | 07:25:14–07:30:14 | completed，已发布 | `completed` / `partial`，15 claim，5 gaps；定位 checkout→payment `Charge`（gRPC 2「Invalid token」→ PlaceOrder gRPC 13），8 trace id 与独立观察完全一致 | P P **F** P **F** | 0 / **5** / 3 | 4 | 10（10 ok） | 83,813 / 13,426 | 60 s |
| 4 | fault-2 | `8ae16b22` / `0ed139d2` | 07:35:47–07:40:47 | completed，已发布 | `completed` / `supported`，17 claim，6 gaps；同一定位，10 trace id 与独立观察完全一致；1 条 `no_data` 视图只进 gaps | P P **F** P **F** | 0 / **3** / 3 | 4 | 16（15 ok，1 no_data） | 92,854 / 10,360 | 45 s |

合计：模型 HTTP 16/16，工具 57 次，四个 Run 均在 4/4 请求预算、≤20 工具、1800 秒 wall 内结束；`budget_unknown` 均为 0；证据行 raw sha256 重算全部一致（57/57，各审查者亦在库内复算）。

## 判定

**本有界开发包不通过（0/4）。** v4 包要求正常/故障各 2 次全部满足 5 条判据且无未处理 P1/P2；实际：

1. 与上一包（0/4，三次交接）的差别：本包四份报告**全部发布**，无一交接；每份报告的引用绑定全部通过（只引用完整 `evidence_id`；fact 类只引 `ok`/`citable_as_fact=true` 视图；fault-2 的 `no_data` 视图只进 gaps）；四份的核心结论均与独立观察一致（两个故障 Run 的全部失败 trace id 逐一吻合），无 P1。判据 1、2、4 四次全过。
2. 判据 3（引用、来源、对象/窗口一致，数值/计数不混淆）四次都因模型报告的可观察数值/时间错误（P2）失败，判据 5 随之失败。四位审查者都明确写出：**没有一条 P2 可归因于产品**（证据绑定、投影、执行器、描述），模型误报的每个值在交付视图里都存在且标注正确。
3. 每次失败都保留在分母，未重跑替换；四个窗口不可比，不计算混合成功率。

## 审查发现的模式（交 lead / 用户判断）

- **回看首点当窗内值（4 个 Run 中 3 个：normal-2 P2-4、fault-1 P2-3、fault-2 P2-1）**：`rate()/increase()` 视图的首点由窗前样本算出，视图已带 `lookback_seconds=300` / `lookback_start_at`，描述已写「A value at the first points may therefore reflect samples from before the window」，模型仍把首点报成「窗内」区间端点或起始时刻。fault-1 还由此把故障起点误定到「第三步」，与自己引用的 trace 时间矛盾。
- **区间端点/计数错误（normal-1 P2-1、normal-2 P2-1~3、fault-1 P2-1/2）**：模型给出的 min–max 与视图值不符（取了非端点、或把子 span 混进父 span 分组）。
- **`increase(...[300s])` 按「每步」表述（fault-1 P2-4）**、**不完整采样外推到「窗内全部」（fault-1 P2-5）**、**显式零 series 说成 unknown（fault-2 P2-3）**、**空 status_tags 说成 status 0（fault-2 P2-2，normal-1 P3）**。
- 产品项（各审查者均记 P3，非本包缺陷）：发布后 `opspilot_incidents.state` 仍为 `queued`，页面显示「queued · concluded」；`persistence.publish()` 只写 `conclusion` 与 Run 状态（上一包已记，未确认是否有意）。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在 6d 范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机唯一使用者本时段的全部调用，本时段无其他调用者（未做独立核实）。
- 描述改写（一句一约束）对模型行为的影响无法从 4 次样本分离：本包的 P2 类型与上一包 normal-1 的 P2 同类（回看首点、数值），未见新类型。

## 目录

`lab-up.log`、`web.log`、`worker.log`、`fault-timeline.jsonl`、`observe-after-restore.json`、`deepseek-balance-*.json`、`run_case.sh`（账本导出沿用 `../m1-01-v4-acceptance/extract_ledger.py`）；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`、`observe-post.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`。
