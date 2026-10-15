> 裁决（r1，2026-10-15）：用户对本表全部「需用户裁决」项采纳推荐；其中 F12 U1（结论发布后追问开新 Run）、F7 U1（#92 记为不适用关闭）、F7 U3（先在 kind 探测 OTLP 写入，确认后加只读鉴权代理）为单独确认。可逆默认按表执行。出处：[M1 退出与 M2 开放决策](2026-10-14-m1-exit-decision.md)。

# F13 第 4 步合同预审：后续调查读取已审核知识，新鲜度与矛盾处理

- 预审者：未参与实现的 Agent（只读，2026-10-14）。代码与文档基准：worktree `/Users/shenghuikevin/dev/AI/poa-m1-exit`（分支 `chore/m1-exit-decision`）。下文的 `path:line` 都相对于这个 worktree。
- 依据：`feature_list.json` F13 第 4 步；SPEC 第 12 行（2026-10-09 段：第 4 步、知识快照、开发集/保留集隔离和保留期清理移到 M2）；SPEC 第 24 行（2026-10-14 段：M2 依次开放 F11 → F13 第 4 步 → F12）；`docs/tasks/2026-10-14-m1-exit-decision.md` D6；C3 `docs/design/technical-proposal-2026-09-07.md:152-156`（上下文）、`:378-380`（复盘与知识）、`:392-394`（Eval）、`:404-406`（保留）；PRODUCT-CONSTRAINTS 第 24、30、32、48 行；M1-03 任务记录 D3、D4、D12、D13。
- 取向：项目用于求职展示，按 F13 第 4 步验收所需的最小切片收敛。上游有现成做法的直接照搬，不额外加固。

## 验收步骤原文与最小切片

> Run a later incident against changed or stale knowledge and verify freshness and contradictory evidence are handled according to the reviewed policy

切片划在验收必须覆盖的四件事上：

1. 后续调查确实读到已批准的知识。
2. 知识被替换或撤销后，后续调查读到的是变化后的版本。
3. 陈旧的知识按策略处理。
4. 当前证据与知识矛盾时按策略处理。

这几点都要在外部能看到、能用确定性断言检查。

**最小切片**：Run 在网页或告警接入时建输入，建输入时由 web 进程按「同一目标、只取 active」读出最多 3 条知识，每条带新鲜度标签，写进 Run 的输入快照（input v5）。模型把知识当作不可信线索收到。报告用约定标签声明每条知识的核对结果：与证据一致、被证据否定，或未核对。验收投影只从输入快照和报告中复制这些记录。

**移出本切片**（U7）：

- 知识快照与开发集/保留集隔离：评测路径本来就不经过 web 建输入，天然隔离，正式隔离随 P7 放到 M3。
- 保留期清理。
- 复盘生成读取跨事故知识，以及跨事故替换。
- 全文检索索引和知识管理后台。
- 「审核知识积累是否改善效果」实验（C3:394）。

验收步骤本身不需要以上任何一项。

## 已核查的现状（事实）

