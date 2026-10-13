# OpsPilot 场景评测套件：合同决定表（r5）

预审者 Codex GPT-6 Astra（high，全新上下文，只读），2026-10-12。lead 补充 D13–D15，并核对了上游完整文件树。用户于 2026-10-12 按推荐裁决 D1–D15；D6 按 lead 修订后的推荐采用「先只用确定性判据」。

- 范围：只做合同预审；未改仓库已跟踪文件、未提交、未运行真实评测。
- 上游：HolmesGPT `46e3a728d762b774d2ec458357decb8698541ce5`。Git clone/fetch 因网络超时未形成可用 Git checkout；该提交的指定文件经 GitHub raw 取得。lead 补核（GitHub tree API）：该提交共 277 个 `test_case.yaml`，没有录制的工具输出文件；各例在 `before_test` 中部署真实 manifest，因此上游实际上只有 live 一档，回放档是本项目自行设计。

## 1. 已核查事实

**本仓库合同与现状。** 外部验收面是 `IncidentScenario -> IncidentOutcome`，只投影证据、决定、动作、权限、人工交互和最终状态；LLM judge 不能替代确定性断言（`AGENTS.md:40-46`）。`IncidentScenario` 只有版本化场景身份、feature、验收步骤、kind、subject，`IncidentOutcome` 投影最终状态、证据 ID、决定、动作、权限、人工交互、handoff 原因和报告可用性（`opspilot/acceptance.py:93-136`）；已有 M1-01 验收例覆盖正常、模型失败、预算、deadline、durable handoff（`tests/acceptance/test_m1_01_acceptance.py:27-89,113-139`）。

v4 冻结包复用同一入口；当前严格入口使用 v4/v2，要求原始输入、实际发出的视图/查询窗/身份/hash、唯一交付和完整报告绑定到当前 Run（`docs/testing/first-investigation-v4-2026-09-10.md:5-22`）。历史候选是正常/故障各 2 个独立 Run；该包明确旧失败保留、没有新模型证明，且不得用报告文字取代可见证据（同文件 `:3,79-81,103-107`）。M1-01 收口记录说明：同 4 个核心场景和两个留出故障连续三包上游口径 6/6，但严格 v4 的 P1/P2 门槛未达成、`passes` 未翻转；每 Run 约 3–9 轮、5–28 次工具调用、0.1–0.3 CNY、50–170 秒（`docs/tasks/2026-09-28-m1-01-closure.md:8-24`）。

现有 profile 选择为 `fixture`（默认、无网络、CI/测试/本地演示）或 `otel-demo`（真实只读 Prometheus/Jaeger；两个进程必须相同）（`opspilot/tools/profiles.py:1-20,45-87`）。fixture 返回固定窗口和固定指标视图（`opspilot/tools/fixture.py:1-15,76-100`）；OTel Demo 合同测试把真实 Prometheus/Jaeger 响应保存于 `tests/fixtures/otel_demo/`，由记录请求的 fake opener 重放（`tests/test_m1_otel_demo_contract.py:1-8,129-145`）。已有 M1-01 离线重放把保存的真实报告重新送进 loop，不发 provider 请求，并能确定性复现 `REPORT_INVALID`（`tests/acceptance/test_m1_live_flash_replay.py:1-9,79-125`）；M1-02 重放只读取已存采样、重算恢复判定，真实四场景加一次重放，且不调用模型（`docs/tasks/2026-10-03-m1-02-recovery-observation.md:58-81,177-195`）。这两类重放不能直接当作模型质量评测。

`m1_live_flash_loop.py` 名称含 live，但实际用 fixture transport、内存 store，固定 `budget_limit=4`、请求模型轮数 2（`scripts/m1_live_flash_loop.py:188-223`）；`m1_live_runner.py` 走真实 PG/runner，但同样是 fixture tool、默认 2 次模型请求、12 分钟 deadline（`scripts/m1_live_runner.py:1-25,254-269,270-320`）。它们共同通过 `lab_evidence.freeze` 写每 Run `summary.json`，原始 ledger 放 ignored 目录，摘要含报告、判定、计数、费用、trace 和 ledger SHA-256（`scripts/lab_evidence.py:1-21,250-293`）；lab 模式每轮一个 `opspilot-lab-<round>` LangSmith project，先验证 lab target、设 longlived，再 bounded read-back trace（同文件 `:53-118,124-158,170-227`）。

