# M1-01 6f：显式视图候选上的 v4 有界开发验收包 2+2 真实 Run（结果：**4/4 发布，v4 口径 0/4 通过；上游口径 4/4**）

- 日期：2026-09-27 UTC 15:00–15:39（本机日志时区为 2026-09-27 08:00–08:39 PDT）。候选代码冻结于 `feature/m1-01-window-points` `f8fac31`（代码与 `531312b` 相同，之后只加了任务记录；相对 6e 候选 `b352230` 的产品改动为[任务记录](../../tasks/2026-09-27-m1-01-window-points.md)「第二轮」A/B/C：trace 行 `status_state`、trace 视图单位化计数、最终报告前的运行级覆盖摘要），4 个 Run 期间无任何代码改动。worktree 专属 PostgreSQL 55431（沿用本 worktree 的 `tmp/m0-b/postgres`，未触碰其他 worktree 的数据；raw 证据字节只在库内，本目录只记 sha256 与长度）。
- 授权：AGENTS.md「费用与真实调用」常设授权；v4 包冻结上限不变（每 Run 4 模型 HTTP、20 工具、300 秒新观测窗、1800 秒 wall；本包总计至多 16 HTTP）。
- 流程：与 6e 完全相同——同一段症状问题（`request.json`）、同一 `run_case.sh`（只改目录与日志名）、同一独立观察脚本 `scripts/otel_demo_observe.py`、同一账本导出 `../m1-01-v4-acceptance/extract_ledger.py`、同一 v4 判据（未修改）。`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 与 worker 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动，本次先起 web 再起 worker，未再出现 6e 的 schema 安装竞争。`worker.log` 首行 `versions` = **`prompt-replay-candidate-177e529f603d`** / `ctx-ctx-policy-v1-b4b4c61e0eec` / **`otel-demo-44ed0d63b0d3`**，与预期冻结 revision 一致。模型 `deepseek-flash`；工具后端是锁定的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-up.log`，Run 后已 `stop`，数据保留）。
- 观测窗由工作台在提交时刻固定（`window.json`），四个窗互不重叠：15:00:19–15:05:19、15:05:39–15:10:39、15:12:09–15:17:09、15:17:25–15:22:25。
- 前提：normal-1 第一次前提检查（15:03:16，环境刚启动 2 分钟）`dependency_traffic=false` 不成立，保留为 `normal-1/observe-pre-attempt1-not-met.json`，等 2 分钟后再查成立才提交；之后三次前提一次成立。控制窗内 checkout trace 7 / 13 / 8 / 10 条，正常窗 0 失败。
- 故障案：工程钩子 `scripts/otel_demo_lab.py fault inject --experiment-id 6f-fault-01`（`paymentFailure` → 100%）15:11:58Z 注入、15:23:53Z 恢复（`fault-timeline.jsonl`，前后 SHA256 往返一致，与 6b/6d/6e 相同的两个摘要；`observe-after-restore.json`：恢复后 15:33:49–15:38:49 窗内 7 条 checkout trace，0 失败）。故障案在调查者看到任何东西之前已确认 ≥2 条不同的 checkout 失败 trace 与失败的 `PaymentService/Charge` 调用关联（fault-1 提交前 8/8、窗内 8/8；fault-2 提交前 10/10、窗内 10/10）。注入与恢复都是工程操作，产品与模型无入口。
- 独立审查：每个 Run 一个全新上下文 Agent（Fable 5.1），拿 v4 判据、PRODUCT-CONSTRAINTS、ADR-0005、模型可见的报告契约、覆盖摘要模板与工具描述、本目录证据与 55431 只读查询，不拿执行者评估；结论原文见各目录 `review.md`。审查者按 v4 判据给 P1/P2/P3，另按 lead 要求对 P2 分类计数（a 空状态写成 0；b 缺失序列写成 0，其中 b-svc 按服务分组缺失序列单独计；c trace limit 与 span 行数混淆；d 不完整视图计数；e 其他），并另答上游式口径。
- 费用：DeepSeek 余额 48.00 → 47.53 CNY（`deepseek-balance-before.json` / `-after.json`；fault-2 结束即时查询只显示 47.66，15 分钟后再查为 47.53，以后者为准），本包 16 次请求合计 **0.47 CNY**；token 见各 `ledger.json` 的 `usage_totals`。
- 资源守卫：VM 启动前与每个 Run 前查内存压力，free 30–36%、swap free 1.0–1.6 GB，均在阈值内。
- 安全：写盘后以 key 全文与末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径与 scratchpad 路径扫描本目录：key 0、口令 0、路径 0；`authorization` 命中均为模型报告正文里的 "outside this authorization" 类措辞，非请求头。`run_case.sh`、`lab-up.log` 中的本机路径已换为占位符。