- **知识读取已经存在，但只能按条目查**：`KnowledgeStore.active_revision(entry_id)` 只返回 `active` 的 revision，并带 `freshness`，包括 `approved_at`、`generated_at`、`observation_ended_at` 和 `evidence_snapshot_sha256`（`opspilot/knowledge/store.py:959-987`）。没有按目标或文本检索的接口（`store.py` 的方法列表见 `:490-1132`）。
- **知识形态**：revision 有 `name`、`tags text[]` 和 `content`。`content` 是规范 JSON，含 symptoms、checks、evidence_refs（`opspilot/migrations/versions/0008_postmortem_knowledge.py:318-357`）。来源链 revision → `source_postmortem_id` → postmortem → incident → `target_id` 完整（`0008:338-339`；`opspilot/persistence/incidents.py:93` 写入 `target_id`）。状态由视图推导：撤销优先，其次是被替换，否则为 active（`0008:368-378`）。
- **调查输入的组装点**：首个 Run 的输入在 web 进程的 `WorkbenchService._fresh_input` 中建立，网页提交（`opspilot/web/service.py:296`）和 Alertmanager 告警（`service.py:480-490`）都走这里。后继 Run 由 `continuation_input` 复制上一个 Run 的 `scope_facts`（`opspilot/investigation/inputs.py:196-227`；`service.py:891-940`）。评测、回放和 live 脚本直接调用 `fixture_face().input_for`（`scripts/m1_live_runner.py:294`、`scripts/m1_takeover_evidence.py:326`），`opspilot/evaluation/` 不引用 `opspilot.web`（grep 结果为空）。
- **输入版本有先例**：v4 把 `alert_context` 作为一条带边界说明的不可信用户消息放进固定前缀（`opspilot/investigation/context.py:73-87`、`:600-632`），边界文字计入 `prompt_revision`（`opspilot/investigation/loop.py:113-121`）。未知但格式合法的版本会被拒为 `INCOMPATIBLE_STATE`（`context.py:530-536`）。
- **报告合同**：fact、counter_evidence、rejected_hypothesis 只能引用本 Run 实际提供的 evidence_id（`opspilot/investigation/reports.py:56-83`），所以知识里旧事故的 evidence_refs 在结构上不能当作事实依据。报告没有专门表达知识核对结果的字段。
- **权限**：`opspilot_web` 对 knowledge_entries、knowledge_revisions、knowledge_revocations、knowledge_revision_states、observation_endings、postmortem_versions、postmortems、incidents 都有 SELECT（`opspilot/persistence/grants.py:103-133`、`:153-154`）。`opspilot_worker` 只能读 revisions 和 states，对 entries 和 revocations 没有任何权限（`grants.py:240-243`）。Observer 对 F13 表零写权限（M1-03 D8）。
- **幂等重放**：intake 重放时不比较 input，以首次写入为准（`opspilot/persistence/incidents.py:83-90`）。
- **实验环境**：kind 实验环境只有一种故障，即 flagd `paymentFailure`（`scripts/kind_lab.py:100-103`）。
- **上游**：HolmesGPT 在系统 prompt 中放按告警过滤的 skill/runbook 目录，用 fetch 工具取全文（`docs/research/holmes-source-audit-2026-09-07.md:36-43`，审计版本 `5e983c1`）。当前 upstream main 本次没有重读，**未确认**；本地 `tmp/*/upstream/holmesgpt-46e3a72…` 副本里没有 `holmes/` 源码目录。

## 九类必答

1. **时间**：待决 U1（新鲜度从何时算起、阈值多少）。可逆默认 R3：时钟取检索快照事务内的数据库 `clock_timestamp()`，按整秒计，不设容差，依据是现有读取都走 `transaction(snapshot=True)`（`store.py:967`）。
2. **计数与口径**：待决 U4（取几条、怎么排序）。可逆默认 R2：单条和总量的字节上限。U2 决定「矛盾已处理」用什么计数口径。
3. **状态转移**：已定的部分是只读 active，superseded 和 revoked 都不返回（M1-03 D3，`store.py:959-987`）。待决 U5：知识在 Run 内固定，还是后继 Run 重新读取。撤销是人工审核动作，不是权限收紧，C3:154「权限收紧即时生效」是否适用需要裁决，见 U5。
4. **主体与权限**：在 web 进程内检索，现有 SELECT 授权已经够用（`grants.py:103-133`），不需要新授权或迁移。如果改在 worker 检索，worker 对 entries 没有权限（`grants.py:240`），需要新授权，因此不推荐。通过视图间接读 revocations 依赖视图属主权限，本次没有实测，**未确认**。
5. **数据合同**：不建新表，不迁移。新增 input v5（`scope_facts.knowledge`），见 R1。部署顺序：v5 的 Run 行会被旧 worker 拒为 `INCOMPATIBLE_STATE`（`context.py:533-536`），按 C3 §11 先停 web、worker、Observer，再一起升级。新边界文字会改变 `prompt_revision`（`loop.py:113-121`），因此升级前要排空进行中的 Run（R6）；排空不了的旧 Run 被重新领取时会被版本屏障阻塞，具体行为**未确认**，实现时需要实测。
6. **旧数据与归档证据**：v1–v4 输入照旧读取（`context.py:97-102`）。旧 Run 视为「无知识输入」（M1-03 记录第 10 行）。已归档的 lab 库里已批准的知识，升级后会被同一目标的新事故读到，不需要回填。知识 content 里的旧 evidence_refs 不能被新报告引用（`reports.py:66-70`），R2 改为只送来源标识。
7. **并发与水位**：检索在 `accept` 事务之外，单独开一个只读快照事务。如果检索与批准、撤销并发，结果要么包含该变化、要么不包含，都合法。intake 重放以首次写入的输入为准（`incidents.py:83-90`），所以检索结果不确定不会造成冲突。复盘水位不受影响，因为知识读取不写任何东西。
8. **外部调用**：不新增模型调用。每个 Run 的输入最多多约 12 KiB（R2）。检索失败的处理见 R5。真实验收共 3 个有界真实 Run，加上 D1 已有的复盘生成，不另申请额度（AGENTS「费用与真实调用」）。trace 中的知识内容只在实验环境导出，产品模式不导出（ADR-0006）。
9. **验收投影与 `passes`**：待决 U2、U6、U8。可逆默认 R9：新增 `KnowledgeUseOutcome` 投影。F13 `passes` 只在第 4 步的真实验收通过、且第 1–3 步已有证据被认可后才翻转（SPEC:12）。

