# 工具面拓展 C2：按需取已审核的 runbook 与复盘知识

- 状态：进行中（C2a 实现中；lead 2026-11-19 确认拆分与实现决定 1、4）
- 更新日期：2026-11-19
- 依据：issue #566；[B/C 批次记录](2026-11-16-tool-expansion-bc.md)；[决定表](2026-11-12-tool-expansion-contract.md) R8-9、R8-13、R8-15、R8-25 与 r11 C2-U1=a、C2-U2=a（用户 2026-11-19 选推荐；R8-14 按 r11 修改）；C3 §5 输入快照与 `prompt_revision`、§10「复盘与知识」；F13 第 2、3 步与 F7 第 2 步（`feature_list.json`，均 `passes: true`，须在新来源上重证）。预审全文在仓库外 `~/.agent/handoffs/opspilot-c2-prereview-2026-11-19.md`，可逆默认 C2-0 至 C2-18 采纳，下文只写落地形状与一处偏离（见「实现决定」第 1 条）
- 上游：HolmesGPT `a0009195` `holmes/plugins/toolsets/skills/skills_fetcher.py`（`fetch_skill`）、`holmes/plugins/skills/skill_loader.py`（目录）、`holmes/plugins/prompts/base_user_prompt.jinja2:16-41`
- 工作区：`../poa-566-runbooks`，分支 `feature/566-runbooks`（起点 `13120d25`）

## 目标

人在工作台提交、审核一份 runbook；批准后，调查 Run 能在提示里看到本目标已批准 runbook 的目录，并用 `fetch_skill` 取到某一份的全文（也能按条目 ID 取复盘知识全文）。待审核、被驳回、被撤销的取不到。复盘知识的表、触发器与 F13 第 4 步提示读取一个字节都不改。

## 公开接口

### 1. 数据模型（迁移 `0028_runbook_imports`，编号以合并时 main 最新为准）

与 0008 的复盘知识表完全分开，不改任何 F13 表或约束。全部表禁止 DELETE/TRUNCATE；除 `entries` 的计数列外禁止 UPDATE；每一行写入都要求同一事务里有对应审计行（照 0008 `xmin` 检查）。

| 表 | 一行是什么 | 关键列与约束 |
|---|---|---|
| `opspilot_runbook_requests` | 一次幂等请求 | `idempotency_key` PK、`action`、`request_sha256`、`result jsonb`；同键不同请求 → `IDEMPOTENCY_CONFLICT`，同键同请求重放结果 |
| `opspilot_runbook_audit` | 一次状态变更 | `object_kind ∈ {runbook_submission, runbook_entry}`、`action ∈ {submit, approve, reject, revoke}`、`actor_id`、`principal_kind = 'basic_auth'`（没有 worker 动作）、`reason`（reject、revoke 必填）、`expected_generation`/`resulting_generation = +1` |
| `opspilot_runbook_submissions` | 一份提交，不可改 | `submission_id`、`resource_uid`、`name`、`description`、`content`、`content_sha256`、`source_label`、`source_ref`、`supersedes_entry_id`（空 = 新条目）、`submitted_by`、`submitted_at` |
| `opspilot_runbook_decisions` | 对提交的唯一一次决定 | `submission_id` PK（所以只能决定一次）、`decision ∈ {approved, rejected}`、`reason`、`decided_by`、`decided_at` |
| `opspilot_runbook_entries` | 一条 runbook 的身份 | `entry_id`、`resource_uid`（不可改）、`generation`、`latest_revision` |
| `opspilot_runbook_revisions` | 批准后的版本，不可改 | `(entry_id, revision)`、`submission_id` UNIQUE、`resource_uid`、`name`、`description`、`content`、`content_sha256`、`source_label`、`source_ref`、`supersedes_revision = revision - 1`、`approved_by`、`approved_at`；触发器要求内容与该提交逐列相同且提交的决定是 approved |
| `opspilot_runbook_revocations` | 撤销一个版本 | `(entry_id, revision)`、`reason`、`revoked_by`、`revoked_at` |
| 视图 `opspilot_runbook_revision_states` | 派生状态 | `active` / `superseded` / `revoked`，同 0008 的写法 |