## 汇总表

| Run | 案例 | 事故 / Run | 观测窗（UTC） | 执行 | 报告状态 | 判据 1–5 | P1 / P2 / P3 | 模型 HTTP | 工具 | token（prompt / completion） | 用时（提交→发布） | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | `a78a963c` / `1b724675` | 15:00:19–15:05:19 | completed，已发布 | `completed` / `partial`，22 claim，7 gaps；7 trace id 与独立观察一致 | P P **F** P **F** | 0 / **4** / 5 | 4 | 18（18 ok） | 116,506 / 17,302 | 75 s | 正确判定无故障 |
| 2 | normal-2 | `2dc9e329` / `4777de1b` | 15:05:39–15:10:39 | completed，已发布 | `completed` / `partial`，14 claim，7 gaps；2 条 `no_data` 视图只进 gaps；13 trace id 一致 | P P **F** P **F** | 0 / **6** / 3 | 4 | 14（12 ok，2 no_data） | 64,120 / 10,970 | 48 s | 正确判定无故障 |
| 3 | fault-1 | `c22b9204` / `cf1872a9` | 15:12:09–15:17:09 | completed，已发布 | `completed` / `supported`，14 claim，6 gaps；定位 checkout→payment `Charge`（gRPC 2「Invalid token」），8 trace id 与独立观察完全一致；1 条 `no_data` 只进 gaps | P P **F** P **F** | 0 / **3** / 4 | 4 | 12（11 ok，1 no_data） | 80,141 / 9,261 | 41 s | 正确指出根因 |
| 4 | fault-2 | `61c4803b` / `5bd4dd44` | 15:17:25–15:22:25 | completed，已发布 | `completed` / `supported`，15 claim，5 gaps；同一定位，10 trace id 完全一致；1 条 `no_data` 只进 gaps | P P **F** P **F** | 0 / **1** / 4 | 4 | 16（15 ok，1 no_data） | 106,644 / 12,604 | 53 s | 正确指出根因 |

合计：模型 HTTP 16/16，工具 60 次，四个 Run 均在 4/4 请求预算、≤20 工具、1800 秒 wall 内结束；`budget_unknown` 均为 0；证据行 raw sha256 与 view sha256 重算全部一致（60/60，各审查者在库内复算）。新视图字段四位审查者逐行核对：`spans_shown` = 行数、`spans_shown + spans_omitted = backend_spans_returned`、`incomplete_reason` 非空当且仅当 `incomplete`、`status_state` 与 `status_tags` 全部一致（0 不符）；覆盖摘要按 `run_coverage_message` 从交付视图重建，四份报告的 gaps 与之逐条一致（实际发送的消息只存哈希，未逐字节核对，审查者已标注）。所有指标视图 `lookback_seconds=300`、单点在窗口终点（模型四次仍只用 `[300s]`）。

## 判定

- **v4 口径：0/4，本有界开发包不通过。** 四次判据 1、2、4 全过，无 P1，无交接，引用绑定全过（`no_data` 视图只进 gaps）；四次都在判据 3 失败（判据 5 随之失败）。
- **上游口径：4/4。** 两个正常窗正确判定无故障且未认证健康；两个故障窗正确定位到 checkout→payment `Charge`，引用的失败 trace id 与独立观察逐一吻合（8/8、10/10）。
- 每次失败都保留在分母，未重跑替换；四个窗口不可比，不计算混合成功率。

