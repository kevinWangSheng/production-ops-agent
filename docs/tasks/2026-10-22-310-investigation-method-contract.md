# #310 合同预审全文（r1）

> 收录自预审工件 `tmp/prereview-310.md`（2026-10-22）。U1–U6 用户裁决：按推荐（2026-10-22）。#310 与 #311 同属 F3 PR-2，同分支同 PR，见[任务记录](2026-10-22-311-source-errors.md)。


- 预审者：未参与实现的 Agent，只读；2026-10-22。基线 `origin/main` @ `aa57f9a`（含 F3 PR-3a `dc474da`）。
- 依据：issue #310；`docs/research/upstream-design-audit-2026-10-22.md` 第四节第 2 行；C3 §5（`docs/design/technical-proposal-2026-09-07.md:162-199`）；`opspilot/instructions/discipline.py`；`opspilot/investigation/reports.py:25-135,205-249`；`opspilot/investigation/context.py:641-648`；`opspilot/investigation/loop.py:79,85-124`；F3 任务记录、合同 r1、PR-3a 合同；rubric `docs/testing/f3-competing-cause-rubric.md`；#307 A 合同（`origin/feature/F3-307-prom-discovery`）；上游 `generic_ask.jinja2@a0009195:46-78`。
- 现状要点：live 变体是 `replay-candidate`（`loop.py:79`），序列 = `REPLAY_OPENING, EVIDENCE_DISCIPLINE, MISSING_SERIES, report_contract`（`discipline.py:173-178`），13 句（`tests/test_instruction_discipline.py:281`）。当前 `discipline_revision` = `l1a-replay-candidate-e70df72344d9`（本地按 origin/main 文件计算），`prompt_revision` = `prompt-replay-candidate-dd9018031487`（钉在 `tests/acceptance/test_f3_step5_competing_scenario.py:103-105`）。模型可用工具只有 metrics、traces（+#307 A 的发现工具），**没有日志工具**（`otel_demo.py:148-149`）。

## 1. 段落草稿（英文，按现有段的风格：段首空格、句号分句、不用 e.g./i.e.，因为句数普查用 `(?<=\.)\s+` 切句）

**`INVESTIGATION_METHOD`（key `investigation_method`，8 句，约 1205 字符 / ~300 token）**

| # | 草稿句 | 上游 | 处理与理由 |
|---|---|---|---|
| M1 | Investigate toward the deepest cause the evidence supports: when a problem in one service traces to a dependency, investigate that dependency too. | :48-49 | 改写。去掉「as many tool calls as needed / keep going」：开场已有停止规则「until the evidence is sufficient」（`discipline.py:61`），PRODUCT-CONSTRAINTS:24 要求不产生无界工作 |
| M2 | After finding a likely cause, keep checking for other possible causes before you conclude, and report each candidate that remains as its own hypothesis. | :50 | 改写。「numbered list」→ 报告是 JSON，用 hypothesis claim 表达 |
| M3 | Report the specific identities the evidence shows, such as exact service, operation, metric and label names, not generic examples. | :51 | 保留，K8s 名词换成本产品的身份（服务/操作/指标/标签） |
| M4 | If a named service, operation or metric returns nothing, compare the name with the names the tools list or have returned before treating it as absent; never query outside the authorized targets. | :52 | 改写。上游「猜拼写变体」与目标身份由可信侧决定冲突（PRODUCT-CONSTRAINTS:32,38）；改成对照工具列出/返回过的名字，与 #307 A U2 句「只用返回过或已列出的名字」同向。**不点工具名**（工具专有知识属 L3，`discipline.py:28-31`） |
| M5 | When the exact entity named has no data but a similarly named one does, report both facts: that nothing was found under the exact name, and what was found for the related entity, labeled as a different entity. | :53 | 保留，压缩 |
| M6 | Treat error text as exact diagnostic evidence; do not add alternatives that the message itself rules out. | :55 | 改写。去掉数据库认证的具体例子 |
| M7 | Data often contains errors that are irrelevant to the question; leave them out of the conclusion unless the evidence ties them to this problem. | :57 | 保留 |
| M8 | A service that is up or ready can still be failing; check its own error and latency signals before calling it healthy. | :58 | 改写。没有日志工具，「ALWAYS check application logs」换成错误与延迟信号 |
| — | （不搬） | :54 | 已被开场「facts/hypotheses/counterevidence/unknowns separately」与 EVIDENCE「do not upgrade correlation to causation」、L2「inconclusive」覆盖 |
| — | （不搬） | :56 | K8s/Secret 专用；「读不到就说未核实」已在 L2（`reports.py:73-80`） |

