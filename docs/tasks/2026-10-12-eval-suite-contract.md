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

- D6 改选预审原推荐的选项 C：确定性 gate（schema、证据/目标/时间绑定、`read_only` 权限、最终状态；证据特征存在只查故障场景）通过后，由校准后的 LLM judge 判因果条件；`overall = deterministic_pass AND judge_pass`。原因：`ClaimV2.kind` 只分事实与假设，因果诊断还会出现在 summary 与正文中，r4/r5 的因果条件无法机读（v4 第 25 行）。另两个选项：给报告加结构化因果字段（改模型可见合同、需改版本），或每 Run 独立证据审查（6×7 次不可扩展）。用户按推荐选 judge，参照上游 HolmesGPT（`tests/llm/test_ask_holmes.py:173-187,270-300`）。
- 不变：r1–r5 的因果条件本身，包括 r5 的「每一条事实型因果诊断都须点名注入故障并由其自身引用的证据满足证据特征」，由 judge 逐条判定；gate 中「引用证据存在证据特征」只是必要条件；judge 不替代确定性断言；expected answer 只在评测侧。
- 新增前提：judge 未按独立标注校准前不记录基线（C3 第 388–398 行）。judge 模型（是否与被测模型分开）、标注来源、校准样本与一致率门槛留给 PR-A 预审。
- 费用前提更新：DeepSeek 余额 202.15 CNY（2026-10-12 充值后）。

### r7（2026-10-13，PR-A 合同预审，用户裁决）

预审者 Codex GPT-6 Astra（high，全新上下文，只读），核查版本 `8f221b5`；lead 复核了四项关键事实（`opspilot/acceptance.py:421` 持久化权限含 `human_control`；`opspilot/investigation/loop.py:550-556` incomplete 报告仍返回执行 completed；`scripts/kind_lab.py:100` 只有 `paymentFailure`；上游 `tests/llm/utils/test_env_vars.py` 的 `CLASSIFIER_MODEL` 默认同被测模型）。用户 2026-10-13 裁决：P1–P4、P6–P10 按推荐；P5 改为 GLM 5.3 作 judge。P5 改判带来的推论：judge 调用不占 DeepSeek 并发（P9 中「共用并发 1」改为 judge 单独串行）；评测证据（报告与引用的可见证据）发往第二家供应商，仅限开发评测；GLM 凭据与端点在接入 judge 的步骤前核查。预审全文如下（标题降级嵌入）。


只读预审，2026-10-12。核查版本：`8f221b556b21c75e61ec3d16bdafcacd57fc9549`。未修改文件、提交、stash/checkout、同步分支或运行评测；用户已有改动未触碰。下列推荐是待裁决方案，不代替已批准的 r6。

引用缩写：

- **T**：`docs/tasks/2026-10-12-eval-suite.md`
- **D**：`docs/tasks/2026-10-12-eval-suite-contract.md`
- **C3**：`docs/design/technical-proposal-2026-09-07.md`
- **V4**：`docs/testing/first-investigation-v4-2026-09-10.md`
- **E**：`docs/evidence/m1-01-alignment-c-effect/`
- **U**：HolmesGPT 固定提交 `46e3a728d762b774d2ec458357decb8698541ce5` 下的路径；已实际读取指定本地文件，补充文件通过固定提交的 GitHub contents API 读取。

#### 1. 已核查事实

**入口与结局。** `opspilot/acceptance.py:93–116` 的 `IncidentScenario` 只有身份、feature、步骤、kind、subject；`IncidentOutcome` 没有报告全文、证据内容或 `assessment_status`，因此评测必须另带证据包，不能只靠 Outcome 判报告质量。终态取值为 `completed/failed/blocked/budget_exhausted/cancelled/paused`（:88–90）；持久化投影依据发布记录、handoff 和人控状态判断（:291–359）。已有外部验收覆盖完成、provider 失败、预算、deadline、持久化交接（`tests/acceptance/test_m1_01_acceptance.py:37–89,113–139`）。

**报告与权限存在两个重要接缝。**

