# #308 合同预审：超限结果存为未采纳证据 + 按 ID 读片段

预审者：全新上下文 Claude 子 Agent，只读，2026-10-22，基线 origin/main `aa57f9a`。上游 HolmesGPT `a0009195`。

## 现状要点（已核对）

- 两条超限路径。字节超限：`len(body) > max_result_bytes`（`executor.py:1266`，脱敏后再查 `:1377`）或 transport 中途中止（`TransportResultTooLarge`，`:953-954`；otel `otel_demo.py:1201-1202`）；这时 body 不完整或超出上限。视图超限：整个视图超过 `max_view_tokens`（25k 真实 token，`otel_demo.py:243`）时 `_record` 抛 `_ViewTooLarge`（`executor.py:1563-1577`），然后拒绝，**什么都不登记**（`:1130-1145`）。
- 脱敏发生在 `_inspect` 里、`_record` 之前（`executor.py:1364-1384`）。所以视图超限时手里的 body 已经脱敏，并且已经确认不超过 `max_result_bytes`（otel 1 MiB，`otel_demo.py:550,651`；全局上限 2 MiB，`registry.py:78`）。
- 已有「未采纳历史」的先例：控制失效时，`_history` 登记 `adopted=False` 的记录，view 的 `content=None`，模型视图里没有 evidence_id（`executor.py:1405-1435,1919-1933`）。对 trace 来说，存下的 `raw` 已经是投影后的 record（`otel_demo.py:1192-1199`）；行由 `result_path` 确定地取出（`executor.py:1782-1807`），所以片段可以从 raw 确定地重算。
- 表 `opspilot_evidence` 已经有 raw、view、adopted、status，status 不加约束（`0001_baseline.py:156-164`，`0002_state_checks.py:17-18`）。worker 有 INSERT、SELECT、UPDATE 权限，**没有 DELETE**（`0011_runtime_roles.py:177-181`）；web 只有 SELECT（`:70`）。目前没有任何清理任务（#244 OPEN）。C3 规定：原始资料关闭后 30 天，删除时要留墓碑（`technical-proposal…md:404-406`）。
- 引用规则：只有 `adopted is True` 并且带 evidence_id 的视图才进入可引用集合（`context.py:677-683`）。引用了不在集合里的 ID，报 `unknown_evidence_id`；引用非 ok 视图报 `cites_non_ok_view`；引用 citable=false 的视图报 `not_citable_as_fact`（`reports.py:255-259,333-343`）。时间绑定按视图的 `source`/`tool`、`window`、`source_*_at`、`dispatch_started_at`/`observed_at` 匹配（`reports.py:808-940`；`interfaces` 名单匹配 source 或 tool，`:896-900`）。
- 结果视图带 evidence_id 时，提交会把行里的 view **替换成模型视图**（pin），并 `mark_stale`（`progress.py:227-244`，`web/evidence.py:197-235`）。去重只记住带 evidence_id 的结果（`loop.py:835-845`），重复调用拿到的是指向原 ID 的 DUPLICATE 视图（`loop.py:1380-1404`）。上游对任何状态都去重（`safeguards.py`）。#309（PR #322，OPEN）在压缩后清空去重集合，同一轮工具调用默认 2 路并行。
- eval 录制要求 `outcome.evidence.view == model_view`，raw 不超过 2 MiB（`recording.py:76,1420-1432`）。拒绝视图的 hint 键集合被写死（`tool_provenance.py:571-640`，只认 METRICS/TRACES）。
- 第二道上限：`visible_view` 按「估算 token = 字节 × 0.25」计，上限是 (1M−65k)×25% ≈ 233k（`context.py:168,202,294-304`）。网关用真实 token（每 token 2.3–3.1 字节，`otel_demo.py:238-242`）。两处单位不同，一个数字合并不了。存根还带 512 字符预览（`:276-291`），这本身就是截断。
- 上游：超限时落盘，返回路径、用法，外加约 `25k×2` 字符的预览（`tool_context_window_limiter.py:69-139`）；Prometheus 超限只给序列数、总点数、标签基数（只有计数）和 topk 建议，不给数据（`prometheus.py:767-852,1937-1950`）。每次工具调用不计上限（`limits.py:7`，`budgets.py:189`；20 次的常量 `executor.py:90` 并未执行）。
- #307 A：新增 `get_metric_names`、`get_label_values`；测试钉住「4 个工具名」；工具名从 registrations 派生（contract R10，验收项 7）；会 bump `tool_schema_revision`。