**`IMPACT_SCOPE`（key `impact_scope`，9 句，约 1704 字符 / ~425 token）**

| # | 草稿句 | 上游 | 处理与理由 |
|---|---|---|---|
| I1 | Name an entity as affected only when you observed evidence for that entity itself, such as its own error spans, error-rate reading or failed operations, and cite that evidence. | :62 | 改写，K8s 证据换成 span/错误率 |
| I2 | Never widen the impact by inference: a failing dependency does not show which of its callers broke, and a sample describes the service or operation it was taken from, not requests you did not see. | :63 | 改写。去 node/pod；副本采样 → trace 采样，与 EVIDENCE「sampled view」一致 |
| I3 | A change, restart or error burst on an entity during the incident window is not impact until its own evidence ties it to this cause; coincidence in time is not causation, and also affected is a claim that needs its own evidence. | :64 | 改写，删 kubelet/OOMKilled/SuccessfulCreate 等 K8s 例子 |
| I4 | Clearing an entity needs evidence too: say it was unaffected only after checking it, and say so when you did not check. | :65 | 保留 |
| I5 | Entities you checked and found healthy are a finding worth reporting, not something to drop. | :66 | 原文 |
| I6 | Consequences you did not observe, such as downstream or customer impact, are either verified before you state them or stated as unverified with what would confirm them. | :67 | 保留，去 namespaces/clusters |
| I7 | Counts and scope words such as all, several or also affected must match what you actually observed. | :68 | 保留 |
| I8 | When you describe impact, place every entity you name in exactly one group and express the group through the report: affected is a fact citing that entity's own failure evidence tied to this cause; checked and healthy is a fact or counter-evidence citing the evidence you looked at; changed during the window for another reason is a fact that cites the change and names what caused it, plus a rejected hypothesis when it was considered as this cause; not checked is a gap. | :69 | 改写。「label them」→ 映射到现有 claim 类别与 gaps（见 D2）；删 K8s reason token 清单；「controller rollout」泛化为 change |
| I9 | Anything that changed during the window and that you cannot place as affected or as changed for another reason is not impact; record it as a gap. | :69 尾 | 保留 |
| — | （不搬） | :71-78 | K8s 专用，issue 明确不搬 |

已核：两段不含 F3 隔离词（payment/charge/f3/unrelated/decoy，`test_f3_step5_competing_scenario.py:175-177`）、不含 checkout/deploy/kube/replicaset、不含任何工具名。按草稿计算：新 `discipline_revision` 约为 `l1a-replay-candidate-9a45deb526fe`（仅供参考，以实现为准，测试不应钉这个值）；句数 13 → 30；system 消息 L1a 部分 1064 → 3973 字符。

## 2. 决定表

