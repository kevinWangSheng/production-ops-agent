# 场景评测套件（回放 + live，参照 HolmesGPT tests/llm）

- 状态：合同 r7；PR-A 已合并（#221）；PR-B 预审完成、B1/B2 已裁决（2026-10-14），拆为 B1 → B2 → B3；PR-B1 已实现并通过独立审查，见执行节
- 更新日期：2026-10-14
- 依据：外部验收入口 `IncidentScenario -> IncidentOutcome`（AGENTS.md「验证与汇报」）；[v4 验收包](../testing/first-investigation-v4-2026-09-10.md)；[M1-01 收口记录](2026-09-28-m1-01-closure.md)中的上游评测口径；ADR-0006；C3 第 388–398 行（judge 校准）；上游 HolmesGPT `46e3a72` 的 `tests/llm/`
- 工作区：每个 PR 一个 worktree，写在执行节

## 目标与范围

多个故障场景各跑多次，分回放和 live 两档，产出成功率表并记录基线，作为后续改动的回归依据。本套件是开发基线，不是 SPEC 所说的 candidate evaluation：不翻任何 `passes`，不支持发布结论。
范围外：跨模型对比、F14 的 matched baseline、生产观察。

## 前提与完成条件

- 前提：SPEC 有界开放段落合并（含 r4 收紧项确认）；DeepSeek 充值（已完成：2026-10-12 余额 202.15 CNY，基线估计 4–13 CNY）；live 档开跑前与其他会话确认 kind lab 空闲（D15）。
- 完成条件：6 个场景回放各 5 次、live 各 2 次，都有冻结的 `summary.json` 与 LangSmith trace；judge 已按独立标注校准；成功率表（`passed/attempted`，`replay_miss` 计为失败并另列 miss 率）作为基线入库；每个 PR 都经过独立审查。

## 合同（r7）

完整决定表（含选项、代价与出处）见 [合同决定表](2026-10-12-eval-suite-contract.md)。r1：用户于 2026-10-12 一次裁决。r2：PR #210 机器人分诊后，D10 与 D13 改判，用户同日裁决。r3：PR #210 第二轮分诊后，补正常场景判据（D6）、修正 CI 档位（D8），用户同日裁决。r4：PR #210 第三轮分诊，四项收紧（D1 改称开发场景、D6 证据支持与正常场景收紧、R3 分母），均按 C3 §11 只收紧不放宽，在本 PR 合并时由用户一并确认。r5：第四轮分诊，故障场景每条因果诊断都须受支持，PR-D 删除 F14 对照；同样只收紧。r6：#211 第 1 项（报告 schema 不标因果，确定性判据无法机读），用户 2026-10-12 改选 D6 选项 C，参照 HolmesGPT。r7：PR-A 合同预审（P1–P13），用户 2026-10-13 裁决，P5 改判为 GLM 5.3。

