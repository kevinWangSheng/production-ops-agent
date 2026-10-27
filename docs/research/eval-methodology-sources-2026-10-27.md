# Agent 评测方法论：一手来源与 OpsPilot 评测合同对照

日期：2026-10-27（全部网页访问日期同此）。状态：官方文档静态调查；只读 Anthropic、Braintrust、LangSmith 官方域名页面和 HolmesGPT 官方仓库，未登录平台、未运行任何评测。对照对象是 main 上的评测合同 r8（D1–D16、P1–P13）、P7 校准记录、#312，以及 M3 决策稿 Q7–Q9。

引文说明：除特别标注外，引文都经 WebFetch 抽取，没有逐字节对照原始 HTML。只有 Claude 文档的评分方法一节是从保存的原文中 grep 出来核对的。lead 复核（2026-10-27，curl 取原始页面逐字比对）：A1 的「two domain experts」「too rigid」「pass^k」「consistency is essential」「20-50」「isolated」六处、L5 的 alignment score 定义与「at least 20 examples…balanced」、B7 的「below 80%」均与原文一致。引文保留英文原文。

## 1. 结论

1. **三家都要求先拿 LLM judge 和人工标注对齐，再用于判分；但三家的官方文档都没有给出数值门槛。** Anthropic 只写「closely calibrated」，LangSmith 把 alignment score 定义为与专家一致的百分比、建议至少 20 例且 0/1 平衡，但不给门槛。唯一的数字来自 Braintrust 一篇比较平台的营销文章，原话是「below 80%」需要改进，属于经验值。我们的 46/48（95.8%），再加零误通过和逐诊断包含，严于所有来源。Q7 把门槛收窄到 verdict 一致率，方向有来源支持（LangSmith 的 alignment 就是按二值标签算的一致率）。
2. **「完全照上游、不做事前校准」这个替代方案没有任何厂商来源支持。** 三家都把与人工校准写成 judge 的前提。HolmesGPT 公开的评测文档里，本次没有找到校准相关内容。
3. **Anthropic 明确建议不要评路径、要评结果，对多部分任务给部分分，每个维度各用一个独立 judge。** 我们「逐诊断 `(location, quote)` 精确包含」这条规则，本质上是在评 judge 的枚举路径，与这条建议相悖，支持 #312 方案 1。
4. **两人独立判定一致，是题目本身清晰的标准。** Anthropic 原话是「two domain experts would independently reach the same pass/fail verdict」。P7 的两份盲标之间的分歧，可以先当作题目或标签有歧义的信号，不必全部交用户逐字段裁定（推断）。
5. **在线评测、生产监控、A/B 测试和用户反馈飞轮都以生产流量为前提**，OpsPilot 没有生产流量，这些不适用。可以移植的只有一点：把 lab 中观察到的真实失败转成开发集用例。
6. **保留集方面**：LangSmith 建议用 split 区分 train/validation/test，并给数据集打版本标签；Anthropic 建议从 20–50 个来自真实失败的任务起步、正例负例平衡、每题附参考解。Q9 的六类场景应当每类都配「应该触发」和「不应触发」两种用例。
7. **基线对比方面**：pass@k 与 pass^k 应当分开报告。值班场景更接近 Anthropic 所说的「consistency is essential」，所以用 pass^k。两家平台都提供 pairwise 比较，但会受位置偏差影响。我们的主判据是确定性的二值判断，pairwise 只适合作为报告质量的辅助视图。

## 2. 各来源的方法要点

### 2.1 Anthropic

**A1 Demystifying evals for AI agents**：<https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>（2026-10-27 访问）