### 需用户裁决（合同层：措辞与 schema）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | 措辞 | a. 按第 1 节草稿（17 句）。<br>b. 只搬影响面（I1-I9），不搬方法。代价：偏离 issue 与上游。<br>c. 逐字照搬上游并保留 K8s 例子。代价：本产品没有 pod/kubelet 证据，模型会去找不存在的信号 | **a** | 所有新 Run 的模型可见面 | |
| U2 | 影响面分组进不进报告 schema | a. 只进提示，映射到现有 claim 类别与 gaps（I8）；`m0-report-v2` 不变（F3 合同 R5 已定 schema 不变）。<br>b. 新增 `impact` 字段（四组列表）。代价：schema 版本、迁移、工作台渲染、F3 rubric 的 gate 都要改，PR-3b 前不可能完成 | **a**（上游同样只靠提示） | 报告、工作台、rubric | |
| U3 | 报告校验是否强制分组 | a. 不强制（上游不强制）。<br>b. 加门（如「有 rejected_hypothesis 时必须有 fact」）。代价：新增报告失败与重试路径（同 F3 U5-c 被否理由） | **a** | `reports.py` 校验、交接率 | |
| U4 | C3 §5 L1a 的内容描述「只读边界、证据可信度、结论分类、反误读约束」（`technical-proposal:171`）没写「调查方法」 | a. 同 PR 在该格加「调查方法与影响面纪律」，属 C3 措辞补充。<br>b. 不改 C3，视为「证据可信度」的延伸。代价：C3 与代码不符 | **a** | C3 文本（用户门） | |
| U5 | 分组映射里用报告字段原词（`counter_evidence`、`rejected_hypothesis`、`gaps`）还是普通词 | a. 普通词（counter-evidence、rejected hypothesis、gap），与开场现有「counterevidence and unknowns」同风格，避免 L1a 内联 L2（C3 §5「不得互相内联」）。<br>b. 原词。更精确，但 L2 改名时 L1a 会过时 | **a** | L1a/L2 边界 | |
| U6 | F3 第 5 步的有效性：I8 把「窗口内因别的原因变化」引向 rejected hypothesis，与 rubric 第 3 条 gate 同向 | a. 接受：句子是上游通用规则，不含场景词；rubric 和答案的 sha256 不动；PR-3b 在新的模型可见面上跑（F3 记录 :56 本来就要求跑在最终可见面上）。<br>b. 把 I8 的映射去掉，只保留分组。代价：分组无处落地 | **a**；PR 说明要写明这一点 | F3 第 5 步证据的解读 | |

### 可逆默认（Agent 自定）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| R1 | 段边界与顺序 | 两段，按上游两个标题分；位置 `REPLAY_OPENING, EVIDENCE, MISSING, investigation_method, impact_scope, report_contract` | 方法在格式之前，与上游一致；前三段字节不变，前缀不变 |
| R2 | 哪些变体 | 只改 `replay-candidate`；两个 baseline 变体逐字节不变 | baseline 锚定 M0 脚本字面量（`test_instruction_discipline.py:187-249`） |
| R3 | revision | `discipline_revision` 与 `prompt_revision` 随内容哈希自然变；L2、run coverage、retry、alert、knowledge 模板与 `tool_schema_revision`、`context_policy_revision`、`PROJECTION_REVISION` 不变 | C3 §5 规则 1、4 |
| R4 | 在途 Run | 部署后被重新领取的在途 Run 进入 `blocked(INCOMPATIBLE_STATE)`，不为避免 blocked 而不 bump | C3:192-193；已有测试 `test_m1_durable_state_postgres.py:1958` 覆盖机制 |
| R5 | 回放 fixture | 不受影响：structural canary 与 B2 回放用合成 SYSTEM（`scripts/eval_structural_fixture.py:72,205`；`test_eval_b2_replay.py:2309`）。历史 live ledger 保留旧 revision（如 `test_m1_01_acceptance.py:436`），不改 | 已核 |
| R6 | 冻结答案 | rubric/answer sha256 不变（`test_f3_step5_competing_scenario.py:37-38`） | 本项不碰 evals/ 与 rubric |
| R7 | 与 #307 A 的合并顺序 | #307 A 先合（已跑 lab，它的测试钉 `prompt_revision` 不变：`test_307a_prom_discovery.py:57,447`）；#310 之后 merge main，改两处钉值：`test_307a_prom_discovery.py:447` 与 `test_f3_step5_competing_scenario.py:103-105`。若 #310 先合则由 #307 A 改。不做 stacked PR | AGENTS.md「PR 与合并」；两处都是独立作者写的验收测试，改钉值按「合同变更」处理，在任务记录写明，断言形式不改 |
| R8 | D3 与发现工具 | L1a 不提 KSM、不提工具名；M4 只说「对照工具列出或返回过的名字」，与 #307 A U2、F3 D3-a 的 metrics 描述同向，没有矛盾 | C3 §5 分层；#307 A R11 已定「先发现再查询」写进工具描述 |
| R9 | 与 F3 U5-a（确定性覆盖缺口节，「不改提示词」）的关系 | 「未检查 → gap」是模型写的补充；源失败的权威展示仍是 U5-a 的产品侧缺口节，两者不互相替代 | F3 合同 :130 |
| R10 | token 成本 | 每次模型请求多约 730 token（字符 / 4 估算，未用 DeepSeek tokenizer，未确认）。参照 `eval-b2-live` 一次 Run：8 次请求 132532 prompt token，约 16.6k/次，所以约 +4.4%/次、每 Run 约 +6k；供应商前缀缓存能省多少未确认 | 成本可接受，上游方法段规模相当 |