## 决定表——需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | 「reviewed policy」中的新鲜度规则（PRODUCT-CONSTRAINTS:30 要求知识保留 freshness） | A 按 `observation_ended_at` 起算年龄，超过阈值 N 标 `stale`，**照样提供但带标签**，排在新鲜知识之后。B 同样起算，超过 N 直接**不提供**，只记被排除的条数。C 除年龄外，「同一目标在知识之后有发布或配置变化」也算陈旧：需要接 F11 的发布事件，范围变大。代价：A 让模型可能被旧知识误导，但运维人员能看到；B 更安全，但「陈旧」在报告里看不到 | A。N 默认 30 天，理由是 C3:404 规定关闭后的原始资料保留 30 天，超过 30 天的知识，其证据可能已经被清理。实验环境用环境变量调小 N，以便在真实验收中造出陈旧知识 | 检索、输入 v5、页面、验收 | |
| U2 | 「矛盾证据已处理」怎样变得可观察、可确定性断言（AGENTS「LLM judge 不替代确定性断言」） | K1 只在提示中说明「知识是线索，证据优先」，不改报告，代价是验收无法确定性判断矛盾是否被处理。K3 沿用现有 claim 种类，用文本标签 `[knowledge:<entry_id>@r<n>]` 表达：被否定写 `rejected_hypothesis` 并引用本 Run 证据，一致写 `fact` 并引用本 Run 证据，未核对写 `hypothesis`。不改 schema，验收解析标签，代价是靠文本约定，稍显脆弱。K2 报告升到 v3，新增 `knowledge_checks` 字段：结构化最清楚，但牵动 `reports.py`、`scripts/m0/*` 报告合同、`incident.html` 和评测编解码，改动面最大 | K3。真实模型可能不按约定打标签，这种失败照实记录，不为通过而放宽 | 提示边界文字、`prompt_revision`、验收投影 | |
| U3 | 知识怎样交给模型 | A 在建输入时放进输入快照：一条带边界说明的不可信消息，照 `alert_context` 先例，满足 C3:152「输入快照固定知识版本」，确定、可重建。B 照搬 Holmes：prompt 放目录，模型用工具取全文。要新增工具注册、证据存储语义，还要定义知识算不算证据，范围大，而且是否取用由模型决定，验收不确定 | A。B 留作以后的可选项。Holmes 的目录加过滤思路通过 U4 的「同一目标」过滤部分采纳 | `context.py`、`inputs.py`、`web/service.py` | |
| U4 | 检索范围和排序（C3:156 写明用 PG 全文检索加名称和标签匹配） | A 只取同一 `target_id` 的 active revision，按 `ts_rank`（问题文本或告警文本对 name、tags、content）排序，并列时按 `approved_at` 新的在前，取前 3 条，全文不命中也不过滤；不建索引。B 跨目标全文检索，可能把无关服务的知识带进来。C 只按目标和时间排序，不用全文，偏离 C3:156 | A。数据量小，顺序扫描足够；GIN 索引等需要时再加 | `KnowledgeStore` 新增只读方法 | |
| U5 | 知识在 Run 内和后继 Run 中的时点 | A 每个 Run 在创建时固定一份；运行中的撤销不影响该 Run；后继 Run（new_run、续期、reopen）**重新检索**，撤销从下一个 Run 开始生效。B 后继 Run 沿用上一个 Run 的知识，与 `continuation_input` 现状一致，改动为零，但撤销在 reopen 后的事故里一直不生效。C 运行中撤销即时生效，需要在 loop 中途改前缀，破坏重建 | A。撤销属于审核动作，C3:154 的「即时生效」针对权限，不适用 | `service.py:891-940` 后继输入 | |
| U6 | 真实验收的场景定义与 `passes` 翻转条件 | 场景见下文「验收运行计划」。矛盾场景有三种造法：a 在系统健康时提问，当前证据否定「付款失败」类知识，不改实验环境；b 给 kind 实验环境加第二种 flagd 故障，扩大实验工具；c 人工写入错误知识，违反来源规则和触发器，**不可行**。`passes` 条件：A 第 1–3 步认可 M1-03 已有证据和 D1 一体化运行的证据，第 4 步用新的真实运行，全部通过后翻转。B 要求第 1–4 步在一次运行中全部重做 | 矛盾场景用 a；`passes` 用 A | 验收证据、`feature_list.json` | |
| U7 | 明确移出的内容（需写进 SPEC 段） | 移出：知识快照与开发集/保留集隔离（随 P7 放 M3，评测路径不经过 web 检索，天然不受污染）、保留期清理（另开 issue，M1-03 D12 的约束保持）、复盘生成读取跨事故知识、知识管理后台和索引、「知识是否改善效果」实验。代价：C3:392 要求每次实验固定知识快照，M3 之前没有落实 | 按上述移出 | SPEC、ROADMAP、issue | |
| U8 | 运维人员能否看到（PRODUCT-CONSTRAINTS:24 要求陈旧数据和矛盾证据对运维可见） | A 事故页的 Run 段落新增只读列表：每条知识的条目和 revision、新鲜度标签、年龄、来源事故链接，以及报告中对应标签的核对结果。B 不改页面，只在验收投影里有，违反第 24 行 | A。展示的数据就是投影的数据，不另算；这也是演示的主画面 | `web/templates/incident.html` | |

