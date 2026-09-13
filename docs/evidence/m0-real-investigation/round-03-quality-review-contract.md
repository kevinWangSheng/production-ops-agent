# M0-03 本步合同：已返回报告的独立证据审查

日期：2026-09-11。工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`（`chore/m0-02-convergence`，HEAD `038401b`）；环境与 tmp 运行数据在 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`。依据 [v4 首片包](../../testing/first-investigation-v4-2026-09-10.md)、[M0 计划](../../plans/m0-validation-plan-2026-09-07.md) 第 5/8 节、[首片计划](../../plans/first-vertical-investigation-2026-09-09.md)、SPEC 实施门槛。本步不开放 M1，不改 feature passes。

## 问题

已执行的 M0-03 真实 Holmes Run 中，哪些最终报告的事实/反证/缺口与本 Run 实际交付 view 及独立工程观察一致？哪些必须记 FAIL 并保留在分母？

## 本步范围

只做只读质量审查与记录。不新增模型 HTTP、trace 上传、故障注入、环境启停、凭据读取或产品实施。不把 schema 通过、HTTP 200 或 `investigation_returned` 当作质量通过。

审查对象：

| Run | 账本 | 机器状态 | 是否计入 v4「各 2 次」候选 |
|---|---|---|---|
| `m003-fault-01` | m0-03 | incomplete（FACT_SCOPE_REQUIRED） | 否；失败保留 |
| `m003-fault-02` | m0-03 | incomplete（FACT_TIME_SCOPE_NOT_DELIVERED） | 否；失败保留 |
| `m003-fault-03` | m0-03 | failed（phase HTTP budget） | 否；失败保留 |
| `m003b-fault-01` | m0-03b | investigation_returned | 是（故障候选，待审） |
| `m003c-normal-01` | m0-03c | investigation_returned | 是（正常候选，待审） |
| `m003c-normal-02` | m0-03c | investigation_returned | 是（正常候选，待审） |
| `m003c-fault-02` | m0-03c | investigation_returned | 是（故障候选，待审） |

## 版本

- 代码：PR #16 HEAD `038401bbb30e7360bf916f4c6f4027f134f1ecfa`
- Holmes：`5e983c17f30e93099c7d775167266d4cd1d586c4`
- 报告协议：`m0-report-v2` / 严格候选；temporal/output 仍要求 public-v4
- 投影：`m0-02-v2`（round03.save_observation 记录）
- 模型：deepseek-v4-flash thinking/high；响应名允许集 deepseek-v4-flash / deepseek-flash
- 环境：OTel Demo 2.0.2，Compose `opspilot-m0`，profile `m0-otel` 当前仍 Running
- 截止：round03 PROFILE `2026-09-12T05:30:00Z`

## 步骤

1. 核对 git / PR #16 / 环境仍在跑；不 stop。
2. 重算各 Run manifest 的 raw/view 文件 hash；只读 `result-business.json`、`report-capture-business.json`、cited views；不读 `.env`、`private-protocol/`、工程注入答案。
3. 逐 claim 对照实际 view：数量（sampled vs limit vs omitted）、累计 vs `increase`、缺 series vs 显式零、跨 trace 关联、投影省略 vs 原始遥测缺失、target/time_scope 是否来自本请求已交付 context。
4. 对照独立工程观察：`docs/evidence/m0-real-environment/m003-*-observation.json`。工程事实不能替代模型结论；故障窗若无可诊断错误，该 Run 不得记故障正向通过。
5. 将 incomplete/failed Run 记入分母，不删除、不重跑。
6. 结论写入本目录审查文件；全文报告留 tmp。Git 只保存 hash、数量、P1/P2 与判定。

## 通过 / 失败判据

采用 v4 包「每份候选报告必须全部满足」五条。P1：权限/人控/预算/状态或核心结论被破坏。P2：可观察的数值、时间、来源、可见范围或因果/反证错误。仅表达问题为 P3。

- 正常 2 次与故障 2 次均无未处置 P1/P2，才记本有界开发包质量通过。
- 任一次 P1/P2 或合同 incomplete/failed：该次 FAIL，保留分母。
- 本步结束不自动授权补跑；若需新 HTTP，另写独立授权合同。
- 质量审查本身 0 模型 / 0 trace。

## 证据位置

- 运行数据：环境 worktree `tmp/m0-environment/holmes-runs/` 与 `m0-03*-request-ledger.json`
- 工程观察：`docs/evidence/m0-real-environment/m003-*.json`
- 本步产出：`round-03-quality-review-contract.md`（本文件）、各 Run 审查、汇总
- 失败处置：保留原 result/report-capture/ledger；不改写旧质量 FAIL

## 失败处置

审查发现只记录，不现场改 prompt/投影来让旧报告变绿。旧原文与 hash 保持。需要代码修复时另开有界离线项，真实复验另授权。