- D1 场景：v4 的 normal-1/2、fault-1/2，加 `productCatalogFailure`、`cartFailure`，共 6 个，全部是开发场景（r4：后两个已在三包中跑过且结果已公开，不再称为留出）。任何留出结论都需要另建隔离的未见场景集；新增场景另开版本；答案只放在评测侧。
- D2 回放：回放从 live 录制的完整模型可见输入、工具 schema、工具调用与结果、视图、hash、时间与权限，模型真实调用；另保留不调用模型的结构重放，不计入质量成功率。
- D3 live 环境：复用 kind + OTel Demo 与 `scripts/kind_lab.py fault`，记录注入前后的 SHA。
- D4 并行：只有回放用 xdist 并行；live 串行（`parallel=1`）；DeepSeek 并发为 1。
- D5 次数：回放 5 次，live 2 次；表中注明样本量带来的置信不足。
- D6 判据（r6）：确定性 gate 与校准后的 LLM judge 都通过才算通过（`overall = deterministic_pass AND judge_pass`），judge 不替代确定性断言（SPEC 验收段、AGENTS.md）。确定性 gate：schema、证据/目标/时间绑定、权限（`IncidentOutcome.permissions` 为 `read_only`，D12）、最终状态；仅故障场景另查引用证据中存在该场景的证据特征（正常场景没有证据特征）。judge：认定报告（含 summary 与正文）里的事实型因果诊断，并按评测侧的 expected answer 逐条判下述因果条件，包括每条诊断自己引用的证据满足证据特征（gate 只查证据特征存在，不能替代逐条支持）；expected answer 不进入被测模型输入。judge 未按独立标注校准前不记录基线（C3 第 388–398 行）。judge 模型、标注来源、校准样本与一致率门槛在 PR-A 预审中定。以下 r1–r5 的因果条件不变，只是改由 judge 判定。故障场景：schema、证据/目标/时间绑定、最终状态通过，根因结论点名注入故障的服务与操作，且引用的证据中至少一条满足该场景预先定义的证据特征（r4：例如故障操作上的错误 span 或错误率读数，按 C3 §11「结论获得证据支持」）；每一条事实型因果诊断都须满足上述条件，存在其他事实型因果诊断即判失败（r5）。证据特征的具体判定在 PR-A 预审中定。正常场景（r3、r4）：绑定与状态检查通过，且没有任何事实型因果诊断（不限于故障目录中的组件）；假设型陈述允许；`inconclusive`/`partial` 满足此条件即算通过。
- D7 存放：仓库保存每 Run 的 `summary.json` 与轮次汇总，LangSmith 每轮一个 project；原始 ledger 不入库。
- D8 CI（r3）：PR CI 只跑不调用模型的结构重放，离线、不带任何凭据；调用模型的回放与 live 都由 lead 或用户手工跑。
- D9 `passes`：不翻转。
- D10 上限：只用每 Run 的技术上限（100 次请求、单次 360 s、Run 7200 s），不设套件级预算或墙钟上限（AGENTS.md「费用与真实调用」）；每轮记录余额差（用户 2026-10-12 按 PR #210 分诊结果裁决）。
- D11 证据：每 Run 冻结 `summary.json`；trace 回读不到时如实记 `not_found`。
- D12 时间与状态：使用 Run 的授权时间框，模型在框内自选查询窗；回放不重签旧 hash；权限固定 read_only。
- D13 回放未命中：该 Run 记为失败并计入分母（C3 第 394 行、v4 第 99 行），另单列 miss 率（用户 2026-10-12 按 PR #210 分诊结果改判）。
- D14 SPEC 关系：定为开发基线，在 SPEC 有界开放段落中写明。
- D15 kind lab：与其他会话共用，同一时间只有一个真实故障，开跑前先确认。
- P1 结局（#211-2/5）：通过须执行 completed、report_available、无 handoff 且 `assessment_status=completed`；正常场景仍允许 partial/inconclusive 但事实型因果诊断为零；故障场景至少一条诊断且每条满足 r6，空集不得通过。
- P2 trace（#211-3）：必需 trace 回读不到或身份不匹配记 `evidence_incomplete`，判失败、计分母、保留 judge 判分，阻止宣告基线完成；补证另建 revision。
- P3 权限：须含 `read_only`，额外只容许 `human_control`（持久化投影，`opspilot/acceptance.py:421`）；写或未知权限拒绝。
- P4 manifest（#211-4）：C3 §11 每个维度必须有 version/hash 或带理由的显式 `none`；缺失为 unknown，阻止基线完成。
- P5 judge 模型：GLM 5.3（异家族），独立配置、全新上下文；须通过 P7 校准。评测证据发往第二家供应商，仅限开发评测；凭据与端点在接入 judge 前核查。
- P6 judge 输入输出：输入报告全文、授权主体与时间、报告引用且已交付的证据内容/ID/hash、expected answer、证据规则结果；输出逐条诊断（定位/引文、是否事实型因果、服务/操作、引用、支持判定、理由）与二值 verdict；代码复核定位与引用并重算 AND；error/timeout/invalid/oversize/未校准均不通过。
- P7 校准：两个独立全新上下文 Agent 盲标，分歧由用户裁决；每场景 2 份调试、4 份冻结验证（共 12+24）；24 份验证各判两次，≥46/48 与最终标签一致，且负例零误通过；不达标不记基线，修 judge 后换验证集。只服务开发基线，不关闭 SPEC 的 M0 formal calibration 入口项。
- P8 证据特征：首版只认可见错误 span，评测侧 `answer.json` 存版本化 `any_of` 规则（精确 service/RPC operation、error=true、时间窗、min_matches=1）；容许故障服务 server span 或 checkout 上同一 RPC 的失败 client span；只靠 metrics 的报告不通过。
- P9 judge 调用：每份报告一次、不自动重试或投票；单次 360 s、请求 8 MiB/响应 2 MiB、输出上限 16384；judge 单独串行（P5 改判后不占 DeepSeek 并发）；费用分供应商、分阶段记录，校准费用另列。
- P10 分母：setup 前登记 attempt；开始后的 setup/error/abort/unknown/replay_miss/judge_error/evidence_incomplete 都计失败，未开始的跳过单列；固定槽位，失败不重抽替换；基线完成 = 槽位执行完、校准合格、证据齐全，不是产品验收通过。
- 可逆默认 R1–R6 与 P11–P13（场景与答案分文件隔离、版本化 eval summary/manifest/aggregate、多 trace `trace.entries[]` 与旧摘要只读兼容）见合同决定表。

## 计划

1. 本 PR：SPEC 有界开放段落、本记录、合同决定表、ROADMAP 行（用户门）。
2. PR-A：评测合同与 schema（D1、D5–D9、D11–D13）；验收测试由未参与实现的 Agent 编写。
3. PR-B：回放的录制与重放，以及基线表（D2、D13、R1–R4）。
4. PR-C：live kind 场景驱动（D3、D4、D10、D15）。
5. PR-D：CI 结构重放入口与第一轮基线入库。