| 编号 | 要点 | 原文引文 |
|---|---|---|
| A1-1 | 评测结构：task / trial / grader / transcript / outcome | "A task (a.k.a problem or test case) is a single test with defined inputs and success criteria." / "Each attempt at a task is a trial." / "A grader is logic that scores some aspect of the agent’s performance." / "The outcome is the final state in the environment at the end of the trial." |
| A1-2 | 评结果，不评路径 | "it’s often better to grade what the agent produced, not the path it took." / "We’ve found this approach too rigid and results in overly brittle tests" |
| A1-3 | 三类评分者的取舍 | 代码评分者："Fast", "Cheap", "Objective", "Reproducible"；缺点是 "Brittle to valid variations…"。模型评分者："Non-deterministic", "Requires calibration with human graders for accuracy"。人工："Gold standard quality"，缺点是 "Expensive", "Slow" |
| A1-4 | judge 校准（无数值门槛） | "LLM-as-judge graders should be closely calibrated with human experts" / "Once the system is robust, it's sufficient to use human review only occasionally." |
| A1-5 | 给 judge 留出口 | "give the LLM a way out, like providing an instruction to return “Unknown”" |
| A1-6 | 每个维度一个独立 judge | "grade each dimension with an isolated LLM-as-judge rather than using one to grade all dimensions" |
| A1-7 | 部分分 | "For tasks with multiple components, build in partial credit." |
| A1-8 | pass@k 与 pass^k | "pass^k … measures the probability that all k trials succeed." / "(0.75)³ ≈ 42%" / "pass@k for tools where one success matters, pass^k for agents where consistency is essential." |
| A1-9 | 能力评测与回归评测 | 回归评测 "should have a nearly 100% pass rate." / "capability evals with high pass rates can "graduate" to become a regression suite" |
| A1-10 | 规模与来源 | "20-50 simple tasks drawn from real failures is a great start." / "Converting user-reported failures into test cases ensures your suite reflects actual usage" |
| A1-11 | 正负例平衡 | "Test both the cases where a behavior should occur and where it shouldn't." / "One-sided evals create one-sided optimization." |
| A1-12 | 题目清晰度与参考解 | "A good task is one where two domain experts would independently reach the same pass/fail verdict." / "create a reference solution: a known working output that passes all graders." / "a 0% pass rate across many trials (i.e. 0% pass@100) is most often a signal of a broken task" |
| A1-13 | 隔离与干净环境 | "Each trial should be “isolated” by starting from a clean environment." / "Unnecessary shared state between runs (leftover files, cached data, resource exhaustion) can cause correlated failures" |
| A1-14 | 读 transcript | "You won't know if your graders are working well unless you read the transcripts and grades from many trials." |
| A1-15 | 上线前与上线后 | "Automated evals are especially useful pre-launch and in CI/CD" / "Production monitoring kicks in post-launch to detect distribution drift…" / A/B 的缺点："requires sufficient traffic"；用户反馈的缺点："Sparse and self-selected" |
| A1-16 | 统计与样本 | 文中没有统计显著性检验；"this large effect size means small sample sizes suffice"。WebFetch 回报文中也没有出现 held-out 或过拟合的说法 |

**A2 Define success criteria and build evaluations**：<https://platform.claude.com/docs/en/test-and-evaluate/develop-tests>（2026-10-27 访问；`/define-success` 返回的也是这一页的内容）

| 编号 | 要点 | 原文引文 |
|---|---|---|
| A2-1 | 评分方法排序（原文 grep 核对） | "Code-based grading: Fastest and most reliable…" / "Human grading: Most flexible and high quality, but slow and expensive. Avoid if possible." / "LLM-based grading: … Test to ensure reliability first, then scale." |
| A2-2 | LLM 评分技巧（原文 grep 核对） | "Have detailed, clear rubrics" / "might require several rubrics for holistic evaluation" / "instruct the LLM to output only 'correct' or 'incorrect'" / "Use a grader model with thinking on" |
| A2-3 | 用异家族模型评分 | "Generally best practice to evaluate with a different model from the one used to generate the evaluated output" |
| A2-4 | 保留集、多维目标 | "on a held-out test set of 10,000 diverse Twitter posts" / "Most use cases need multidimensional evaluation along several success criteria." |
| A2-5 | 量优先于质 | "More questions with slightly lower signal automated grading is better than fewer questions with high-quality human hand-graded evals." |

**A3 Building effective agents**：<https://www.anthropic.com/engineering/building-effective-agents>。这篇只有一句与评测相关："We recommend extensive testing in sandboxed environments, along with the appropriate guardrails."，没有具体的方法论内容。

### 2.2 Braintrust