## 3. 允许修改的文件、可观察行为、最小冒烟

**允许修改的文件**：`opspilot/instructions/discipline.py`（两个新常量 + 两个 Segment + `replay-candidate` 序列 + 模块 docstring 说明）；`tests/test_instruction_discipline.py`（句数普查 13 → 30 及 docstring）；`tests/acceptance/test_f3_step5_competing_scenario.py:103-105` 的钉值（R7）；若 #307 A 先合，再加 `tests/acceptance/test_307a_prom_discovery.py:447`；新的独立验收测试文件；C3 §5 L1a 那一格（U4-a）；任务记录、ROADMAP 行。**不改** `reports.py`、`otel_demo.py`、`evals/`、rubric、`passes`、迁移。

**给独立测试作者的可观察行为**（全部确定性，不用 LLM judge）：
1. `render("replay-candidate", …)` 的输出按顺序包含两段新文本，位置在 `MISSING_SERIES` 之后、报告契约之前；前三段字节不变。
2. 新段落里没有 F3 隔离词（payment、charge、f3、unrelated、decoy），没有 checkout，没有工具名（`metrics_range_query`、`traces_search`、`get_metric_names`、`get_label_values`），没有 K8s 专用词（kubelet、OOMKilled、Evicted、pod）。
3. `discipline_revision("replay-candidate")` 不等于 `l1a-replay-candidate-e70df72344d9`；`investigation_versions()["prompt_revision"]` 不等于 `prompt-replay-candidate-dd9018031487`。沿用旧值做不等比较（`test_m1_alignment_c_contract.py:125-131` 的写法），不猜新值。
4. 两个 baseline 变体的 `render` 字节与 `discipline_revision` 不变；`tool_schema_revision`、`context_policy_revision`、`PROJECTION_REVISION`、`REPORT_SCHEMA_VERSION` 不变；`REPORT_CONTRACT` 字节不变。
5. `replay-candidate` 没有 `tool_specific` 段（`test_instruction_discipline.py:453-459` 继续成立）。
6. 实例值仍不进 revision：只有 `model_requests` 不同的两次 render，`prompt_revision` 相同。

**最小冒烟（B 批次，不做质量测量）**：一次有界真实 Run（deepseek-flash，现有 live 入口），只核对：ledger/summary 的 `prompt_revision` 是新值；system 消息含两段新文本（看 trace）；Run 正常结束（报告通过校验或按既有规则交接），工具请求全部只读。附 LangSmith trace 链接与冻结 `summary.json`（ADR-0006；本 PR 改的是调查 loop 的模型可见面，AGENTS.md「验证与汇报」要求）。不判报告质量，不算作 F3 第 5 步证据。效果测量放到本批合并后，由 F3 PR-3b 的两次 Run 承担。

## 未确认

- token 数是按字符估算，没有用 DeepSeek tokenizer；前缀缓存的节省也没有核对。
- 新提示是否真让模型多写 counter-evidence / rejected hypothesis，要等真实 Run（这里不做声明）。
- #307 A 与 #310 谁先合，要看两个 PR 的实际就绪时间；R7 两种顺序都已写明。