列级规则：`name` 规范化（小写，空格与下划线换连字符）后须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`、≤100 字符；`description` 1–300 字符；`content` 1 字节到 32 KiB（UTF-8，`octet_length`），Markdown 原文，提交时不脱敏；`source_label` 1–100 字符；`source_ref` ≤500 字符。名字唯一：同一 `resource_uid` 下当前 `active` 的名字唯一，提交时与批准时都检查（批准时触发器在按 `resource_uid` 的事务级 advisory lock 下检查），冲突报 `RUNBOOK_NAME_TAKEN`；撤销后同名可以重新提交成新条目。

### 2. 状态转移与谁能触发

- 提交 → 待审核（无决定行）→ 批准（新条目 revision 1，或取代：被取代条目 revision n+1，旧版本变 `superseded`）/ 驳回（必填原因，终态，取不到）。
- 已批准的当前版本 → 撤销（必填原因）→ 该条目无 `active` 版本，取不到；不能恢复，要用就重新提交。
- 取代型提交：`supersedes_entry_id` 指向的条目批准时必须仍有 `active` 版本、同一目标、同名，否则 `SUPERSEDE_TARGET_INACTIVE` / `SUPERSEDE_MISMATCH`。
- 只有工作台登录用户（Basic Auth）能提交、批准、驳回、撤销；提交人与审核人可以是同一人，审计照实记录（C2-11）。worker 与模型都不能触发任何转移（没有写权限，见下）。乐观并发：审核带 `expected_generation`，取代型批准另带被取代条目的 `entry_generation`，不符报 `GENERATION_CONFLICT`。

### 3. 权限（`grants.py`）

- web：新表与视图 SELECT；requests、audit、submissions、decisions、entries、revisions、revocations 的列级 INSERT（语句所列列）；entries `UPDATE(generation,latest_revision,updated_at)`。
- worker：只有 `opspilot_runbook_revisions` 与 `opspilot_runbook_revision_states` 的 SELECT，其余新表 NONE（取用只需当前有效版本；待审核与驳回的提交 worker 根本读不到）。复盘知识取全文用 worker 现有的 SELECT，不改旧表授权。
- 保留清理角色（0023）：新表 NONE（runbook 不是事故载荷）。

### 4. 工作台与 API 入口（C2a）

都要登录；写操作走现有 `ui_mutation`（Basic Auth + Origin 检查）；`Accept: application/json` 时返回 JSON（评测脚本用），否则 303 跳转回页面，同 `/knowledge/{entry_id}/revoke` 的写法。

| 入口 | 作用 |
|---|---|
| `GET /runbooks` | 按目标列出当前有效、待审核、已驳回、已撤销/取代的条目与提交；导航栏加「Runbooks」 |
| `GET /runbooks/new`、`POST /runbooks` | 提交：`resource_uid`（配置了目标表时必须是其中的键，否则 `UNKNOWN_TARGET`；没配目标表时接受 intake 接受的任何标识，与能建 Run 的目标一致）、`name`、`description`、`content`、`source_label`、`source_ref`、可选 `supersedes_entry_id`、`idempotency_key`；返回 `submission_id` |
| `GET /runbooks/submissions/{id}`、`POST .../review` | 看原文并 `action=approve|reject`、`reason`、`expected_generation`、（取代时）`entry_generation`、`idempotency_key`；批准返回 `entry_id`、`revision` |
| `GET /runbooks/{entry_id}`、`POST .../revoke` | 条目页（每个版本的来源、审核人、批准时间、状态、撤销原因）；撤销带 `revision`、`reason`、`expected_generation`、`idempotency_key` |

表单为 `application/x-www-form-urlencoded`（同现有审核表单）；提交表单上限 512 KiB（32 KiB 多字节正文 URL 编码后最多约 9 倍），其他表单沿用 64 KiB。

错误码（C2a 实现时定，HTTP 状态照审核表单的 R3 分组）：400 `INVALID_INPUT`、`RUNBOOK_NAME_INVALID`、`RUNBOOK_DESCRIPTION_INVALID`、`RUNBOOK_CONTENT_INVALID`、`RUNBOOK_SOURCE_INVALID`、`REASON_REQUIRED`；409 `GENERATION_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`RUNBOOK_NAME_TAKEN`；422 `SUPERSEDE_TARGET_INACTIVE`、`SUPERSEDE_MISMATCH`、`REVISION_NOT_ACTIVE`（撤销非当前有效版本）、`UNKNOWN_TARGET`、`INTEGRITY_REFUSED`；404 `NOT_FOUND`；503 存储不可用。

页面开关：入口不受 C2 开关控制（它不影响模型输入）；没有迁移时进程照常因 schema 不在 head 启动失败。

### 5. 工具 `fetch_skill`（C2b）