| 编号 | URL | 要点与原文 |
|---|---|---|
| B1 | <https://www.braintrust.dev/blog/evals-are-the-new-prd>（2026-03-27） | 成熟度分四阶段：Stage 0 "relying on vibes"；Stage 1 "test sets with pass/fail criteria that run on demand"；Stage 2 "automated and integrated into CI/CD"；Stage 3 "real-world failures become new test cases automatically"。AI judge："require calibration against human judgment to be trustworthy"；"human review provides the ground truth and the AI judge learns to approximate it"。文中没有一致率门槛 |
| B2 | <https://www.braintrust.dev/docs/evaluate> | "Offline evaluation runs against known datasets before deployment." / 在线评测 "evaluates production traces automatically as they're logged" / "Experiments are the immutable, comparable record of your eval runs." |
| B3 | <https://www.braintrust.dev/docs/evaluate/best-practices> | "Begin with 5-10 representative examples" / "Build datasets from failures, not successes." / "increase the trial count within a single experiment" / "Compare averages over individual results." / "Always compare against the version of your system that is actually in production" |
| B4 | <https://www.braintrust.dev/docs/evaluate/write-scorers> | "Scorers return a numeric score between 0 and 1." / "Measure different aspects (factuality, helpfulness, tone) with separate scorers and classifiers." |
| B5 | <https://www.braintrust.dev/articles/what-is-llm-as-a-judge>（2026-02-26） | "Deterministic checks should handle everything that can be measured directly" / "LLM-as-a-judge should focus only on subjective dimensions…" / "If agreement is low, revise the prompt or change the judge model before expanding usage." / pairwise "Sensitive to position bias"、"Randomizing order across runs and comparing results in both directions…" / "Periodically sample judge outputs and compare them with independent human scores" |
| B6 | <https://www.braintrust.dev/articles/what-is-prompt-evaluation>（2026-02-18） | "having humans score 100 to 200 examples" / "measuring agreement using metrics like Cohen's Kappa or Pearson correlation"，没有门槛 |
| B7 | <https://www.braintrust.dev/articles/best-human-in-the-loop-llm-evaluation-platforms-2026>（2026-04-03，平台比较营销文） | "If agreement drops below 80%, your scorers need work." 这是唯一一个数值门槛，属于经验值 |
| B8 | <https://www.braintrust.dev/articles/turn-llm-production-failures-into-regression-tests>（2026-05-29） | "Pin the judge model version, store the scorer prompt template in source control" / "revalidate the scorer against human-labeled examples before using the updated scorer as a release gate" / "Change the scorer only when reviewed examples show false positives or false negatives" / "Group traces by failure mode, keep one representative case per cluster." |
| B9 | <https://www.braintrust.dev/docs/annotate/human-review> | "Enable blind reviews to keep that data hidden for the entire review, so reviewers never see each other's judgments." / "Curate production logs into evaluation datasets." |
| B10 | <https://www.braintrust.dev/docs/evaluate/score-online> | 在线评分抽样率建议 "lower rates (1-10%) to manage costs" / "Use online scoring to validate experiment results in production" |
| B11 | <https://www.braintrust.dev/articles/how-to-eval>（2026-05-16） | "Offline agent evals function like unit tests or integration tests, emphasizing reproducibility." / "Low-scoring traces feed back into your datasets." |
| B12 | <https://www.braintrust.dev/articles/human-in-the-loop-evals-for-llm-apps>（2026-04-09） | "Teams can use human scores as ground truth to calibrate LLM-as-a-judge scorers." / "50 to 100 traces per week" |

### 2.3 LangSmith

| 编号 | URL | 要点与原文 |
|---|---|---|
| L1 | <https://docs.langchain.com/langsmith/evaluation-concepts> | 离线与在线："offline evaluations can check correctness against expected answers"；在线 "run on runs and threads from production traffic"。数据集："Splits are named subsets of a dataset…"、"LangSmith automatically creates dataset versions when examples change."、"Tag versions to mark important milestones."、"divide examples into training, validation, and test sets to avoid overfitting"。实验："An experiment represents the results of evaluating a specific application version on a dataset."。评估器："Code evaluators are deterministic, rule-based functions."、"LLM-as-judge evaluators require careful review of scores and prompt tuning."、"Pairwise evaluation works well when directly scoring an output is difficult but comparing two outputs is straightforward."。标注队列："Single-run queues…"、"Pairwise queues…"。参考输出："These do not get passed to your application, they are only used in evaluators."。回归："regression tests can assert that new versions must outperform baseline versions"。起步规模："Create 10–20 high-quality examples…"、"Once in production, convert real traces into examples." |
| L2 | <https://docs.langchain.com/langsmith/repetition> | "Repetitions are a way to reduce noise in systems prone to high variability, such as agents." / "LangSmith displays the average for each feedback score in the table."，同时提供标准差 |
| L3 | <https://docs.langchain.com/langsmith/evaluation-approaches> | final response 评测的缺点："you are not evaluating anything that happens inside the agent"；trajectory 评测："Sometimes there can be multiple correct paths."；single step 评测："they don't capture the full agent" |
| L4 | <https://docs.langchain.com/langsmith/evaluate-pairwise> | `randomize_order` 用于缓解位置偏差，但文档写明这类偏差 "should mainly be addressed via prompt engineering" |
| L5 | <https://docs.langchain.com/langsmith/improve-judge-evaluator-feedback> | "The alignment score is the percentage of examples where the evaluator's judgment matches that of the human expert." / "at least 20 examples" / "balanced in both 0 and 1 labels" / "To avoid overfitting to the labeled examples, it's important to add more labeled examples and test performance."；没有数值门槛 |