## 决定表——可逆默认（Agent 自定）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R1 | 输入快照的形态 | 版本为 `opspilot-investigation-input-v5`，`scope_facts.knowledge` = `{policy_version, retrieved_at, max_age_seconds, status, entries:[{entry_id, revision, name, tags, content, content_sha256, source_incident_id, source_postmortem_id, source_version, approved_by, approved_at, observation_ended_at, age_seconds, freshness, truncated}]}`。只要启用检索，即使 entries 为空也记录，「没有知识」因此可见 | 采用。v4 加 knowledge 也算 v5，`version` 属性按优先级推导（`context.py:497-505`） | `context.py` | 理由：沿用 v2–v4 的增量做法 |
| R2 | 单条和总量上限 | 单条 content 规范化后上限 4 KiB，超出截断并标 `truncated`；总量上限 12 KiB；`evidence_refs` 不送原 ID，改为只送条数和来源事故 ID，避免模型引用后触发 `REPORT_INVALID` | 采用 | 检索与边界文字 | 理由：上下文 1M，影响可以忽略；旧证据 ID 引用了也必然无效 |
| R3 | 时钟 | 在检索快照事务内取数据库 `clock_timestamp()`，记为 `retrieved_at`；年龄 = `retrieved_at − observation_ended_at`，整秒，不设容差 | 采用 | 检索 | 理由：与现有 F13 时间来源一致 |
| R4 | 检索落在哪里 | 新增 `KnowledgeStore.knowledge_for_target(target_id, query_text, *, limit, max_age)`，只读、快照事务，web 调用 | 采用 | `knowledge/store.py`、`web/service.py` | 理由：web 已有权限（`grants.py:103-133`），不需要迁移 |
| R5 | 检索失败 | 存储不可用时**不阻塞** intake，记 `status:"unavailable"`、`entries:[]`，页面可见 | 采用 | `service.py` | 理由：知识只是线索，调查不应因此停摆；失败可见 |
| R6 | 边界文字与版本 | 新增常量 `KNOWLEDGE_BOUNDARY`（「已审核的历史知识，是不可信线索，不是本 Run 的证据，须用工具核对，当前证据优先，按约定打标签」），计入 `prompt_revision_versions`；部署前排空 | 采用 | `loop.py:113-121` | 理由：照 `ALERT_CONTEXT_BOUNDARY` 先例；PRODUCT-CONSTRAINTS:32 规定 runbook 类内容是不可信的 |
| R7 | 开关与评测隔离 | 产品 web 默认启用，环境变量可关；脚本和评测走 `fixture_face().input_for`，不受影响 | 采用 | `web/__main__.py` | 理由：不接触 C3:394 的隔离 |
| R8 | 标签的运行时校验 | 运行时**不**因标签缺失或异常拒绝报告，校验只放在验收投影里（标签引用了未提供的条目、`fact` 或 `rejected_hypothesis` 没有引用本 Run 证据，都判为未处理） | 采用 | `reports.py` 不动 | 理由：不新增报告失败模式和重试 |
| R9 | 验收投影 | 新增 `KnowledgeUseOutcome`：从 Run 输入快照复制 entries 及标签，从已发布报告复制每条知识的核对结果。主体不一致返回 `unknown`，同 D11 写法；零模型、无自有 SQL（同 R12） | 采用 | `opspilot/acceptance_*.py` | 理由：沿用 F13 第 1–3 步的投影风格 |
| R10 | 测试分工 | 验收和合同测试由未参与实现的 Codex 按本表与 F13 原文编写；PG 场景用确定性桩模型输出带标签的报告 | 采用 | `tests/` | 理由：AGENTS「独立审查」 |