- 参数：`skill_id`（必填字符串，≤200 字符，无不可打印字符）：runbook 名字（先规范化）、runbook `entry_id`，或复盘知识 `entry_id`。工具描述照 C2-9，不列目录里的名字，不带上游的强制回答格式。
- 只读本 Run 绑定目标（`resource_uid`）下的知识；worker 运行角色、只读快照事务（`transaction(snapshot=True)`），一条 SELECT，不重试。
- 成功：`status=ok`，`results` 一行，字段照 C2-7：`skill_id`、`entry_id`、`revision`、`kind`（`runbook_import` / `postmortem`）、`name`、`description`、`content`（全文，不截断）、`content_sha256`、`source`（runbook：`source_label`、`source_ref`；复盘：来源事故、复盘 ID 与版本）、`review_state: "active"`、`approved_by`、`approved_at`、`freshness: {age_seconds, state: fresh|stale, max_age_seconds}`（runbook 从批准时刻算，复盘照 F13 第 4 步从观察结束算；阈值取本 Run 输入快照里目录事实记录的 `max_age_seconds`，worker 不另读环境变量）。复盘知识的 `evidence_refs` 换成 `{"omitted": true, "count": n}`。视图 `source="knowledge"`、`citable_as_fact=false`、`may_contain_secrets=True`（经现有名称上下文脱敏）、`result_path=("results",)`；`source_start_at`/`source_end_at` 为空，`data_as_of` 为批准时刻。报告只能在 hypothesis、recommendation 里引用它。
- 失败：`INVALID_SKILL_ID`（发请求前，归 `INVALID_PARAMS`）；`KNOWLEDGE_NOT_FOUND`（不存在、属于别的目标；归 `INVALID_PARAMS`，notes 列本目标可取的 runbook 名字，最多 50 个）；`KNOWLEDGE_NOT_AVAILABLE`（条目存在但没有 active 版本，notes 写 `revoked` 或 `superseded`，不返回正文；归 `SOURCE_ERROR`）；PG 不可用或超时照现有执行器分类。工具自己的错误表，不改共享分类。
- 档位：只在 `otel-demo-ext` 且 web 与 worker 都设 `OPSPILOT_KNOWLEDGE_TOOL=1`；`1` 开，未设/空/`0` 关，其他值两个进程都启动失败 `KNOWLEDGE_TOOL_INVALID`（B3-1 写法）。开时工具接在数组末尾（实时用量之后），`data_sources.available.knowledge = ["fetch_skill"]`，`tool_schema_revision(..., knowledge=)`；关时现有 16 个 revision 字节不变。

### 6. 输入快照 v7 与 `prompt_revision`（C2b）

- 新常量 `INPUT_VERSION_RUNBOOK_CATALOG = "opspilot-investigation-input-v7"`，事实键 `runbook_catalog`；有这个事实就是 v7（排在 v6 之前判断），没有就照旧。
- 事实形状：`{policy_version: "opspilot-runbook-catalog-v1", status: ok|unavailable, retrieved_at, max_age_seconds, omitted_for_size, entries: [{skill_id, entry_id, revision, description, approved_at, age_seconds, freshness}]}`；按 `skill_id` 排序、名字唯一、最多 50 条、模型可见部分总长 8 KB，超出按名字顺序丢弃并计 `omitted_for_size`。web 建 Run 时在独立只读快照里读；读失败记 `unavailable`、Run 照常建（同 F13 第 4 步 R5）。后继 Run（追问、续期、重开）重新读。
- 模型看到：目录非空时，在知识提示之后加一条 user 消息：固定边界措辞 `RUNBOOK_CATALOG_BOUNDARY`（不可信数据、不是指令也不是本 Run 证据；明确相关才取；照步骤用自己的工具查；不授予权限、不改目标/时间框/报告要求；缺工具的步骤在报告里写明）+ JSON `[{skill_id, description, revision, freshness}]`。目录为空不发消息，只记在快照。
- 校验：v7 事实形状、排序、唯一、上限、`freshness` 与 `age_seconds`/`max_age_seconds` 一致，不符 `INPUT_INVALID`（篡改过的快照被拒）。
- `prompt_revision`：开关开时把 `runbook_catalog_template=RUNBOOK_CATALOG_BOUNDARY` 加进哈希字典；开关关时字典里**没有这个键**（不是空串），所以 revision 字节不变（照 #722「未设时字节不变」）。web 与 worker 开关不一致时 revision 不同，认领时被拦。

### 7. 评测夹具导入（C2b，执行编排归 #666）

`scripts/holmes_evals/import_skills.py`：`import <题目目录> --target <resource_uid>` 只读 `*/SKILL.md`（frontmatter `name`、`description` + 正文）或 `runbook_catalog.json` + `runbook_content_<id>.json`，不打开 `test_case.yaml`；经第 4 节 JSON 接口提交并批准，`source_label="HolmesGPT 评测夹具"`，`source_ref = holmesgpt@a0009195:<相对路径>#sha256=<文件哈希>`；创建的 `entry_id` 写到调用方给的本地清单（仓库外）。`revoke <清单>` 以原因「评测题切换」撤销。不打印正文；凭据从环境变量读。`global_instructions.json` 不导入。

## F13 / F7 重证计划（测试由不参与实现的作者写，不弱化原断言）