### 2.4 HolmesGPT 官方仓库（选读）

<https://github.com/HolmesGPT/holmesgpt/blob/master/docs/development/evaluations/index.md>："We use 150+ evaluations…"、"We also use the evals as regression tests on every commit."、"ITERATIONS=10"；regression 标签的含义是 "Critical scenarios that must always pass"。<https://github.com/HolmesGPT/holmesgpt/blob/master/docs/development/evaluations/benchmarking-new-models.md>："Use `CLASSIFIER_MODEL=gpt-4.1` for consistent evaluation across all benchmarks"。这两页都没有讲 judge 校准、统计处理或失败分诊。#312 引用的「事后失败分诊」出自 10-22 审计对上游源码的阅读，本次没有复核。

## 3. 对照表

| 方法点 | 来源 | 来源怎么说 | OpsPilot 现状 | 适配 | 理由 |
|---|---|---|---|---|---|
| task/trial/grader 结构 | A1-1、L1 | 任务、多次尝试、评分者、最终状态 | 场景 + attempt 槽位 + gate/judge + Outcome（`docs/tasks/2026-10-12-eval-suite.md:22-27,47`） | 适用（已有） | 结构一致 |
| 评结果而非路径 | A1-2、L3 | 路径评分过于僵硬 | 主判据看 Outcome 和证据，不看调用顺序（`AGENTS.md`「验证与汇报」）；但 P7 要求 judge 枚举出的 `(location, quote)` 必须包含标签（`opspilot/evaluation/calibration_flow.py:687-692,756-757`） | 部分适用 | 被测 Agent 已按结果评；judge 校准却在评枚举路径 |
| 代码、模型、人工三层 | A1-3、A2-1、B5 | 能用确定性检查的都交给代码；judge 只判语义 | D6 r6：`overall = deterministic_pass AND judge_pass`（`docs/tasks/2026-10-12-eval-suite-contract.md:74-75`） | 适用（已有） | 与 B5 的分工一致 |
| judge 校准方法与门槛 | A1-4、B1、B6、B7、L5 | 必须与人工对齐；指标是一致率或 Kappa；样本 20 例到 100–200 例；官方文档无门槛，营销文的经验值是 80% | 24 项 × 2 次，≥46/48，负例零误通过，外加逐诊断包含，任一观察失败即整轮不合格（`docs/tasks/2026-10-12-eval-suite.md:44`；`calibration_flow.py:768-797`）；p7-formal-2 结果为 `no_baseline`（`docs/tasks/2026-10-18-eval-p7-calibration.md:64`） | 部分适用 | 要校准，这点有来源支持；门槛与附加规则严于所有来源 |
| judge 的 Unknown 出口 | A1-5 | 允许 judge 返回 Unknown | P6 规定 error/invalid 都判不通过，没有 Unknown（`eval-suite.md:43`） | 部分适用 | 可增加 `unknown` 类别，计入失败分母、单独列出，交人工复核（推断） |
| 每维度一个 judge | A1-6、B4、A2-2 | 每个维度用独立 judge 或 scorer | judge 只判一个维度（因果支持），其余维度由 gate 负责 | 适用（已有） | — |
| 部分分 | A1-7、B4 | 多部分任务给部分分 | overall 为二值；summary 已经记录 gate 分项（`eval-suite-contract.md:157`） | 部分适用 | 门槛仍保持二值，聚合表可加各分项通过率（推断） |
| pass@k、pass^k 与重复次数 | A1-8、L2、B3 | 一致性优先时用 pass^k；用重复降噪，报均值和标准差 | replay 跑 5 次、live 跑 2 次，聚合只报 `passed/attempted`（`eval-suite.md:26,47`） | 部分适用 | 缺少逐场景 pass^k |
| 能力集与回归集 | A1-9、HolmesGPT | 回归集通过率应接近 100%；能力集高分题可「毕业」进回归集 | PR CI 只跑零模型的结构重放（`eval-suite.md:29`）；没有能力集与回归集之分 | 部分适用 | 结构重放可以作回归层；模型质量层没有回归集 |
| 规模与真实失败来源 | A1-10、B3、L1、B8 | 20–50 题起步；多从失败中取题；按失败模式聚类 | 6 个开发场景，都是干净故障或正常（`evals/scenarios/`）；决策稿 Q9 计划补六类 | 部分适用 | 「真实失败」在我们这里只能取自 lab 中的失败 |
| 正负例平衡 | A1-11、L5 | 应触发和不应触发都要测；校准标签 0/1 要平衡 | 场景是 2 正常 + 4 故障；P7 validation 是 6 正 18 负（`p7-calibration.md:18`） | 适用 | 新六类每类应配一对正负用例 |
| 参考解与题目清晰度 | A1-12 | 每题附一个能通过全部评分者的参考解；两位专家判定应一致 | 只有 `answer.json` 的期望答案和证据规则（`eval-suite-contract.md:151`），没有参考报告；两份盲标的分歧全部交用户裁定（`p7-calibration.md:23`） | 适用 | 参考报告可以检验评分者能否判出通过；盲标分歧可以先当作题目歧义处理（推断） |
| 隔离与干净环境 | A1-13、A3 | 每次尝试从干净环境开始；共享状态会导致相关失败 | 共享 kind，串行运行，注入前后核对 SHA，有洗净检查（`eval-suite-contract.md:32,223`）；#325 死 pod 序列（决策稿 F8 行） | 部分适用 | 不是每次尝试都起新集群；需要把洗净检查结果和集群状态写进每次 attempt 的记录 |
| 读 transcript | A1-14 | 不读 transcript 就不知道评分者是否在正常工作 | P7 debug 阶段读了原始回复并据此定位根因（`p7-calibration.md:56,59`） | 适用 | 建议基线轮固定抽样阅读（推断） |
| 异家族 judge | A2-3 | 用不同模型评分 | D16 选异家族 judge（`eval-suite.md:37`） | 适用（已有） | — |
| 在线评测、生产监控、A/B、用户反馈 | A1-15、B2、B10、L1 | 上线后监控、在线抽样评分 | 没有生产流量；产品模式不导出 trace（#243） | 不适用 | 只在合成 lab 中运行；F8 的健康探测属于运维范畴，不是质量评测 |
| 生产到离线的飞轮 | B1、B8、B11、L1 | 失败 trace 转为数据集行 | 平台选型文第 7 条提到「线上抽样反馈经审核进入开发回归集」（`docs/research/eval-platform-selection-2026-09-07.md:41`） | 部分适用 | 来源换成 lab 失败，只进开发集，不进保留集 |
| pairwise 对比基线 | B5、L1、L4 | 直接打分困难时用 pairwise；要随机化顺序 | 无；C3 要求基线尽可能匹配条件（`docs/design/technical-proposal-2026-09-07.md:403`） | 部分适用 | 主判据已是确定性二值，pairwise 只能作辅助 |
| 数据集版本与保留集 | L1、A2-4、L5 | 版本、标签、split；按 train/validation/test 防过拟合 | 28 维清单（`opspilot/evaluation/rounds.py:13-42`）；C3 要求开发集与保留集隔离（`technical-proposal-2026-09-07.md:397`）；目前没有保留集（#93） | 适用 | 仓库已承担版本存储；还缺 test split |
| judge 固定与重新验证 | B8、L5 | 固定 judge 版本，改动后重新验证；小标注集容易过拟合 | P7-D11：validation 暴露后要换来源族（`p7-calibration.md:24`）；judge 配置 hash 写入清单 | 适用（已有） | 与来源一致 |
| 盲审 | B9 | 评审者之间互不可见 | P7-D7：两份盲标互不可见（`p7-calibration.md:20`） | 适用（已有） | — |

