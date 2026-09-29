# M1-01 整视图 token 计量与超限拒绝：有界真实 Run（otel-demo profile）

- 日期：2026-09-29 UTC 11:16–11:52（本机 04:16–04:52 PDT）。
- 被测代码：等于 PR 分支 HEAD 去掉"拒绝视图回显窗口与 query"这处修复，即 `executor.py` 的 +36/−5；此后还有独立审查后的两处小修（fixture 工具文案与 `fixture-2`、未采纳的历史视图不带适配器 `view_fields`），都不影响 otel-demo 的 ok 视图与拒绝路径（Run 时的提交只在未推送的旧分支上，外部解析不到；整视图 token 计量与 `RESULT_TOO_LARGE` 拒绝，合同见 [任务记录](../../tasks/2026-09-29-m1-01-view-bytes-timeout.md) A 节）。`worker.log` 首行 `versions`：`prompt-replay-candidate-bd28790117a0` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-34bc78980747`。拒绝视图的 `window`/`query` 回显（见"发现的产品缺陷"）在这些 Run **之后**才修，未重跑。
- 授权：AGENTS.md「费用与真实调用」常设授权；拿 lab 锁（11:16Z 起，PG 集成与审查完成后释放），本 worktree 的 lab PostgreSQL 55431，colima `m0-otel`。用完已 `stop` 两者，数据保留。
- 流程：与第三批效果测量相同（[alignment-c-effect/run.md](../m1-01-alignment-c-effect/run.md)）：`otel_demo_observe.py` 独立观察 → `POST /intake/ui` → 常驻 `worker_main` → `InvestigationRunner`；问题文本与第三批相同（"for the last 5 minutes"）。新增 1 个宽查询 Run：`vt-broad-question.txt`（开发脚本给出的问题，跨全部授权服务、24 h 窗口、要求 15 s 步长与 limit≥50），不改产品代码。
- 费用：DeepSeek 余额 12.70 → 11.19 CNY（`deepseek-balance-before/after.json`），差 1.51 CNY，含 6 个真实 Run（含 1 个作废）与本机同时段全部调用；账户有其他并发使用，未做独立对账。
- 安全：写盘后对本目录与 `view-bytes-timeout` 目录按 key 全文/末 12 位、工作台口令、`Bearer`、`Authorization`、本机路径全文扫描：0 命中。`request.json` 的幂等键字段记为 `idempotency_label`（值与提交时相同），原因同[第二批证据](../m1-01-alignment-b-effect/run.md)：原字段名与值的组合触发密钥扫描误报。
- 审查：每个 Run 一位全新上下文 Agent（**Sonnet 5.5**，因 Fable 额度耗尽；前几批用 Opus，审查粒度可能不同，P2 对比含此噪声），读 Run 目录、v4 判据、合同与 55431 只读 SQL，未见其他审查者或执行者结论；原文在各 Run 的 `review.md`。

## 故障时间线与环境

`fault-timeline.jsonl` 共 4 条：`vt-fault-1` 注入 11:29:33Z / 恢复 11:33:54Z，`vt-fault-2` 注入 11:43:42Z / 恢复 11:46:08Z（fault-2 的 5 分钟观测窗 11:39:42–11:44:42 只含最后约 1 分钟故障，审查者已按此核对）；两次恢复后独立观察均确认 0 失败（`observe-after-restore.json`、`observe-after-fault2-restore.json`）。

**作废的 Run**：第一次 `normal-2`（现目录 `normal-2-invalid-window/`）的观测窗 11:32:49–11:37:49 与 fault-1 恢复（11:33:54）重叠，`observe-pre.json` 显示 `fault_confirmed: true`，不是有效正常对照，不计入对比，只作拒绝行为旁证；随后在窗口干净后重跑为 `normal-2`。

## 汇总表（5 个有效 Run + 1 个作废）

| Run | 案例 | 模型请求 | 工具结果 | RESULT_TOO_LARGE | 拒绝的 view_tokens | 最大 ok 视图 tokens | token（prompt/completion） | 报告 | 审查 P2 | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | 7 | 16（ok 11 / no_data 3 / 拒绝 2） | 2 | 43,015；43,012 | 12,093 | 136,369 / 14,940 | completed/partial | 4（a、范围、b-svc、e） | 正确判定无故障 |
| 2 | normal-2（重跑） | 8 | 15（13 / 1 / 1） | 1 | 57,502 | 21,159 | 340,021 / 26,234 | completed（1 次报告修复重试） | 2（b+e、a） | 正确判定无故障 |
| 3 | fault-1 | 6 | 18（13 / 3 / 2） | 2 | 46,549；33,739 | 14,551 | 110,033 / 12,889 | completed | 1（b-svc，临界） | 找到 payment Charge |
| 4 | fault-2 | 6 | 12（8 / 2 / 2） | 2 | 108,409；42,534 | 16,818 | 104,375 / 16,045 | completed | 0 | 找到 payment Charge |
| 5 | broad-1（宽查询，健康） | 9 | 35（29 / 2 / 4） | 4 | 88,680；36,184；88,682；88,680 | 23,157 | 756,590 / 55,674 | completed（1 次报告修复重试） | 5（e×3、范围、引用） | 不适用 |
| 作废 | normal-2-invalid-window | 9 | 25（20 / 1 / 4） | 4 | 91,967；91,963；59,528；30,398 | 21,816 | 563,305 / 43,611 | 未审查 | — | 窗口被故障污染 |

逐 Run 的工具结果分类在各目录 `tool-summary.json`（`vt_summary.py` 生成，用真实 tokenizer 重算每个 ok 视图的 token 数）。**最大 ok 视图 23,157 tokens 出现在 broad-1，仍在 25,000 内**；所有 ok 视图都不超限，所有超限视图都被整份拒绝，没有部分行。

## 对比基线（第三批 alignment-c，核心 4 场景）

| | 第三批（+C1-C3，截断） | 本批（token 计量 + 拒绝） |
|---|---|---|
| P2 合计（normal-1/2、fault-1/2） | **6** | **7**（4 + 2 + 1 + 0） |
| 模型请求（合计） | 6+6+5+4 = 21 | 7+8+6+6 = 27 |
| 工具结果（合计） | 17+23+12+9 = 61 | 16+15+18+12 = 61 |
| prompt tokens（合计） | 2,314,396 | 690,798 |
| completion tokens（合计） | 65,749 | 70,108 |
| 上游口径 | 4/4 | 4/4 |
| 报告修复重试 | 2 | 1（normal-2；broad-1 另有 1） |

如实：**核心 4 场景 P2 从 6 变为 7（+1），没有改善**。样本为每格 1 次，审查者从 Opus 换成 Sonnet，P2 定级噪声不能排除，不据此作因果结论。P2 类别：normal-1 的 a（`not_recorded` 写成"grpc status 0"）、b-svc（缺失序列被拒作反证）、e（"只反映一个评估瞬间"）与范围误述；normal-2 的 b+e、a；fault-1 的 b-svc（临界）。全部归因为 MODEL，没有归因为 PRODUCT 的 P2。prompt token 明显少（-70%），原因是没有部分视图叠加，本批 5 分钟窗内 trace 很少（每次 4–8 条），但不能据此说明一般情形。

## 拒绝机制本身（审查者与执行者一致的观察）

- 5 个有效 Run 共 11 次 `RESULT_TOO_LARGE`，全部是 `traces_search`（没有 `metrics_range_query` 被拒）。全部是令牌路径（`max_view_tokens: 25000`、`view_tokens` 43k–108k），字节路径 0 次。
- **拒绝后的行为**：没有任何 Run 原样重试被拒的查询；每个被拒的服务最终都拿到了 ok 视图（缩小 limit、缩窗或两者）。被拒数据没有被任何报告引用。
- **恢复方式**：最有效的是降低 `limit` 到 1–3 或把窗口缩到 1–2 分钟。normal-1 中 limit 20 与 limit 5 的结果几乎一样大（43,015 对 43,012，因为窗口内只有 4 条 trace），"降低 limit"这条提示对它无效，模型只能缩窗到 60 秒、limit 3 才拿到数据。broad-1 从未缩短 24 h 窗口，只降 limit（50 → 10 → 1/2），中间有一轮 3 个服务 limit=10 全部再次被拒，浪费一轮。
- **披露情况**：normal-2、fault-1/2、broad-1 的报告在 gaps 中承认了拒绝造成的 trace 覆盖缺口；normal-1 没有，并因此把 1 分钟切片说成"窗口内唯一一条 trace"（P2）；broad-1 概括"per-service trace searches"但实际有 3 个服务没有搜索（P2）。用户点名的"有 2 份报告没有披露更大样本被拒"即 normal-1 与 fault-2（fault-2 的报告披露了受限样本但没点名拒绝原因，审查者评为 P3）。

## 发现的产品缺陷（已在本 PR 修，未重跑）

5 份审查一致指出：拒绝视图的 `window` 是 24 h 授权框架窗口而不是本次调用的查询窗，也没有回显 `query`。后果：模型无法分辨是哪次调用被拒，并把框架窗读成自己请求的窗口（"trace view keeps returning the full window"，在 3 个 Run 里出现）。对照 ok 视图记录的是实际查询窗（第二批已修同类问题）。修复：拒绝视图 `window` 改为实际查询窗，新增 `query`（与 ok 视图同源）；合同见任务记录"拒绝路径的逐项澄清"。用户要求"不必重跑全部 Run"，本批未重跑，修复由确定性测试而非真实 Run 验证。

## 未完成 / 未验证

- 拒绝视图 `window`/`query` 回显修复后没有真实 Run 复核；只有确定性合同测试（待测试作者补断言，键集合由 14 变 15）。
- `traces_search` 在 checkout 上体量很重：`limit=5` 也有 33k–43k tokens，`limit` 提示只在 limit 低于窗口内 trace 数时有效。模型只能缩到 1–2 分钟或 limit 1–3，样本因此很小。这是否影响调查质量、要不要在拒绝信息里给出窗口内实际 trace 数或 span 数的提示，未做，留待后续。
- 审查者用 Sonnet，与前几批的 Opus 不同；核心 4 场景 P2 +1 不能区分产品影响与审查粒度。
- 每种场景只有 1–2 个 Run，无统计意义；宽查询 Run 在只运行了约 10 分钟的实验环境上执行，24 h 窗口大部分为空，不代表长期运行的实验环境。
- 未覆盖：暂停/取消下的控制拒绝演示、重启恢复（有确定性与 PG 合同用例，PG 集成全套已跑：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_CONTROL_POSTGRES=1 M0_STEP_POSTGRES=1`，含 `tests/integration` 220 passed，当时全量仅剩 9 个待测试作者补断言的合同用例失败，此后已修订，全量 pytest 现为 0 失败）。
- 拒绝时 raw 不落库（合同决定）；因此拒绝时无法事后核对源返回了什么，只有 `view_tokens`。
- 费用未对账（账户有并发使用）。

## 目录

`run_case_vt.sh`（本机路径已占位）、`vt_summary.py`、`vt-broad-question.txt`、`fault-timeline.jsonl`、`observe-after-restore.json`、`observe-after-fault2-restore.json`、`deepseek-balance-before.json`/`-after.json`、`worker.log`；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`stats.json`、`tool-summary.json`、`review.md`。