kind lab 的固定 host 端口是 18080/19090/16686（`scripts/kind_lab/kind-config.yaml:1-25`）；故障脚本改 flagd ConfigMap，并在写入后回读 SHA-256、恢复前核对注入快照（`scripts/kind_lab.py:867-984`）。现有记录显示调查与 Observer 共用单个实验端点，PG 使用临时实例/端口；没有“每 xdist worker 自动独立 kind+PG+端口”的现成隔离（`docs/tasks/2026-10-03-m1-02-recovery-observation.md:88,100,197,237`）。因此共享 kind 上故障注入不能并行，DeepSeek 并发/费用水位尚未被本次预审实测。

**HolmesGPT 固定提交的评测口径。** `tests/llm/test_ask_holmes.py:56-74` 从 `fixtures/test_ask_holmes` 收集目录，按环境、模型、case 参数化；case 字段由 `HolmesTestCase` 定义：`user_prompt`、`expected_output`、`evaluation`、`tags`、`before_test`/`after_test`、setup timeout、conversation history、env、toolsets、port forwards、max tokens 等（`tests/llm/utils/test_case_utils.py:118-153`，闭环 replay 字段见 `:193-217`）。`expected_output` 只给 LLM-as-judge，看不到被测模型；`before_test`/`after_test` 负责环境准备和清理，port forward 在 setup 后建立、要求本地端口唯一并提前检测冲突（`.claude/skills/create-eval/references/test-case-format.md:23-32,45-69`）。示例以中性 prompt、固定唯一值和可验证的 readiness/cleanup 脚本防猜测（`tests/llm/fixtures/test_ask_holmes/09_crashpod/test_case.yaml:1-55`；复杂 traces 例 `114_checkout_latency_tracing_rebuild/test_case.yaml:4-22`）。

重复与并行是 pytest 参数展开：`ITERATIONS` 把每 case 平铺重复，标签转为 pytest marker（`tests/llm/utils/iteration_utils.py:15-53`）；本提交 runner 把迭代上限设为 10，并把 `ITERATIONS`、`RUN_LIVE`、`CLASSIFIER_MODEL`、`EXPERIMENT_ID` 导出，pytest 用 `-n auto/指定 worker`（`run_benchmarks_local.py:39-43,118-157,380-399`）。`RUN_LIVE` 在 conftest 中只控制 before/after 基础设施 setup/cleanup 是否执行；未设置时仍是收集/模型测试路径，不是“工具输出回放”开关（`tests/llm/conftest.py:131-145`）。所谓 mock 主要是 DAL 的 `issue_data.json`/`resource_instructions.json` 文件替身（`tests/llm/utils/mock_dal.py:16-53,106-133`），没有通用的 mock tool-output 录制协议；主测试仍创建 `ToolCallingLLM`、使用临时 tool-result storage 并调用评测模型（`tests/llm/test_ask_holmes.py:574-685`）。

判分先由 judge 依据 `expected_output` 产出 correctness，再叠加确定性检查（例如 forbidden tools、max tokens）；pytest 状态与 judge 分数都通过才算 pass（`tests/llm/test_ask_holmes.py:173-187,270-300`；`tests/llm/utils/test_results.py:48-74`）。结果收集按 case 排序，输出 GitHub/终端表和 Braintrust 评测链接（`tests/llm/conftest.py:728-738,744-965`）。这可借鉴“重复展开、标签、before/after、结果表”，但本仓库必须改为 LangSmith + 冻结摘要，并保留确定性入口。

## 2. 决定表（需用户裁决）