## 拆分与顺序（推荐）

两个 PR，都以 `main` 为目标，先合 PR1。

- **PR1（只做持久化，模型面不变）**：视图超限时，把脱敏后的 raw 和拒绝视图（另加 evidence_id）登记为 `adopted=False` 的证据；模型视图逐字节不变，任何 revision 都不 bump，eval 和回放也不受影响。
- **PR2（模型可见面一次改完）**：拒绝视图带上 evidence_id 和基数摘要；新增读取工具；修改描述；`tool_schema_revision` 和 `PROJECTION_REVISION` 各 bump 一次。PR2 必须在 F3 PR-3b 冻结之前合并（审计 §7，`upstream-design-audit…md:83`）。和 #307 A 谁后合，谁 `git merge origin/main`，并由该 PR 的测试作者更新工具数和 revision 的钉住断言（沿用 #307 U3）。
- PR2 在 #322 合并之后开工（去重和并行的语义以合并后为准）。

## 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 |
|---|---|---|---|---|
| U1 | 存什么 | a) 存脱敏后的 raw，view 用拒绝视图另加 evidence_id 和来源字段（窗口、observed_at、data_as_of、source_*_at、result_count、incomplete）；b) 另存一份完整行视图，会翻倍；c) 字节超限也存，但 body 不完整，违反「截断须标记」 | **a**；字节超限仍然只拒绝、不存 | executor、存储 | 用户 2026-10-22 裁决：按推荐 |
| U2 | 容量上限 | 单条记录 ≤ 该工具注册的 `max_result_bytes`（已强制）。每 Run 上限：a) 16 条或 16 MiB，先到为准；超过后照旧拒绝，视图加 `stored:false`；b) 不设上限，100 次模型请求 × 并行理论上可达数百 MiB | **a**。计数走库内 SELECT（worker 有权限）；#309 两路并行最多超 1 条，写入合同 | 存储 | 用户 2026-10-22 裁决：按推荐 |
| U3 | 保留与删除 | a) 本项不做清理，归入 C3「原始资料，关闭后 30 天」，由 #244 统一实现（需要 DELETE 权限和墓碑）；b) 本项单独做「Run 终态即删」，需要新加 grant 和任务，与 #244 重复 | **a**，并在 #244 补一条评论：清理范围包括未采纳的超限证据 | #244 | 用户 2026-10-22 裁决：按推荐 |
| U4 | 片段能否作为 fact 引用 | a) 能：片段是新的 adopted 证据（新 evidence_id），`truncated:true`，带 `slice{parent_evidence_id,parent_raw_sha256,offset,returned,total_rows}`，来源和时间字段继承父记录（C3 :260：保留原观察时间）；b) 不能：模型只能看，最后还得收窄重查才能引用，回读就失去意义 | **a**。片段按稳定的行顺序取精确区间，与 #96 的抽样重叠无关；C3 :158 写的是「截断须显式标记」，不是禁止截断 | 报告校验、L3 描述 | 用户 2026-10-22 裁决：按推荐 |
| U5 | 拒绝消息给模型看什么 | 固定字段：`evidence_id`、`view_tokens/max_view_tokens`、`total_rows`、`row_unit`、`suggested_limit`（按行 token 算出能放下的首段行数），加上用法文案。metrics 另加 `series_count`、`total_points`、`label_cardinality`（前 10 个标签键及各自的值数，只有键名和计数，键名已脱敏且限长）和 `suggestion` topk 文案（照搬上游 :803-813）。**不给内容预览**（上游有预览，但这与「拒绝而不截断」冲突） | 照推荐。它放宽了「拒绝视图不含源内容」的原则（`executor.py:1617-1621`）：标签键名来自数据源 | 模型面、`tool_provenance` | 用户 2026-10-22 裁决：按推荐 |
| U6 | 新工具名与参数 | `read_evidence(evidence_id, offset=0, limit)`，单位为 `result_path` 下的行（指标是 series，trace 是 span）；不支持 JSONPath 或 grep（上游靠 bash，本项目禁任意执行）；结果整段返回，超 25k token 仍整段拒绝 | 照推荐。风险：单个 series 本身超过 25k 时读不出来，只能收窄原查询，作为已知限制写进 `cannot_prove`/`limits` | 新工具 | 用户 2026-10-22 裁决：按推荐 |
| U7 | 合并第二道上限（issue 要求） | a) 不合并：保留 `visible_view` 作为备用防线（审计 §3 :45 也认为理由成立），另在 registry 加 `MAX_VIEW_TOKENS ≤ 25_000` 硬上限，`context_policy_revision` 不动；b) 删除存根，或把它改成指向 read_evidence，需要 bump `context_policy_revision`，单位仍不同 | **a**，并在 issue 里写明不合并的理由 | context、registry | 用户 2026-10-22 裁决：按推荐 |

