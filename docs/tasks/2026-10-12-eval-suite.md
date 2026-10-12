# 场景评测套件（回放 + live，参照 HolmesGPT tests/llm）

- 状态：合同 r2 已裁决；SPEC 有界开放待用户合并（本 PR）；PR-A 未开工
- 更新日期：2026-10-12
- 依据：外部验收入口 `IncidentScenario -> IncidentOutcome`（AGENTS.md「验证与汇报」）；[v4 验收包](../testing/first-investigation-v4-2026-09-10.md)；[M1-01 收口记录](2026-09-28-m1-01-closure.md)中的上游评测口径；ADR-0006；C3 第 388–398 行（judge 校准）；上游 HolmesGPT `46e3a72` 的 `tests/llm/`
- 工作区：本 PR `chore/eval-suite-gate`，`../production-ops-agent-eval-gate`；实施时每个 PR 一个 worktree，写在执行节

## 目标与范围

多个故障场景各跑多次，分回放和 live 两档，产出成功率表并记录基线，作为后续改动的回归依据。本套件是开发基线，不是 SPEC 所说的 candidate evaluation：不翻任何 `passes`，不支持发布结论。
范围外：LLM judge（校准后再加）、跨模型对比、F14 的 matched baseline、生产观察。

## 前提与完成条件

- 前提：SPEC 有界开放段落合并；DeepSeek 充值（余额 2.96 CNY，基线估计 4–13 CNY）；live 档开跑前与其他会话确认 kind lab 空闲（D15）。
- 完成条件：6 个场景回放各 5 次、live 各 2 次，都有冻结的 `summary.json` 与 LangSmith trace；成功率表（`passed/attempted`，`replay_miss` 计为失败并另列 miss 率）作为基线入库；每个 PR 都经过独立审查。

## 合同（r2）

完整决定表（含选项、代价与出处）见 [合同决定表](2026-10-12-eval-suite-contract.md)。r1：用户于 2026-10-12 一次裁决。r2：PR #210 机器人分诊后，D10 与 D13 改判，用户同日裁决。

- D1 场景：v4 的 normal-1/2、fault-1/2，加留出的 `productCatalogFailure`、`cartFailure`，共 6 个；新增场景另开版本；答案只放在评测侧。
- D2 回放：回放从 live 录制的完整模型可见输入、工具 schema、工具调用与结果、视图、hash、时间与权限，模型真实调用；另保留不调用模型的结构重放，不计入质量成功率。
- D3 live 环境：复用 kind + OTel Demo 与 `scripts/kind_lab.py fault`，记录注入前后的 SHA。
- D4 并行：只有回放用 xdist 并行；live 串行（`parallel=1`）；DeepSeek 并发为 1。
- D5 次数：回放 5 次，live 2 次；表中注明样本量带来的置信不足。
- D6 判据：先只用确定性判据，包括 schema、证据/目标/时间绑定、最终状态，以及根因结论点名注入故障的服务与操作并引用证据 ID；LLM judge 校准后再加。
- D7 存放：仓库保存每 Run 的 `summary.json` 与轮次汇总，LangSmith 每轮一个 project；原始 ledger 不入库。
- D8 CI：PR CI 只跑回放档 N=1 的确定性冒烟测试；live 手工跑。
- D9 `passes`：不翻转。
- D10 上限：只用每 Run 的技术上限（100 次请求、单次 360 s、Run 7200 s），不设套件级预算或墙钟上限（AGENTS.md「费用与真实调用」）；每轮记录余额差（用户 2026-10-12 按 PR #210 分诊结果裁决）。
- D11 证据：每 Run 冻结 `summary.json`；trace 回读不到时如实记 `not_found`。
- D12 时间与状态：使用 Run 的授权时间框，模型在框内自选查询窗；回放不重签旧 hash；权限固定 read_only。
- D13 回放未命中：该 Run 记为失败并计入分母（C3 第 394 行、v4 第 99 行），另单列 miss 率（用户 2026-10-12 按 PR #210 分诊结果改判）。
- D14 SPEC 关系：定为开发基线，在 SPEC 有界开放段落中写明。
- D15 kind lab：与其他会话共用，同一时间只有一个真实故障，开跑前先确认。
- 可逆默认 R1–R6 见合同决定表。

## 计划

1. 本 PR：SPEC 有界开放段落、本记录、合同决定表、ROADMAP 行（用户门）。
2. PR-A：评测合同与 schema（D1、D5–D9、D11–D13）；验收测试由未参与实现的 Agent 编写。
3. PR-B：回放的录制与重放，以及基线表（D2、D13、R1–R4）。
4. PR-C：live kind 场景驱动（D3、D4、D10、D15）。
5. PR-D：CI 入口与第一轮基线入库。

## 下一步与交接

- 待用户：合并本 PR（SPEC 变更，用户门）；为 DeepSeek 充值。