| 编号 | 问题 | 选项与代价 | 推荐（优先上游且快速） | 影响面（不可逆/合同） | 裁决 |
|---|---|---|---|---|---|
| D1 | 场景清单与来源 | A 复用 v4 的 normal-1/2、fault-1/2，加已有留出 `productCatalogFailure`/`cartFailure`；B 立即新增 no-data/provider-failure/wrong-target 等，覆盖更广但改变分母 | **A；新增场景另开版本**。六例先冻结，故障答案放 evaluator 侧 | 场景 ID、分母、F1/F14 验收 | 2026-10-12 按推荐 |
| D2 | replay 档录制什么、模型是否调用 | A 只回放最终报告/解析（零模型，便宜但只测 parser）；B 回放完整 model-visible 输入、tool schema、tool call/result、视图/hash/时间/权限，模型真实再跑；C 录制最终输出直接重放（不可测模型） | **B；另保留 A 作为结构重放，不计质量率**。`RUN_LIVE` 不作为语义开关 | evidence/data contract、费用、可复现性 | 2026-10-12 按推荐 |
| D3 | live 环境与故障注入 | A 复用 kind + OTel Demo 2.0.2 + `scripts/kind_lab.py fault`；B 每场景独立 kind/PG/端口（隔离强、成本和实现量高） | **A，串行复用共享 lab**；按 flag 注入前后 SHA、独立观察先行 | 运行权限、恢复、数据出口、不可并行 | 2026-10-12 按推荐 |
| D4 | 并行粒度 | A replay worker 可 `-n auto`；live 全部串行；B 每场景独立环境后并行；C 共享环境 xdist（会相互污染） | **A**；live `parallel=1`，只并行无网络 replay/解析；DeepSeek 并发先留 1 | 共享 ConfigMap、19090/16686、PG/费用水位 | 2026-10-12 按推荐 |
| D5 | 每场景次数 N | A replay/live 都 3；B replay 5、live 2（复用既有 5 次 replay 与 v4 2 Run）；C 统一 10（上游上限，成本/时长高） | **B：replay=5、live=2；留出 live 先 2**，写明置信息不足 | 成功率分母、预算、基线可比性 | 2026-10-12 按推荐 |
| D6 | 成功判据 | A 只 LLM judge；B 只确定性；C 确定性 gate（schema、evidence/target/time bind、权限、final state、根因映射）后再以 judge 评叙述支持度 | 预审推荐 C；**裁决：先只用 B**。确定性判据包括 schema、证据/目标/时间绑定、最终状态，以及根因结论必须点名注入故障的服务与操作并引用证据 ID；LLM judge 等 C3 第 388–398 行的校准完成后再加，届时表中加 `judge_pass` 列 | IncidentOutcome、F1 第 5 步、失败分类 | 2026-10-12 用户裁决 B |
| D7 | 成功率表与基线存放 | A 仓库冻结每 Run/aggregate `summary.json`，LangSmith project 存 trace；B 只存 LangSmith dataset/experiment；C 只存 Markdown | **A**：仓库是长期依据，LangSmith 是 trace/read-back；每轮一个 project，raw ledger 不入库 | ADR-0006、长期复核、数据保留 | 2026-10-12 按推荐 |
| D8 | CI 档位 | A PR CI 跑 replay deterministic N=1；B CI 跑 live；C 两档都不进 CI、全手工 | **A**；live 仅手工/夜间 lab，失败不把外部环境抖动伪装成代码回归 | CI 凭据/外部依赖、F1 passes | 2026-10-12 按推荐 |
| D9 | 与 feature `passes` | A 实现 harness 即翻 F1/F14；B 只在全部场景、重复、held-out、版本/权限/成本证据和独立审查通过后翻 | **B**；本 PR 只落合同与证据，不翻任何 `passes` | 产品验收门槛（当前 F1/F14 false） | 2026-10-12 按推荐 |
| D10 | 费用/时长上限 | A 沿当前每 Run 100 请求、单次 model 360s、Run backstop 7200s；B 恢复脚本旧 2/4 请求和 12min；另设 suite 总额 | **A 作为技术上限；另裁 suite 总预算/墙钟**（当前上限见 `opspilot/investigation/limits.py:22-34,87-93`）。费用按余额差，摘要记录 planned/actual/unknown | AGENTS 费用合同、DeepSeek 余额、超时状态 | r2 2026-10-12 用户裁决：不设套件级上限，只用每 Run 技术上限，每轮记录余额差 |
| D11 | trace 与 `summary.json` | A 只留 LangSmith URL；B 每 Run 必有冻结摘要：Outcome、versions、scenario、counts/tokens/cost、trace id/link/read-back、ledger SHA，原始 ledger 外置 | **B**，沿 ADR-0006 现有 `freeze`；trace read-back 找不到如实记 `not_found` | 审查证据、不可伪造链接、保留期 | 2026-10-12 按推荐 |
| D12 | 时间、状态、主体/权限、旧证据兼容 | A 场景固定 v4 300s；B 使用 Run 授权 frame，工具 query 窗由模型在 frame 内选；状态以当前 PG/Outcome 为准，旧摘要只读重放 | **B**（已实现的 otel-demo 语义，`opspilot/tools/otel_demo.py:15-33,43-51`）；任何 replay 不重签旧 hash、不把 stale/unknown 变 fact；权限固定 read_only | C3 §4/5/8/11、历史证据兼容、状态陈旧 | 2026-10-12 按推荐 |