## 与 6e 的逐类对比（P2）

| 类 | 6e（`b352230`，P2 合计 6） | 6f（`f8fac31`，P2 合计 14） |
|---|---|---|
| a. 空状态 / `not_recorded` 写成 status 0 / OK / 200 | 2（normal-2 P2-3、fault-2 P2-1） | **6**（normal-1 ×2：summary、claim 11；normal-2 ×4：claims 3、4、7、summary；fault-1 0，fault-2 0） |
| b. 缺失序列写成 0 / 「无非零码」 | 2（normal-2 P2-1、P2-2） | **0** |
| b-svc. 按服务分组的缺失序列写成该服务 0 / 无错误 | 1（normal-2 P2-1：checkout 错误计数「reported as 0」） | **0**（四位审查者单独计数均为 0；相关措辞「no ERROR series present / no non-zero code」四份里都降为 P3，报告均在 gaps 明确写「absent = unknown」） |
| c. trace limit 与 span 行数混淆 | 1（normal-1 P2） | **0** |
| d. 不完整 / 截断视图计数错误 | 1（normal-2 P2-4） | **0**（四份的 incomplete / truncated / no_data 集合与重建的覆盖摘要完全一致） |
| e. 其他 | 0 | **8**：区间端点混入父/子 span 或其他操作（normal-2 claim 3 与 claim 8、fault-1 claim 6、fault-2 claims 6/13）、summary 区间上界低报（normal-1）、错误 span 计数 4 实为 5（normal-1 claim 8）、时长归到错误 span（fault-1 claim 3）、p95 数值错误且来源视图未引用（fault-1 claim 12） |

- **B（单位化计数）与 C（覆盖摘要）对应的 b / b-svc / c / d 四类本包为 0**，包括 lead 指定的决策依据「按服务分组缺失序列」类；6e 里这四类共 5 个 P2。
- **A（`status_state`）未消除 a 类，反而 2 → 6**：视图已在每行标出 `not_recorded`，描述已写明「不等于状态码 0 或 OK」，两份正常报告仍把 `not_recorded` 的 checkout/cart/email/payment `charge` 行写成 status 0 / HTTP 200；两份故障报告 0（fault-2 写成「no error details」，fault-1 一处「non-error status」审查者记 P3）。
- **e 类 8 个是 6d「区间端点 / 计数与视图不符」类的再现**，不是新类型：6d 该类 P2 出现在 3/4 Run，6e 两处同类被当时审查者记为 P3（normal-2「mostly」范围、fault-2 GetQuote 范围混入子 span），本包审查者按判据 5「可观察的数值错误」记 P2。跨包审查者对同类问题的定级不完全一致（6e P3 vs 6f P2），这是审查粒度的变异，判据未变。
- 审查者未把任何 P2 归因于产品的证据绑定、投影、执行器或描述；产品侧备注（可选）：发布后 `opspilot_incidents.state` 仍为 `queued`（四包都记）；最终轮模型输入（含覆盖摘要）只存哈希，覆盖陈述只能对确定性重建核对。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在本包范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机本时段的全部调用，本时段无其他调用者（未做独立核实）。
- 多点（L < 窗口长度）指标视图在真实 Run 中的模型行为仍未观察到。
- 4 次样本无法分离 A/B/C 各自对模型行为的影响；a 类 2 → 6 与 e 类 0 → 8 的变化含审查者定级变异与样本噪声，不能单独归因于候选改动。

## 目录

`lab-up.log`、`web.log`、`worker.log`、`fault-timeline.jsonl`、`observe-after-restore.json`、`deepseek-balance-*.json`、`run_case.sh`（账本导出沿用 `../m1-01-v4-acceptance/extract_ledger.py`）；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`（normal-1 另有 `observe-pre-attempt1-not-met.json`）、`observe-post.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`。