- `opspilot/investigation/reports.py:93–139`：`ClaimV2.kind` 有 fact、hypothesis、recommendation、counter_evidence、rejected_hypothesis，没有 causal 标记；因果文字可能位于 summary、claims[].text、gaps、next_steps。`assessment_status` 是 completed/incomplete，`conclusion` 是 supported/partial/inconclusive。
- `opspilot/investigation/loop.py:550–556` 对 incomplete 报告仍可返回 execution=completed，另记 `INCOMPLETE_INVESTIGATION`。仅检查执行 completed 会放过未完成调查。V4:93 明确 handoff 不能抵作正向能力通过，:99 要保留失败分母。
- loop/live-record 投影返回 `("read_only",)`，持久化投影返回 `("read_only","human_control")`（`opspilot/acceptance.py:126–135,415–426,458–469`）。权限 gate 不能直接要求单元素数组相等。

**冻结摘要与 trace。** `scripts/lab_evidence.py:250–293` 写 `opspilot-lab-summary-1`、experiment/run_id/frozen_at、调用者传入的业务字段及外置 ledger 的 hash/bytes/path，并未定义统一业务 schema。其回读状态实际为 found、root_run_not_found、no_trace_id、not_exported、error:*（:180–226）。

已有单 trace 示例为 `docs/evidence/m1-01-handoff-runner/live-runs/93372aa7-8fd5-4f8e-bb88-161b2cbc5d1f/summary.json:134–150`；多 trace 示例为 `docs/evidence/m1-pg-roles-live/live-runs/8e8ad43b-0b9e-5545-8b19-a3c202bc7855/summary.json:339–374`，包含 investigation、postmortem 两个 root。`record_in_summary` 实际位于 `scripts/lab_review_feedback.py:102–110`，追加反馈时重写 summary；其 CLI 仍只认 `trace.langsmith_run_id`（:129–137）。

**六场景及证据规则的可实现性。**

| 场景 | 注入与诊断对象 | 已核查依据 |
|---|---|---|
| normal-1、normal-2 | 无故障注入 | E `run.md:4–6,15–16` |
| fault-1、fault-2 | paymentFailure→100%；payment 的 Charge | E `fault-timeline.jsonl:1–2`、`run.md:17–18` |
| productCatalogFailure | →on；product-catalog 的 GetProduct | E `fault-timeline.jsonl:3–4`、`pc-fault/ledger.json:970–1002` |
| cartFailure | →on；cart 的 EmptyCart，展示的错误涉及 storage 连接 | E `fault-timeline.jsonl:5–6`、`cart-fault/ledger.json:254–287` |

当前 `scripts/kind_lab.py:100–103,867–881` 只硬编码 paymentFailure；另两项需要 PR-C 扩展。当前工具视图提供 service、operation、start_us、status_tags、error_by_visible_tags（`opspilot/tools/otel_demo.py:1342–1403`），可以写确定性规则。注意已有 GetProduct、EmptyCart 证据是 **checkout 上的失败 RPC client span**，不能只接受故障服务自身的 server span。`span_groups` 只汇总实际展示行（:1511–1559），不能代表全部流量。

**上游可直接借鉴的机制。** U `tests/llm/utils/test_env_vars.py:5–9` 支持独立 `CLASSIFIER_MODEL`，默认同 MODEL；`classifiers.py:135–173,215–252` 以 expected 和回答判二值分数，strict 要求全部 expected elements 满足，输出上限 16384。该接口没有独立传入证据正文；不能原样满足本项目逐条引用支持要求。`test_ask_holmes.py:152–187,270–298` 判回答后仍执行确定性约束，`test_results.py:64–74` 要求 pytest 成功且 judge=1。

U `.claude/skills/create-eval/references/test-case-format.md:23–32` 明确 expected_output 只给 evaluator；`anti-hallucination.md:5–7,54–103` 要求从工具发现答案。已读同目录 `running-evals.md:49–75` 及 `docs/development/evaluations/{index,adding-evals,reporting,running-evals}.md`；reporting:81–86 同样采用 0/1。未据这些局部文件推断“上游全仓没有校准机制”。