### lead 补充（2026-10-12，用户按推荐裁决）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| D13 | 回放档里模型发出未录制的查询 | A 记为 `replay_miss`，单列，不计入成功率分母；B 计为失败；C 回退为真实调用 | r1 推荐 A；**r2 改为 B**：A 与 C3 第 394 行、v4 第 99 行「失败和放弃必须进入对应分母」冲突。该 Run 计为失败，另单列 miss 率；录制集合可随 live 轮次增补 | 成功率口径、回放可信度 | r2 2026-10-12 用户裁决 B |
| D14 | 与 SPEC 中「M0 遗留项须在 candidate evaluation 之前完成」的关系 | A 本套件定为开发基线，不是候选评测，不支持任何发布或 `passes` 结论；B 先补齐 M0 遗留项 | **A**，在 SPEC 有界开放段落中写明 | SPEC 门槛 | 2026-10-12 按推荐 |
| D15 | kind lab 与其他会话共用 | live 档开跑前须向其他会话确认 lab 空闲；同一时间只允许一个真实故障 | 照此执行 | 并发、实验环境 | 2026-10-12 按推荐 |

费用前提：DeepSeek 余额 2.96 CNY（2026-10-12）。基线约 12 次 live Run 加 30 次回放 Run，按每 Run 0.1–0.3 CNY 估计需要 4–13 CNY，跑基线前须充值。

### r3（2026-10-12，PR #210 第二轮分诊，用户裁决）

- D6 补正常场景：绑定与状态检查通过，且没有事实型 claim 把故障目录中的服务或操作说成原因；`inconclusive`/`partial` 满足此条件即算通过。原因：r1 只定义了故障场景的判据，两个正常场景的结果会取决于实现。
- D8 修正：PR CI 只跑不调用模型的结构重放（离线、无凭据）；调用模型的回放手工跑。原因：调用模型的回放需要 DeepSeek/LangSmith 凭据，把凭据给 PR CI 会暴露给不可信 PR 代码。

### r4（2026-10-12，PR #210 第三轮分诊，只收紧）

- D1：`productCatalogFailure`、`cartFailure` 已在三包中跑过，结果已公开，改称开发场景；留出结论需要另建隔离的未见场景集。
- D6 故障场景：除点名服务与操作外，引用的证据中须有一条满足场景预先定义的证据特征（C3 §11）；具体判定在 PR-A 预审中定。
- D6 正常场景：任何事实型因果诊断都判失败，不限于故障目录中的组件。
- R3：已开始的 Run 若 setup 失败或结果未知，计入分母并判失败。

### r5（2026-10-12，PR #210 第四轮分诊，只收紧）

- D6 故障场景：每一条事实型因果诊断都须点名注入故障的服务与操作，并满足场景的证据特征；存在其他事实型因果诊断即判失败。
- PR-D 删除 F14 对照报告，与任务记录的「范围外」和 SPEC 的开发基线定位保持一致。
- 机器人审查循环：已到第 4 轮。此后同类的判据细化发现按类拒绝，转入 PR-A 预审处理（见 bot-review 停机规则）。

### r6（2026-10-12，#211 第 1 项，用户裁决）

- D6 改选预审原推荐的选项 C：确定性 gate 通过后，由校准后的 LLM judge 判因果条件；`overall = deterministic_pass AND judge_pass`。原因：`ClaimV2.kind` 只分事实与假设，因果诊断还会出现在 summary 与正文中，r4/r5 的因果条件无法机读（v4 第 25 行）。另两个选项：给报告加结构化因果字段（改模型可见合同、需改版本），或每 Run 独立证据审查（6×7 次不可扩展）。用户按推荐选 judge，参照上游 HolmesGPT（`tests/llm/test_ask_holmes.py:173-187,270-300`）。
- 不变：r1–r5 的因果条件本身；judge 不替代确定性断言；expected answer 只在评测侧。
- 新增前提：judge 未按独立标注校准前不记录基线（C3 第 388–398 行）。judge 模型（是否与被测模型分开）、标注来源、校准样本与一致率门槛留给 PR-A 预审。
- 费用前提更新：DeepSeek 余额 202.15 CNY（2026-10-12 充值后）。