## 可逆默认（Agent 自定）

| 编号 | 决定 | 理由 |
|---|---|---|
| R1 | PR1 只通过 sink 登记，不挂到 `outcome.evidence` | 挂上去会让 `recording.py:1428` 判 HASH_MISMATCH；PR2 让模型视图等于存储视图之后再挂 |
| R2 | PR2 的拒绝视图沿用 `evidence_id` 键，加 `adopted:false`、`citable_as_fact:false`。提交时 pin 成同一视图；去重会记住它，同参重查得到 DUPLICATE 视图（与上游一致）；#309 压缩后重置照常生效 | 复用现有提交、去重和页面链接；引用会落到 `unknown_evidence_id`，因为它没进入可引用集合 |
| R3 | read_evidence 走 ToolExecutor：transport 只读 `DurableEvidenceStore.get`，source 为 `opspilot-evidence`，verb 为 `get`。只允许读本 Run 内 `reason=RESULT_TOO_LARGE` 的记录，目标是父记录里的 target，且必须仍在授权范围内；其余一律 `EVIDENCE_NOT_READABLE`（denied） | 控制、暂停、fence、计费和登记全部复用。这样不能借回读读到被控制失效的历史，也不能读到跨 Run 或已撤销目标的数据 |
| R4 | 片段视图的 `tool` 为 `read_evidence`，另加 `parent_tool`；`delivered_view` 做时间策略匹配时用 `parent_tool` 和父记录的 source | 否则 `interfaces` 名单里只写指标工具名的时间策略会判 `time_scope_not_bound` |
| R5 | 片段的 raw 存 `canonical({parent_evidence_id,parent_raw_sha256,offset,limit,rows})` | 操作员可以对照父记录核验 hash |
| R6 | 片段读取不限次数，只受 100 次模型请求上限约束；同参重复读取被去重 | 与上游一致，也符合 AGENTS「只保留一个防循环上限」 |
| R7 | 「片段只证明所示行」写进 read_evidence 的 `cannot_prove`，不改 L2 和 `prompt_revision` | 同 #307 R11 |
| R8 | 只加到 otel_demo profile，fixture profile 不加 | 同 #307 R12；fixture 视图很小，触发不到超限 |
| R9 | 操作员可见性：PR1 只能通过 evidence 页（`app.py:663-690`）按 ID 访问；PR2 在事故页工具行里显示链接和「(not adopted)」（`incident.html:320`） | PR1 的视图里没有 ID，`_tool_committed_payload` 取不到 |
| R10 | eval：`tool_provenance` 接受新的 hint 键和 read_evidence；PR2 之前的录制因 revision 不同而不可比，不迁移 | C3 §5 规定 bump 之后在途 Run 进入 `blocked(INCOMPATIBLE_STATE)`，不为避免 blocked 而不 bump |
| R11 | `count_spilled(run_id)` 的返回值 | 返回 `(records, raw_bytes)`：U2 的每 Run 上限同时按条数与字节数判断，两者都要；sink 无法回答（异常或类型不符）时执行器不存，拒绝结果不变。U2 的 16 MiB 读作「已存字节 + 本条 > 16 MiB 即不存」。（lead 2026-10-22 补充，验收测试按此改） |
| R12 | 超限记录 view 的来源字段范围 | U1 括号所列字段之外，另允许网关计算的 `source`、`tool_version`、`projection_revision`、`dispatch_started_at`、`freshness_seconds`：PR2 的片段按 U4 继承父记录的来源与时间字段需要它们；均不含行内容。除此之外不得新增键。（lead 2026-10-22 补充） |