**合同与凭据边界。** 已通过 `gh issue view 211` 读取原文。C3:392 要求冻结全部实验维度，:396–400 要求可诊断性基于可见证据、失败进入分母、先开发校准再候选评测。SPEC:20 只开放开发基线，不能关闭 SPEC:6 的 formal judge calibration。仅检查 `.env` 变量存在性：有 DeepSeek/LangSmith key，没有 OpenAI/Anthropic/OpenRouter key；未输出值，也未验证有效性。202.15 CNY 是 T:15 的记录，本次未回读余额。

#### 2. 决定表

### 需用户裁决：合同、权限、验收与数据流

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| P1 | #211-2/5：完成结局与正面诊断 | A 同时查执行、报告、交接；B 只查无误诊，会放过未完成 | **A**。六例均要求 Outcome completed、report_available、decision=report_available、无 handoff，且 assessment_status=completed。正常仍允许 partial/inconclusive，但事实型因果诊断为零；故障至少一条且每条满足 r6，禁止空集 `all()` 通过 | V4:93/99；正常与故障通过条件 | 2026-10-13 用户按推荐 |
| P2 | #211-3：trace 回读不到 | A evidence-incomplete 并算失败；B 当诊断错误；C 忽略 | **A**。必需 trace 未回读或身份不匹配：deterministic_pass=false、overall=false，保留原 judge 判分，计失败分母，baseline_complete=false。有限回读后冻结，后来补证另建 revision，不覆盖旧结果。setup 失败无产品 Run 时保留评测 attempt root trace，不伪造产品 trace | T:16/32；证据完整性与基线完成声明 | 2026-10-13 用户按推荐 |
| P3 | read_only 如何匹配真实入口 | A 容许 human_control；B 精确单元素会误拒；C 改产品投影扩大范围 | **A**。必须含 read_only，额外只容许 human_control；写权限和未知权限拒绝，保留原数组并绑定实际权限配置 hash。该 gate 不宣称已经证明基础设施隔离 | r6 与 acceptance.py:421 的兼容 | 2026-10-13 用户按推荐 |
| P4 | #211-4：manifest 每个维度 | A 逐项必填；B 自由 versions 字典容易遗漏 | **A**。下述冻结清单每项必须有 version/hash，或带理由的显式 none；缺失是 unknown，不能冒充 none，unknown 阻止基线完成 | C3:392；PR-B/C/D 不再猜字段 | 2026-10-13 用户按推荐 |
| P5 | judge 模型 | A 独立调用现有 deepseek-flash，最快但同家族相关偏差；B 另一 DeepSeek 型号仍同家族；C 异家族，需要确认凭据、费用和数据出口 | **A 仅作开发起点**：显式独立配置、全新消息上下文，不沿用调查会话或自评分；披露 same-family，必须通过 P7。校准不达标再选 C，不能宣称换家族自动消除偏差。沿上游可独配且默认同模型的机制 | 模型、凭据、费用；不承诺偏差已消除 | 2026-10-13 用户改判：judge 用 GLM 5.3（异家族，用户已在 .env 配置凭据，未核查） |
| P6 | judge 输入、输出、引用归属与失败 | A 上游 expected+回答，不能核逐条支持；B 加真实可见证据与逐诊断记录 | **B**。输入完整安全报告、问题/授权主体时间、报告引用且已交付的 view 内容/ID/hash、expected answer、证据规则及匹配结果。输出每条诊断的定位/引文、是否事实型因果、服务/操作、引用、支持判定、短理由和二值 verdict。summary 不得继承全报告任意引用；可关联明确同义且带引文的 claim，并记录关联供复核。代码验证定位、ID和规则匹配并重算 AND。error/timeout/invalid/oversize/uncalibrated 均不通过；gate 失败可跳过 judge，但不能通过 | r6 全文识别、逐条支持、异常口径；无隐藏 raw、私有 reasoning、注入器答案或旧评分 | 2026-10-13 用户按推荐 |
| P7 | 校准标签、数量、门槛与失败处置 | A 人工盲标加独立复核，较慢；B 双独立 Agent 盲标加人工裁决，较快但标签仍有相关偏差 | **B，限开发基线**。两个未参与实现的全新上下文 Agent，独立于待校准 judge，先盲标、不看 judge 输出；记录模型身份，分歧交用户裁决。6 例各 2 份调试、4 份冻结验证，共12+24；优先历史完整报告与实际可见证据，不足用独立作者最小反例并标来源，同源变体不跨集合。24 份验证每份判两次：至少46/48与最终标签一致，至少12份负例两次均不得误通过；另核关键因果诊断有无漏检。失败不记基线；修 judge 后换验证集，不反复调同一验证集 | 提议的开发门槛，非统计可靠性保证；不关闭 SPEC:6 的 formal calibration/held-out 项 | 2026-10-13 用户按推荐 |
| P8 | D6 证据特征具体表达 | A 首版只支持可见错误 span，快但会拒仅有充分 metrics 的报告；B 同时解释通用 PromQL，工作量和误判面大 | **A，作为明确收紧提交裁决**。评测侧 `answer.json` 保存版本化 `any_of` 规则：精确 service/RPC operation 别名、error=true、时间窗、min_matches=1；四故障按上表映射，正常为 none。容许对应服务 server span，或 checkout 上同一完整 RPC 的失败 client span；不接受只匹配方法后缀或任意 checkout error。规则返回 evidence_id、trace/span_id、规则 ID；行须已交付且被引用、目标/hash/时间合格、operation 未截断，错误由展示标签复算。cart storage 错误支持 EmptyCart 失败，不能自动支持“valkey 服务宕机” | 首版证据资格；V4:81 不要求猜隐藏 flag；后续新增规则需版本化 | 2026-10-13 用户按推荐 |
| P9 | judge 调用上限、超时、费用 | A 单次固定分类；B 自动修复/投票，增加费用并改变判分 | **A**。生产评测每份报告一次、无自动重试；调查100请求/7200s保持，judge 是结束后的独立 evaluation 阶段。单次360s、请求8MiB/响应2MiB，输出初值16384沿上游；超限失败，不静默截断。DeepSeek调查与judge共用并发1；无 suite 预算上限。按轮余额差对账，分阶段记请求/token/耗时，无法拆分的费用标 unknown，校准费用另列 | D10 与外部调用；限值依据 limits.py:26,87–93，输出值待校准验证 | 2026-10-13 用户按推荐 |
| P10 | attempt 起点、分母与完成 | A setup 前登记 attempt；B 产品 Run 建好才计数，会漏 setup 失败 | **A**。先持久化 attempt_started，再 setup，product_run_id 可空。开始后的 setup/error/abort/unknown/replay_miss/judge_error/evidence_incomplete 全计失败；未开始 skipped 单列。每例固定 replay5/live2，失败不重抽替换，重跑保留新 attempt 与原失败。基线完成指固定槽位已执行、校准合格且证据齐全，可以得到低于100%的成功率；不是产品验收通过 | R3 起点、次数、失败分母及完成声明 | 2026-10-13 用户按推荐 |