## 3. 可逆默认（R 项，Agent 自决并记录理由）

- R1：目录采用 `docs/evidence/eval/<round>/runs/<run_id>/summary.json`、`aggregate.json`、`manifest.json`；理由是与现有 `freeze`/ADR-0006 对齐，便于删除整轮而不改历史。
- R2：`manifest.json` 固定 code/upstream/model/prompt/tool/profile/evaluator/permission/fixture hash、N、seed、时间和命令；理由是满足 v4/C3 冻结且不把答案送入模型。
- R3：aggregate 每行固定 `scenario_id, mode, repetition, outcome_state, deterministic_pass, judge_pass, overall, failure_class, model_requests, tokens, cost, trace_link, summary_path`，同时输出 `passed/attempted` 和失败分母；理由是禁止把 skipped/setup/unknown 混进成功率。
- R3 修订（r4）：分母只排除尚未开始的跳过（单列）；已开始的 Run 若 setup 失败或结果未知，都计为失败（C3 §11「失败和放弃必须进入对应分母」）。
- R4：replay 采用固定 seed 的场景顺序和 `pytest` 参数 ID；仅无共享环境的 replay 使用 xdist，live runner 默认拒绝 `parallel>1`；理由是可重现且 fail-closed。
- R5：after/restore 必须幂等，任何注入/恢复都保存前后 SHA；临时 PG/端口不自动抢占已有资源；理由是现有 kind_lab 的安全边界和可回滚。
- R6：不新增 pytest marker；复用现有 tags/feature_id，避免上游所说的无效 tag 导致收集失败。

## 4. 建议的 PR 切分与验收

1. **PR-A：F1 外部评测合同与 schema**（D1、D5–D9、D11–D12）。新上下文验收作者只按 `IncidentScenario -> IncidentOutcome` 写确定性 fixture/aggregate 合同测试；不得翻 `passes`。验收：收集、分母、状态/权限/evidence bind、失败分类、旧摘要只读兼容。
2. **PR-B：replay 录制/回放与基线表**（D2、R1–R4）。验收：同一 manifest 在无网络 fixture 下重放 N 次；结构重放零模型；模型 replay 的每次调用、summary、LangSmith read-back 和余额差均可对账；独立审查确认 expected answer 未进入模型输入。
3. **PR-C：live kind/OTel 场景驱动**（D3–D4、D10）。验收：单场景注入→独立观察→调查→restore 串行跑通；端口/PG 冲突 fail-closed；至少一次真实 Run 有 LangSmith trace + 冻结 `summary.json`；不声称生产证明。
4. **PR-D：CI 结构重放入口与第一轮基线入库**（D7–D9）。验收：PR CI 只跑不调用模型的结构重放（离线、无凭据）；手工 live 须明确 `--mode live`、round 与 N；基线表入库。（r5：删除原 F14 对照报告。F14 matched baseline 不在本有界开放范围内，见任务记录与 SPEC。）

每个 PR 一个合同条款、目标 `main`、不做 stacked PR；完成前由未参与实现的 Agent 独立审查并按影响项复验（`AGENTS.md:48-60`）。

## 5. 风险与未核查项

- **未核查**：本次没有启动 kind、PG、worker、DeepSeek 或 LangSmith；因此 DeepSeek 并发上限、实际费用/时延、PG 多实例隔离和 live 故障互扰尚无运行证据。
- **未核查**：现有 `m1_live_*` 脚本没有 `otel-demo` live adapter；把它们直接扩成并行评测会混淆 fixture 与真实环境，需 PR-C 先定入口。
- **未核查**：judge 尚无新套件的人工校准/held-out 资格；C3 要求先校准再冻结 N、统计方法和阈值（`docs/design/technical-proposal-2026-09-07.md:388-398`）。
- **未核查**：当前 SPEC 仍把若干 M0 遗留（真实恢复/权限隔离/RBAC/held-out/judge calibration）列为候选评测或更大 M1 的入口条件（`SPEC.md:3-10`）；本预审只形成合同，不代表已经获准开跑。
- 上游来源边界：固定提交源码已按 path:line 核查，但 Git checkout 元数据因网络失败；未来若实施需在任务记录固定 tar/raw SHA 或重新取得可验证 Git checkout，并再次确认没有提交漂移。


