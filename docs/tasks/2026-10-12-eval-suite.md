# 场景评测套件（回放 + live，参照 HolmesGPT tests/llm）

- 状态：合同 r6 已定（D6 改为确定性 gate + 校准后的 LLM judge）；SPEC 有界开放已合并（#210）；PR-A 未开工，预审须先回答 #211 其余四项
- 更新日期：2026-10-12
- 依据：外部验收入口 `IncidentScenario -> IncidentOutcome`（AGENTS.md「验证与汇报」）；[v4 验收包](../testing/first-investigation-v4-2026-09-10.md)；[M1-01 收口记录](2026-09-28-m1-01-closure.md)中的上游评测口径；ADR-0006；C3 第 388–398 行（judge 校准）；上游 HolmesGPT `46e3a72` 的 `tests/llm/`
- 工作区：本 PR `chore/eval-suite-gate`，`../production-ops-agent-eval-gate`；实施时每个 PR 一个 worktree，写在执行节

## 目标与范围

多个故障场景各跑多次，分回放和 live 两档，产出成功率表并记录基线，作为后续改动的回归依据。本套件是开发基线，不是 SPEC 所说的 candidate evaluation：不翻任何 `passes`，不支持发布结论。
范围外：跨模型对比、F14 的 matched baseline、生产观察。

## 前提与完成条件

- 前提：SPEC 有界开放段落合并（含 r4 收紧项确认）；DeepSeek 充值（已完成：2026-10-12 余额 202.15 CNY，基线估计 4–13 CNY）；live 档开跑前与其他会话确认 kind lab 空闲（D15）。
- 完成条件：6 个场景回放各 5 次、live 各 2 次，都有冻结的 `summary.json` 与 LangSmith trace；judge 已按独立标注校准；成功率表（`passed/attempted`，`replay_miss` 计为失败并另列 miss 率）作为基线入库；每个 PR 都经过独立审查。

## 合同（r6）

完整决定表（含选项、代价与出处）见 [合同决定表](2026-10-12-eval-suite-contract.md)。r1：用户于 2026-10-12 一次裁决。r2：PR #210 机器人分诊后，D10 与 D13 改判，用户同日裁决。r3：PR #210 第二轮分诊后，补正常场景判据（D6）、修正 CI 档位（D8），用户同日裁决。r4：PR #210 第三轮分诊，四项收紧（D1 改称开发场景、D6 证据支持与正常场景收紧、R3 分母），均按 C3 §11 只收紧不放宽，在本 PR 合并时由用户一并确认。r5：第四轮分诊，故障场景每条因果诊断都须受支持，PR-D 删除 F14 对照；同样只收紧。r6：#211 第 1 项（报告 schema 不标因果，确定性判据无法机读），用户 2026-10-12 改选 D6 选项 C，参照 HolmesGPT。

- D1 场景：v4 的 normal-1/2、fault-1/2，加 `productCatalogFailure`、`cartFailure`，共 6 个，全部是开发场景（r4：后两个已在三包中跑过且结果已公开，不再称为留出）。任何留出结论都需要另建隔离的未见场景集；新增场景另开版本；答案只放在评测侧。
- D2 回放：回放从 live 录制的完整模型可见输入、工具 schema、工具调用与结果、视图、hash、时间与权限，模型真实调用；另保留不调用模型的结构重放，不计入质量成功率。
- D3 live 环境：复用 kind + OTel Demo 与 `scripts/kind_lab.py fault`，记录注入前后的 SHA。
- D4 并行：只有回放用 xdist 并行；live 串行（`parallel=1`）；DeepSeek 并发为 1。
- D5 次数：回放 5 次，live 2 次；表中注明样本量带来的置信不足。
- D6 判据（r6）：确定性 gate 与校准后的 LLM judge 都通过才算通过（`overall = deterministic_pass AND judge_pass`），judge 不替代确定性断言（SPEC 验收段、AGENTS.md）。确定性 gate：schema、证据/目标/时间绑定、最终状态，以及引用证据中存在场景的证据特征。judge：认定报告（含 summary 与正文）里的事实型因果诊断，并按评测侧的 expected answer 逐条判下述因果条件，包括每条诊断自己引用的证据满足证据特征（gate 只查证据特征存在，不能替代逐条支持）；expected answer 不进入被测模型输入。judge 未按独立标注校准前不记录基线（C3 第 388–398 行）。judge 模型、标注来源、校准样本与一致率门槛在 PR-A 预审中定。以下 r1–r5 的因果条件不变，只是改由 judge 判定。故障场景：schema、证据/目标/时间绑定、最终状态通过，根因结论点名注入故障的服务与操作，且引用的证据中至少一条满足该场景预先定义的证据特征（r4：例如故障操作上的错误 span 或错误率读数，按 C3 §11「结论获得证据支持」）；每一条事实型因果诊断都须满足上述条件，存在其他事实型因果诊断即判失败（r5）。证据特征的具体判定在 PR-A 预审中定。正常场景（r3、r4）：绑定与状态检查通过，且没有任何事实型因果诊断（不限于故障目录中的组件）；假设型陈述允许；`inconclusive`/`partial` 满足此条件即算通过。
- D7 存放：仓库保存每 Run 的 `summary.json` 与轮次汇总，LangSmith 每轮一个 project；原始 ledger 不入库。
- D8 CI（r3）：PR CI 只跑不调用模型的结构重放，离线、不带任何凭据；调用模型的回放与 live 都由 lead 或用户手工跑。
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
5. PR-D：CI 结构重放入口与第一轮基线入库。

## 下一步与交接

- SPEC 有界开放已合并（#210，2026-10-12）。#211 第 1 项已裁决为 r6（judge）；PR-A 预审须回答其余四项，并定 judge 模型、标注来源、校准样本与一致率门槛。DeepSeek 已充值（202.15 CNY）。