**P4 的冻结清单：** code/upstream；场景、expected answer、证据规则；模型请求名及预期响应身份；prompt、tool/schema、projection、registry、profile、adapter、evaluator；judge 配置与校准包；权限；预算/超时/大小保护；kind/chart/images/config；主体初态、session、人工反馈、workload、知识快照；replay fixture；N、seed、命令、时钟策略。轮次配置在执行前冻结；每 Run 另绑定实际窗口、主体状态、注入前后 hash、实际响应模型名与录制身份。`none` 表示明确不适用，不能表示尚未取得。

**P7 验证样本必须覆盖：** 正常报告的隐蔽归因、summary 与 claims 不一致、额外错误归因、借用其他诊断引用、纯假设、故障无正面诊断、证据不足。标签保留逐条因果片段、引用支持与最终二值结果；先冻结标签/裁决，再读取 judge 输出。两次 judge 调用用于校准稳定性检查，不形成生产评测投票机制。

### 可逆默认：Agent 自定并记录理由

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| P11 | 场景与答案存放、隔离 | 单对象容易整包送模；分文件多一层序列化 | 分 `scenario.json` 与 evaluator-only `answer.json`；前者含版本/外部 Scenario/症状/授权引用，后者含 expected/signature/注入映射。模型输入仅从白名单 stimulus 生成，不含答案型场景 ID、路径或注入映射。理由：落实上游 expected 隔离且可直接测试 | PR-B/C 驱动边界，不改 ReportV2 | Agent 自定（理由见推荐列） |
| P12 | 每 Run 与 aggregate schema | 扩历史摘要会破坏既有消费方；新版包可显式兼容 | 沿 D:81 路径，新增版本化 eval summary/manifest/aggregate。字段见下；未知值附原因，不默认为零。理由：既有 freeze 的业务字段由调用者自由传入，需要评测层冻结接口 | PR-A 数据合同；PR-B/D 汇总消费 | Agent 自定（理由见推荐列） |
| P13 | 多 trace、旧摘要与反馈 | 强塞单 trace 会丢角色；列表可显式绑定 | 新 `trace.entries[]` 记录 role、required、attempt/product_run/evaluation 身份、project/id/url、normalized/raw read_back、checked_at。旧单 trace 转一项，多 trace 按角色/身份选取，不取第一项充数。旧件只读，不改 hash/原 verdict；新版反馈和补证另写 revision。理由：两种历史形状已存在，旧 record_in_summary 会重写摘要 | 历史兼容、冻结摘要 hash 与 trace 消费 | Agent 自定（理由见推荐列） |