## 建议的 PR 拆分（3 个，均以 `main` 为目标，前一个合并后再开下一个）

1. **PR-A 授权与合同（用户门，文档类）**：SPEC 新段（F13 第 4 步有界开放、U7 移出项）、任务记录及决定表 r1、ROADMAP 行。
2. **PR-B 检索与输入（功能 PR，用户门）**：R4 检索、R1 input v5、R6 边界、U5 后继 Run 重新检索、U8 页面段落、R5 失败处理。内容包括实现者测试、Codex 独立合同测试和独立审查。这个 PR 改了调查 loop 的输入，所以附一次有界真实 Run 的 LangSmith trace 和 `summary.json`（AGENTS「验证与汇报」，ADR-0006）。
3. **PR-C 验收投影与真实验收（用户门）**：R9 投影、Codex 独立验收测试、下文真实验收的证据，验收通过后翻转 F13 `passes`。

## 验收运行计划

- **起点**：D1 M1 退出一体化运行留下的库的副本，已有目标 T 上从事故 I1 发布的 active 知识 K1 r1（`docs/tasks/2026-10-14-m1-exit-decision.md` D1）。如果 D1 的批准没有走通，退回用 #203 归档库副本中已批准的条目（`docs/tasks/2026-10-11-m1-pg-runtime-roles.md` 第 49-50 行；归档在主仓库被忽略的 `tmp/pg-roles-archive/`，目录内容**未确认**）。
- **运行环境**：web、worker、Observer 都用非 owner 角色；kind 实验环境；`deepseek-flash`；trace 只在实验环境导出；每个 Run 沿用现有防循环上限。
- **确定性层（PG 集成和验收测试，不调用模型）**：
  - 只返回 active。
  - 撤销后不返回。
  - 替换后返回新 revision。
  - 其他目标的知识不返回。
  - 阈值边界上 fresh/stale 判定正确（注入时钟）。
  - 截断和总量上限生效。
  - 存储不可用时记 `unavailable`。
  - v5 能往返序列化，旧版本照旧读取。
  - 后继 Run 重新检索。
  - 页面与投影逐字段一致。
  - 桩模型输出带标签的报告，投影的判定正确。
- **真实层（3 个有界 Run，按顺序执行）**：
  1. **知识一致**：再次注入 `paymentFailure`，触发告警，新事故 I2。断言 I2 输入含 K1 r1、标为 fresh；报告中 K1 的标签核对结果按实际记录（期望是一致并引用本 Run 证据）。
  2. **知识变化**：通过工作台 HTTP 撤销 K1（如果 D1 或 I2 的复盘产出了替换，就同时验证替换），然后恢复故障、再注入，新事故 I3。断言 I3 输入不含被撤销的 revision；如有替换，含新 revision。
  3. **知识陈旧且与证据矛盾**（U6-a）：事前在 D1 库中保留一条 active 知识，或用 I2 批准产生的新知识。把实验环境的 N 调小，使它变成 stale。在系统健康时通过网页提交「checkout 付款是否失败」。断言输入中该知识带 `stale` 标签；报告中该知识是 `rejected_hypothesis` 或未核对；没有任何 fact 依赖知识；页面显示陈旧和否定。
- **证据**：每个 Run 的 trace 链接、冻结的 `summary.json`、余额差额，按 ADR-0006 入库，原始 ledger 不入库。某一步失败就照实记录并交接，不为通过而反复重跑（D1 取向）。
- **`passes`**：确定性层全部通过、3 个真实 Run 的断言全部通过、第 1–3 步证据按 U6 被认可，满足这三项才把 F13 改为 `true`。