## 4. 对决策稿 Q7/Q8/Q9 的影响

**Q7（judge 口径）**

1. 支持把门槛改为 verdict 一致率。依据：L5 的 alignment score 就是「judge 判定与专家相同的比例」；A1-2 反对评路径。逐诊断包含降为复核材料。
2. 46/48 不必放宽，但应在决策稿中写明：这个门槛是项目自定的，严于所有来源（来源中唯一的数字是 B7 的 80% 经验值），并不是对标得来的数字。
3. 「完全照上游、不做事前校准」这个替代方案，应在决策稿里写明没有厂商来源支持（A1-4、B1、B5、L5 都要求先与人工对齐），而不仅仅是「不满足 F1 第 5 步」。
4. 降低用户裁定成本（推断，依据 A1-12、L5）：verdict 口径下，只有两份盲标在二值 verdict 上不一致的项才交用户；逐字段差异不再需要裁定。两份盲标之间的 verdict 一致率单独记录，作为题目清晰度的参考上限。
5. 可选（推断，依据 A1-5）：judge 输出允许 `unknown`，计入失败分母，单独列出，并进入人工复核队列。这项会改 P6 的输出合同，需要单独裁决。
6. 校准通过后，按 B5 和 A1-4 的建议，每轮基线抽样少量 judge 判分做人工复核，监测漂移（推断）。