九类必答：时间见 U4、R4。计数见 U2、U6。状态：未采纳记录只在登记时写入，提交时 pin（R2）。权限：已有 grant 够用，无迁移。数据合同：表、列都不变，无迁移，部署顺序不限。旧数据：旧行不受影响。并发：U2 允许超 1 条。外部调用：read_evidence 不接触数据源。`passes`：不翻。

## 允许修改的文件

- PR1：`opspilot/tools/executor.py`；`opspilot/web/evidence.py`（加 `count_spilled(run_id)`，并扩 `EvidenceSink` protocol）；`tests/…`；`docs/tasks/2026-10-22-308-*.md`。
- PR2：在 PR1 范围之外，再加 `opspilot/tools/outcomes.py`（v9）；`opspilot/tools/otel_demo.py`（注册、描述、metrics 摘要）；新增 `opspilot/tools/evidence_read.py`；`opspilot/tools/registry.py`（U7 上限）；`opspilot/investigation/context.py`（R4）；工具与 store 的装配处（`profiles.py`/runner，未确认具体在哪个文件）；`opspilot/evaluation/tool_provenance.py`；`scripts/eval_live.py`（如果 #307 尚未改成派生）；`opspilot/web/templates/incident.html`；`docs/evidence/308-live/`。

## 给独立测试作者的可观察行为

1. （PR1）一个视图超过 25k 的 ok 读取：模型视图与改动前逐字节相同；库里新增一行，`adopted=false`、`status=error`，raw 等于脱敏后的 body，sha 校验通过，`run_id` 正确。字节超限不新增行。
2. （PR1）同一 Run 第 17 次超限（或累计超过 16 MiB）不再新增行，模型视图仍然不变。body 里的 `password=xyz` 在存储中已经脱敏。控制失效的读取按原路径处理。
3. （PR2）超限拒绝视图带 evidence_id 和 U5 的字段：metrics 的 `label_cardinality` 按计数降序、最多 10 个，只有计数没有值；没有 content 预览。用这个 ID 作为 fact 引用时，校验报 `unknown_evidence_id`。
4. `read_evidence(id, offset, limit)` 返回 ok 且 adopted 的片段：行等于父记录 raw 在 `[offset, offset+limit)` 区间的行；带 `truncated:true` 和 `slice` 元数据；时间和来源字段等于父记录；可以作为 fact 引用并通过时间绑定。片段超过 25k 时整段拒绝。
5. 跨 Run 的 ID、非超限记录、已撤销的目标、不存在的 ID、负数 offset：发送前拒绝，不接触数据源。暂停之后的读取被控制拒绝。
6. 同参重查超限调用得到 DUPLICATE 视图；压缩之后（#309）可以再次读取。
7. `TOOL_SCHEMA_REVISION`、`PROJECTION_REVISION` 已变化且可重算；`prompt_revision`、`context_policy_revision` 不变；工具名集合含 read_evidence，并从 registrations 派生。

## 最小 smoke Run（B 批：只看机制，不评质量）

PR2：一次有界真实 Run，真实 DeepSeek 加产品 otel-demo profile，后端用 `scripts/m1_otel_stub.py`，并让替身对某个查询返回超过 25k 的结果。判据：出现超限拒绝（带 evidence_id 和摘要），并且至少一次 `read_evidence` 返回 ok；附 LangSmith trace 和 `summary.json`。模型没有回读算失败，可以重跑一次，失败轮保留。PR1 不碰 loop，以 PG 集成测试为证据。

## 未确认与风险

- 历史记录挂在 `outcome.evidence` 上时，eval 录制是否本来就会判 mismatch，没有核实。
- F3 冻结答案（`dc474da`）和 #300 是否钉住当前 revision，没有核实；工具与 store 的装配点没有定位。
- 单个 series 超过 25k 时无法回读（U6）。标签键名进入拒绝视图，依赖带命名上下文的脱敏，无法识别裸秘密（`executor.py:19-21`）。