**P12 最小字段合同：**

- `summary`：schema、round/attempt/scenario/version/mode/repetition、nullable product_run_id、轮次 manifest hash、每 Run 绑定记录、started/ended、完整 Outcome、报告原文/解析/hash、证据与实际交付记录的引用/hash、gate 分项、judge 状态/逐诊断结果/配置与校准 hash、overall、failure_class 与全部 failure_reasons、调查/judge usage、cost、trace、外置 ledger hash。
- `aggregate`：每 attempt 保留 D:83 的行字段并加 manifest_hash、judge_status、evidence_status；按 **scenario＋mode＋同一轮次冻结配置** 报 planned/attempted/passed/failed/not_started、passed/attempted、replay_miss/attempted、失败分类。零分母为 null；结构重放 quality_rate=null；live/replay 不混算。
- 固定主失败类及优先级：setup → replay_miss → execution → deterministic_gate → judge_error → diagnosis → evidence_incomplete；全部原因始终保留，避免主类掩盖 trace 缺失。started 槽位缺 summary 计 unknown 失败；重复身份、串轮次或不兼容版本拒绝汇总。

**合同预审九类覆盖：**

| 类别 | 已定或待决 |
|---|---|
| 时间 | 已定 T:33、V4:21–22：授权框与历史时间保真；P8 固定 span 起点落在查询窗，查询窗落在授权框，UTC、无额外容差；耗时单调钟，judge 阶段另记 |
| 计数与口径 | 已定 T:26/34、D:84；setup 前起点、缺结果、补跑与聚合待 P10/P12 |
| 状态转移 | 产品状态沿 acceptance.py:291–359，不由 judge 修改；评测 terminal、缺 trace 补证待 P1/P2 |
| 主体与权限 | 已定 T:33、PRODUCT-CONSTRAINTS.md:16/48；真实权限数组兼容待 P3，judge 凭据仅 evaluator 持有 |
| 数据合同 | 已定 D:81–83；完整字段待 P4/P6/P11–13；先 schema/reader，再 B/C writer，最后 D，无数据库迁移 |
| 旧数据与归档兼容 | 已定 V4:27、T:33；P13 只读适配，缺字段为 legacy/unknown，不能自动升级通过 |
| 并发与水位 | 已定 T:25/36；P9 明确 judge 共用 DeepSeek 并发1；P10/P13 防重复计数和覆盖冻结结果 |
| 外部调用 | 已定 T:31，无 suite 上限；模型、超时、大小、异常和费用待 P5/P6/P9 |
| 验收投影与 passes | 已定 SPEC:20、T:30/42；保留 Scenario→Outcome，增加评测证据包，不翻 passes |