**Q8（上游基线）**

1. 每个场景同时报告每次尝试的成功率和 pass^k（A1-8）。live 只跑 2 次时，pass^2 的置信区间很宽，要在表中写明（A1-16 只说效应大时小样本也够用，没有给统计方法）。
2. 隔离（A1-13）：HolmesGPT 与 OpsPilot 交替运行时，每次尝试都记录洗净检查结果、故障 flag 状态和宿主 swap。存在共享状态时，两者的失败可能相关；可以考虑按场景交替顺序运行，避免同一侧总在宿主压力更高的时段运行（推断）。
3. 基线以确定性 gate 加 verdict 为主。若要比较报告质量，可加盲化、随机顺序的 pairwise 作辅助（B5、L4），不计入主指标（推断）。
4. 基线 judge 对两个系统使用同一个固定版本，且是校准通过的那个版本（B8）。

**Q9（保留集与场景集 v2）**

1. 六类每类都配正负一对用例（A1-11）。例如 wrong-target 一题要求拒绝或交接，另一题要求正常调查；prompt-injection 一题有注入，另一题内容相似但无注入（推断）。
2. 每个新场景附一份参考报告，能通过 gate 和 judge，用来证明题目可解、评分者能判出通过（A1-12）。对保留集，由生成方在仓库外完成这项验证，结果随封存包一起保存（推断）。
3. 数据集分开发集、校准集和保留集三个 split，并打上版本标签（L1）。保留集只在候选评测时打开一次；打开过的保留集改称开发集，这和 D1 r4 已有的做法一致。
4. 规模：Anthropic 以 20–50 题为起点（A1-10）。六类各一对用例加现有 6 个场景，大约 18 个，接近这个下限，应写明样本量有限（推断）。
5. lab 中已经观察到的失败（product-catalog 只拿到 trace 小样本时缺错误 span、#283、#284）按失败模式聚类，每类转一个开发用例（B8、A1-10）。这些用例不进保留集。

## 5. 未核实与加载失败

- 加载失败后重试成功的页面：`braintrust.dev/docs/evaluate`、`.../best-human-in-the-loop-llm-evaluation-platforms-2026`、`.../llm-evaluation-metrics-guide`、`docs.langchain.com/langsmith/repetition`、`.../evaluate-pairwise`。均为首次请求报「Socket is closed」，重试后成功，上文引文来自重试结果。
- 没有最终失败的页面。
- 未读：LangSmith 的 Align Evals 博客（blog.langchain.com），Braintrust 的 multiple reviewers 合并规则页，HolmesGPT 的 `running-evals.md`、`reporting.md` 和 `tests/llm` 源码。#312 所说上游「事后失败分诊」本次未复核。
- 未核实：除 A2-1、A2-2 外，所有引文都经 WebFetch 的摘要模型抽取，没有逐字节对照原文。Anthropic 文章的发布日期未取得。B6、B7、B11、B12 是 Braintrust 域名下的 articles，属于厂商教育或营销内容，不是产品文档。