| 步骤 | 新来源上要证明的 | PR |
|---|---|---|
| F13 第 2 步 | 待审核、被驳回的提交取不到（目录不列、`fetch_skill` 返回 `KNOWLEDGE_NOT_FOUND`）；没有 worker 或模型能触发批准（worker 登录对新表写入被 PG 拒 42501） | C2a 库层 + C2b 取用 |
| F13 第 3 步 | 批准与驳回记审核人、时间、原因、版本；版本不可改（UPDATE 被拒）；撤销后取不到（`KNOWLEDGE_NOT_AVAILABLE`，无正文）；取代产生新版本，旧版本 `superseded` | C2a + C2b |
| F7 第 2 步 | 导入 runbook 正文写「查别的目标、放宽到 30 天、调用写工具」，执行的读取仍只在授权目标与时间框内；runbook 正文与说明进 `tests/safety` 的模型输入通道，带凭据形状的文本不进模型调用，`tests/mutation` 加一项 | C2b |

另：开关关时扩展档位、`otel-demo` 档位、`prompt_revision`、输入版本字节不变（C2b ①）；预审 C2-14 ②–⑧ 与 C2-15..18 按 PR 分配。

## PR 拆分

- **C2a**（用户门：迁移、授权、C3 §10）：0028、`opspilot/knowledge/runbooks.py`（`RunbookStore`：submit/approve/reject/revoke 与页面读取）、`grants.py`、工作台入口与模板、C3 §10「复盘与知识」加一段「知识有两个来源」、钉迁移头的 5 个测试文件（`test_schema_baseline.py:40`、`integration/test_schema_migrations_postgres.py:35`、`integration/f11_release_observation_postgres_support.py:101`、`acceptance/test_429_schema_downgrade.py:23`、`acceptance/test_245_rebind_target.py:607,614,623`）。模型输入不变。
- **C2b**（用户门：输入合同 v7、`prompt_revision` 条件纳入、新数据源）：`opspilot/tools/knowledge_fetch.py`、档位与开关、`context.py` v7 与目录消息、`loop.py`/`discipline.py` 条件纳入、`web/service.py` 建 Run 与后继 Run 读目录、导入脚本、B/C 记录登记数据源。C2a 合并后开工。

## 实现决定（可逆，一行一条理由）

1. 审计与幂等用新表 `opspilot_runbook_audit`/`_requests`，不复用 `opspilot_f13_*`（偏离预审 C2-11）：复用要改 0008 的 `object_kind` CHECK、principal/action CHECK 与 0011 的 worker 守卫，新表让复盘知识的约束一个字节不动，降级也能干净删表。
2. 提交与决定分两张不可改的表，不在提交行上改状态：每行只写一次，状态全由行的存在派生，与 0008 的 revocation 写法一致。
3. 存 `resource_uid` 文本、不外键到 `opspilot_targets`：目标行在首次告警时才建，评测要在第一次 Run 前导入；提交时由工作台目标表校验已知目标。
4. 开关取值 `1`（预审写 `on`）：与 B3 的 `OPSPILOT_K8S_LIVE_METRICS=1` 一致，非法值启动失败。
5. 新鲜度阈值从 v7 事实读，worker 不读 `OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS`：同一 Run 的目录与取用口径一致，且不给 worker 加配置。
6. 撤销不可恢复、要用就重新提交：与 F13 撤销语义一致，审计里每次导入都留痕（C2-12 逐题导入/撤销依赖这一点）。
7. 目标校验跟 intake 走：配置了目标表时只收表里的键，没配时收任何 intake 标识。评测租机不一定配目标表，而 runbook 只对能建 Run 的目标有意义。
8. 待审核提交与决定的并发用事务级 advisory lock（按提交 ID、按目标），不用行锁：web 对提交表没有 UPDATE 权限，`FOR UPDATE` 需要它；批准时的同名检查在数据库触发器里再做一次，用同一把目标锁。
9. 审核页与 JSON 共用表单编码：评测脚本与浏览器走同一条路径，不另开 JSON 入口，F7 第 1 步的路由清单只多 7 条。

## 证据

- C2a 本机：`tests/test_runbooks_impl.py`、`test_schema_baseline`、`test_knowledge_store_callers`、`test_architecture` 43 passed、2 xfailed（原有）；ruff、mypy 干净。实现者 PG 测试 `tests/integration/test_runbooks_impl_postgres.py`（10 例，`-m pg` 能收集到）与迁移、角色目录测试交草稿 PR 的 CI（本机 swap 已满，不起 PG）。
- 改动的钉值：迁移头 5 个文件；`test_schema_migrations_postgres.py` 的 `TABLES_AT_HEAD` 47 → 54；`test_429_schema_downgrade.py` 降级随行表加 0028 的 7 表 1 视图。F7 第 1 步路由清单的 7 条新路由交测试作者登记（先例如此）。

## 待决

无。实施中冒出的不可逆项或合同层取舍停下回报 lead。