#### 3. 与已裁决 r6 的冲突

1. **权限的字面实现冲突。** T:27/33 的 read_only 若实现成精确数组 `["read_only"]`，会拒绝 `opspilot/acceptance.py:421` 的合法持久化投影。应裁决 P3，不能擅改产品入口。
2. **完成状态存在会触发 V4 冲突的缺口。** T:27 允许 partial/inconclusive，但未明确 assessment_status；`loop.py:550–556` 证明 execution=completed 不充分。若仅凭执行状态通过，会违反 V4:93/99；P1 补足，不撤销正常场景允许不确定结论的决定。
3. **manifest 清单不完整。** D:82 未逐项列出 C3:392 的主体初态、session、人工反馈、workload、知识快照等维度；P4 补齐。

r6 引入校准 judge 本身与 SPEC:20 一致，无需重开。它不等同 V4 全文事实审查完成，也不关闭 SPEC:6 的 M0 formal calibration。P8 的 trace-only 是本次明确提交裁决的收紧，不能当作 r6 已经批准。

#### 4. PR-A 的验收方式

独立验收作者只拿批准后的合同、C3/V4/r6、公开 DTO/schema 和接口规格，不看实现。保留 `IncidentScenario -> IncidentOutcome`；建议公开评测接口为：

- `evaluate(ScenarioSpec, Outcome, ReportCapture, DeliveredEvidence, Manifest, JudgeResult) -> RunEvaluation`
- `aggregate(Manifest, PlannedAttempts, RunEvaluations) -> Aggregate`
- `investigator_input(ScenarioSpec) -> ModelVisibleStimulus`

使用固定 JudgeResult 和假客户端编写确定性合同测试，覆盖：

1. 六例身份/版本映射；改变 expected 或注入映射不改变被测模型输入。
2. 错目标、错窗、坏 hash、未交付引用拒绝；错误 span 的 operation、状态、时间及缺失/截断反例。
3. completed＋partial 正常报告可过，incomplete/handoff 不过；故障零诊断、额外诊断、错误引用不通过；summary 诊断与引用关联规则。
4. read_only＋human_control 可过，写权限拒绝；gate/judge AND 真值表；超时、非法 JSON、未知引用、未校准均失败。
5. 校准计数与门槛边界、缺标签拒绝；这些测试只验证计算，不能证明真实语义识别质量。
6. trace 缺失/串 Run/多 root、旧摘要 hash 保真；manifest 每维度缺失、unknown、合法 none。
7. setup 前后跳过、失败/放弃/未知/replay_miss、缺 summary、重复 ID、零分母、跨模式/版本拒混；结构重放零模型、零凭据、无质量率。

PR-A 完成只代表合同/schema、离线测试和独立审查完成。真实 judge 校准、PR-B 录制隔离、PR-C 注入与真实 Run、PR-D CI 与基线入库仍须各自证据；不得翻任何 `passes`。

#### 5. 风险与未核查项

- **未核查：** 本次没有执行测试、调用模型、校准 judge、回读 LangSmith 或运行 kind/PG；仅审阅代码、合同、历史证据及固定上游文件。未验证凭据有效性、当前余额、judge 可用性/费用/延迟。既有4–13 CNY估计不能直接覆盖新增 judge 和校准调用。
- **未核查：** 当前 kind 的实际 chart/config 与另两个 flag 是否可用；历史记录不能代替现场预检。`scripts/otel_demo_observe.py:174–189` 的流量前提包含 payment，不能原样当成所有故障的充分可诊断性判据。
- **未核查：** PR-B 完整输入、实际交付与 hash 录制的实现可行性。旧 Outcome/summary 不足以重建可见证据；材料缺失的报告不得充作校准金标。
- **边界：** 上游完整树的 `truncated=false` 已核实，但未审全仓，不断言其不存在其他校准机制。双 Agent 标签、同家族 judge、小开发样本均可能相关失误；P7 门槛是待裁决建议，尚无运行证据。模型、prompt、规则或标签变更须新版本并重新校准；本套件不支持生产可靠性、发布或 M0 完成结论。

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