## 执行

- PR-A：分支 `feature/eval-a-contract`，worktree `../production-ops-agent-eval-a`。公开接口 [PR-A 接口](2026-10-13-eval-pr-a-interface.md)；代码 `opspilot/evaluation/`；6 个场景 `evals/scenarios/`。
  - 验收测试由未参与实现的 Codex 只按接口文档编写（`tests/acceptance/test_eval_contract.py`），首轮 406 例 405 过，唯一失败是实现把窗口检查挂在时间引用检查之后，改实现；接口歧义 9 项的处置写在接口文档末尾。
  - 独立审查（Codex，全新上下文）首轮 fail：P1 judge 认定为事实型因果的 hypothesis 可绕过证据资格；P2 payment 规则漏掉真实 server span；P2 场景与产品 Run 身份未绑定；P2 aggregate 未校验 manifest。四项复现后修复，接口同步修订，测试作者补测（既有断言经 AST 核对未改）。复验 pass with fixes，残余 1 项（Outcome 的 scenario_id 未核对）修复并补测。终态 531 例全过；PR #221 机器人三项（引用须属本 Run、记录须在场景 target、越界时间戳不抛错）采纳修复后 589 例全过；第二、三轮共四项（反向与非 UTC 窗口、记录 ID 绑定、签名只来自合格引用）采纳修复后 633 例全过；第四轮两项（judge 身份绑定 manifest、signature 拒绝未知字段）采纳修复后 686 例全过；第五轮三项（judged 结果必须带 judge hash、校准结果绑定冻结 hash、manifest 拒绝未知 kind）修复后 738 例全过，全仓 `make check` 等价检查通过。
  - 证据特征规则对 6 个冻结证据包核对：故障包各自命中，正常包零命中；76 条记录的 `view_sha256` 与产品算法一致。
  - 实施中冒出的合同问题（用户 2026-10-13 裁决按 P8 原样）：product-catalog 故障中 `frontend` 上的 `grpc.oteldemo.ProductCatalogService/GetProduct` 失败 client span（真实证据 6 条）不计入证据特征，只认 product-catalog server span（同一故障包 11 条）或 checkout 上的 client span。
  - 机器人审查共五轮，均为身份与输入校验的收紧；第五轮按用户选择修复后停止推送，后续发现汇入一个 PR-B issue。

- PR-B 预审（Codex gpt-6-astra，2026-10-14）：[PR-B 合同](2026-10-14-eval-pr-b-contract.md)。用户裁决 B1 = 工具按冻结请求键精确匹配、缺键即 `REPLAY_MISS`；B2 = 冻结证据快照、业务钟固定在录制结束时刻；B3–B8 为 lead 采纳的可逆默认。拆为 PR-B1（持久化边界与表，含 #222）→ PR-B2（录制与回放）→ PR-B3（judge 接入与校准）。
- PR-B1：分支 `feature/eval-b1-loader`，worktree `../poa-eval-b1`；公开接口 [PR-B1 接口](2026-10-14-eval-pr-b1-interface.md)；代码 `opspilot/evaluation/{codec,storage,baseline}.py`、`rounds.py` 防御。实现 Codex gpt-6.1-sol；独立验收测试由全新上下文 Claude 子 Agent 只按接口编写（`tests/acceptance/test_eval_b1_storage.py`，168 例，歧义 16 条见 `EVAL_B1_AMBIGUITIES.md`），首轮 158/160，两条均为实现偏离接口（异常链、映射键错误码），改实现。独立审查（Codex gpt-6-astra）首轮 fail 4 项 P2：不可能的通过校准仍使基线完成、校准失败时 loader 仍公布 100% 通过率、解析异常链携带原始值、清理失败暴露路径；均改实现、测试作者补测。lead 裁决 r2：校准联合约束只对 `passed=True` 生效（失败 DTO 不抬高通过率，保留 I:99 兼容，PR-A 与 B1 既有断言未改）。复验 pass，三组 pytest 1026 passed；实现者 `make check` 4994 passed、1054 skipped、2 xfailed。无模型、网络或 PG 调用。
- PR-B 可选遗留（审查者）：PR-A `calibrate()` 的 `min_items` 语义允许多于 24 项，与 T:43「24 项、46/48」口径需对齐，不属 B1。

## 下一步与交接

- PR-A 已合并（#221）；PR-B 预审已裁决（2026-10-14），拆为 B1 → B2 → B3。B1 即本 PR #224。下一步 PR-B2（录制与回放，分支 `feature/eval-b2-replay`）；PR-B3 judge 接入与校准需 #219 合并与私有 `.env.judge`。DeepSeek 已充值（202.15 CNY）。
